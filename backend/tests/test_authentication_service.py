"""登录状态检查的离线测试；不连接 MySQL，不读取 Cookie。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from pydantic import SecretStr
from sqlalchemy.exc import SQLAlchemyError

from app.core.tokens import hash_session_token
from app.services.authentication import get_current_user
from app.services.errors import AuthenticationRequiredError, AuthenticationUnavailableError


class AuthenticationServiceTest(unittest.TestCase):
    def setUp(self):
        self.token = SecretStr("a" * 43)
        self.factory = MagicMock()
        self.reader = self.factory.return_value.__enter__.return_value
        self.now = datetime(2026, 9, 25, tzinfo=UTC)
        self.record = SimpleNamespace(user_id=123, email="student@example.com", display_name="学生",
                                      status="ACTIVE", revoked_at=None,
                                      expires_at=self.now.replace(tzinfo=None) + timedelta(days=1))
        self.reader.execute.return_value.one_or_none.return_value = self.record
        clock = patch("app.services.authentication.datetime")
        self.clock = clock.start()
        self.clock.now.return_value = self.now
        self.addCleanup(clock.stop)

    def test_success_returns_only_public_user_fields_without_writes(self):
        result = get_current_user(self.token, self.factory)
        self.assertEqual(result.model_dump(), {
            "user_id": "123", "email": "student@example.com", "display_name": "学生",
        })
        self.assertEqual(self.record.expires_at, datetime(2026, 9, 26))
        for operation in ("add", "flush", "commit", "delete"):
            getattr(self.reader, operation).assert_not_called()
        self.factory.begin.assert_not_called()
        self.factory.return_value.__exit__.assert_called_once_with(None, None, None)

    def test_missing_or_malformed_token_does_not_open_database(self):
        for token in (None, SecretStr(""), SecretStr("a" * 42), SecretStr("a" * 44),
                      SecretStr("中" * 43), SecretStr(" " * 43)):
            with self.assertRaises(AuthenticationRequiredError):
                get_current_user(token, self.factory)
        self.factory.assert_not_called()

    def test_unknown_token_is_rejected(self):
        self.reader.execute.return_value.one_or_none.return_value = None
        with self.assertRaises(AuthenticationRequiredError) as caught:
            get_current_user(self.token, self.factory)
        self.assertEqual(caught.exception.code, "UNAUTHENTICATED")

    def test_expiration_boundary_and_past_are_rejected_future_is_valid(self):
        for delta in (-1, 0, 1):
            self.record.expires_at = self.now.replace(tzinfo=None) + timedelta(microseconds=delta)
            if delta <= 0:
                with self.assertRaises(AuthenticationRequiredError):
                    get_current_user(self.token, self.factory)
            else:
                self.assertEqual(get_current_user(self.token, self.factory).user_id, "123")

    def test_revoked_token_is_rejected(self):
        self.record.revoked_at = self.now.replace(tzinfo=None)
        with self.assertRaises(AuthenticationRequiredError):
            get_current_user(self.token, self.factory)

    def test_disabled_account_is_rejected(self):
        self.record.status = "DISABLED"
        with self.assertRaises(AuthenticationRequiredError):
            get_current_user(self.token, self.factory)

    def test_query_joins_owner_and_uses_hash_never_raw_token(self):
        get_current_user(self.token, self.factory)
        statement = self.reader.execute.call_args.args[0]
        self.assertEqual(statement.compile().params, {"token_hash_1": hash_session_token(self.token)})
        self.assertIn("auth_sessions.user_id = users.user_id", str(statement))
        self.assertNotIn("password_hash", str(statement))
        self.reader.execute.assert_called_once()

    def test_database_failure_is_distinct_redacted_and_not_retried(self):
        self.reader.execute.side_effect = SQLAlchemyError("private-dsn-and-token")
        with self.assertRaises(AuthenticationUnavailableError) as caught:
            get_current_user(self.token, self.factory)
        self.assertEqual(caught.exception.code, "AUTHENTICATION_UNAVAILABLE")
        self.assertNotIn("private-dsn", str(caught.exception))
        self.reader.execute.assert_called_once()
        self.factory.return_value.__exit__.assert_called_once()
