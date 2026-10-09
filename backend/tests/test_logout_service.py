"""退出业务离线测试；不连接数据库，不操作 Cookie。"""

from datetime import UTC, datetime
import unittest
from unittest.mock import MagicMock, patch

from pydantic import SecretStr
from sqlalchemy.exc import SQLAlchemyError

from app.core.tokens import hash_session_token
from app.services.errors import LogoutUnavailableError
from app.services.logout import logout_user


class LogoutServiceTest(unittest.TestCase):
    def setUp(self):
        self.token = SecretStr("a" * 43)
        self.factory = MagicMock()
        self.writer = self.factory.begin.return_value.__enter__.return_value

    def test_missing_or_malformed_token_succeeds_without_database(self):
        for token in (None, SecretStr(""), SecretStr("a" * 42), SecretStr("a" * 44),
                      SecretStr("中" * 43), SecretStr(" " * 43)):
            self.assertIsNone(logout_user(token, self.factory))
        self.factory.begin.assert_not_called()
        self.factory.assert_not_called()

    def test_conditional_update_only_current_hash_and_utc_revocation(self):
        now = datetime(2026, 9, 25, 12, tzinfo=UTC)
        with patch("app.services.logout.datetime") as clock:
            clock.now.return_value = now
            self.assertIsNone(logout_user(self.token, self.factory))
        statement = self.writer.execute.call_args.args[0]
        self.assertEqual(statement.compile().params, {
            "token_hash_1": hash_session_token(self.token),
            "expires_at_1": now.replace(tzinfo=None), "revoked_at": now.replace(tzinfo=None),
        })
        sql = str(statement)
        self.assertIn("UPDATE auth_sessions SET revoked_at=", sql)
        self.assertIn("auth_sessions.revoked_at IS NULL", sql)
        self.assertNotIn("user_id", sql)
        self.assertNotIn(self.token.get_secret_value(), str(statement.compile().params))
        self.writer.execute.assert_called_once()
        self.writer.delete.assert_not_called()
        self.factory.begin.return_value.__exit__.assert_called_once_with(None, None, None)

    def test_zero_affected_rows_is_success(self):
        self.writer.execute.return_value.rowcount = 0
        self.assertIsNone(logout_user(self.token, self.factory))

    def test_update_failure_is_redacted_and_not_retried(self):
        self.writer.execute.side_effect = SQLAlchemyError("private-token-dsn")
        with self.assertRaises(LogoutUnavailableError) as caught:
            logout_user(self.token, self.factory)
        self.assertEqual(caught.exception.code, "LOGOUT_UNAVAILABLE")
        self.assertNotIn("private-token-dsn", str(caught.exception))
        self.writer.execute.assert_called_once()
        self.factory.begin.return_value.__exit__.assert_called_once()

    def test_commit_failure_is_not_reported_as_success(self):
        self.factory.begin.return_value.__exit__.side_effect = SQLAlchemyError("private-commit")
        with self.assertRaises(LogoutUnavailableError) as caught:
            logout_user(self.token, self.factory)
        self.assertNotIn("private-commit", str(caught.exception))
        self.factory.begin.assert_called_once()

    def test_connection_failure_is_safe(self):
        self.factory.begin.return_value.__enter__.side_effect = SQLAlchemyError("private-host")
        with self.assertRaises(LogoutUnavailableError) as caught:
            logout_user(self.token, self.factory)
        self.assertNotIn("private-host", str(caught.exception))
        self.writer.execute.assert_not_called()
