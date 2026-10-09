"""消息准入离线测试；没有模型、网络或真实数据库。"""

from datetime import datetime
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError

from app.agent.input_policy import policy_for_model
from app.models import ChatSession, Message
from app.schemas import DuplicateMessageResponse, SendMessageRequest, UserResponse
from app.services.errors import (
    IdempotencyConflictError, MessageSendUnavailableError, SessionBusyError, SessionNotFoundError,
)
from app.services.generation_registry import GenerationRegistry
from app.services.message_submission import AcceptedMessage, accept_user_message


class MessageSubmissionTest(unittest.TestCase):
    def setUp(self):
        self.user = UserResponse(user_id="123", email="student@example.com", display_name="Student")
        self.request = SendMessageRequest(client_message_key=str(uuid4()), content="  First\nquestion🙂  ")
        self.factory = MagicMock()
        self.writer = self.factory.begin.return_value.__enter__.return_value
        self.chat = ChatSession(chat_session_id=10, user_id=123, title="new chat session", title_is_manual=False,
                                created_at=datetime(2026, 9, 25), updated_at=datetime(2026, 9, 25),
                                last_activity_at=datetime(2026, 9, 25))
        self.writer.scalar.side_effect = [self.chat, None, None, None]
        self.writer.flush.side_effect = lambda: setattr(self.writer.add.call_args.args[0], "message_id", 20)
        self.registry = GenerationRegistry()
        self.check = MagicMock()
        self.policy = policy_for_model("deepseek-v4-pro")
        preparer = patch("app.services.message_submission.prepare_agent_input")
        self.prepare = preparer.start()
        self.addCleanup(preparer.stop)

    def accept(self):
        return accept_user_message(self.user, "10", self.request, self.factory, self.registry,
                                   check_new_message=self.check, input_policy=self.policy)

    def test_new_message_commits_before_yield_and_cleans_up_on_exit(self):
        with patch("app.services.message_submission._settle_interrupted") as settle:
            with self.accept() as result:
                self.assertIsInstance(result, AcceptedMessage)
                self.assertEqual(result.user_message_id, "20")
                self.assertIs(result.prepared_input, self.prepare.return_value)
                self.factory.begin.return_value.__exit__.assert_called_once_with(None, None, None)
                settle.assert_not_called()
                saved = self.writer.add.call_args.args[0]
                self.assertEqual(saved.content, self.request.content)
                self.assertEqual((saved.role, saved.generation_status), ("USER", "RUNNING"))
                self.assertEqual(self.chat.title, "First question🙂")
                self.assertEqual(self.chat.last_activity_at, saved.created_at)
                self.assertEqual(self.chat.updated_at, saved.created_at)
                with self.registry.locked(10) as slot:
                    self.assertEqual(slot.attempt_id, result.attempt_id)
            settle.assert_called_once()

    def test_missing_session_rejected_before_key_query(self):
        self.writer.scalar.side_effect = [None]
        with self.assertRaises(SessionNotFoundError), self.accept():
            self.fail("Must not yield")
        self.writer.scalar.assert_called_once()
        self.writer.add.assert_not_called()

    def test_duplicate_is_checked_before_busy_and_has_no_cleanup_ownership(self):
        attempt = str(uuid4())
        row = Message(message_id=20, chat_session_id=10, role="USER", content=self.request.content,
                      attempt_id=attempt, generation_status="RUNNING")
        self.writer.scalar.side_effect = [self.chat, row, None]
        with self.registry.locked(10) as slot:
            slot.claim(attempt)
        with patch("app.services.message_submission._settle_interrupted") as settle:
            with self.accept() as result:
                self.assertIsInstance(result, DuplicateMessageResponse)
                self.assertTrue(result.duplicate)
            settle.assert_not_called()
        self.check.assert_not_called()
        self.prepare.assert_not_called()
        self.writer.add.assert_not_called()

    def test_same_key_different_text_conflicts_before_busy(self):
        self.writer.scalar.side_effect = [self.chat, Message(role="USER", content="different")]
        with self.registry.locked(10) as slot:
            slot.claim(str(uuid4()))
        with self.assertRaises(IdempotencyConflictError), self.accept():
            self.fail("Must not yield")
        self.check.assert_not_called()
        self.writer.add.assert_not_called()

    def test_busy_new_request_does_not_save_or_check_new_quota(self):
        with self.registry.locked(10) as slot:
            slot.claim(str(uuid4()))
        with self.assertRaises(SessionBusyError), self.accept():
            self.fail("Must not yield")
        self.check.assert_not_called()
        self.writer.add.assert_not_called()

    def test_orphan_running_is_not_overwritten(self):
        self.writer.scalar.side_effect = [self.chat, None, 999]
        with self.assertRaises(MessageSendUnavailableError), self.accept():
            self.fail("Must not yield")
        self.writer.add.assert_not_called()

    def test_preflight_rejection_leaves_no_occupancy_or_message(self):
        self.check.side_effect = ValueError("Input budget exceeded")
        with self.assertRaisesRegex(ValueError, "budget"), self.accept():
            self.fail("Must not yield")
        self.writer.add.assert_not_called()
        self.assertEqual(self.registry._entries, {})

    def test_manual_title_and_later_question_do_not_reinitialize_title(self):
        for manual, existing_id in ((True, None), (False, 1)):
            self.chat.title = "Keep title"
            self.chat.title_is_manual = manual
            self.writer.scalar.side_effect = [self.chat, None, None, existing_id]
            self.registry = GenerationRegistry()
            with patch("app.services.message_submission._settle_interrupted"), self.accept():
                self.assertEqual(self.chat.title, "Keep title")

    def test_body_exception_still_cleans_and_propagates(self):
        with patch("app.services.message_submission._settle_interrupted") as settle:
            with self.assertRaisesRegex(RuntimeError, "executor stopped"), self.accept():
                raise RuntimeError("executor stopped")
            settle.assert_called_once()

    def test_commit_failure_reconciles_without_auto_resubmission(self):
        self.factory.begin.return_value.__exit__.side_effect = SQLAlchemyError("private-commit")
        with patch("app.services.message_submission._settle_interrupted") as settle:
            with self.assertRaises(MessageSendUnavailableError) as caught, self.accept():
                self.fail("Must not yield")
            settle.assert_called_once()
        self.assertNotIn("private-commit", str(caught.exception))
        self.writer.add.assert_called_once()

    def test_invalid_id_rejected_without_database(self):
        with self.assertRaises(ValueError):
            with accept_user_message(self.user, "0", self.request, self.factory, self.registry,
                                     check_new_message=self.check, input_policy=self.policy):
                self.fail("Must not yield")
        self.factory.begin.assert_not_called()

    def test_title_truncation_and_request_snapshot(self):
        self.request = SendMessageRequest(client_message_key=str(uuid4()), content=" \r\n" + "文" * 40)
        original_key = self.request.client_message_key
        with patch("app.services.message_submission._settle_interrupted") as settle:
            with self.accept():
                self.assertEqual(self.chat.title, "文" * 30)
                self.request.client_message_key = str(uuid4())
            self.assertEqual(settle.call_args.args[2], original_key)
