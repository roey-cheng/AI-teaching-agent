"""历史读取离线测试；不连接数据库、不运行 Agent。"""

from datetime import UTC, datetime
import unittest
from unittest.mock import MagicMock
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError

from app.models import Message
from app.schemas import UserResponse
from app.services.errors import MessageHistoryUnavailableError, SessionNotFoundError
from app.services.generation_registry import GenerationRegistry
from app.services.message_history import get_message_history


def question(number=1, status="FAILED"):
    return Message(message_id=number, chat_session_id=10, role="USER", content="  Question\n    code",
                   created_at=datetime(2026, 9, 25), attempt_id=str(uuid4()), generation_status=status,
                   generation_error_code="GENERATION_TIMEOUT" if status == "FAILED" else None,
                   generation_error_message="private-provider-url" if status == "FAILED" else None)


def answer(number=2, reply_to=1):
    return Message(message_id=number, chat_session_id=10, role="ASSISTANT", content="Answer",
                   in_reply_to_message_id=reply_to, created_at=datetime(2026, 9, 25), model_key="private-model")


class MessageHistoryTest(unittest.TestCase):
    def setUp(self):
        self.user = UserResponse(user_id="123", email="student@example.com", display_name="Student")
        self.factory = MagicMock()
        self.reader = self.factory.return_value.__enter__.return_value
        self.reader.scalar.return_value = 10
        self.reader.scalars.return_value.all.return_value = []
        self.registry = GenerationRegistry()

    def read(self, messages, active=None):
        self.reader.scalars.return_value.all.return_value = messages
        if active is not None:
            with self.registry.locked(10) as slot:
                self.assertTrue(slot.claim(active))
        return get_message_history(self.user, "10", self.factory, self.registry)

    def test_empty_history_is_idle_without_writes(self):
        self.assertEqual(self.read([]).model_dump(), {"session_id": "10", "is_generating": False, "items": []})
        self.factory.begin.assert_not_called()
        for name in ("add", "flush", "commit", "delete"):
            getattr(self.reader, name).assert_not_called()

    def test_query_checks_ownership_before_loading_only_session_messages(self):
        self.read([])
        owner = self.reader.scalar.call_args.args[0]
        self.assertEqual(owner.compile().params, {"chat_session_id_1": 10, "user_id_1": 123})
        messages = self.reader.scalars.call_args.args[0]
        self.assertEqual(messages.compile().params, {"chat_session_id_1": 10})
        self.assertIn("ORDER BY messages.created_at ASC, messages.message_id ASC", str(messages))
        self.assertNotIn("LIMIT", str(messages))

    def test_missing_or_other_users_session_does_not_read_messages(self):
        self.reader.scalar.return_value = None
        with self.assertRaises(SessionNotFoundError):
            self.read([])
        self.reader.scalars.assert_not_called()

    def test_invalid_id_fails_before_database(self):
        with self.assertRaises(ValueError):
            get_message_history(self.user, "0", self.factory, self.registry)
        self.factory.assert_not_called()

    def test_successful_pair_maps_answer_and_filters_internal_fields(self):
        result = self.read([question(status="SUCCEEDED"), answer()])
        self.assertEqual(result.items[0].generation.assistant_message_id, "2")
        self.assertFalse(result.items[0].generation.can_retry)
        self.assertEqual(result.items[0].content, "  Question\n    code")
        self.assertEqual(result.items[0].created_at.tzinfo, UTC)
        self.assertNotIn("generation", result.items[1].model_dump())
        for private in ("model_key", "client_message_key", "retry_of_attempt_id", "private-model"):
            self.assertNotIn(private, result.model_dump_json())

    def test_old_failure_remains_visible_but_only_last_failed_user_can_retry(self):
        result = self.read([question(), question(2)])
        self.assertEqual([item.generation.can_retry for item in result.items], [False, True])
        result = self.read([question(), question(2, "SUCCEEDED"), answer(3, 2)])
        self.assertFalse(result.items[0].generation.can_retry)
        self.assertEqual(result.items[0].generation.status, "FAILED")

    def test_known_and_unknown_errors_use_safe_summaries(self):
        q = question()
        result = self.read([q])
        self.assertEqual(result.items[0].generation.error.code, "GENERATION_TIMEOUT")
        self.assertNotIn("private-provider-url", result.model_dump_json())
        q.generation_error_code = "private-token-code"
        result = self.read([q])
        self.assertEqual(result.items[0].generation.error.code, "GENERATION_FAILED")
        self.assertNotIn("private", result.model_dump_json())

    def test_running_and_finalizing_are_busy_until_matching_release(self):
        q = question(status="RUNNING")
        result = self.read([q], active=q.attempt_id)
        self.assertTrue(result.is_generating)
        q.generation_status = "FAILED"
        q.generation_error_code = "GENERATION_INTERRUPTED"
        result = self.read([q])
        self.assertTrue(result.is_generating)
        self.assertFalse(result.items[0].generation.can_retry)
        with self.registry.locked(10) as slot:
            slot.release(q.attempt_id)
        self.assertTrue(self.read([q]).items[0].generation.can_retry)

    def test_orphan_or_mismatched_running_is_not_reported_idle_or_repaired(self):
        q = question(status="RUNNING")
        with self.assertRaises(MessageHistoryUnavailableError):
            self.read([q])
        with self.assertRaises(MessageHistoryUnavailableError):
            self.read([q], active=str(uuid4()))
        self.assertEqual(q.generation_status, "RUNNING")
        self.reader.flush.assert_not_called()

    def test_multiple_running_or_active_without_matching_latest_question_fails(self):
        q = question(status="RUNNING")
        with self.assertRaises(MessageHistoryUnavailableError):
            self.read([q, question(2, "RUNNING")], active=q.attempt_id)
        with self.assertRaises(MessageHistoryUnavailableError):
            self.read([])

    def test_inconsistent_answer_links_and_missing_answers_are_safe_errors(self):
        for messages in ([question(status="SUCCEEDED")], [answer()],
                         [question(), answer()], [question(status="SUCCEEDED"), answer(2, 999)],
                         [question(status="SUCCEEDED"), answer(), answer(3)],
                         [question(2), question(1)]):
            with self.subTest(count=len(messages)), self.assertRaises(MessageHistoryUnavailableError):
                self.read(messages)

    def test_database_failures_are_redacted_not_retried_or_empty(self):
        for call in (self.reader.scalar, self.reader.scalars, self.reader.scalars.return_value.all):
            call.side_effect = SQLAlchemyError("private-query")
            before = self.factory.call_count
            with self.assertRaises(MessageHistoryUnavailableError) as caught:
                self.read([])
            self.assertNotIn("private-query", str(caught.exception))
            self.assertEqual(self.factory.call_count, before + 1)
            call.side_effect = None
