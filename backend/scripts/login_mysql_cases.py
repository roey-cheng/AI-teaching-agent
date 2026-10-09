"""登录业务的隔离 MySQL 验收；只接收测试容器 Engine，不读取项目配置。"""

from datetime import UTC, datetime, timedelta
import unittest
from unittest.mock import patch
from uuid import uuid4

from pydantic import SecretStr
from sqlalchemy import event, select, update
from sqlalchemy.exc import SQLAlchemyError

from app.core.passwords import verify_password
from app.core.tokens import hash_session_token
from app.db.session import build_session_factory
from app.models import AuthSession, User
from app.schemas import LoginRequest, RegisterRequest
from app.services.auth import register_user
from app.services.errors import InvalidCredentialsError, LoginUnavailableError
from app.services.login import login_user


class LoginMySQLTest(unittest.TestCase):
    engine = None

    def setUp(self):
        self.factory = build_session_factory(self.engine)
        self.email = f"login-{uuid4().hex}@example.com"
        self.password = "  Case-sensitive 密码🙂  "
        self.user_id = int(register_user(RegisterRequest(
            email=self.email, password=self.password, display_name="Login tester",
        ), self.factory).user_id)
        self.request = LoginRequest(email=self.email, password=self.password)

    def login_rows(self):
        with self.factory() as session:
            return list(session.scalars(select(AuthSession).where(AuthSession.user_id == self.user_id)))

    def test_success_persists_hash_seven_day_expiry_and_login_times(self):
        with self.factory() as session:
            created = session.get(User, self.user_id).created_at
        result = login_user(self.request, self.factory)
        rows = self.login_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.token_hash, hash_session_token(result.token))
        self.assertNotEqual(row.token_hash, result.token.get_secret_value())
        self.assertEqual(row.expires_at - row.created_at, timedelta(days=7))
        self.assertEqual(result.expires_at, row.expires_at.replace(tzinfo=UTC))
        self.assertIsNone(row.revoked_at)
        with self.factory() as session:
            user = session.get(User, self.user_id)
            self.assertEqual(user.last_login_at, row.created_at)
            self.assertEqual(user.updated_at, row.created_at)
            self.assertEqual(user.created_at, created)
        self.assertEqual(result.response.user.user_id, str(self.user_id))
        self.assertNotIn(result.token.get_secret_value(), repr(result))
        self.assertNotIn("token", result.response.model_dump_json())

    def test_invalid_unknown_and_disabled_accounts_do_not_create_sessions(self):
        requests = [LoginRequest(email=self.email, password="wrong-password"),
                    LoginRequest(email=f"missing-{uuid4().hex}@example.com", password=self.password)]
        failures = []
        for request in requests:
            with self.assertRaises(InvalidCredentialsError) as caught:
                login_user(request, self.factory)
            failures.append(str(caught.exception))
        with self.factory.begin() as session:
            session.execute(update(User).where(User.user_id == self.user_id).values(status="DISABLED"))
        with self.assertRaises(InvalidCredentialsError) as caught:
            login_user(self.request, self.factory)
        failures.append(str(caught.exception))
        self.assertEqual(len(set(failures)), 1)
        self.assertEqual(self.login_rows(), [])
        with self.factory() as session:
            self.assertIsNone(session.get(User, self.user_id).last_login_at)

    def test_rotation_revokes_only_current_cookie_and_preserves_other_device(self):
        current = login_user(self.request, self.factory)
        other = login_user(self.request, self.factory)
        with self.factory() as session:
            other_expiry = session.scalar(select(AuthSession.expires_at).where(
                AuthSession.token_hash == hash_session_token(other.token)))
        replacement = login_user(self.request, self.factory, current_token=current.token)
        rows = {row.token_hash: row for row in self.login_rows()}
        self.assertEqual(len(rows), 3)
        self.assertIsNotNone(rows[hash_session_token(current.token)].revoked_at)
        self.assertIsNone(rows[hash_session_token(other.token)].revoked_at)
        self.assertEqual(rows[hash_session_token(other.token)].expires_at, other_expiry)
        self.assertIsNone(rows[hash_session_token(replacement.token)].revoked_at)
        self.assertEqual(len({current.token.get_secret_value(), other.token.get_secret_value(),
                              replacement.token.get_secret_value()}), 3)

    def test_switch_account_revokes_presented_cookie_not_all_old_account_sessions(self):
        current = login_user(self.request, self.factory)
        other = login_user(self.request, self.factory)
        email = f"switch-{uuid4().hex}@example.com"
        second_id = register_user(RegisterRequest(email=email, password="second-password", display_name="Second"), self.factory).user_id
        result = login_user(LoginRequest(email=email, password="second-password"), self.factory,
                            current_token=current.token)
        self.assertEqual(result.response.user.user_id, second_id)
        rows = {row.token_hash: row for row in self.login_rows()}
        self.assertIsNotNone(rows[hash_session_token(current.token)].revoked_at)
        self.assertIsNone(rows[hash_session_token(other.token)].revoked_at)

    def test_wrong_password_does_not_revoke_existing_session(self):
        old = login_user(self.request, self.factory)
        request = LoginRequest(email=self.email, password="incorrect")
        with self.assertRaises(InvalidCredentialsError):
            login_user(request, self.factory, current_token=old.token)
        rows = self.login_rows()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].revoked_at)

    def test_expired_and_malformed_old_tokens_do_not_block_valid_login(self):
        old = login_user(self.request, self.factory)
        now = datetime.now(UTC).replace(tzinfo=None)
        with self.factory.begin() as session:
            session.execute(update(AuthSession).where(AuthSession.token_hash == hash_session_token(old.token)).values(
                created_at=now - timedelta(days=8), expires_at=now - timedelta(days=1),
            ))
        login_user(self.request, self.factory, current_token=old.token)
        login_user(self.request, self.factory, current_token=SecretStr("malformed"))
        rows = {row.token_hash: row for row in self.login_rows()}
        self.assertEqual(len(rows), 3)
        self.assertIsNone(rows[hash_session_token(old.token)].revoked_at)

    def test_commit_failure_rolls_back_rotation_new_row_and_login_timestamp(self):
        old = login_user(self.request, self.factory)
        with self.factory() as session:
            before = session.get(User, self.user_id).last_login_at

        def fail(session):
            raise SQLAlchemyError("private-fixture-error")

        event.listen(self.factory.class_, "before_commit", fail)
        try:
            with self.assertRaises(LoginUnavailableError):
                login_user(self.request, self.factory, current_token=old.token)
        finally:
            event.remove(self.factory.class_, "before_commit", fail)
        rows = self.login_rows()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].revoked_at)
        with self.factory() as session:
            self.assertEqual(session.get(User, self.user_id).last_login_at, before)

    def test_account_disabled_between_password_check_and_write_is_rejected(self):
        def disable_after_verify(password, hashed):
            matched = verify_password(password, hashed)
            with self.factory.begin() as session:
                session.execute(update(User).where(User.user_id == self.user_id).values(status="DISABLED"))
            return matched

        with patch("app.services.login.verify_password", side_effect=disable_after_verify):
            with self.assertRaises(InvalidCredentialsError):
                login_user(self.request, self.factory)
        self.assertEqual(self.login_rows(), [])
