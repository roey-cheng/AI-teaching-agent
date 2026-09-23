import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from app.core.config import BACKEND_ENV_FILE, DatabaseSettings


class DatabaseSettingsTest(unittest.TestCase):
    def test_default_env_path_is_backend_not_project_root(self):
        self.assertEqual(BACKEND_ENV_FILE, Path(__file__).resolve().parents[1] / ".env")

    def test_read_dotenv_and_allow_environment_override(self):
        # 只使用临时文件和虚构密码，不读取真实 .env，也不连接数据库。
        with TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "DB_HOST=127.0.0.1\nDB_PORT=3306\nDB_NAME=test_db\n"
                "DB_USER=test_user\nDB_PASSWORD='fake$pass#123'\nAPP_ENV=development\n",
                encoding="utf-8",
            )
            settings = DatabaseSettings(_env_file=env_file)
            self.assertEqual(settings.name, "test_db")
            self.assertEqual(settings.port, 3306)
            self.assertEqual(settings.password.get_secret_value(), "fake$pass#123")
            self.assertNotIn("fake$pass#123", repr(settings))
            with patch.dict(os.environ, {"DB_PORT": "3307"}):
                self.assertEqual(DatabaseSettings(_env_file=env_file).port, 3307)

    def test_missing_or_blank_password_is_rejected(self):
        with patch.dict(os.environ, {}, clear=True):
            for password_fields in ({}, {"password": ""}, {"password": "   "}):
                with self.subTest(password_fields=password_fields), self.assertRaises(ValidationError):
                    DatabaseSettings(_env_file=None, name="test_db", user="test_user", **password_fields)

    def test_invalid_port_is_rejected_without_showing_secret(self):
        with patch.dict(os.environ, {}, clear=True):
            for port in (0, 65536, "not-a-port"):
                with self.subTest(port=port), self.assertRaises(ValidationError) as captured:
                    DatabaseSettings(_env_file=None, name="test_db", user="test_user", password="fake-secret", port=port)
                self.assertNotIn("fake-secret", str(captured.exception))
