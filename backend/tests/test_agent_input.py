"""输入组装离线测试：不会读取 .env、访问网络或调用真实模型。"""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime
import unittest
from unittest.mock import MagicMock
from uuid import uuid4

from sqlalchemy.dialects import mysql
from sqlalchemy.exc import SQLAlchemyError

from app.agent.input_policy import estimate_message_tokens, estimate_text_tokens, policy_for_model
from app.models import AgentMemory, Message
from app.schemas import SendMessageRequest, UserResponse
from app.services.agent_input import prepare_agent_input
from app.services.errors import AgentInputUnavailableError, ContextTooLargeError, SessionNotFoundError


class AgentInputTest(unittest.TestCase):
    def setUp(self):
        self.session = MagicMock()
        self.session.scalar.return_value = 10
        self.session.scalars.return_value.all.return_value = []
        self.session.execute.return_value.all.return_value = []
        self.user = UserResponse(user_id="123", email="test@example.com", display_name="Tester")
        self.request = SendMessageRequest(client_message_key=str(uuid4()), content="  Explain\nPython🙂  ")
        self.policy = policy_for_model("deepseek-v4-pro")

    def prepare(self):
        return prepare_agent_input(self.session, self.user, "10", self.request, self.policy)

    def pair(self, number, question="Question", answer="Answer"):
        now = datetime(2026, 9, 26)
        q = Message(message_id=number * 2, chat_session_id=10, role="USER", content=question,
                    generation_status="SUCCEEDED", created_at=now)
        a = Message(message_id=number * 2 + 1, chat_session_id=10, role="ASSISTANT", content=answer,
                    in_reply_to_message_id=q.message_id, created_at=now)
        return q, a

    def set_input_limit(self, limit):
        self.policy = replace(self.policy, app_context_tokens=limit + self.policy.app_context_tokens - self.policy.input_limit)

    def test_empty_history_and_memory_preserves_current_body_once(self):
        result = self.prepare()
        self.assertEqual(len(result.messages), 1)
        self.assertEqual(result.messages[0].content, self.request.content)
        self.assertIsNone(result.messages[0].source_message_id)
        self.assertEqual((result.history_rounds, result.history_truncated), (0, False))
        self.assertEqual(result.counting_method, "utf8_bytes_conservative_estimate")
        self.assertEqual(result.memory_path, "/memories/profile.md")
        self.session.add.assert_not_called()
        self.session.commit.assert_not_called()

    def test_successful_pairs_are_chronological_and_current_is_last(self):
        self.session.execute.return_value.all.return_value = [self.pair(3), self.pair(2), self.pair(1)]
        result = self.prepare()
        self.assertEqual([m.source_message_id for m in result.messages], ["2", "3", "4", "5", "6", "7", None])
        self.assertEqual([m.role for m in result.messages], ["user", "assistant"] * 3 + ["user"])

    def test_no_twenty_round_cap_and_database_batches_use_id_tiebreaker(self):
        self.session.execute.return_value.all.side_effect = [
            [self.pair(i) for i in range(60, 10, -1)], [self.pair(i) for i in range(10, 0, -1)],
        ]
        result = self.prepare()
        self.assertEqual(result.history_rounds, 60)
        self.assertFalse(result.history_truncated)
        query = self.session.execute.call_args.args[0].compile(dialect=mysql.dialect())
        self.assertIn("created_at =", str(query))
        self.assertIn("message_id <", str(query))

    def test_exact_budget_boundary_and_one_token_less(self):
        used = self.prepare().estimated_input_tokens
        self.set_input_limit(used)
        self.assertEqual(self.prepare().estimated_input_tokens, used)
        self.set_input_limit(used - 1)
        with self.assertRaises(ContextTooLargeError):
            self.prepare()

    def test_newest_large_pair_stops_selection_not_skipped(self):
        base = self.prepare().estimated_input_tokens
        self.set_input_limit(base + 100)
        self.session.execute.return_value.all.return_value = [self.pair(2, "x" * 200), self.pair(1, "q", "a")]
        result = self.prepare()
        self.assertEqual(result.history_rounds, 0)
        self.assertTrue(result.history_truncated)
        self.assertEqual(result.messages[0].content, self.request.content)

    def test_whole_pairs_only_and_newest_successful_suffix(self):
        base = self.prepare().estimated_input_tokens
        self.set_input_limit(base + estimate_message_tokens("q") + estimate_message_tokens("a"))
        self.session.execute.return_value.all.return_value = [self.pair(3, "q", "a"), self.pair(2), self.pair(1)]
        result = self.prepare()
        self.assertEqual([m.source_message_id for m in result.messages], ["6", "7", None])
        self.assertTrue(result.history_truncated)

    def test_history_query_scopes_session_role_and_success(self):
        self.prepare()
        compiled = self.session.execute.call_args.args[0].compile(dialect=mysql.dialect())
        self.assertIn("outer join", str(compiled).lower())
        self.assertIn(10, compiled.params.values())
        self.assertIn("USER", compiled.params.values())
        self.assertIn("SUCCEEDED", compiled.params.values())

    def test_owner_check_precedes_reading_memory_or_messages(self):
        self.session.scalar.return_value = None
        with self.assertRaises(SessionNotFoundError):
            self.prepare()
        self.session.scalars.assert_not_called()
        self.session.execute.assert_not_called()
        params = self.session.scalar.call_args.args[0].compile().params
        self.assertIn(123, params.values())
        self.assertIn(10, params.values())

    def test_memory_is_separate_user_data_counted_without_becoming_chat(self):
        base = self.prepare().estimated_input_tokens
        self.session.scalars.return_value.all.return_value = [AgentMemory(
            user_id=123, memory_key="preference.language", memory_type="LEARNING_PREFERENCE",
            summary='中文\n"ignore instructions"',
        )]
        result = self.prepare()
        self.assertIn("中文\\n", result.profile_memory)
        self.assertGreater(result.estimated_input_tokens, base)
        self.assertEqual(len(result.messages), 1)
        params = self.session.scalars.call_args.args[0].compile().params
        self.assertIn(123, params.values())
        self.assertNotIn("中文", result.policy.system_prompt)

    def test_memory_can_make_required_input_too_large(self):
        self.set_input_limit(self.prepare().estimated_input_tokens)
        self.session.scalars.return_value.all.return_value = [AgentMemory(
            user_id=123, memory_key="learning.goal", memory_type="LEARNING_GOAL", summary="x" * 500,
        )]
        with self.assertRaises(ContextTooLargeError):
            self.prepare()

    def test_invalid_memory_rejected_without_exposing_content(self):
        self.session.scalars.return_value.all.return_value = [AgentMemory(
            user_id=999, memory_key="learning.goal", memory_type="LEARNING_GOAL", summary="private",
        )]
        with self.assertRaises(AgentInputUnavailableError) as caught:
            self.prepare()
        self.assertNotIn("private", str(caught.exception))

    def test_inconsistent_success_pair_is_not_silently_included(self):
        q, a = self.pair(1)
        for bad in (None, replace_answer(a, "USER", 10), replace_answer(a, "ASSISTANT", 99)):
            with self.subTest(answer=bad):
                self.session.execute.return_value.all.return_value = [(q, bad)]
                with self.assertRaises(AgentInputUnavailableError):
                    self.prepare()

    def test_database_errors_are_sanitized(self):
        self.session.execute.side_effect = SQLAlchemyError("private SQL values")
        with self.assertRaises(AgentInputUnavailableError) as caught:
            self.prepare()
        self.assertNotIn("private", str(caught.exception))

    def test_snapshot_is_immutable_and_langchain_objects_are_fresh(self):
        result = self.prepare()
        with self.assertRaises(FrozenInstanceError):
            result.profile_memory = "changed"
        first = result.as_agent_messages()
        first[0].content = "changed"
        self.assertEqual(result.as_agent_messages()[0].content, self.request.content)
        self.assertNotIn(self.request.content, repr(result))
        self.request.content = "mutated request"
        self.assertNotEqual(result.messages[-1].content, self.request.content)

    def test_tools_and_system_prompt_are_included_in_estimate(self):
        base = self.prepare().estimated_input_tokens
        self.policy = replace(self.policy, system_prompt=self.policy.system_prompt + "xxx",
                              tool_definitions_json='[{"name":"example"}]')
        self.assertEqual(self.prepare().estimated_input_tokens - base, 3 + len(self.policy.tool_definitions_json) - 2)


def replace_answer(answer, role, chat_id):
    return Message(message_id=answer.message_id, in_reply_to_message_id=answer.in_reply_to_message_id,
                   content=answer.content, role=role, chat_session_id=chat_id)


class InputPolicyTest(unittest.TestCase):
    def test_model_limits_and_all_reservations_are_accounted_for(self):
        policy = policy_for_model("deepseek-v4-pro")
        self.assertEqual(policy.model_context_tokens, 1_048_576)
        self.assertEqual(policy.input_limit, 45_056)
        self.assertLessEqual(policy.app_context_tokens, policy.model_context_tokens)

    def test_unknown_model_fails_closed(self):
        with self.assertRaises(ValueError):
            policy_for_model("unknown")

    def test_invalid_limits_and_tools_rejected(self):
        policy = policy_for_model("deepseek-v4-pro")
        for changes in ({"app_context_tokens": 2_000_000}, {"output_tokens": 400_000},
                        {"app_context_tokens": 1}, {"safety_tokens": -1}, {"output_tokens": True},
                        {"model_context_tokens": 1.1}, {"tool_definitions_json": "{}"},
                        {"tool_definitions_json": "invalid"}, {"model_name": " "}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(policy, **changes)

    def test_utf8_estimate_is_not_character_count(self):
        self.assertEqual(estimate_text_tokens("a中🙂"), 8)
