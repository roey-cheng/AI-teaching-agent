"""Durable memory-write receipts and retry output; no application DB or network."""

import unittest
from dataclasses import replace
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from test_chat_agent_factory import prepared
from test_profile_memory import row

from app.agent.orchestrator import ChatAttemptState
from app.agent.profile_memory_tool import BoundMemoryTool, MemoryCandidate
from app.models import ChatSession, MemoryWriteReceipt, Message
from app.schemas import MemoryListResponse
from app.services.agent_input import InputMessage
from app.services.errors import MemoryUnavailableError
from app.services.generation_registry import GenerationRegistry
from app.services.message_submission import AcceptedMessage
from app.services.profile_memory import MemorySaveResult, ProfileFact, save_profile_facts


class MemoryWriteReceiptTest(unittest.TestCase):
    def setUp(self):
        self.accepted = AcceptedMessage("1", "10", str(uuid4()), prepared())
        self.registry = GenerationRegistry()
        with self.registry.locked(1) as slot:
            slot.claim(self.accepted.attempt_id)
        self.factory = MagicMock()
        self.session = self.factory.begin.return_value.__enter__.return_value
        self.session.scalar.side_effect = [
            ChatSession(chat_session_id=1, user_id=1),
            Message(
                message_id=10,
                chat_session_id=1,
                role="USER",
                attempt_id=self.accepted.attempt_id,
                generation_status="RUNNING",
            ),
            None,
        ]
        self.receipt = MemoryWriteReceipt(
            question_id=10,
            user_id=1,
            facts=[{"memory_key": "preference.language", "summary": "喜欢中文"}],
            created_at=datetime(2026, 10, 25),
        )
        self.session.get.return_value = self.receipt
        current = row()
        current.summary = "后来改成英文"
        self.session.scalars.return_value.all.return_value = [current]

    def save(self, summary="喜欢中文"):
        return save_profile_facts(
            self.accepted,
            [ProfileFact(memory_key="preference.language", summary=summary)],
            self.factory,
            self.registry,
            expected_revisions={"preference.language": (0, 0)},
        )

    def test_replay_returns_receipt_and_current_values_without_any_write(self):
        with patch("app.services.memory_tasks.check_and_bump_revisions") as bump:
            result = self.save()
        self.assertTrue(result.replayed)
        self.assertEqual(result.saved_facts, {"preference.language": "喜欢中文"})
        self.assertEqual(result.current_facts, {"preference.language": "后来改成英文"})
        self.assertEqual(result.response.items[0].summary, "后来改成英文")
        self.session.get.assert_called_once_with(MemoryWriteReceipt, 10)
        self.session.execute.assert_not_called()
        self.session.add.assert_not_called()
        bump.assert_not_called()

    def test_conflicting_retry_proposal_is_not_applied_or_reported_as_saved(self):
        result = self.save("本次模型随意改成法语")
        self.assertNotIn("本次模型随意改成法语", result.saved_facts.values())
        self.assertNotIn("本次模型随意改成法语", result.current_facts.values())
        self.session.execute.assert_not_called()

    def test_receipt_is_bound_to_user_even_if_database_contents_are_corrupt(self):
        self.receipt.user_id = 2
        with self.assertRaises(MemoryUnavailableError):
            self.save()
        self.session.execute.assert_not_called()

    def test_corrupt_receipt_is_fail_closed_not_reapplied(self):
        self.receipt.facts = [{"memory_key": "unknown", "summary": "invalid"}]
        with self.assertRaises(MemoryUnavailableError):
            self.save()
        self.session.execute.assert_not_called()


class MemoryReceiptToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        snapshot = replace(
            prepared(),
            messages=(InputMessage("user", "Please remember I prefer English."),),
        )
        self.accepted = AcceptedMessage("1", "10", str(uuid4()), snapshot)
        self.result = MemorySaveResult(
            response=MemoryListResponse(items=[]),
            saved_facts={"preference.language": "English"},
            current_facts={"preference.language": "Chinese"},
            replayed=True,
        )
        self.fact = MemoryCandidate(
            memory_key="preference.language",
            summary="English with examples",
            source_quote="I prefer English",
        )

    async def test_receipt_text_distinguishes_past_save_from_current_profile(self):
        bound = BoundMemoryTool(self.accepted, MagicMock(), MagicMock(), AsyncMock())
        with patch("app.agent.profile_memory_tool.save_profile_facts", return_value=self.result):
            message = await bound.save([self.fact])
        self.assertEqual(bound.status, "already_saved")
        self.assertEqual(bound.current_facts, {"preference.language": "Chinese"})
        self.assertIn("No new write was performed", message)
        self.assertIn('"previously_saved": {"preference.language": "English"}', message)
        self.assertIn('"current_profile": {"preference.language": "Chinese"}', message)
        self.assertNotIn("English with examples", message)

    async def test_turn_uses_database_values_not_the_retried_proposal(self):
        progress = AsyncMock()
        attempt_state = ChatAttemptState(self.accepted, MagicMock(), MagicMock(), progress)
        attempt_state.profile_facts = {"preference.language": "English"}
        attempt_state.read_topic_keys.add("preference.language")
        with patch("app.agent.profile_memory_tool.save_profile_facts", return_value=self.result):
            message = await attempt_state.save_profile_facts([self.fact])
        self.assertIn("No new write", message)
        self.assertEqual(attempt_state.profile_facts, {"preference.language": "Chinese"})
        progress.assert_any_await("memory_saved")
