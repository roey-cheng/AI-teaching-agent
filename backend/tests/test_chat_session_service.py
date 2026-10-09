"""会话管理业务离线测试；不连接数据库，不调用模型。"""

from datetime import UTC, datetime
import unittest
from unittest.mock import MagicMock, patch

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.models import ChatSession
from app.schemas import RenameSessionRequest, UserResponse
from app.services.chat_sessions import create_chat_session, list_chat_sessions, rename_chat_session
from app.services.errors import SessionNotFoundError, SessionUnavailableError


class CreateChatSessionTest(unittest.TestCase):
    def setUp(self):
        self.user = UserResponse(user_id="123", email="student@example.com", display_name="学生")
        self.factory = MagicMock()
        self.writer = self.factory.begin.return_value.__enter__.return_value
        self.writer.flush.side_effect = self.assign_id

    def assign_id(self):
        self.writer.add.call_args.args[0].chat_session_id = 1001

    def test_defaults_same_utc_time_and_public_response_after_commit(self):
        now = datetime(2026, 9, 25, 12, 0, 0, 123456, tzinfo=UTC)
        with patch("app.services.chat_sessions.datetime") as clock:
            clock.now.return_value = now
            result = create_chat_session(self.user, self.factory)
        chat = self.writer.add.call_args.args[0]
        self.assertIsInstance(chat, ChatSession)
        self.assertEqual((chat.user_id, chat.title, chat.title_is_manual), (123, "new chat session", False))
        for name in ("created_at", "updated_at", "last_activity_at"):
            self.assertEqual(getattr(chat, name), now.replace(tzinfo=None))
        self.assertEqual(result.model_dump(), {
            "session_id": "1001", "title": "new chat session", "created_at": now,
            "updated_at": now, "last_activity_at": now,
        })
        self.writer.add.assert_called_once()
        self.writer.flush.assert_called_once()
        self.factory.begin.return_value.__exit__.assert_called_once_with(None, None, None)

    def test_owner_is_taken_from_authenticated_user_including_large_id(self):
        user = UserResponse(user_id="18446744073709551615", email="other@example.com", display_name="Other")
        create_chat_session(user, self.factory)
        self.assertEqual(self.writer.add.call_args.args[0].user_id, 18446744073709551615)

    def test_flush_failure_is_safe_and_not_retried(self):
        error = SQLAlchemyError("private-sql-and-dsn")
        self.writer.flush.side_effect = error
        with self.assertRaises(SessionUnavailableError) as caught:
            create_chat_session(self.user, self.factory)
        self.assertEqual(caught.exception.code, "SESSION_UNAVAILABLE")
        self.assertNotIn("private", str(caught.exception))
        self.assertIs(self.factory.begin.return_value.__exit__.call_args.args[1], error)
        self.writer.add.assert_called_once()
        self.writer.flush.assert_called_once()

    def test_commit_failure_does_not_return_success_or_retry(self):
        self.factory.begin.return_value.__exit__.side_effect = SQLAlchemyError("private-commit")
        with self.assertRaises(SessionUnavailableError):
            create_chat_session(self.user, self.factory)
        self.factory.begin.assert_called_once()
        self.writer.add.assert_called_once()

    def test_connection_failure_is_safe(self):
        self.factory.begin.return_value.__enter__.side_effect = SQLAlchemyError("private-host")
        with self.assertRaises(SessionUnavailableError) as caught:
            create_chat_session(self.user, self.factory)
        self.assertNotIn("private-host", str(caught.exception))
        self.writer.add.assert_not_called()

    def test_response_validation_happens_before_commit(self):
        self.writer.flush.side_effect = None  # 缺少数据库生成的编号，响应校验必须失败。
        with self.assertRaises(ValidationError):
            create_chat_session(self.user, self.factory)
        exit_args = self.factory.begin.return_value.__exit__.call_args.args
        self.assertIs(exit_args[0], ValidationError)


class RenameChatSessionTest(unittest.TestCase):
    def setUp(self):
        self.user = UserResponse(user_id="123", email="student@example.com", display_name="学生")
        self.request = RenameSessionRequest(title="  Python 学习  ")
        self.factory = MagicMock()
        self.writer = self.factory.begin.return_value.__enter__.return_value
        self.old_time = datetime(2026, 9, 24)
        self.chat = ChatSession(chat_session_id=1001, user_id=123, title="Original", title_is_manual=False,
                                created_at=self.old_time, updated_at=self.old_time, last_activity_at=self.old_time)
        self.writer.scalar.return_value = self.chat

    def test_rename_changes_only_title_manual_flag_and_update_time(self):
        now = datetime(2026, 9, 25, tzinfo=UTC)
        with patch("app.services.chat_sessions.datetime") as clock:
            clock.now.return_value = now
            result = rename_chat_session(self.user, "1001", self.request, self.factory)
        self.assertEqual(result.title, "Python 学习")
        self.assertTrue(self.chat.title_is_manual)
        self.assertEqual(result.updated_at, now)
        self.assertEqual((self.chat.user_id, self.chat.created_at, self.chat.last_activity_at),
                         (123, self.old_time, self.old_time))
        self.assertEqual(set(result.model_dump()), {"session_id", "title", "created_at", "updated_at", "last_activity_at"})
        self.writer.flush.assert_called_once()
        self.factory.begin.return_value.__exit__.assert_called_once_with(None, None, None)

    def test_query_checks_owner_and_id_and_locks_row(self):
        rename_chat_session(self.user, "1001", self.request, self.factory)
        query = self.writer.scalar.call_args.args[0]
        self.assertEqual(query.compile().params, {"chat_session_id_1": 1001, "user_id_1": 123})
        self.assertIn("FOR UPDATE", str(query))
        self.assertNotIn("messages", str(query))

    def test_same_title_still_marks_manual(self):
        rename_chat_session(self.user, "1001", RenameSessionRequest(title="Original"), self.factory)
        self.assertTrue(self.chat.title_is_manual)
        self.assertEqual(self.chat.title, "Original")

    def test_invalid_session_id_rejected_before_database(self):
        for value in ("0", "-1", "01", "1.5", "abc", "18446744073709551616", True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                rename_chat_session(self.user, value, self.request, self.factory)
        self.factory.begin.assert_not_called()

    def test_invalid_title_rejected_by_existing_schema(self):
        for value in ("", "   ", "x" * 101, None, 12):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                rename_chat_session(self.user, "1001", RenameSessionRequest(title=value), self.factory)
        self.factory.begin.assert_not_called()

    def test_missing_or_inaccessible_row_uses_safe_not_found_error(self):
        self.writer.scalar.return_value = None
        with self.assertRaises(SessionNotFoundError) as caught:
            rename_chat_session(self.user, "1001", self.request, self.factory)
        self.assertEqual(caught.exception.code, "SESSION_NOT_FOUND")
        self.writer.flush.assert_not_called()

    def test_database_failures_are_redacted_and_not_retried(self):
        for point in (self.writer.scalar, self.writer.flush, self.factory.begin.return_value.__exit__):
            with self.subTest(point=point):
                point.side_effect = SQLAlchemyError("private-sql-dsn")
                before = self.factory.begin.call_count
                with self.assertRaises(SessionUnavailableError) as caught:
                    rename_chat_session(self.user, "1001", self.request, self.factory)
                self.assertNotIn("private-sql-dsn", str(caught.exception))
                self.assertEqual(self.factory.begin.call_count, before + 1)
                point.side_effect = None

    def test_response_validation_failure_leaves_transaction_with_error(self):
        self.chat.created_at = None
        with self.assertRaises(ValidationError):
            rename_chat_session(self.user, "1001", self.request, self.factory)
        self.assertIs(self.factory.begin.return_value.__exit__.call_args.args[0], ValidationError)


class ListChatSessionsTest(unittest.TestCase):
    def setUp(self):
        self.user = UserResponse(user_id="123", email="student@example.com", display_name="学生")
        self.factory = MagicMock()
        self.reader = self.factory.return_value.__enter__.return_value
        self.reader.scalars.return_value.all.return_value = []

    def test_empty_list_does_not_create_session_or_write(self):
        self.assertEqual(list_chat_sessions(self.user, self.factory).model_dump(), {"items": []})
        self.factory.begin.assert_not_called()
        for name in ("add", "flush", "commit", "delete"):
            getattr(self.reader, name).assert_not_called()
        self.factory.return_value.__exit__.assert_called_once_with(None, None, None)

    def test_query_filters_owner_and_orders_without_limit_or_messages(self):
        list_chat_sessions(self.user, self.factory)
        statement = self.reader.scalars.call_args.args[0]
        self.assertEqual(statement.compile().params, {"user_id_1": 123})
        sql = str(statement)
        self.assertIn("WHERE chat_sessions.user_id =", sql)
        self.assertIn("ORDER BY chat_sessions.last_activity_at DESC, chat_sessions.chat_session_id DESC", sql)
        for forbidden in ("LIMIT", "OFFSET", "messages", "JOIN"):
            self.assertNotIn(forbidden, sql)
        self.reader.scalars.assert_called_once()

    def test_public_response_preserves_database_order_and_formats_ids_times(self):
        now = datetime(2026, 9, 25)
        chats = [ChatSession(chat_session_id=i, user_id=123, title=f"Title {i}", title_is_manual=True,
                             created_at=now, updated_at=now, last_activity_at=now)
                 for i in (9007199254740993, 12)]
        self.reader.scalars.return_value.all.return_value = chats
        result = list_chat_sessions(self.user, self.factory)
        self.assertEqual([item.session_id for item in result.items], ["9007199254740993", "12"])
        self.assertEqual(result.items[0].created_at, now.replace(tzinfo=UTC))
        self.assertEqual(set(result.items[0].model_dump()), {
            "session_id", "title", "created_at", "updated_at", "last_activity_at",
        })
        self.assertEqual(chats[0].last_activity_at, now)

    def test_query_failure_is_redacted_not_empty_result_or_retry(self):
        error = SQLAlchemyError("private-query-dsn")
        self.reader.scalars.side_effect = error
        with self.assertRaises(SessionUnavailableError) as caught:
            list_chat_sessions(self.user, self.factory)
        self.assertEqual(caught.exception.code, "SESSION_UNAVAILABLE")
        self.assertNotIn("private-query-dsn", str(caught.exception))
        self.reader.scalars.assert_called_once()
        self.assertIs(self.factory.return_value.__exit__.call_args.args[1], error)

    def test_connection_failure_is_safe(self):
        self.factory.return_value.__enter__.side_effect = SQLAlchemyError("private-host")
        with self.assertRaises(SessionUnavailableError):
            list_chat_sessions(self.user, self.factory)
        self.reader.scalars.assert_not_called()

    def test_fetch_failure_is_safe_and_does_not_return_partial_list(self):
        self.reader.scalars.return_value.all.side_effect = SQLAlchemyError("private-fetch")
        with self.assertRaises(SessionUnavailableError):
            list_chat_sessions(self.user, self.factory)
        self.reader.scalars.assert_called_once()
