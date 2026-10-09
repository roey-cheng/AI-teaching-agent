"""注册业务的离线测试：模拟数据库，真实 MySQL 验收由独立脚本运行。"""

from datetime import UTC, datetime
import unittest
from unittest.mock import MagicMock, patch

from pymysql.err import IntegrityError as MySQLIntegrityError
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.core.passwords import PasswordHashError
from app.db.session import build_session_factory
from app.models import User
from app.schemas import RegisterRequest
from app.services.auth import register_user
from app.services.errors import EmailAlreadyRegisteredError, RegistrationUnavailableError


class RegistrationServiceTest(unittest.TestCase):
    def setUp(self):
        self.request = RegisterRequest(email="  Student@Example.COM ", password="  Password123  ",
                                       display_name=" 小 雨 ")
        self.factory = MagicMock()
        self.reader = self.factory.return_value.__enter__.return_value
        self.reader.scalar.return_value = None
        self.writer = self.factory.begin.return_value.__enter__.return_value

        def assign_id():
            self.writer.add.call_args.args[0].user_id = 123

        self.writer.flush.side_effect = assign_id

    def test_success_uses_normalized_values_and_returns_only_public_fields(self):
        with patch("app.services.auth.hash_password", return_value="fixture-hash") as hash_fn:
            response = register_user(self.request, self.factory)
        user = self.writer.add.call_args.args[0]
        self.assertIsInstance(user, User)
        self.assertEqual((user.email, user.display_name), ("student@example.com", "小 雨"))
        self.assertEqual((user.password_hash, user.status, user.system_role), ("fixture-hash", "ACTIVE", "USER"))
        self.assertIsNone(user.last_login_at)
        self.assertIsNone(user.created_at.tzinfo)
        self.assertEqual(user.created_at, user.updated_at)
        self.assertLess(abs((datetime.now(UTC).replace(tzinfo=None) - user.created_at).total_seconds()), 5)
        hash_fn.assert_called_once_with(self.request.password)
        self.assertEqual(set(response.model_dump()), {"user_id", "email", "display_name", "created_at"})
        self.assertEqual(response.user_id, "123")
        self.assertEqual(response.created_at.tzinfo, UTC)
        self.assertNotIn("fixture-hash", response.model_dump_json())
        self.assertNotIn("Password123", response.model_dump_json())
        self.writer.add.assert_called_once()  # 不写登录/聊天/记忆等其他记录。
        self.factory.begin.return_value.__exit__.assert_called_once_with(None, None, None)

    def test_hashing_occurs_after_read_session_closes_before_write_transaction(self):
        def hash_fn(password):
            self.factory.return_value.__exit__.assert_called_once_with(None, None, None)
            self.factory.begin.assert_not_called()
            self.assertEqual(password.get_secret_value(), "  Password123  ")
            return "fixture-hash"

        with patch("app.services.auth.hash_password", side_effect=hash_fn):
            register_user(self.request, self.factory)
        query = self.reader.scalar.call_args.args[0]
        self.assertEqual(query.compile().params, {"email_1": "student@example.com"})
        self.assertNotIn("Password123", str(query))

    def test_existing_email_rejected_before_hashing_or_writing(self):
        self.reader.scalar.return_value = 12
        with patch("app.services.auth.hash_password") as hash_fn:
            with self.assertRaises(EmailAlreadyRegisteredError) as caught:
                register_user(self.request, self.factory)
        self.assertEqual(caught.exception.code, "EMAIL_ALREADY_REGISTERED")
        hash_fn.assert_not_called()
        self.factory.begin.assert_not_called()
        self.factory.return_value.__exit__.assert_called_once()

    def test_concurrent_duplicate_is_mapped_to_email_error(self):
        for key in ("uq_users_email", "users.uq_users_email"):
            error = IntegrityError("INSERT", {}, MySQLIntegrityError(1062, f"Duplicate entry 'private' for key '{key}'"))
            self.writer.flush.side_effect = error
            with self.subTest(key=key), patch("app.services.auth.hash_password", return_value="hash"):
                with self.assertRaises(EmailAlreadyRegisteredError) as caught:
                    register_user(self.request, self.factory)
            self.assertNotIn("private", str(caught.exception))
            self.assertTrue(caught.exception.__suppress_context__)
            self.assertIs(self.factory.begin.return_value.__exit__.call_args.args[1], error)

    def test_other_integrity_errors_are_not_misreported_as_email_conflicts(self):
        for number, detail in ((1062, "Duplicate entry 'x' for key 'PRIMARY'"),
                               (3819, "Check constraint failed"), (1048, "Column cannot be null")):
            self.writer.flush.side_effect = IntegrityError("INSERT", {}, MySQLIntegrityError(number, detail))
            with self.subTest(number=number), patch("app.services.auth.hash_password", return_value="hash"):
                with self.assertRaises(RegistrationUnavailableError):
                    register_user(self.request, self.factory)

    def test_query_failure_is_safe_and_does_not_hash_or_write(self):
        self.reader.scalar.side_effect = SQLAlchemyError("fixture-password")
        with patch("app.services.auth.hash_password") as hash_fn:
            with self.assertRaises(RegistrationUnavailableError) as caught:
                register_user(self.request, self.factory)
        hash_fn.assert_not_called()
        self.factory.begin.assert_not_called()
        self.assertNotIn("fixture-password", str(caught.exception))

    def test_hash_failure_does_not_start_write_transaction(self):
        with patch("app.services.auth.hash_password", side_effect=PasswordHashError("safe")):
            with self.assertRaises(RegistrationUnavailableError):
                register_user(self.request, self.factory)
        self.factory.begin.assert_not_called()

    def test_commit_failure_does_not_return_success_or_retry(self):
        self.factory.begin.return_value.__exit__.side_effect = SQLAlchemyError("private-connection-info")
        with patch("app.services.auth.hash_password", return_value="fixture-hash"):
            with self.assertRaises(RegistrationUnavailableError) as caught:
                register_user(self.request, self.factory)
        self.assertNotIn("private-connection-info", str(caught.exception))
        self.assertEqual(caught.exception.code, "REGISTRATION_UNAVAILABLE")
        self.writer.add.assert_called_once()
        self.factory.begin.assert_called_once()

    def test_response_validation_failure_leaves_transaction_via_error_path(self):
        with patch("app.services.auth.hash_password", return_value="fixture-hash"), \
             patch("app.services.auth.RegisterResponse.model_validate", side_effect=ValueError("invalid public data")):
            with self.assertRaises(ValueError):
                register_user(self.request, self.factory)
        self.assertEqual(self.factory.begin.return_value.__exit__.call_args.args[0], ValueError)

    def test_session_factory_is_lazy_and_creates_independent_sessions(self):
        with patch("pymysql.connect") as connect:
            engine = create_engine("mysql+pymysql://fixture:fixture@127.0.0.1/fixture")
            try:
                factory = build_session_factory(engine)
                with factory() as first, factory() as second:
                    self.assertIsNot(first, second)
                    self.assertIs(first.bind, engine)
                    self.assertFalse(first.autoflush)
                    self.assertFalse(first.expire_on_commit)
                connect.assert_not_called()
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
