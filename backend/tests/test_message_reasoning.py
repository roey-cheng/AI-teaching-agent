"""Offline tests for the narrow, authenticated reasoning projection."""

import unittest
from unittest.mock import MagicMock

from langchain_core.messages import AIMessage, ToolMessage, messages_to_dict
from sqlalchemy.exc import SQLAlchemyError

from app.models import AgentTurnRecord, Message
from app.schemas import UserResponse
from app.services.errors import MessageNotFoundError, MessageReasoningUnavailableError
from app.services.message_reasoning import get_message_reasoning


class MessageReasoningTest(unittest.TestCase):
    def setUp(self):
        self.user = UserResponse(user_id="123", email="a@example.com", display_name="A")
        self.factory = MagicMock()
        self.reader = self.factory.return_value.__enter__.return_value
        self.reader.scalar.return_value = 10
        self.answer = Message(
            message_id=2, chat_session_id=10, role="ASSISTANT", in_reply_to_message_id=1
        )
        self.question = Message(
            message_id=1, chat_session_id=10, role="USER", generation_status="SUCCEEDED"
        )
        self.record = AgentTurnRecord(
            question_id=1,
            user_id=123,
            messages=messages_to_dict(
                [AIMessage(content="final", additional_kwargs={"reasoning_content": "想法\n原文"})]
            ),
        )

    def read(self):
        self.reader.get.side_effect = [self.answer, self.question, self.record]
        return get_message_reasoning(self.user, "10", "2", self.factory)

    def test_single_step_and_owner_scoped_query(self):
        result = self.read()
        self.assertEqual(
            result.model_dump(),
            {
                "message_id": "2",
                "available": True,
                "steps": [{"step": 1, "text": "想法\n原文"}],
            },
        )
        query = self.reader.scalar.call_args.args[0].compile()
        self.assertIn("chat_sessions.user_id =", str(query))
        self.assertEqual(set(query.params.values()), {10, 123})

    def test_tool_loop_only_exposes_reasoning(self):
        assert self.record is not None
        self.record.messages = messages_to_dict(
            [
                AIMessage(
                    content="private preamble",
                    additional_kwargs={"reasoning_content": "first", "private": "secret"},
                    tool_calls=[
                        {"name": "save_profile_facts", "args": {"private": "argument"}, "id": "c1"}
                    ],
                ),
                ToolMessage(content="private tool result", tool_call_id="c1"),
                AIMessage(
                    content="final answer", additional_kwargs={"reasoning_content": "second"}
                ),
            ]
        )
        result = self.read()
        self.assertEqual([s.text for s in result.steps], ["first", "second"])
        self.assertEqual([s.step for s in result.steps], [1, 2])
        serialized = result.model_dump_json()
        for excluded in ("private", "argument", "tool_calls", "final answer", "secret"):
            self.assertNotIn(excluded, serialized)

    def test_legacy_missing_record_is_unavailable_not_an_error(self):
        self.record = None
        self.assertEqual(
            self.read().model_dump(),
            {
                "message_id": "2",
                "available": False,
                "steps": [],
            },
        )

    def test_empty_reasoning(self):
        assert self.record is not None
        self.record.messages = messages_to_dict(
            [AIMessage(content="answer", additional_kwargs={"reasoning_content": " \n"})]
        )
        self.assertFalse(self.read().available)

    def test_foreign_or_missing_session_stops_before_reading_messages(self):
        self.reader.scalar.return_value = None
        with self.assertRaises(MessageNotFoundError):
            self.read()
        self.reader.get.assert_not_called()

    def test_wrong_message_or_question_rejected(self):
        for target, field, value in (
            (self.answer, "chat_session_id", 99),
            (self.answer, "role", "USER"),
            (self.question, "chat_session_id", 99),
            (self.question, "role", "ASSISTANT"),
            (self.question, "generation_status", "FAILED"),
            (self.question, "generation_status", "RUNNING"),
            (self.record, "user_id", 999),
        ):
            with self.subTest(field=field, value=value):
                previous = getattr(target, field)
                setattr(target, field, value)
                with self.assertRaises(MessageNotFoundError):
                    self.read()
                setattr(target, field, previous)

    def test_missing_answer_and_question(self):
        self.answer = None
        with self.assertRaises(MessageNotFoundError):
            self.read()
        self.answer = Message(chat_session_id=10, role="ASSISTANT", in_reply_to_message_id=1)
        self.question = None
        with self.assertRaises(MessageNotFoundError):
            self.read()

    def test_corrupt_protocol_is_safe_error(self):
        for records in (
            [],
            None,
            [None],
            [{"type": "human", "data": {}}],
            [{"type": "ai"}],
            [{"type": "ai", "data": {"content": "missing reasoning"}}],
            messages_to_dict([ToolMessage(content="private", tool_call_id="orphan")]),
        ):
            with self.subTest(records=records):
                self.record = AgentTurnRecord(question_id=1, user_id=123, messages=records)
                with self.assertRaises(MessageReasoningUnavailableError) as error:
                    self.read()
                self.assertNotIn("private", str(error.exception))

    def test_database_failure_is_safe_error(self):
        self.reader.scalar.side_effect = SQLAlchemyError("private db credentials")
        with self.assertRaises(MessageReasoningUnavailableError) as error:
            self.read()
        self.assertNotIn("private", str(error.exception))
