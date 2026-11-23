"""Offline storage wiring tests: no credentials, MySQL, or model API calls."""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.mysql.pymysql import PyMySQLSaver
from sqlalchemy import URL, create_engine

from app.db.checkpoints import (
    CHECKPOINT_TABLES,
    CheckpointStorageNotReadyError,
    check_checkpoint_schema,
    checkpoint_serializer,
    open_checkpoint_saver,
    setup_checkpoint_schema,
)


def mock_engine():
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.dialect.name = "mysql"
    connection.dialect.is_mariadb = False
    connection.dialect.server_version_info = (8, 4, 0)
    connection.execution_options.return_value = connection
    return engine, connection


class CheckpointSchemaTests(unittest.TestCase):
    def test_migration_entrypoint_runs_native_setup_after_alembic_under_lock(self):
        from app.db.migrate import main

        events = []
        with (
            patch("app.db.migrate.load_database_settings"),
            patch("app.db.migrate.build_database_engine") as build,
            patch("app.db.migrate.DatabaseRuntimeLock") as lock,
            patch(
                "app.db.migrate.command.upgrade", side_effect=lambda *_: events.append("alembic")
            ),
            patch(
                "app.db.migrate.setup_checkpoint_schema",
                side_effect=lambda *_: events.append("checkpoints"),
            ) as setup,
        ):
            lock.return_value.__enter__.side_effect = lambda: events.append("locked")
            lock.return_value.__exit__.side_effect = lambda *_: events.append("unlocked")
            main()
            setup.assert_called_once_with(build.return_value)
            build.return_value.dispose.assert_called_once_with()
        self.assertEqual(events, ["locked", "alembic", "checkpoints", "unlocked"])

    def test_explicit_setup_delegates_to_native_saver_with_autocommit(self):
        engine, connection = mock_engine()
        with patch("app.db.checkpoints.PyMySQLSaver") as saver:
            setup_checkpoint_schema(engine)
        connection.execution_options.assert_called_once_with(isolation_level="AUTOCOMMIT")
        self.assertIs(saver.call_args.args[0], connection.connection.driver_connection)
        saver.return_value.setup.assert_called_once_with()

    def test_readiness_is_read_only_and_checks_all_native_migration_versions(self):
        engine, connection = mock_engine()
        connection.execute.return_value.scalars.return_value = range(len(PyMySQLSaver.MIGRATIONS))
        with (
            patch("app.db.checkpoints.inspect") as inspect,
            patch.object(PyMySQLSaver, "setup") as setup,
        ):
            inspect.return_value.get_table_names.return_value = list(CHECKPOINT_TABLES)
            check_checkpoint_schema(engine)
        setup.assert_not_called()
        self.assertEqual(
            str(connection.execute.call_args.args[0]),
            "SELECT v FROM checkpoint_migrations ORDER BY v",
        )
        connection.execution_options.assert_not_called()

    def test_readiness_rejects_missing_tables_or_migrations(self):
        for tables, versions in (([], []), (list(CHECKPOINT_TABLES), [0])):
            with self.subTest(tables=tables):
                engine, connection = mock_engine()
                connection.execute.return_value.scalars.return_value = versions
                with patch("app.db.checkpoints.inspect") as inspect:
                    inspect.return_value.get_table_names.return_value = tables
                    with self.assertRaises(CheckpointStorageNotReadyError):
                        check_checkpoint_schema(engine)

    def test_rejects_incompatible_mysql_before_running_native_migrations(self):
        for version in ((8, 0, 40), (9, 6, 0)):
            engine, connection = mock_engine()
            connection.dialect.server_version_info = version
            with (
                patch.object(PyMySQLSaver, "setup") as setup,
                self.assertRaises(CheckpointStorageNotReadyError),
            ):
                setup_checkpoint_schema(engine)
            setup.assert_not_called()

    def test_strict_serializer_preserves_messages_tools_files_and_summary(self):
        state = {
            "messages": [
                HumanMessage(content="Question", id="u1"),
                AIMessage(
                    content="",
                    id="a1",
                    additional_kwargs={"reasoning_content": "Recorded model field"},
                    tool_calls=[
                        {"id": "tool1", "name": "read_memory", "args": {}, "type": "tool_call"}
                    ],
                ),
                ToolMessage(content="Result", tool_call_id="tool1", id="t1"),
            ],
            "files": {"/history/test.md": {"content": ["Earlier conversation"]}},
            "_summarization_event": {
                "cutoff_index": 1,
                "summary_message": HumanMessage(content="Summary"),
                "file_path": "/history/test.md",
            },
        }
        serializer = checkpoint_serializer()
        self.assertEqual(serializer.loads_typed(serializer.dumps_typed(state)), state)
        self.assertFalse(serializer.pickle_fallback)
        self.assertIsNot(serializer._allowed_msgpack_modules, True)


class CheckpointLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_pool_is_bounded_closed_and_does_not_run_setup(self):
        engine = create_engine(
            URL.create(
                "mysql+pymysql",
                username="example",
                password="test@/password",
                host="127.0.0.1",
                port=3307,
                database="example_db",
            )
        )
        pool = MagicMock()
        pool.wait_closed = AsyncMock()
        try:
            with (
                patch(
                    "app.db.checkpoints.asyncmy.create_pool", AsyncMock(return_value=pool)
                ) as create,
                patch("app.db.checkpoints.AsyncMySaver") as saver_class,
                self.assertRaisesRegex(RuntimeError, "test failure"),
            ):
                async with open_checkpoint_saver(engine) as saver:
                    self.assertIs(saver, saver_class.return_value)
                    raise RuntimeError("test failure")
            self.assertEqual(create.call_args.kwargs["password"], "test@/password")
            self.assertEqual(create.call_args.kwargs["maxsize"], 4)
            self.assertTrue(create.call_args.kwargs["autocommit"])
            saver_class.return_value.setup.assert_not_called()
            pool.close.assert_called_once_with()
            pool.wait_closed.assert_awaited_once_with()
        finally:
            engine.dispose()
