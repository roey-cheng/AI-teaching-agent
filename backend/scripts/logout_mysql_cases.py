"""退出登录的真实 MySQL 验收；只使用临时测试容器，不读取项目配置。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
import unittest
from unittest.mock import patch
from uuid import uuid4

from pydantic import SecretStr
from sqlalchemy import event, select, update
from sqlalchemy.exc import SQLAlchemyError

from app.core.tokens import create_session_token, hash_session_token
from app.db.session import build_session_factory
from app.models import AgentMemory, AuthSession, ChatSession, Message, User
from app.schemas import LoginRequest, RegisterRequest
from app.services.auth import register_user
from app.services.authentication import get_current_user
from app.services.errors import AuthenticationRequiredError, LogoutUnavailableError
from app.services.login import login_user
from app.services.logout import logout_user


class LogoutMySQLTest(unittest.TestCase):
    engine = None

    def setUp(self):
        self.factory = build_session_factory(self.engine)
        email = f"logout-{uuid4().hex}@example.com"
        self.user_id = int(register_user(RegisterRequest(
            email=email, password="test-password", display_name="Logout tester",
        ), self.factory).user_id)
        self.request = LoginRequest(email=email, password="test-password")
        self.login = login_user(self.request, self.factory)

    def login_record(self):
        with self.factory() as session:
            return session.execute(select(AuthSession.__table__).where(
                AuthSession.token_hash == hash_session_token(self.login.token))).one()

    def test_login_check_logout_rejects_token_and_preserves_business_data(self):
        now = datetime.now(UTC).replace(tzinfo=None)
        with self.factory.begin() as session:
            chat = ChatSession(user_id=self.user_id, created_at=now, updated_at=now, last_activity_at=now)
            session.add(chat)
            session.flush()
            session.add(Message(chat_session_id=chat.chat_session_id, role="USER", content="Keep this question",
                                client_message_key=str(uuid4()), attempt_id=str(uuid4()),
                                generation_status="RUNNING", created_at=now, updated_at=now))
            session.add(AgentMemory(user_id=self.user_id, memory_key="preference.language",
                                    memory_type="LEARNING_PREFERENCE", summary="喜欢中文",
                                    created_at=now, updated_at=now))
        def snapshot():
            with self.factory() as session:
                return [session.execute(select(model.__table__)).all()
                        for model in (User, ChatSession, Message, AgentMemory)]
        before = snapshot()
        self.assertEqual(get_current_user(self.login.token, self.factory).user_id, str(self.user_id))
        self.assertIsNone(logout_user(self.login.token, self.factory))
        self.assertIsNotNone(self.login_record().revoked_at)
        with self.assertRaises(AuthenticationRequiredError):
            get_current_user(self.login.token, self.factory)
        self.assertEqual(snapshot(), before)

    def test_repeat_unknown_missing_and_expired_tokens_are_success(self):
        logout_user(self.login.token, self.factory)
        revoked = tuple(self.login_record())
        for token in (self.login.token, create_session_token(), None, SecretStr("bad")):
            self.assertIsNone(logout_user(token, self.factory))
        self.assertEqual(tuple(self.login_record()), revoked)
        now = datetime.now(UTC)
        expired = login_user(self.request, self.factory)
        with self.factory.begin() as session:
            session.execute(update(AuthSession).where(AuthSession.token_hash == hash_session_token(expired.token))
                            .values(created_at=now.replace(tzinfo=None) - timedelta(days=7),
                                    expires_at=now.replace(tzinfo=None)))
        with patch("app.services.logout.datetime") as clock:
            for delta in (0, 1):
                clock.now.return_value = now + timedelta(seconds=delta)
                self.assertIsNone(logout_user(expired.token, self.factory))
        with self.factory() as session:
            row = session.scalar(select(AuthSession).where(AuthSession.token_hash == hash_session_token(expired.token)))
            self.assertIsNone(row.revoked_at)
            self.assertEqual(row.expires_at, now.replace(tzinfo=None))

    def test_other_device_and_other_user_remain_authenticated(self):
        other_device = login_user(self.request, self.factory)
        email = f"other-logout-{uuid4().hex}@example.com"
        other_user = register_user(RegisterRequest(email=email, password="test-password", display_name="Other"), self.factory)
        other_login = login_user(LoginRequest(email=email, password="test-password"), self.factory)
        logout_user(self.login.token, self.factory)
        self.assertEqual(get_current_user(other_device.token, self.factory).user_id, str(self.user_id))
        self.assertEqual(get_current_user(other_login.token, self.factory).user_id, other_user.user_id)

    def test_disabled_user_can_still_revoke_token(self):
        with self.factory.begin() as session:
            session.execute(update(User).where(User.user_id == self.user_id).values(status="DISABLED"))
        self.assertIsNone(logout_user(self.login.token, self.factory))
        self.assertIsNotNone(self.login_record().revoked_at)

    def test_failure_before_commit_rolls_back_revocation(self):
        def fail_before_commit(session):
            raise SQLAlchemyError("fixture-private-error")
        event.listen(self.factory, "before_commit", fail_before_commit)
        try:
            with self.assertRaises(LogoutUnavailableError):
                logout_user(self.login.token, self.factory)
        finally:
            event.remove(self.factory, "before_commit", fail_before_commit)
        self.assertIsNone(self.login_record().revoked_at)
        self.assertEqual(get_current_user(self.login.token, self.factory).user_id, str(self.user_id))

    def test_concurrent_logout_calls_both_succeed(self):
        barrier = Barrier(2)
        def run():
            barrier.wait(timeout=10)
            return logout_user(self.login.token, self.factory)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(run) for _ in range(2)]
            self.assertEqual([result.result(timeout=15) for result in results], [None, None])
        self.assertIsNotNone(self.login_record().revoked_at)
        with self.assertRaises(AuthenticationRequiredError):
            get_current_user(self.login.token, self.factory)
