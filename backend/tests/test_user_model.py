"""只检查 Python 表描述和生成的 SQL 文本；不连接 MySQL，不执行建表。"""

import unittest
from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DefaultClause, String, Table, UniqueConstraint, inspect
from sqlalchemy.dialects import mysql
from sqlalchemy.dialects.mysql import BIGINT, DATETIME, VARCHAR
from sqlalchemy.schema import CreateTable

from app.db.base import Base
from app.models import User
from testing_support import require_type


class UserModelTest(unittest.TestCase):
    def test_model_is_registered_with_expected_columns(self):
        table = require_type(User.__table__, Table)
        self.assertIs(Base.metadata.tables["users"], table)
        self.assertEqual(require_type(inspect(User).local_table, Table).name, "users")
        self.assertEqual(
            list(table.columns.keys()),
            [
                "user_id",
                "email",
                "password_hash",
                "display_name",
                "status",
                "system_role",
                "created_at",
                "updated_at",
                "last_login_at",
            ],
        )
        self.assertEqual(list(table.primary_key.columns.keys()), ["user_id"])
        self.assertTrue(require_type(table.c.user_id.type, BIGINT).unsigned)
        self.assertTrue(table.c.user_id.autoincrement)
        self.assertEqual(
            [column.name for column in table.columns if column.nullable], ["last_login_at"]
        )

    def test_string_types_and_comparison_rules(self):
        columns = require_type(User.__table__, Table).c
        for name, length in {
            "email": 320,
            "password_hash": 255,
            "display_name": 255,
            "status": 16,
            "system_role": 16,
        }.items():
            with self.subTest(name=name):
                self.assertEqual(require_type(columns[name].type, String).length, length)
        self.assertEqual(require_type(columns.email.type, String).collation, "utf8mb4_bin")
        for name in ("password_hash", "status", "system_role"):
            self.assertEqual(require_type(columns[name].type, VARCHAR).charset, "ascii")
            self.assertEqual(require_type(columns[name].type, String).collation, "ascii_bin")

    def test_unique_and_check_constraints_are_declared(self):
        constraints = require_type(User.__table__, Table).constraints
        unique = [c for c in constraints if isinstance(c, UniqueConstraint)]
        self.assertEqual(
            [(c.name, list(c.columns.keys())) for c in unique], [("uq_users_email", ["email"])]
        )
        checks = {c.name: str(c.sqltext) for c in constraints if isinstance(c, CheckConstraint)}
        self.assertEqual(
            checks,
            {
                "ck_users_email_not_empty": "CHAR_LENGTH(email) > 0",
                "ck_users_password_hash_not_empty": "CHAR_LENGTH(password_hash) > 0",
                "ck_users_display_name_not_empty": "CHAR_LENGTH(display_name) > 0",
                "ck_users_status": "status IN ('ACTIVE', 'DISABLED')",
                "ck_users_system_role": "system_role IN ('USER', 'ADMIN')",
            },
        )

    def test_defaults_and_explicit_timestamp_management(self):
        columns = require_type(User.__table__, Table).c
        self.assertEqual(require_type(columns.status.server_default, DefaultClause).arg, "ACTIVE")
        self.assertEqual(
            require_type(columns.system_role.server_default, DefaultClause).arg, "USER"
        )
        for name in ("created_at", "updated_at", "last_login_at"):
            column = columns[name]
            self.assertEqual(require_type(column.type, DATETIME).fsp, 6)
            self.assertIsNone(column.default)
            self.assertIsNone(column.server_default)
            self.assertIsNone(column.onupdate)
            self.assertIsNone(column.server_onupdate)

    def test_mysql_ddl_can_be_compiled_without_execution(self):
        # compile 只生成字符串，不执行这段 CREATE TABLE。
        ddl = str(CreateTable(require_type(User.__table__, Table)).compile(dialect=mysql.dialect()))
        for expected in (
            "CREATE TABLE users",
            "BIGINT UNSIGNED NOT NULL AUTO_INCREMENT",
            "PRIMARY KEY (user_id)",
            "CONSTRAINT uq_users_email UNIQUE (email)",
            "DATETIME(6)",
            "DEFAULT 'ACTIVE'",
            "DEFAULT 'USER'",
            "ENGINE=InnoDB",
            "CHARSET=utf8mb4",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, ddl)

    def test_constructing_an_object_does_not_persist_it(self):
        # 明确这是 UTC；MySQL DATETIME 不保存时区，所以写入模型前转换为无时区 UTC。
        now = datetime(2026, 9, 24, 0, 0, tzinfo=UTC).replace(tzinfo=None)
        user = User(
            email="example@example.com",
            password_hash="dummy-test-hash",
            display_name="小雨",
            status="ACTIVE",
            system_role="USER",
            created_at=now,
            updated_at=now,
        )
        self.assertTrue(inspect(user).transient)
        self.assertIsNone(user.user_id)
        self.assertIsNone(user.last_login_at)
        self.assertEqual(user.display_name, "小雨")
