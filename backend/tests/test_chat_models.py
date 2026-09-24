"""离线验证五张表的结构；不连接 MySQL，不执行 SQL，不以此替代迁移验收。"""

import unittest
from collections import Counter

from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.dialects import mysql
from sqlalchemy.dialects.mysql import BIGINT, CHAR, DATETIME, LONGTEXT, TEXT, VARCHAR
from sqlalchemy.orm import configure_mappers
from sqlalchemy.schema import CreateIndex, CreateTable

from app.db.base import Base
from app.models import AgentMemory, AuthSession, ChatSession, Message, User
from app.models.agent_memory import MEMORY_TOPIC_TYPES


def checks_for(model):
    """把 CHECK 文本的换行压成空格，方便验证；不会执行检查表达式。"""
    return {
        constraint.name: " ".join(str(constraint.sqltext).split())
        for constraint in model.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }


class ChatModelsTest(unittest.TestCase):
    def test_all_five_models_share_metadata_and_map_successfully(self):
        configure_mappers()
        models = (User, AuthSession, ChatSession, Message, AgentMemory)
        self.assertEqual(set(Base.metadata.tables), {model.__tablename__ for model in models})
        for model in models:
            with self.subTest(model=model.__name__):
                self.assertIs(model.__table__.metadata, Base.metadata)

    def test_columns_and_nullability_match_design(self):
        definitions = {
            AuthSession: (
                "auth_session_id user_id token_hash created_at expires_at revoked_at",
                {"revoked_at"},
            ),
            ChatSession: (
                "chat_session_id user_id title title_is_manual last_activity_at created_at updated_at",
                set(),
            ),
            Message: (
                "message_id chat_session_id role content in_reply_to_message_id client_message_key "
                "model_key created_at updated_at attempt_id retry_of_attempt_id generation_status "
                "generation_error_code generation_error_message",
                {"in_reply_to_message_id", "client_message_key", "model_key", "attempt_id",
                 "retry_of_attempt_id", "generation_status", "generation_error_code",
                 "generation_error_message"},
            ),
            AgentMemory: (
                "memory_id user_id memory_key memory_type summary created_at updated_at",
                set(),
            ),
        }
        for model, (names, nullable) in definitions.items():
            with self.subTest(model=model.__name__):
                columns = model.__table__.columns
                self.assertEqual(list(columns.keys()), names.split())
                self.assertEqual({column.name for column in columns if column.nullable}, nullable)
                self.assertEqual(list(model.__table__.primary_key.columns.keys()), [names.split()[0]])

    def test_ids_and_foreign_keys_use_unsigned_bigint_and_restrict(self):
        expected = {
            "auth_sessions.user_id": "users.user_id",
            "chat_sessions.user_id": "users.user_id",
            "messages.chat_session_id": "chat_sessions.chat_session_id",
            "messages.in_reply_to_message_id": "messages.message_id",
            "agent_memory.user_id": "users.user_id",
        }
        actual = {}
        for table in Base.metadata.tables.values():
            for column in table.columns:
                if column.primary_key or column.foreign_keys:
                    self.assertIsInstance(column.type, BIGINT)
                    self.assertTrue(column.type.unsigned)
                if column.primary_key:
                    self.assertTrue(column.autoincrement)
                    self.assertFalse(column.nullable)
                for foreign_key in column.foreign_keys:
                    actual[f"{table.name}.{column.name}"] = foreign_key.target_fullname
                    self.assertEqual(foreign_key.ondelete, "RESTRICT")
                    self.assertEqual(foreign_key.onupdate, "RESTRICT")
                    # 访问 .column 会实际解析目标，发现拼错表名/字段名时测试会失败。
                    self.assertIs(foreign_key.column.table.metadata, Base.metadata)
        self.assertEqual(actual, expected)
        self.assertFalse(Message.__table__.c.retry_of_attempt_id.foreign_keys)

    def test_unique_constraints_match_design(self):
        expected = {
            "users": {("email",)},
            "auth_sessions": {("token_hash",)},
            "chat_sessions": set(),
            "messages": {("chat_session_id", "client_message_key"),
                         ("in_reply_to_message_id",), ("attempt_id",)},
            "agent_memory": {("user_id", "memory_key")},
        }
        for table_name, unique_columns in expected.items():
            with self.subTest(table=table_name):
                actual = {
                    tuple(constraint.columns.keys())
                    for constraint in Base.metadata.tables[table_name].constraints
                    if isinstance(constraint, UniqueConstraint)
                }
                self.assertEqual(actual, unique_columns)

    def test_non_unique_indexes_match_design(self):
        expected = {
            "users": set(),
            "auth_sessions": {("user_id", "expires_at")},
            "chat_sessions": {("user_id", "last_activity_at", "chat_session_id")},
            "messages": {("chat_session_id", "created_at", "message_id"),
                         ("generation_status", "message_id")},
            "agent_memory": {("user_id", "updated_at", "memory_id")},
        }
        for table_name, indexes in expected.items():
            table = Base.metadata.tables[table_name]
            with self.subTest(table=table_name):
                self.assertEqual({tuple(index.columns.keys()) for index in table.indexes}, indexes)
                self.assertTrue(all(not index.unique for index in table.indexes))

    def test_text_types_lengths_and_binary_comparisons(self):
        expected = {
            (AuthSession, "token_hash"): (CHAR, 64),
            (Message, "role"): (VARCHAR, 16),
            (Message, "client_message_key"): (VARCHAR, 64),
            (Message, "attempt_id"): (CHAR, 36),
            (Message, "retry_of_attempt_id"): (CHAR, 36),
            (Message, "generation_status"): (VARCHAR, 16),
            (Message, "generation_error_code"): (VARCHAR, 64),
            (AgentMemory, "memory_key"): (VARCHAR, 64),
            (AgentMemory, "memory_type"): (VARCHAR, 32),
        }
        for (model, name), (column_type, length) in expected.items():
            with self.subTest(model=model.__name__, column=name):
                actual = model.__table__.c[name].type
                self.assertIsInstance(actual, column_type)
                self.assertEqual(actual.length, length)
                self.assertEqual(actual.charset, "ascii")
                self.assertEqual(actual.collation, "ascii_bin")
        self.assertIsInstance(Message.__table__.c.content.type, LONGTEXT)
        self.assertIsInstance(AgentMemory.__table__.c.summary.type, TEXT)
        self.assertEqual(Message.__table__.c.generation_error_message.type.length, 500)
        self.assertEqual(Message.__table__.c.model_key.type.length, 128)
        self.assertEqual(ChatSession.__table__.c.title.type.length, 100)

    def test_auth_and_chat_checks_and_defaults(self):
        self.assertEqual(checks_for(AuthSession), {
            "ck_auth_sessions_expiry": "expires_at > created_at",
            "ck_auth_sessions_revocation": "revoked_at IS NULL OR revoked_at >= created_at",
        })
        self.assertEqual(checks_for(ChatSession), {
            "ck_chat_sessions_title": "CHAR_LENGTH(title) BETWEEN 1 AND 100",
            "ck_chat_sessions_manual_title": "title_is_manual IN (0, 1)",
        })
        self.assertEqual(ChatSession.__table__.c.title.server_default.arg, "新对话")
        self.assertEqual(str(ChatSession.__table__.c.title_is_manual.server_default.arg), "0")

    def test_message_checks_explicitly_guard_nulls_and_roles(self):
        checks = checks_for(Message)
        self.assertEqual(set(checks), {
            "ck_messages_role", "ck_messages_content_not_empty",
            "ck_messages_user_content_length", "ck_messages_role_fields",
        })
        self.assertEqual(checks["ck_messages_role"], "role IN ('USER', 'ASSISTANT')")
        self.assertEqual(checks["ck_messages_content_not_empty"], "CHAR_LENGTH(content) > 0")
        self.assertEqual(checks["ck_messages_user_content_length"],
                         "role <> 'USER' OR CHAR_LENGTH(content) <= 20000")
        rule = checks["ck_messages_role_fields"]
        user_rule, assistant_rule = rule.split("OR (role = 'ASSISTANT'", 1)
        for name in ("client_message_key", "attempt_id", "generation_status",
                     "generation_error_code", "generation_error_message"):
            self.assertIn(f"{name} IS NOT NULL", user_rule)
            self.assertIn(f"{name} IS NULL", assistant_rule)
        for fragment in (
            "role = 'USER'", "in_reply_to_message_id IS NULL", "model_key IS NULL",
            "generation_status IN ('RUNNING', 'SUCCEEDED', 'FAILED')",
            "retry_of_attempt_id IS NULL OR retry_of_attempt_id <> attempt_id",
            "generation_status = 'FAILED'", "CHAR_LENGTH(generation_error_code) > 0",
            "CHAR_LENGTH(generation_error_message) > 0",
            "generation_status IN ('RUNNING', 'SUCCEEDED') AND generation_error_code IS NULL "
            "AND generation_error_message IS NULL",
        ):
            self.assertIn(fragment, user_rule)
        self.assertIn("retry_of_attempt_id IS NULL", assistant_rule)
        self.assertIn("in_reply_to_message_id IS NOT NULL", assistant_rule)

    def test_memory_has_exactly_25_documented_topic_pairs(self):
        groups = {
            "LEARNING_PREFERENCE": (
                "preference.language preference.explanation_style preference.detail_level "
                "preference.example_style preference.code_style preference.hint_style "
                "preference.pacing preference.knowledge_connections"
            ),
            "LEARNING_GOAL": "learning.goal learning.current_topic",
            "PROGRAMMING_BACKGROUND": (
                "programming.level programming.languages programming.known_concepts "
                "programming.tools programming.environment"
            ),
            "PERSONAL_BACKGROUND": (
                "personal.preferred_name personal.occupation personal.interests "
                "personal.long_term_goal personal.timezone personal.general_location"
            ),
            "DAILY_PREFERENCE": (
                "personal.daily_routine preference.conversation_tone "
                "lifestyle.food_preferences lifestyle.activity_preferences"
            ),
        }
        expected = {key: category for category, keys in groups.items() for key in keys.split()}
        self.assertEqual(dict(MEMORY_TOPIC_TYPES), expected)
        self.assertEqual(len(MEMORY_TOPIC_TYPES), 25)
        self.assertEqual(Counter(MEMORY_TOPIC_TYPES.values()), {
            "LEARNING_PREFERENCE": 8, "LEARNING_GOAL": 2, "PROGRAMMING_BACKGROUND": 5,
            "PERSONAL_BACKGROUND": 6, "DAILY_PREFERENCE": 4,
        })
        checks = checks_for(AgentMemory)
        self.assertEqual(checks["ck_agent_memory_summary"], "CHAR_LENGTH(summary) BETWEEN 1 AND 500")
        # 每个 OR 分支必须同时匹配 key 和 type，不能让合法 key 搭配错误分类。
        self.assertEqual(set(checks["ck_agent_memory_topic_type"].split(" OR ")), {
            f"(memory_key = '{key}' AND memory_type = '{category}')"
            for key, category in expected.items()
        })

    def test_time_columns_are_explicit_utc_values_not_automatic_updates(self):
        for table in Base.metadata.tables.values():
            for column in table.columns:
                if column.name.endswith("_at"):
                    with self.subTest(table=table.name, column=column.name):
                        self.assertIsInstance(column.type, DATETIME)
                        self.assertEqual(column.type.fsp, 6)
                        self.assertIsNone(column.default)
                        self.assertIsNone(column.server_default)
                        self.assertIsNone(column.onupdate)
                        self.assertIsNone(column.server_onupdate)

    def test_all_mysql_tables_and_indexes_compile_without_execution(self):
        # sorted_tables 同时核验跨表依赖可解析，不会执行 CREATE TABLE。
        tables = Base.metadata.sorted_tables
        order = [table.name for table in tables]
        self.assertLess(order.index("users"), order.index("chat_sessions"))
        self.assertLess(order.index("chat_sessions"), order.index("messages"))
        for table in tables:
            with self.subTest(table=table.name):
                ddl = str(CreateTable(table).compile(dialect=mysql.dialect()))
                self.assertIn(f"CREATE TABLE {table.name}", ddl)
                self.assertIn("ENGINE=InnoDB", ddl)
                self.assertIn("CHARSET=utf8mb4", ddl)
                for constraint in table.constraints:
                    if constraint.name:
                        self.assertIn(f"CONSTRAINT {constraint.name}", ddl)
                for index in table.indexes:
                    sql = str(CreateIndex(index).compile(dialect=mysql.dialect()))
                    self.assertIn(f"CREATE INDEX {index.name}", sql)


if __name__ == "__main__":
    unittest.main()
