from contextlib import redirect_stdout
from io import StringIO
import os
import unittest
from unittest.mock import MagicMock, patch

import pymysql

from app.core.config import DatabaseSettings
from app.db import check
from app.db.connection import open_database_connection


class DatabaseCheckTest(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ, {}, clear=True):
            self.settings = DatabaseSettings(
                _env_file=None, name="test_db", user="test_user", password="fake-secret"
            )

    def test_connection_parameters_use_settings_and_timeouts(self):
        with patch("app.db.connection.pymysql.connect") as connect:
            self.assertIs(open_database_connection(self.settings), connect.return_value)
            connect.assert_called_once_with(
                host="127.0.0.1", port=3306, user="test_user", password="fake-secret",
                database="test_db", charset="utf8mb4", connect_timeout=5,
                read_timeout=5, write_timeout=5, autocommit=False,
            )

    def run_check(self, connection):
        output = StringIO()
        with patch.object(check, "load_database_settings", return_value=self.settings), \
             patch.object(check, "open_database_connection", return_value=connection), \
             redirect_stdout(output):
            result = check.main()
        return result, output.getvalue()

    def test_success_closes_connection_and_runs_only_select(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (1,)
        result, output = self.run_check(connection)
        self.assertEqual(result, 0)
        cursor.execute.assert_called_once_with("SELECT 1")
        connection.close.assert_called_once()
        self.assertIn("连接成功", output)

    def test_query_failure_closes_connection_without_leaking_error(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.execute.side_effect = pymysql.OperationalError("fake-secret")
        result, output = self.run_check(connection)
        self.assertEqual(result, 1)
        connection.close.assert_called_once()
        self.assertNotIn("fake-secret", output)

    def test_unexpected_result_is_failure_and_closes_connection(self):
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value.fetchone.return_value = (2,)
        result, _ = self.run_check(connection)
        self.assertEqual(result, 1)
        connection.close.assert_called_once()

    def test_connection_failure_is_reported_without_secret(self):
        output = StringIO()
        with patch.object(check, "load_database_settings", return_value=self.settings), \
             patch.object(check, "open_database_connection", side_effect=pymysql.OperationalError("fake-secret")), \
             redirect_stdout(output):
            self.assertEqual(check.main(), 1)
        self.assertNotIn("fake-secret", output.getvalue())

    def test_configuration_failure_does_not_open_connection(self):
        with patch.object(check, "load_database_settings", side_effect=OSError("fake-secret")), \
             patch.object(check, "open_database_connection") as connect, \
             redirect_stdout(StringIO()) as output:
            self.assertEqual(check.main(), 1)
        connect.assert_not_called()
        self.assertNotIn("fake-secret", output.getvalue())
