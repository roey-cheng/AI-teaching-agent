"""离线检查迁移；不读真实凭据、不连接 MySQL、不执行建表或删表。"""

import ast
from io import StringIO
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from mako.template import Template
import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.schema import CreateColumn

from app import models  # noqa: F401
from app.db.base import Base

BACKEND = Path(__file__).resolve().parents[1]
REVISION = "20260925_0001"


def make_config(output=None):
    return Config(str(BACKEND / "alembic.ini"), output_buffer=output)


def compact(value):
    return " ".join(str(value).split())


def constraints_for(table):
    result = set()
    for constraint in table.constraints:
        details = tuple(constraint.columns.keys())
        if isinstance(constraint, sa.CheckConstraint):
            details = (compact(constraint.sqltext),)
        elif isinstance(constraint, sa.ForeignKeyConstraint):
            details += tuple(element.target_fullname for element in constraint.elements)
            details += (constraint.ondelete, constraint.onupdate)
        result.add((type(constraint).__name__, constraint.name, details))
    return result


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.script = ScriptDirectory.from_config(make_config())
        self.revision = self.script.get_revision(REVISION).module

    def test_single_initial_revision_and_no_live_model_imports(self):
        self.assertEqual(self.script.get_heads(), [REVISION])
        self.assertIsNone(self.revision.down_revision)
        source = Path(self.revision.__file__).read_text()
        tree = ast.parse(source)
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
            elif isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
        self.assertFalse(any(name == "app" or name.startswith("app.") for name in imports))
        self.assertNotIn("create_all", source)

    def test_upgrade_snapshot_matches_all_five_models(self):
        metadata = sa.MetaData()
        created = []

        def create_table(name, *elements, **options):
            created.append(name)
            return sa.Table(name, metadata, *elements, **options)

        def create_index(name, table_name, columns, **options):
            table = metadata.tables[table_name]
            return sa.Index(name, *(table.c[column] for column in columns), **options)

        with patch.object(self.revision.context, "is_offline_mode", return_value=True), \
             patch.object(self.revision.op, "create_table", side_effect=create_table), \
             patch.object(self.revision.op, "create_index", side_effect=create_index):
            self.revision.upgrade()

        self.assertEqual(set(metadata.tables), set(Base.metadata.tables))
        self.assertEqual(len(created), 5)
        for name, table in metadata.tables.items():
            model = Base.metadata.tables[name]
            with self.subTest(table=name):
                self.assertEqual(list(table.columns.keys()), list(model.columns.keys()))
                self.assertEqual(dict(table.kwargs), dict(model.kwargs))
                self.assertEqual(constraints_for(table), constraints_for(model))
                self.assertEqual(
                    {(i.name, tuple(i.columns.keys()), i.unique) for i in table.indexes},
                    {(i.name, tuple(i.columns.keys()), i.unique) for i in model.indexes},
                )
                for column in table.columns:
                    actual = compact(CreateColumn(column).compile(dialect=mysql.dialect()))
                    expected = compact(CreateColumn(model.c[column.name]).compile(dialect=mysql.dialect()))
                    self.assertEqual(actual, expected)
                for fk in table.foreign_key_constraints:
                    parent = fk.referred_table.name
                    if parent != name:
                        self.assertLess(created.index(parent), created.index(name))

    def test_offline_upgrade_outputs_sql_without_settings_or_connection(self):
        output = StringIO()
        with patch("app.core.config.load_database_settings") as settings, \
             patch("app.db.engine.build_database_engine") as engine, \
             patch("pymysql.connect") as connect:
            command.upgrade(make_config(output), "head", sql=True)
        settings.assert_not_called()
        engine.assert_not_called()
        connect.assert_not_called()
        sql = output.getvalue()
        for name in Base.metadata.tables:
            self.assertEqual(sql.count(f"CREATE TABLE {name} ("), 1)
        self.assertEqual(sql.count("CREATE TABLE "), 6)  # 五张业务表 + 版本表。
        for expected in (
            "CREATE TABLE alembic_version", "INSERT INTO alembic_version", REVISION,
            "BIGINT UNSIGNED", "AUTO_INCREMENT", "DATETIME(6)",
            "ON DELETE RESTRICT ON UPDATE RESTRICT", "DEFAULT '新对话'",
            "ck_messages_role_fields", "ck_agent_memory_topic_type",
        ):
            self.assertIn(expected, sql)
        self.assertNotIn("DROP TABLE", sql)

    def test_downgrade_sql_drops_children_before_parents(self):
        output = StringIO()
        with patch("pymysql.connect") as connect:
            command.downgrade(make_config(output), f"{REVISION}:base", sql=True)
        connect.assert_not_called()
        sql = output.getvalue()
        expected_order = ["messages", "agent_memory", "chat_sessions", "auth_sessions", "users"]
        positions = [sql.index(f"DROP TABLE {name};") for name in expected_order]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("DELETE FROM alembic_version", sql)

    def test_initial_upgrade_refuses_existing_tables_or_views_before_ddl(self):
        for kind in ("get_table_names", "get_view_names"):
            inspector = MagicMock()
            inspector.get_table_names.return_value = []
            inspector.get_view_names.return_value = []
            getattr(inspector, kind).return_value = ["users"]
            with self.subTest(kind=kind), \
                 patch.object(self.revision.context, "is_offline_mode", return_value=False), \
                 patch.object(self.revision.op, "get_bind"), \
                 patch.object(self.revision.sa, "inspect", return_value=inspector), \
                 patch.object(self.revision.op, "create_table") as create:
                with self.assertRaisesRegex(CommandError, "Existing tables or views"):
                    self.revision.upgrade()
                create.assert_not_called()

    def test_initial_upgrade_allows_empty_database(self):
        inspector = MagicMock()
        inspector.get_table_names.return_value = ["alembic_version"]
        inspector.get_view_names.return_value = []
        with patch.object(self.revision.context, "is_offline_mode", return_value=False), \
             patch.object(self.revision.op, "get_bind"), \
             patch.object(self.revision.sa, "inspect", return_value=inspector), \
             patch.object(self.revision.op, "create_table") as create, \
             patch.object(self.revision.op, "create_index") as index:
            self.revision.upgrade()
        self.assertEqual(create.call_count, 5)
        self.assertEqual(index.call_count, 5)

    def test_online_environment_binds_metadata_and_releases_engine(self):
        engine = MagicMock()
        connection = engine.connect.return_value.__enter__.return_value
        connection.dialect.name = "mysql"
        connection.dialect.is_mariadb = False
        connection.dialect.server_version_info = (8, 4, 0)
        with patch("app.core.config.load_database_settings", return_value="fake-settings"), \
             patch("app.db.engine.build_database_engine", return_value=engine) as build, \
             patch("alembic.context.configure") as configure, \
             patch("alembic.context.begin_transaction"), \
             patch("alembic.context.run_migrations") as run:
            command.upgrade(make_config(), "head")
        build.assert_called_once_with("fake-settings")
        self.assertIs(configure.call_args.kwargs["target_metadata"], Base.metadata)
        self.assertIs(configure.call_args.kwargs["connection"], connection)
        self.assertTrue(configure.call_args.kwargs["compare_type"])
        self.assertTrue(configure.call_args.kwargs["compare_server_default"])
        run.assert_called_once_with()
        engine.connect.return_value.__exit__.assert_called_once()
        engine.dispose.assert_called_once_with()

    def test_online_environment_rejects_wrong_server_before_migration(self):
        for name, maria, version in [("sqlite", False, (3, 0)), ("mysql", True, (10, 11)), ("mysql", False, (8, 0, 40))]:
            engine = MagicMock()
            connection = engine.connect.return_value.__enter__.return_value
            connection.dialect.name = name
            connection.dialect.is_mariadb = maria
            connection.dialect.server_version_info = version
            with self.subTest(name=name, version=version), \
                 patch("app.core.config.load_database_settings"), \
                 patch("app.db.engine.build_database_engine", return_value=engine), \
                 patch("alembic.context.run_migrations") as run:
                with self.assertRaisesRegex(CommandError, "MySQL 8.4"):
                    command.upgrade(make_config(), "head")
            run.assert_not_called()
            engine.dispose.assert_called_once_with()

    def test_connection_failure_is_redacted_and_engine_disposed(self):
        engine = MagicMock()
        engine.connect.side_effect = SQLAlchemyError("fake-secret-password")
        with patch("app.core.config.load_database_settings"), \
             patch("app.db.engine.build_database_engine", return_value=engine):
            with self.assertRaises(CommandError) as error:
                command.upgrade(make_config(), "head")
        self.assertNotIn("fake-secret-password", str(error.exception))
        self.assertIn("partially applied", str(error.exception))
        self.assertTrue(error.exception.__suppress_context__)
        engine.dispose.assert_called_once_with()

    def test_configuration_failure_does_not_build_engine(self):
        with patch("app.core.config.load_database_settings", side_effect=OSError("fake-secret")), \
             patch("app.db.engine.build_database_engine") as build:
            with self.assertRaises(CommandError) as error:
                command.upgrade(make_config(), "head")
        build.assert_not_called()
        self.assertNotIn("fake-secret", str(error.exception))

    def test_revision_template_renders_valid_python(self):
        template = Template(filename=str(BACKEND / "alembic" / "script.py.mako"))
        source = template.render(
            message="Example migration", up_revision="example", down_revision=REVISION,
            create_date="2026-09-25", imports="", branch_labels=None, depends_on=None,
            upgrades="pass", downgrades="pass", comma=lambda value: value or "",
        )
        compile(source, "example_migration.py", "exec")


if __name__ == "__main__":
    unittest.main()
