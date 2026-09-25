"""显式运行的真实 MySQL 验收：自建临时容器，不使用项目数据库凭据，不接受外部目标。"""

from datetime import datetime, timedelta
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import unittest
from uuid import uuid4

from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DBAPIError

# 支持从项目根目录直接运行本文件。
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.core.config import DatabaseSettings
from app.db.engine import build_database_engine
from app.db.base import Base
from app.models import AgentMemory, AuthSession, ChatSession, Message, User
from app.models.agent_memory import MEMORY_TOPIC_TYPES


def docker(*args, env=None):
    result = subprocess.run(
        ["docker", *args], env=env, capture_output=True, text=True, timeout=45,
    )
    if result.returncode:
        # 不输出容器日志或配置；临时凭据也不写到输出中。
        raise RuntimeError(f"Docker {args[0]} failed; check Docker availability and permissions.")
    return result.stdout.strip()


class MySQLMigrationTest(unittest.TestCase):
    """仅由 main 注入本脚本新建的临时 MySQL Engine。"""

    engine = None

    def setUp(self):
        self.connection = self.engine.connect()
        self.addCleanup(self.connection.close)
        transaction = self.connection.begin()
        self.addCleanup(transaction.rollback)
        self.now = datetime(2026, 9, 25, 0, 0, 0, 123456)
        self.user_id = self.insert(User, self.user(email="first@example.com"))
        self.other_user_id = self.insert(User, self.user(email="second@example.com"))
        self.session_id = self.insert(ChatSession, self.chat(self.user_id))
        self.other_session_id = self.insert(ChatSession, self.chat(self.other_user_id))

    def insert(self, model, values):
        return self.connection.execute(model.__table__.insert().values(**values)).inserted_primary_key[0]

    def user(self, **changes):
        return dict(email="new@example.com", password_hash="fixture-only-not-a-real-hash",
                    display_name="测试🙂", created_at=self.now, updated_at=self.now) | changes

    def chat(self, user_id, **changes):
        return dict(user_id=user_id, created_at=self.now, updated_at=self.now,
                    last_activity_at=self.now) | changes

    def question(self, **changes):
        return dict(chat_session_id=self.session_id, role="USER", content="解释 Python🙂",
                    client_message_key=str(uuid4()), attempt_id=str(uuid4()),
                    generation_status="RUNNING", created_at=self.now, updated_at=self.now) | changes

    def answer(self, question_id, **changes):
        return dict(chat_session_id=self.session_id, role="ASSISTANT", content="这是回答🙂",
                    in_reply_to_message_id=question_id, created_at=self.now,
                    updated_at=self.now) | changes

    def memory(self, **changes):
        return dict(user_id=self.user_id, memory_key="preference.language",
                    memory_type="LEARNING_PREFERENCE", summary="喜欢中文🙂",
                    created_at=self.now, updated_at=self.now) | changes

    def rejected(self, statement, code):
        # 一条非法语句不污染后面用例；只接受预期数据库错误编号。
        with self.assertRaises(DBAPIError) as caught:
            with self.connection.begin_nested():
                self.connection.execute(statement)
        self.assertEqual(caught.exception.orig.args[0], code)

    def test_real_table_definitions_and_version(self):
        inspector = inspect(self.connection)
        self.assertEqual(set(inspector.get_table_names()), set(Base.metadata.tables) | {"alembic_version"})
        self.assertEqual(self.connection.scalar(text("SELECT version_num FROM alembic_version")), "20260925_0001")
        for name, table in Base.metadata.tables.items():
            with self.subTest(table=name):
                self.assertEqual([c["name"] for c in inspector.get_columns(name)], list(table.columns.keys()))
                ddl = self.connection.exec_driver_sql(f"SHOW CREATE TABLE `{name}`").one()[1]
                self.assertIn("ENGINE=InnoDB", ddl)
                self.assertIn("CHARSET=utf8mb4", ddl)
                self.assertIn("datetime(6)", ddl)
                self.assertIn("AUTO_INCREMENT", ddl)
                for constraint in table.constraints:
                    if constraint.name:
                        self.assertIn(f"`{constraint.name}`", ddl)
                actual_indexes = {i["name"]: i["column_names"] for i in inspector.get_indexes(name)}
                for index in table.indexes:
                    self.assertEqual(actual_indexes[index.name], list(index.columns.keys()))
                for column in table.columns:
                    collation = getattr(column.type, "collation", None)
                    if collation:
                        actual = self.connection.scalar(text(
                            "SELECT COLLATION_NAME FROM information_schema.COLUMNS "
                            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table AND COLUMN_NAME = :column"
                        ), {"table": name, "column": column.name})
                        self.assertEqual(actual, collation)

    def test_valid_rows_defaults_unicode_and_multiple_nulls(self):
        for token in ("a" * 64, "b" * 64):
            self.insert(AuthSession, dict(user_id=self.user_id, token_hash=token, created_at=self.now,
                                         expires_at=self.now + timedelta(days=7)))
        # 多行 USER 的 reply=NULL，多行 ASSISTANT 的 attempt/key=NULL 均合法。
        for _ in range(2):
            question = self.insert(Message, self.question(generation_status="SUCCEEDED"))
            self.insert(Message, self.answer(question))
        for key, category in MEMORY_TOPIC_TYPES.items():
            self.insert(AgentMemory, self.memory(memory_key=key, memory_type=category))
        user = self.connection.execute(select(User.__table__).where(User.user_id == self.user_id)).one()
        self.assertEqual((user.status, user.system_role), ("ACTIVE", "USER"))
        self.assertEqual(user.display_name, "测试🙂")
        self.assertEqual(user.created_at.microsecond, 123456)
        chat = self.connection.execute(select(ChatSession.__table__).where(ChatSession.chat_session_id == self.session_id)).one()
        self.assertEqual(chat.title, "新对话")
        self.assertFalse(chat.title_is_manual)

    def test_unique_email_token_message_reply_attempt_and_memory(self):
        self.rejected(User.__table__.insert().values(**self.user(email="first@example.com")), 1062)
        login = dict(user_id=self.user_id, token_hash="a" * 64, created_at=self.now,
                     expires_at=self.now + timedelta(days=7))
        self.insert(AuthSession, login)
        self.rejected(AuthSession.__table__.insert().values(**login), 1062)
        values = self.question()
        question = self.insert(Message, values)
        self.rejected(Message.__table__.insert().values(**(values | {"attempt_id": str(uuid4())})), 1062)
        self.rejected(Message.__table__.insert().values(**(values | {"client_message_key": str(uuid4())})), 1062)
        self.insert(Message, self.answer(question))
        self.rejected(Message.__table__.insert().values(**self.answer(question)), 1062)
        self.insert(AgentMemory, self.memory())
        self.rejected(AgentMemory.__table__.insert().values(**self.memory()), 1062)
        # 同一消息键可用于另一会话；同一记忆主题可属于另一用户。
        self.insert(Message, values | {"chat_session_id": self.other_session_id, "attempt_id": str(uuid4())})
        self.insert(AgentMemory, self.memory(user_id=self.other_user_id))

    def test_user_auth_and_chat_checks(self):
        for changes in ({"email": ""}, {"password_hash": ""}, {"display_name": ""},
                        {"status": "OTHER"}, {"system_role": "ROOT"}):
            with self.subTest(changes=changes):
                self.rejected(User.__table__.insert().values(**self.user(**changes)), 3819)
        for changes in ({"expires_at": self.now}, {"revoked_at": self.now - timedelta(seconds=1)}):
            values = dict(user_id=self.user_id, token_hash="c" * 64, created_at=self.now,
                          expires_at=self.now + timedelta(days=7)) | changes
            self.rejected(AuthSession.__table__.insert().values(**values), 3819)
        for title in ("", "x" * 101):
            self.rejected(ChatSession.__table__.insert().values(**self.chat(self.user_id, title=title)),
                          3819 if not title else 1406)
        self.rejected(text("UPDATE chat_sessions SET title_is_manual = 2 WHERE chat_session_id = :id").bindparams(id=self.session_id), 3819)

    def test_user_message_checks(self):
        cases = [
            {"role": "SYSTEM"}, {"content": ""}, {"content": "中" * 20001},
            {"attempt_id": None}, {"client_message_key": None}, {"generation_status": None},
            {"generation_status": "QUEUED"}, {"generation_status": "FAILED"},
            {"generation_status": "FAILED", "generation_error_code": "", "generation_error_message": "Error"},
            {"generation_status": "FAILED", "generation_error_code": "ERROR", "generation_error_message": ""},
            {"generation_error_code": "ERROR"}, {"model_key": "model"},
        ]
        for changes in cases:
            with self.subTest(fields=list(changes)):
                self.rejected(Message.__table__.insert().values(**self.question(**changes)), 3819)
        values = self.question()
        values["retry_of_attempt_id"] = values["attempt_id"]
        self.rejected(Message.__table__.insert().values(**values), 3819)
        self.insert(Message, self.question(content="中" * 20000))
        self.insert(Message, self.question(generation_status="FAILED", generation_error_code="ERROR", generation_error_message="Failed"))

    def test_assistant_message_checks(self):
        question = self.insert(Message, self.question())
        for changes in ({"in_reply_to_message_id": None}, {"attempt_id": str(uuid4())},
                        {"retry_of_attempt_id": str(uuid4())}, {"client_message_key": str(uuid4())},
                        {"generation_status": "RUNNING"}, {"generation_error_code": "ERROR"},
                        {"generation_error_message": "Failed"}):
            self.rejected(Message.__table__.insert().values(**self.answer(question, **changes)), 3819)

    def test_memory_topic_type_and_length_checks(self):
        for changes in ({"memory_key": "unknown.topic"}, {"memory_type": "LEARNING_GOAL"},
                        {"summary": ""}, {"summary": "中" * 501}):
            self.rejected(AgentMemory.__table__.insert().values(**self.memory(**changes)), 3819)
        self.insert(AgentMemory, self.memory(summary="中" * 500))

    def test_foreign_keys_and_restrict_delete_update(self):
        missing = 18446744073709551614
        self.rejected(ChatSession.__table__.insert().values(**self.chat(missing)), 1452)
        self.rejected(AgentMemory.__table__.insert().values(**self.memory(user_id=missing)), 1452)
        self.rejected(AuthSession.__table__.insert().values(user_id=missing, token_hash="d" * 64,
                      created_at=self.now, expires_at=self.now + timedelta(days=1)), 1452)
        self.rejected(Message.__table__.insert().values(**self.question(chat_session_id=missing)), 1452)
        self.rejected(Message.__table__.insert().values(**self.answer(missing)), 1452)
        self.rejected(User.__table__.delete().where(User.user_id == self.user_id), 1451)
        self.rejected(User.__table__.update().where(User.user_id == self.user_id).values(user_id=missing), 1451)
        question = self.insert(Message, self.question())
        self.insert(Message, self.answer(question))
        self.rejected(ChatSession.__table__.delete().where(ChatSession.chat_session_id == self.session_id), 1451)
        self.rejected(Message.__table__.delete().where(Message.message_id == question), 1451)

    def test_dml_transaction_rollback(self):
        # 用独立连接验证整笔 DML 事务回滚，不把 DDL 的行为混为一谈。
        with self.engine.connect() as connection:
            transaction = connection.begin()
            user_id = connection.execute(User.__table__.insert().values(**self.user(email="rollback@example.com"))).inserted_primary_key[0]
            connection.execute(ChatSession.__table__.insert().values(**self.chat(user_id)))
            transaction.rollback()
            self.assertIsNone(connection.scalar(select(User.user_id).where(User.user_id == user_id)))
            self.assertIsNone(connection.scalar(select(ChatSession.chat_session_id).where(ChatSession.user_id == user_id)))


def main():
    name = f"ai-chat-migration-test-{uuid4().hex}"
    database = "migration_test"
    password = secrets.token_hex(24)
    container_id = None
    engine = None
    marker = "ai-chat-migration-test"
    try:
        # 只用现有镜像，不拉取镜像；不挂载项目目录或现有数据库数据卷。
        docker("image", "inspect", "mysql:8.4", "--format", "{{.Id}}")
        container_env = os.environ | {"MYSQL_ROOT_PASSWORD": password}
        container_id = docker(
            "create", "--pull=never", "--name", name,
            "--label", f"purpose={marker}", "--tmpfs", "/var/lib/mysql:rw",
            "--publish", "127.0.0.1::3306", "--env", "MYSQL_ROOT_PASSWORD",
            "--env", "MYSQL_ROOT_HOST=%", "--env", f"MYSQL_DATABASE={database}",
            "mysql:8.4", "--character-set-server=utf8mb4", "--collation-server=utf8mb4_unicode_ci",
            env=container_env,
        )
        print(f"Created isolated test container: {name}", flush=True)
        # 先记录创建结果，再启动；即使启动失败，finally 也能清理本次容器。
        docker("start", container_id)
        binding = docker("port", container_id, "3306/tcp")
        if not binding.startswith("127.0.0.1:") or "\n" in binding:
            raise RuntimeError("Unexpected test container port binding.")
        port = int(binding.rsplit(":", 1)[1])
        if port == 3306:
            raise RuntimeError("Refusing the project database port.")
        settings = DatabaseSettings(_env_file=None, host="127.0.0.1", port=port,
                                    user="root", password=password, name=database)
        engine = build_database_engine(settings)
        deadline = time.monotonic() + 90
        while True:
            try:
                with engine.connect() as connection:
                    if connection.scalar(text("SELECT DATABASE()")) != database:
                        raise RuntimeError("Unexpected database target.")
                    version = connection.scalar(text("SELECT VERSION()"))
                break
            except DBAPIError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Temporary MySQL did not become ready in time.") from None
                time.sleep(2)
        print(f"Temporary MySQL {version} ready on loopback port {port}.", flush=True)

        # 所有 DB_* 都覆盖成临时目标；只在子进程传递，不编辑用户的 .env。
        migration_env = {key: value for key, value in os.environ.items() if not key.startswith("DB_")}
        migration_env.update(DB_HOST="127.0.0.1", DB_PORT=str(port), DB_USER="root",
                             DB_PASSWORD=password, DB_NAME=database)

        def migrate(*arguments):
            result = subprocess.run(
                [sys.executable, "-m", "alembic", "-c", str(BACKEND / "alembic.ini"), *arguments],
                cwd=BACKEND, env=migration_env, capture_output=True, text=True, timeout=45,
            )
            if result.returncode:
                # 输出限于迁移 CLI 的已脱敏提示，不打印临时凭据。
                detail = (result.stdout + result.stderr).replace(password, "[redacted]")
                raise RuntimeError(f"Alembic {' '.join(arguments)} failed: {detail}")
            print(f"PASS: alembic {' '.join(arguments)}", flush=True)

        migrate("upgrade", "head")
        migrate("current")
        migrate("check")
        MySQLMigrationTest.engine = engine
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(MySQLMigrationTest)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        if not result.wasSuccessful():
            return 1
        # head 重跑不能重复建表或删除数据。
        with engine.begin() as connection:
            connection.execute(User.__table__.insert().values(
                email="preserve@example.com", password_hash="fixture", display_name="Keep",
                created_at=datetime(2026, 9, 25), updated_at=datetime(2026, 9, 25),
            ))
        migrate("upgrade", "head")
        with engine.connect() as connection:
            if connection.scalar(text("SELECT COUNT(*) FROM users WHERE email = 'preserve@example.com'")) != 1:
                raise RuntimeError("Repeated upgrade did not preserve data.")
        print("PASS: repeated upgrade preserves data", flush=True)
        migrate("downgrade", "base")
        with engine.connect() as connection:
            if set(inspect(connection).get_table_names()) != {"alembic_version"}:
                raise RuntimeError("Downgrade left unexpected business tables.")
            if connection.scalar(text("SELECT COUNT(*) FROM alembic_version")) != 0:
                raise RuntimeError("Downgrade left a revision entry.")
        migrate("upgrade", "head")
        migrate("check")
        print("PASS: upgrade -> downgrade -> upgrade on real MySQL", flush=True)
        return 0
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f"FAIL: {error}", file=sys.stderr, flush=True)
        return 1
    finally:
        if engine is not None:
            engine.dispose()
        if container_id is not None:
            # 只删除刚创建且标记吻合的容器；不调用 compose down，不删除任何现有数据卷。
            actual_name = docker("inspect", "--format", "{{.Name}}", container_id)
            actual_label = docker("inspect", "--format", '{{index .Config.Labels "purpose"}}', container_id)
            if actual_name != f"/{name}" or actual_label != marker:
                raise RuntimeError("Refusing cleanup: temporary container identity mismatch.")
            docker("rm", "--force", container_id)
            print(f"Removed temporary container and disposable test data: {name}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
