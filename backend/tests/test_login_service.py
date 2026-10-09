"""登录业务离线测试；不连接数据库、不生成浏览器 Cookie。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from pydantic import SecretStr
from sqlalchemy.exc import SQLAlchemyError

from app.core.passwords import DUMMY_PASSWORD_HASH, hash_password, verify_password
from app.core.tokens import create_session_token, hash_session_token
from app.models import AuthSession, User
from app.schemas import LoginRequest
from app.services.errors import InvalidCredentialsError, LoginUnavailableError
from app.services.login import login_user


class LoginServiceTest(unittest.TestCase):
    def setUp(self):
        self.request = LoginRequest(email=" STUDENT@EXAMPLE.COM ", password="  Password123  ")
        self.factory = MagicMock()
        self.reader = self.factory.return_value.__enter__.return_value
        self.reader.execute.return_value.one_or_none.return_value = SimpleNamespace(
            user_id=123, password_hash="fixture-hash", status="ACTIVE",
        )
        self.writer = self.factory.begin.return_value.__enter__.return_value
        self.user = User(user_id=123, email="student@example.com", password_hash="fixture-hash",
                         display_name="学生", status="ACTIVE", system_role="USER",
                         created_at=datetime(2026, 9, 24), updated_at=datetime(2026, 9, 24))
        self.writer.scalar.return_value = self.user

    def test_success_stores_only_hash_and_returns_public_body_plus_internal_token(self):
        with patch("app.services.login.verify_password", return_value=True):
            result = login_user(self.request, self.factory)
        saved = self.writer.add.call_args.args[0]
        self.assertIsInstance(saved, AuthSession)
        self.assertEqual(saved.token_hash, hash_session_token(result.token))
        self.assertNotEqual(saved.token_hash, result.token.get_secret_value())
        self.assertEqual(saved.expires_at - saved.created_at, timedelta(days=7))
        self.assertEqual(result.expires_at, saved.expires_at.replace(tzinfo=UTC))
        self.assertEqual((saved.user_id, saved.revoked_at), (123, None))
        self.assertEqual(self.user.last_login_at, saved.created_at)
        self.assertEqual(self.user.updated_at, saved.created_at)
        self.assertEqual(self.user.created_at, datetime(2026, 9, 24))
        self.assertEqual(result.response.model_dump(), {
            "user": {"user_id": "123", "email": "student@example.com", "display_name": "学生"},
        })
        self.assertNotIn(result.token.get_secret_value(), repr(result))
        self.assertNotIn(result.token.get_secret_value(), result.response.model_dump_json())
        self.writer.execute.assert_not_called()  # 没有旧凭据就不做撤销。
        self.factory.begin.return_value.__exit__.assert_called_once_with(None, None, None)

    def test_password_verification_is_outside_database_transaction(self):
        def verify(password, stored_hash):
            self.factory.return_value.__exit__.assert_called_once_with(None, None, None)
            self.factory.begin.assert_not_called()
            self.assertEqual(password.get_secret_value(), "  Password123  ")
            self.assertEqual(stored_hash, "fixture-hash")
            return True

        with patch("app.services.login.verify_password", side_effect=verify):
            login_user(self.request, self.factory)
        self.assertEqual(self.reader.execute.call_args.args[0].compile().params, {"email_1": "student@example.com"})

    def test_unknown_wrong_password_and_disabled_share_one_error(self):
        outcomes = []
        for missing, valid, status in ((True, True, "ACTIVE"), (False, False, "ACTIVE"), (False, True, "DISABLED")):
            snapshot = None if missing else SimpleNamespace(user_id=123, password_hash="hash", status=status)
            self.reader.execute.return_value.one_or_none.return_value = snapshot
            with patch("app.services.login.verify_password", return_value=valid) as verify, \
                 patch("app.services.login.create_session_token") as create:
                with self.assertRaises(InvalidCredentialsError) as caught:
                    login_user(self.request, self.factory)
                outcomes.append((caught.exception.code, str(caught.exception)))
                self.assertEqual(verify.call_args.args[1], DUMMY_PASSWORD_HASH if missing else "hash")
                create.assert_not_called()
            self.factory.begin.assert_not_called()
        self.assertEqual(len(set(outcomes)), 1)

    def test_account_change_during_verification_is_rejected(self):
        for field, value in (("status", "DISABLED"), ("password_hash", "changed-hash")):
            with self.subTest(field=field), patch.object(self.user, field, value), \
                 patch("app.services.login.verify_password", return_value=True):
                with self.assertRaises(InvalidCredentialsError):
                    login_user(self.request, self.factory)
        self.writer.add.assert_not_called()
        self.writer.execute.assert_not_called()

    def test_deleted_account_is_rejected(self):
        self.writer.scalar.return_value = None
        with patch("app.services.login.verify_password", return_value=True):
            with self.assertRaises(InvalidCredentialsError):
                login_user(self.request, self.factory)
        self.writer.add.assert_not_called()

    def test_rotation_uses_hash_not_raw_old_cookie(self):
        old = create_session_token()
        with patch("app.services.login.verify_password", return_value=True):
            result = login_user(self.request, self.factory, current_token=old)
        query = self.writer.execute.call_args.args[0]
        params = query.compile().params
        self.assertIn(hash_session_token(old), params.values())
        self.assertNotIn(old.get_secret_value(), params.values())
        self.assertNotEqual(old.get_secret_value(), result.token.get_secret_value())

    def test_malformed_old_cookie_is_ignored(self):
        with patch("app.services.login.verify_password", return_value=True):
            login_user(self.request, self.factory, current_token=SecretStr("bad-cookie"))
        self.writer.execute.assert_not_called()

    def test_database_failure_and_commit_failure_are_safe_without_retry(self):
        self.reader.execute.side_effect = SQLAlchemyError("fixture-secret")
        with self.assertRaises(LoginUnavailableError) as caught:
            login_user(self.request, self.factory)
        self.assertNotIn("fixture-secret", str(caught.exception))
        self.factory.begin.assert_not_called()
        self.reader.execute.side_effect = None
        self.factory.begin.return_value.__exit__.side_effect = SQLAlchemyError("private-dsn")
        with patch("app.services.login.verify_password", return_value=True):
            with self.assertRaises(LoginUnavailableError) as caught:
                login_user(self.request, self.factory)
        self.assertEqual(caught.exception.code, "LOGIN_UNAVAILABLE")
        self.assertNotIn("private-dsn", str(caught.exception))
        self.factory.begin.assert_called_once()


class LoginCryptoTest(unittest.TestCase):
    def test_password_verification_preserves_spaces_case_and_rejects_bad_hash(self):
        password = SecretStr("  Case-sensitive密码  ")
        hashed = hash_password(password)
        self.assertTrue(verify_password(password, hashed))
        for wrong in ("Case-sensitive密码", "  case-sensitive密码  ", "wrong", "**********"):
            self.assertFalse(verify_password(SecretStr(wrong), hashed))
        for bad_hash in ("", "broken", "$argon2id$malformed"):
            self.assertFalse(verify_password(password, bad_hash))

    def test_dummy_hash_is_valid_but_never_a_real_account(self):
        self.assertTrue(verify_password(SecretStr("dummy-login-comparison-only"), DUMMY_PASSWORD_HASH))

    def test_token_entropy_format_hash_and_masking(self):
        first, second = create_session_token(), create_session_token()
        self.assertNotEqual(first.get_secret_value(), second.get_secret_value())
        self.assertRegex(first.get_secret_value(), r"^[A-Za-z0-9_-]{43}$")
        self.assertRegex(hash_session_token(first), r"^[0-9a-f]{64}$")
        self.assertEqual(hash_session_token(first), hash_session_token(SecretStr(first.get_secret_value())))
        self.assertNotIn(first.get_secret_value(), repr(first))
        with patch("app.core.tokens.secrets.token_urlsafe", return_value="a" * 43) as generate:
            create_session_token()
            generate.assert_called_once_with(32)

    def test_invalid_tokens_are_not_hashed(self):
        for value in ("", "a" * 42, "a" * 44, " " * 43, "中" * 43, "a" * 10000):
            with self.subTest(length=len(value)):
                self.assertIsNone(hash_session_token(SecretStr(value)))


if __name__ == "__main__":
    unittest.main()
