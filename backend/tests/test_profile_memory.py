"""离线检查记忆存取约定；不连接数据库，也不调用模型。"""

from dataclasses import replace
from datetime import datetime
import unittest
from unittest.mock import MagicMock
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import SQLAlchemyError

from app.models import AgentMemory, ChatSession, Message
from app.models.agent_memory import MEMORY_TOPIC_TYPES
from app.schemas import UserResponse
from app.services.errors import MemoryUnavailableError, SessionNotFoundError
from app.services.generation_registry import GenerationRegistry
from app.services.generation_result import StaleGenerationError
from app.services.message_submission import AcceptedMessage
from app.services.profile_memory import ProfileFact, list_profile_memory, save_profile_facts
from test_chat_agent_factory import prepared


def row():
    return AgentMemory(memory_id=7, user_id=1, memory_key="preference.language",
                       memory_type="LEARNING_PREFERENCE", summary="喜欢中文",
                       created_at=datetime(2026, 10, 11), updated_at=datetime(2026, 10, 11))


class ProfileFactTest(unittest.TestCase):
    def test_all_25_topics_and_summary_boundaries(self):
        for key in MEMORY_TOPIC_TYPES:
            self.assertEqual(ProfileFact(memory_key=key, summary="  喜欢中文  ").summary, "喜欢中文")
        self.assertEqual(len(ProfileFact(memory_key="preference.language", summary="字" * 500).summary), 500)
        for summary in ("", " \n\t", "字" * 501, None, 123):
            with self.subTest(summary_type=type(summary)), self.assertRaises(ValidationError):
                ProfileFact(memory_key="preference.language", summary=summary)

    def test_unknown_topics_identity_and_types_are_not_accepted(self):
        for changes in ({"memory_key": "arbitrary.topic"}, {"user_id": "2"},
                        {"memory_type": "DAILY_PREFERENCE"}, {"attempt_id": str(uuid4())}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                ProfileFact(**(dict(memory_key="preference.language", summary="Chinese") | changes))

    def test_facts_are_frozen_and_summary_not_in_repr(self):
        fact = ProfileFact(memory_key="preference.language", summary="private fact")
        self.assertNotIn("private fact", repr(fact))
        with self.assertRaises(ValidationError):
            fact.summary = "changed"


class ProfileMemorySaveTest(unittest.TestCase):
    def setUp(self):
        self.accepted = AcceptedMessage("1", "10", str(uuid4()), prepared())
        self.registry = GenerationRegistry()
        with self.registry.locked(1) as slot:
            slot.claim(self.accepted.attempt_id)
        self.factory = MagicMock()
        self.session = self.factory.begin.return_value.__enter__.return_value
        self.chat = ChatSession(chat_session_id=1, user_id=1)
        self.question = Message(message_id=10, chat_session_id=1, role="USER",
                                attempt_id=self.accepted.attempt_id, generation_status="RUNNING")
        self.session.scalar.side_effect = [self.chat, self.question, None]
        self.session.scalars.return_value.all.return_value = [row()]
        self.facts = [ProfileFact(memory_key="preference.language", summary="喜欢中文")]

    def save(self, **overrides):
        values = dict(accepted=self.accepted, facts=self.facts, session_factory=self.factory, registry=self.registry)
        return save_profile_facts(**(values | overrides))

    def test_upsert_uses_backend_identity_and_preserves_creation_columns(self):
        result = self.save()
        statement = self.session.execute.call_args.args[0].compile(dialect=mysql.dialect())
        self.assertIn("ON DUPLICATE KEY UPDATE", str(statement))
        updates = str(statement).split("ON DUPLICATE KEY UPDATE")[1]
        self.assertNotIn("created_at", updates)
        self.assertNotIn("memory_id", updates)
        self.assertEqual(statement.params["user_id_m0"], 1)
        self.assertEqual(statement.params["memory_type_m0"], "LEARNING_PREFERENCE")
        self.assertEqual(result.items[0].memory_id, "7")
        self.assertNotIn("user_id", result.model_dump_json())
        self.assertNotIn("memory_key", result.model_dump_json())
        self.factory.begin.return_value.__exit__.assert_called_once_with(None, None, None)
        with self.registry.locked(1) as slot:
            self.assertEqual(slot.attempt_id, self.accepted.attempt_id)

    def test_empty_duplicate_large_or_bypassed_invalid_batch_never_opens_database(self):
        bad = ProfileFact.model_construct(memory_key="unknown", summary="private fact")
        for facts in ([], self.facts * 2, self.facts * 26, [bad]):
            with self.subTest(size=len(facts)), self.assertRaises(ValueError):
                self.save(facts=facts)
        self.factory.begin.assert_not_called()

    def test_missing_or_replaced_registry_attempt_never_opens_database(self):
        with self.registry.locked(1) as slot:
            slot.release(self.accepted.attempt_id)
        with self.assertRaises(StaleGenerationError):
            self.save()
        with self.registry.locked(1) as slot:
            slot.claim(str(uuid4()))
        with self.assertRaises(StaleGenerationError):
            self.save()
        self.factory.begin.assert_not_called()

    def test_mismatched_snapshot_session_rejected(self):
        bad = replace(self.accepted, prepared_input=replace(self.accepted.prepared_input, session_id="2"))
        with self.assertRaises(StaleGenerationError):
            self.save(accepted=bad)
        self.factory.begin.assert_not_called()

    def test_missing_question_wrong_role_attempt_or_final_status_cannot_write(self):
        for field, value in (("role", "ASSISTANT"), ("attempt_id", str(uuid4())),
                             ("generation_status", "FAILED"), ("generation_status", "SUCCEEDED")):
            old = getattr(self.question, field)
            setattr(self.question, field, value)
            self.session.scalar.side_effect = [self.chat, self.question]
            with self.assertRaises(StaleGenerationError):
                self.save()
            setattr(self.question, field, old)
        self.session.scalar.side_effect = [self.chat, None]
        with self.assertRaises(StaleGenerationError):
            self.save()
        self.session.execute.assert_not_called()

    def test_existing_answer_or_foreign_session_cannot_write(self):
        self.session.scalar.side_effect = [self.chat, self.question, 11]
        with self.assertRaises(StaleGenerationError):
            self.save()
        self.session.scalar.side_effect = [None]
        with self.assertRaises(SessionNotFoundError):
            self.save()
        self.session.execute.assert_not_called()

    def test_commit_failure_returns_safe_error_not_success(self):
        self.factory.begin.return_value.__exit__.side_effect = SQLAlchemyError("password and private SQL")
        with self.assertRaises(MemoryUnavailableError) as error:
            self.save()
        self.assertNotIn("password", str(error.exception))
        self.assertEqual(self.session.execute.call_count, 1)  # 没有自动重试。


class ProfileMemoryListTest(unittest.TestCase):
    def setUp(self):
        self.user = UserResponse(user_id="1", email="a@example.com", display_name="Test", created_at=datetime(2026, 10, 11))
        self.factory = MagicMock()
        self.session = self.factory.return_value.__enter__.return_value

    def test_query_filters_user_sorts_and_only_exposes_public_fields(self):
        self.session.scalars.return_value.all.return_value = [row()]
        result = list_profile_memory(self.user, self.factory)
        query = self.session.scalars.call_args.args[0].compile(dialect=mysql.dialect())
        self.assertIn("WHERE agent_memory.user_id =", str(query))
        self.assertIn("ORDER BY agent_memory.updated_at DESC, agent_memory.memory_id DESC", str(query))
        self.assertEqual(query.params["user_id_1"], 1)
        self.assertEqual(set(result.items[0].model_dump()), {"memory_id", "memory_type", "summary", "updated_at"})
        self.session.commit.assert_not_called()
        self.session.execute.assert_not_called()

    def test_no_memory_returns_explicit_empty_items(self):
        self.session.scalars.return_value.all.return_value = []
        self.assertEqual(list_profile_memory(self.user, self.factory).model_dump(), {"items": []})

    def test_database_error_not_reported_as_empty_list(self):
        self.session.scalars.side_effect = SQLAlchemyError("private SQL")
        with self.assertRaises(MemoryUnavailableError) as error:
            list_profile_memory(self.user, self.factory)
        self.assertNotIn("private SQL", str(error.exception))

    def test_invalid_stored_response_is_a_safe_error(self):
        invalid = row()
        invalid.summary = ""
        self.session.scalars.return_value.all.return_value = [invalid]
        with self.assertRaises(MemoryUnavailableError):
            list_profile_memory(self.user, self.factory)
