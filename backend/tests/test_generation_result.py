from datetime import datetime
import unittest
from unittest.mock import MagicMock
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError

from app.models import ChatSession, Message
from app.services.errors import MessageSendUnavailableError
from app.services.generation_registry import GenerationRegistry
from app.services.generation_result import StaleGenerationError, settle_generation
from app.services.message_submission import AcceptedMessage
from test_chat_agent_factory import prepared


class GenerationResultTest(unittest.TestCase):
    def setUp(self):
        self.accepted = AcceptedMessage("1", "10", str(uuid4()), prepared())
        self.registry = GenerationRegistry()
        with self.registry.locked(1) as slot:
            slot.claim(self.accepted.attempt_id)
        self.factory = MagicMock()
        self.session = self.factory.begin.return_value.__enter__.return_value
        self.chat = ChatSession(chat_session_id=1, user_id=1)
        self.question = Message(message_id=10, chat_session_id=1, role="USER", content="question",
                                attempt_id=self.accepted.attempt_id, generation_status="RUNNING")
        self.session.scalar.side_effect = [self.chat, self.question, None]
        self.session.flush.side_effect = lambda: setattr(self.session.add.call_args.args[0], "message_id", 11) if self.session.add.called else None

    def settle(self, **kwargs):
        return settle_generation(self.accepted, self.factory, self.registry, **kwargs)

    def saved(self):
        return Message(message_id=11, chat_session_id=1, role="ASSISTANT", content="saved",
                       in_reply_to_message_id=10, created_at=datetime(2026, 10, 11))

    def test_save_answer_and_success_in_one_transaction_without_releasing_slot(self):
        outcome = self.settle(answer=" final answer\n")
        self.assertEqual(outcome.assistant.content, " final answer\n")
        self.assertEqual(self.question.generation_status, "SUCCEEDED")
        self.factory.begin.return_value.__exit__.assert_called_once_with(None, None, None)
        with self.registry.locked(1) as slot:
            self.assertEqual(slot.attempt_id, self.accepted.attempt_id)

    def test_failure_does_not_insert_partial_answer(self):
        outcome = self.settle(error_code="GENERATION_TIMEOUT")
        self.assertEqual(outcome.error.code, "GENERATION_TIMEOUT")
        self.assertEqual(self.question.generation_status, "FAILED")
        self.session.add.assert_not_called()

    def test_reconcile_committed_success_never_overwrites_with_failure(self):
        self.question.generation_status = "SUCCEEDED"
        self.session.scalar.side_effect = [self.chat, self.question, self.saved()]
        outcome = self.settle(error_code="MODEL_REQUEST_FAILED")
        self.assertEqual(outcome.assistant.content, "saved")
        self.assertEqual(self.question.generation_status, "SUCCEEDED")
        self.session.add.assert_not_called()

    def test_failed_attempt_rejects_late_answer(self):
        self.question.generation_status = "FAILED"
        with self.assertRaises(StaleGenerationError):
            self.settle(answer="late")
        self.session.add.assert_not_called()

    def test_old_attempt_cannot_modify_new_registry_owner(self):
        with self.registry.locked(1) as slot:
            slot.release(self.accepted.attempt_id)
            slot.claim(str(uuid4()))
        with self.assertRaises(StaleGenerationError):
            self.settle(answer="late")
        self.factory.begin.assert_not_called()

    def test_db_attempt_mismatch_is_rejected(self):
        self.question.attempt_id = str(uuid4())
        with self.assertRaises(StaleGenerationError):
            self.settle(answer="late")
        self.session.add.assert_not_called()

    def test_commit_error_is_safe_not_reported_as_success(self):
        self.factory.begin.return_value.__exit__.side_effect = SQLAlchemyError("private credentials")
        with self.assertRaises(MessageSendUnavailableError) as caught:
            self.settle(answer="answer")
        self.assertNotIn("private", str(caught.exception))

    def test_blank_answer_and_unknown_error_rejected_before_database(self):
        for values in ({"answer": " "}, {"error_code": "private exception"}, {}):
            with self.assertRaises(ValueError):
                self.settle(**values)
        self.factory.begin.assert_not_called()
