"""供隔离 MySQL 验收器加载；不单独连接数据库，也不读取 .env。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC
from threading import Barrier
import unittest
from unittest.mock import patch
from uuid import uuid4

from argon2 import PasswordHasher
from sqlalchemy import event, func, select
from sqlalchemy.exc import SQLAlchemyError

from app.core.passwords import PasswordHashError, hash_password
from app.db.session import build_session_factory
from app.models import AgentMemory, AuthSession, ChatSession, Message, User
from app.schemas import RegisterRequest, RegisterResponse
from app.services.auth import register_user
from app.services.errors import EmailAlreadyRegisteredError, RegistrationUnavailableError


class RegistrationMySQLTest(unittest.TestCase):
    engine = None  # 仅由创建临时容器的脚本注入。

    def setUp(self):
        self.factory = build_session_factory(self.engine)
        self.email = f"registration-{uuid4().hex}@example.com"
        self.request = RegisterRequest(email=self.email, password="  Secret-fixture 密码🙂  ",
                                       display_name="  新用户🙂  ")

    def count_email(self):
        with self.factory() as session:
            return session.scalar(select(func.count()).select_from(User).where(User.email == self.email))

    def test_registration_persists_hash_and_public_response_without_auto_login(self):
        response = register_user(self.request, self.factory)
        self.assertIsInstance(response, RegisterResponse)
        self.assertEqual(set(response.model_dump()), {"user_id", "email", "display_name", "created_at"})
        self.assertEqual(response.created_at.tzinfo, UTC)
        self.assertEqual(response.display_name, "新用户🙂")
        # 用全新的 Session 查询，证明已经提交，而不是只存在于内存中。
        with self.factory() as session:
            row = session.get(User, int(response.user_id))
            self.assertIsNotNone(row)
            self.assertEqual((row.email, row.status, row.system_role), (self.email, "ACTIVE", "USER"))
            self.assertIsNone(row.last_login_at)
            self.assertEqual(row.created_at, row.updated_at)
            self.assertEqual(response.created_at.replace(tzinfo=None), row.created_at)
            self.assertTrue(row.password_hash.startswith("$argon2id$"))
            self.assertTrue(PasswordHasher().verify(row.password_hash, self.request.password.get_secret_value()))
            self.assertNotIn(row.password_hash, response.model_dump_json())
            for model in (AuthSession, ChatSession, Message, AgentMemory):
                self.assertEqual(session.scalar(select(func.count()).select_from(model)), 0)

    def test_normalized_duplicate_is_rejected_and_sessions_remain_usable(self):
        response = register_user(self.request, self.factory)
        duplicate = RegisterRequest(email=f"  {self.email.upper()}  ", password="different-password",
                                    display_name="Other")
        with patch("app.services.auth.hash_password") as hasher:
            with self.assertRaises(EmailAlreadyRegisteredError):
                register_user(duplicate, self.factory)
            hasher.assert_not_called()
        self.assertEqual(self.count_email(), 1)
        with self.factory() as session:
            row = session.get(User, int(response.user_id))
            self.assertEqual(row.display_name, "新用户🙂")
        another = RegisterRequest(email=f"another-{uuid4().hex}@example.com", password="new-password",
                                  display_name="Another")
        self.assertIsInstance(register_user(another, self.factory), RegisterResponse)

    def test_concurrent_registrations_only_one_succeeds(self):
        barrier = Barrier(2)

        def synchronized_hash(password):
            hashed = hash_password(password)
            # 强制两个请求都完成查重后才开始写入，真实触发唯一约束兜底。
            barrier.wait(timeout=15)
            return hashed

        def attempt():
            request = RegisterRequest(email=self.email, password="concurrent-password", display_name="Concurrent")
            try:
                register_user(request, self.factory)
                return "created"
            except EmailAlreadyRegisteredError:
                return "duplicate"

        with patch("app.services.auth.hash_password", side_effect=synchronized_hash):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(attempt) for _ in range(2)]
                outcomes = [future.result(timeout=30) for future in futures]
        self.assertCountEqual(outcomes, ["created", "duplicate"])
        self.assertEqual(self.count_email(), 1)

    def test_failure_before_commit_rolls_back_actual_insert(self):
        def fail_before_commit(session):
            raise SQLAlchemyError("fixture-private-database-error")

        # 此时 flush 已执行 INSERT；人为让提交前失败，检验真实事务回滚。
        event.listen(self.factory.class_, "before_commit", fail_before_commit)
        try:
            with self.assertRaises(RegistrationUnavailableError) as caught:
                register_user(self.request, self.factory)
        finally:
            event.remove(self.factory.class_, "before_commit", fail_before_commit)
        self.assertNotIn("fixture-private", str(caught.exception))
        self.assertEqual(self.count_email(), 0)
        # 失败关闭 Session 后，可以重新完成正常注册。
        register_user(self.request, self.factory)
        self.assertEqual(self.count_email(), 1)

    def test_hash_failure_does_not_create_account(self):
        with patch("app.services.auth.hash_password", side_effect=PasswordHashError("safe failure")):
            with self.assertRaises(RegistrationUnavailableError):
                register_user(self.request, self.factory)
        self.assertEqual(self.count_email(), 0)
