"""失败重试的准入/执行接线；不连接数据库，不访问模型或 LangSmith。"""

from contextlib import contextmanager
from datetime import datetime
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError

from app.agent.input_policy import policy_for_model
from app.core.config import TracingSettings
from app.models import ChatSession, Message
from app.schemas import DuplicateMessageResponse, RetryMessageRequest, UserResponse
from app.services.chat_execution import execute_chat_retry
from app.services.errors import (
    ContextTooLargeError, MessageNotFoundError, MessageSendUnavailableError,
    RetryNotAllowedError, SessionBusyError, SessionNotFoundError, StaleAttemptError,
)
from app.services.generation_registry import GenerationRegistry
from app.services.message_retry import accept_failed_message_retry
from app.services.message_submission import AcceptedMessage
from test_chat_agent_factory import prepared, settings


class MessageRetryTest(unittest.TestCase):
    def setUp(self):
        self.user = UserResponse(user_id="123", email="a@example.com", display_name="A")
        self.request = RetryMessageRequest(failed_attempt_id=str(uuid4()))
        self.factory = MagicMock()
        self.writer = self.factory.begin.return_value.__enter__.return_value
        self.chat = ChatSession(chat_session_id=10, user_id=123, title="Keep title")
        self.question = Message(
            message_id=20, chat_session_id=10, role="USER", content="  Original\nquestion  ",
            client_message_key=str(uuid4()), attempt_id=self.request.failed_attempt_id,
            generation_status="FAILED", generation_error_code="MODEL_REQUEST_FAILED",
            generation_error_message="Safe failure", created_at=datetime(2026, 10, 11),
        )
        # owner, target, answer, orphan RUNNING, latest USER
        self.writer.scalar.side_effect = [self.chat, self.question, None, None, 20]
        self.registry, self.check = GenerationRegistry(), MagicMock()
        self.policy = policy_for_model("deepseek-v4-pro")
        prepare_patch = patch("app.services.message_retry.prepare_agent_input", return_value=prepared())
        self.prepare = prepare_patch.start()
        self.addCleanup(prepare_patch.stop)
        cleanup_patch = patch("app.services.message_retry._settle_interrupted")
        self.cleanup = cleanup_patch.start()
        self.addCleanup(cleanup_patch.stop)

    def accept(self):
        return accept_failed_message_retry(
            self.user, "10", "20", self.request, self.factory, self.registry,
            check_retry=self.check, input_policy=self.policy,
        )

    def test_updates_original_only_and_commits_before_yield(self):
        old_time, old_key = self.question.created_at, self.question.client_message_key
        with self.accept() as result:
            self.assertIsInstance(result, AcceptedMessage)
            self.assertEqual(result.user_message_id, "20")
            self.assertNotEqual(result.attempt_id, self.request.failed_attempt_id)
            self.assertEqual(self.question.retry_of_attempt_id, self.request.failed_attempt_id)
            self.assertEqual(self.question.generation_status, "RUNNING")
            self.assertIsNone(self.question.generation_error_code)
            self.assertIsNone(self.question.generation_error_message)
            self.assertEqual((self.question.created_at, self.question.client_message_key), (old_time, old_key))
            self.assertEqual(self.chat.title, "Keep title")
            self.assertEqual(self.chat.last_activity_at, self.question.updated_at)
            self.writer.add.assert_not_called()
            self.factory.begin.return_value.__exit__.assert_called_once_with(None, None, None)
            self.cleanup.assert_not_called()
            self.assertEqual(self.prepare.call_args.kwargs["retry_message_id"], "20")
            self.assertEqual(self.prepare.call_args.args[3].content, self.question.content)
        self.cleanup.assert_called_once()
        self.assertEqual(self.cleanup.call_args.kwargs["previous_attempt_id"], self.request.failed_attempt_id)
        self.check.assert_called_once()

    def test_owner_is_checked_before_message(self):
        self.writer.scalar.side_effect = [None]
        with self.assertRaises(SessionNotFoundError), self.accept():
            self.fail("Must not yield")
        self.writer.scalar.assert_called_once()
        self.prepare.assert_not_called()

    def test_missing_message_is_not_found(self):
        self.writer.scalar.side_effect = [self.chat, None]
        with self.assertRaises(MessageNotFoundError), self.accept():
            self.fail("Must not yield")
        self.cleanup.assert_not_called()

    def test_assistant_message_cannot_be_retried(self):
        self.question.role = "ASSISTANT"
        with self.assertRaises(RetryNotAllowedError), self.accept():
            self.fail("Must not yield")
        self.check.assert_not_called()

    def test_recent_duplicate_before_busy_does_not_take_ownership(self):
        self.question.retry_of_attempt_id = self.request.failed_attempt_id
        self.question.attempt_id = str(uuid4())
        self.question.generation_status = "RUNNING"
        with self.registry.locked(10) as slot:
            slot.claim(self.question.attempt_id)
        with self.accept() as receipt:
            self.assertIsInstance(receipt, DuplicateMessageResponse)
            self.assertEqual(receipt.attempt_id, self.question.attempt_id)
        self.prepare.assert_not_called()
        self.check.assert_not_called()
        self.cleanup.assert_not_called()
        self.writer.flush.assert_not_called()

    def test_duplicate_orphan_does_not_claim_running(self):
        self.question.retry_of_attempt_id = self.request.failed_attempt_id
        self.question.attempt_id = str(uuid4())
        self.question.generation_status = "RUNNING"
        with self.assertRaises(MessageSendUnavailableError), self.accept():
            self.fail("Must not yield")
        self.check.assert_not_called()

    def test_stale_attempt_rejected_without_side_effects(self):
        self.question.attempt_id = str(uuid4())
        with self.assertRaises(StaleAttemptError), self.accept():
            self.fail("Must not yield")
        self.assertEqual(self.registry._entries, {})
        self.check.assert_not_called()

    def test_busy_and_orphan_are_not_overwritten(self):
        with self.registry.locked(10) as slot:
            slot.claim(str(uuid4()))
        with self.assertRaises(SessionBusyError), self.accept():
            self.fail("Must not yield")
        self.registry = GenerationRegistry()
        self.writer.scalar.side_effect = [self.chat, self.question, None, 99]
        with self.assertRaises(MessageSendUnavailableError), self.accept():
            self.fail("Must not yield")
        self.prepare.assert_not_called()
        self.cleanup.assert_not_called()

    def test_older_failed_question_cannot_be_retried(self):
        self.writer.scalar.side_effect = [self.chat, self.question, None, None, 21]
        with self.assertRaises(RetryNotAllowedError), self.accept():
            self.fail("Must not yield")
        self.check.assert_not_called()

    def test_budget_and_rate_rejections_do_not_change_old_failure(self):
        self.prepare.side_effect = ContextTooLargeError()
        with self.assertRaises(ContextTooLargeError), self.accept():
            self.fail("Must not yield")
        self.check.assert_not_called()
        self.prepare.side_effect = None
        self.writer.scalar.side_effect = [self.chat, self.question, None, None, 20]
        self.check.side_effect = RuntimeError("Rate limited")
        with self.assertRaisesRegex(RuntimeError, "Rate limited"), self.accept():
            self.fail("Must not yield")
        self.assertEqual(self.question.attempt_id, self.request.failed_attempt_id)
        self.assertEqual(self.question.generation_status, "FAILED")
        self.assertEqual(self.registry._entries, {})
        self.cleanup.assert_not_called()

    def test_write_failure_uses_independent_reconciliation_and_safe_error(self):
        self.writer.flush.side_effect = SQLAlchemyError("private-database-secret")
        with self.assertRaises(MessageSendUnavailableError) as caught, self.accept():
            self.fail("Must not yield")
        self.assertNotIn("private-database-secret", str(caught.exception))
        self.cleanup.assert_called_once()


class RetryExecutionTest(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_retry_does_not_create_model_or_emit_events(self):
        receipt = DuplicateMessageResponse(
            duplicate=True, session_id="1", user_message_id="10", attempt_id=str(uuid4()),
            status="FAILED", assistant_message_id=None,
        )
        exited = []

        @contextmanager
        def accept(*args, **kwargs):
            try:
                yield receipt
            finally:
                exited.append(True)

        with patch("app.services.chat_execution.accept_failed_message_retry", side_effect=accept) as admission, \
                patch("app.services.chat_execution.build_chat_agent") as model, \
                patch("app.services.chat_execution.prepare_memory_for_turn") as memory:
            callback = MagicMock()
            request = RetryMessageRequest(failed_attempt_id=str(uuid4()))
            result = await execute_chat_retry(
                UserResponse(user_id="1", email="a@example.com", display_name="A"), "1", "10", request,
                MagicMock(), GenerationRegistry(), model_settings=settings(),
                tracing_settings=TracingSettings(_env_file=None, tracing=False, api_key=None),
                input_policy=prepared().policy, check_retry=MagicMock(), on_event=callback,
            )
        self.assertIs(result, receipt)
        self.assertEqual(exited, [True])
        self.assertEqual(admission.call_args.args[2:4], ("10", request))
        callback.assert_not_called()
        model.assert_not_called()
        memory.assert_not_called()
