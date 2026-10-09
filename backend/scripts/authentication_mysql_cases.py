"""登录状态检查的隔离 MySQL 测试；只使用测试脚本注入的 Engine。"""

from datetime import UTC, datetime, timedelta
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import select, update

from app.core.tokens import create_session_token, hash_session_token
from app.db.session import build_session_factory
from app.models import AuthSession, User
from app.schemas import LoginRequest, RegisterRequest
from app.services.auth import register_user
from app.services.authentication import get_current_user
from app.services.errors import AuthenticationRequiredError
from app.services.login import login_user


class AuthenticationMySQLTest(unittest.TestCase):
    engine = None

    def setUp(self):
        self.factory = build_session_factory(self.engine)
        self.email = f"authenticate-{uuid4().hex}@example.com"
        self.user = register_user(RegisterRequest(
            email=self.email, password="test-password", display_name="Current user",
        ), self.factory)
        self.request = LoginRequest(email=self.email, password="test-password")
        self.login = login_user(self.request, self.factory)

    def snapshot(self):
        with self.factory() as session:
            login = session.execute(select(AuthSession.__table__).where(
                AuthSession.token_hash == hash_session_token(self.login.token))).one()
            user = session.execute(select(User.__table__).where(
                User.user_id == int(self.user.user_id))).one()
            return tuple(login), tuple(user)

    def test_valid_login_survives_new_factory_and_does_not_modify_records(self):
        before = self.snapshot()
        for _ in range(2):
            result = get_current_user(self.login.token, build_session_factory(self.engine))
            self.assertEqual(result.model_dump(), {
                "user_id": self.user.user_id, "email": self.email, "display_name": "Current user",
            })
        self.assertEqual(self.snapshot(), before)

    def test_two_users_resolve_to_their_own_identity_and_unknown_token_fails(self):
        other_email = f"other-{uuid4().hex}@example.com"
        other = register_user(RegisterRequest(email=other_email, password="test-password",
                                              display_name="Other user"), self.factory)
        other_login = login_user(LoginRequest(email=other_email, password="test-password"), self.factory)
        self.assertEqual(get_current_user(self.login.token, self.factory).user_id, self.user.user_id)
        self.assertEqual(get_current_user(other_login.token, self.factory).user_id, other.user_id)
        with self.assertRaises(AuthenticationRequiredError):
            get_current_user(create_session_token(), self.factory)

    def test_exact_expiry_and_past_fail_without_deleting_or_renewing(self):
        now = datetime.now(UTC)
        with self.factory.begin() as session:
            session.execute(update(AuthSession).where(
                AuthSession.token_hash == hash_session_token(self.login.token)).values(
                    created_at=now.replace(tzinfo=None) - timedelta(days=7),
                    expires_at=now.replace(tzinfo=None)))
        before = self.snapshot()
        with patch("app.services.authentication.datetime") as clock:
            for delta in (-1, 0, 1):
                clock.now.return_value = now + timedelta(microseconds=delta)
                if delta < 0:
                    self.assertEqual(get_current_user(self.login.token, self.factory).user_id, self.user.user_id)
                else:
                    with self.assertRaises(AuthenticationRequiredError):
                        get_current_user(self.login.token, self.factory)
        self.assertEqual(self.snapshot(), before)

    def test_rotated_token_fails_but_replacement_and_other_device_still_work(self):
        other_device = login_user(self.request, self.factory)
        replacement = login_user(self.request, self.factory, current_token=self.login.token)
        with self.assertRaises(AuthenticationRequiredError):
            get_current_user(self.login.token, self.factory)
        for token in (replacement.token, other_device.token):
            self.assertEqual(get_current_user(token, self.factory).user_id, self.user.user_id)

    def test_disabling_account_blocks_previously_valid_token(self):
        self.assertEqual(get_current_user(self.login.token, self.factory).user_id, self.user.user_id)
        with self.factory.begin() as session:
            session.execute(update(User).where(User.user_id == int(self.user.user_id)).values(status="DISABLED"))
        with self.assertRaises(AuthenticationRequiredError):
            get_current_user(self.login.token, self.factory)
