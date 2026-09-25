"""使用假配置和模拟连接，不访问真实 .env 或数据库。"""

import os
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import MagicMock, patch

from sqlalchemy.exc import SQLAlchemyError

from app.core.config import DatabaseSettings
from app.db import sqlalchemy_check
from app.db.engine import build_database_engine


class SQLAlchemyCheckTest(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ, {}, clear=True):
            self.settings = DatabaseSettings(
                _env_file=None, name="fake_db", user="fake_user", password="fake@secret:/#%"
            )

    def test_engine_uses_driver_settings_timeouts_and_safe_logging(self):
        with patch("app.db.engine.create_engine") as create:
            self.assertIs(build_database_engine(self.settings), create.return_value)
        create.assert_called_once()
        url = create.call_args.args[0]
        self.assertEqual(url.drivername, "mysql+pymysql")
        self.assertEqual(url.username, "fake_user")
        self.assertEqual(url.password, "fake@secret:/#%")
        self.assertEqual(url.host, "127.0.0.1")
        self.assertEqual(url.port, 3306)
        self.assertEqual(url.database, "fake_db")
        self.assertEqual(url.query, {"charset": "utf8mb4"})
        self.assertNotIn("fake@secret:/#%", str(url))
        self.assertEqual(create.call_args.kwargs, {
            "echo": False, "hide_parameters": True, "pool_pre_ping": True,
            "connect_args": {"connect_timeout": 5, "read_timeout": 5,
                             "write_timeout": 5, "autocommit": False},
        })

    def test_building_a_real_engine_does_not_connect(self):
        with patch("pymysql.connect") as connect:
            engine = build_database_engine(self.settings)
            try:
                self.assertEqual(engine.dialect.name, "mysql")
                self.assertEqual(engine.dialect.driver, "pymysql")
                connect.assert_not_called()
            finally:
                engine.dispose()

    def run_check(self, engine):
        output = StringIO()
        with patch.object(sqlalchemy_check, "load_database_settings", return_value=self.settings), \
             patch.object(sqlalchemy_check, "build_database_engine", return_value=engine), \
             redirect_stdout(output):
            result = sqlalchemy_check.main()
        self.assertNotIn("fake@secret:/#%", output.getvalue())
        return result, output.getvalue()

    def test_success_runs_only_select_and_releases_connections(self):
        engine = MagicMock()
        connection = engine.connect.return_value.__enter__.return_value
        connection.execute.return_value.scalar_one.return_value = 1
        result, output = self.run_check(engine)
        self.assertEqual(result, 0)
        self.assertIn("connection successful", output)
        connection.execute.assert_called_once()
        self.assertEqual(str(connection.execute.call_args.args[0]), "SELECT 1")
        connection.commit.assert_not_called()
        engine.connect.return_value.__exit__.assert_called_once()
        engine.dispose.assert_called_once_with()

    def test_connection_failure_still_disposes_engine(self):
        engine = MagicMock()
        engine.connect.side_effect = SQLAlchemyError("fake@secret:/#%")
        result, output = self.run_check(engine)
        self.assertEqual(result, 1)
        self.assertIn("failed", output)
        engine.dispose.assert_called_once_with()

    def test_query_failure_releases_connection_and_disposes_engine(self):
        engine = MagicMock()
        connection = engine.connect.return_value.__enter__.return_value
        connection.execute.side_effect = SQLAlchemyError("fake@secret:/#%")
        result, _ = self.run_check(engine)
        self.assertEqual(result, 1)
        engine.connect.return_value.__exit__.assert_called_once()
        engine.dispose.assert_called_once_with()

    def test_unexpected_result_is_not_reported_as_success(self):
        engine = MagicMock()
        engine.connect.return_value.__enter__.return_value.execute.return_value.scalar_one.return_value = 2
        result, output = self.run_check(engine)
        self.assertEqual(result, 1)
        self.assertNotIn("connection successful", output)
        engine.dispose.assert_called_once_with()

    def test_configuration_failure_does_not_build_engine_or_leak_error(self):
        with patch.object(sqlalchemy_check, "load_database_settings", side_effect=OSError("fake-secret")), \
             patch.object(sqlalchemy_check, "build_database_engine") as build, \
             redirect_stdout(StringIO()) as output:
            self.assertEqual(sqlalchemy_check.main(), 1)
        build.assert_not_called()
        self.assertNotIn("fake-secret", output.getvalue())

    def test_engine_creation_failure_is_reported_safely(self):
        with patch.object(sqlalchemy_check, "load_database_settings", return_value=self.settings), \
             patch.object(sqlalchemy_check, "build_database_engine", side_effect=SQLAlchemyError("fake-secret")), \
             redirect_stdout(StringIO()) as output:
            self.assertEqual(sqlalchemy_check.main(), 1)
        self.assertNotIn("fake-secret", output.getvalue())
