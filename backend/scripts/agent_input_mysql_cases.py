"""只在验收脚本创建的临时 MySQL 上检查输入快照和准入回滚；不调用模型。"""

from dataclasses import replace
from datetime import datetime
import unittest
from uuid import uuid4

from sqlalchemy import select

from app.models import AgentMemory, Message
from app.services.agent_input import prepare_agent_input
from app.services.chat_sessions import create_chat_session
from app.services.errors import AgentInputUnavailableError, ContextTooLargeError, SessionNotFoundError
from message_submission_mysql_cases import MessageSubmissionMySQLTest


class AgentInputMySQLTest(unittest.TestCase):
    engine = None
    # 复用测试夹具方法，不继承其他 TestCase 的测试用例。
    setUp = MessageSubmissionMySQLTest.setUp
    accept = MessageSubmissionMySQLTest.accept
    rows = MessageSubmissionMySQLTest.rows
    snapshot_chat = MessageSubmissionMySQLTest.snapshot_chat

    def prepare(self, user=None, chat_id=None):
        with self.factory() as session:
            return prepare_agent_input(session, user or self.user, chat_id or self.chat.session_id,
                                       self.request, self.policy)

    def pair(self, label, chat_id=None, failed=False, missing_answer=False):
        # 相同时间戳特意覆盖 message_id 排序/翻页。
        now = datetime(2026, 9, 25)
        with self.factory.begin() as session:
            q = Message(chat_session_id=int(chat_id or self.chat.session_id), role="USER", content=label,
                        client_message_key=str(uuid4()), attempt_id=str(uuid4()),
                        generation_status="FAILED" if failed else "SUCCEEDED", created_at=now, updated_at=now)
            if failed:
                q.generation_error_code = "MODEL_REQUEST_FAILED"
                q.generation_error_message = "Safe error"
            session.add(q)
            session.flush()
            if not failed and not missing_answer:
                session.add(Message(chat_session_id=q.chat_session_id, role="ASSISTANT", content=f"Answer {label}",
                                    in_reply_to_message_id=q.message_id, created_at=now, updated_at=now))
            return str(q.message_id)

    def memory(self, user_id=None, summary="喜欢中文"):
        with self.factory.begin() as session:
            session.add(AgentMemory(user_id=int(user_id or self.user.user_id), memory_key="preference.language",
                                    memory_type="LEARNING_PREFERENCE", summary=summary,
                                    created_at=datetime(2026, 9, 25), updated_at=datetime(2026, 9, 25)))

    def test_sixty_rounds_sorted_and_failed_questions_excluded(self):
        ids = [self.pair(f"question-{i}") for i in range(60)]
        self.pair("failed question secret", failed=True)
        result = self.prepare()
        self.assertEqual(result.history_rounds, 60)
        self.assertEqual([m.source_message_id for m in result.messages[:-1:2]], ids)
        self.assertNotIn("failed question secret", [m.content for m in result.messages])
        self.assertEqual(result.messages[-1].content, self.request.content)
        self.assertEqual(len(self.rows()), 121)  # 组装没有插入当前问题。

    def test_same_user_other_chat_only_shares_memory_not_raw_messages(self):
        other = create_chat_session(self.user, self.factory)
        self.pair("other chat private", chat_id=other.session_id)
        self.memory()
        result = self.prepare()
        self.assertEqual(result.history_rounds, 0)
        self.assertIn("喜欢中文", result.profile_memory)
        self.assertNotIn("other chat private", [m.content for m in result.messages])
        with self.factory() as session:
            self.assertEqual(len(session.scalars(select(AgentMemory).where(
                AgentMemory.user_id == int(self.user.user_id))).all()), 1)

    def test_cross_user_access_and_memory_are_isolated(self):
        fixture = MessageSubmissionMySQLTest()
        fixture.engine = self.engine
        fixture.setUp()
        self.memory(summary="owner memory")
        self.memory(user_id=fixture.user.user_id, summary="other user secret")
        self.pair("other user's chat", chat_id=fixture.chat.session_id)
        result = self.prepare()
        self.assertIn("owner memory", result.profile_memory)
        self.assertNotIn("other user secret", result.profile_memory)
        for user, chat_id in ((self.user, fixture.chat.session_id), (fixture.user, self.chat.session_id)):
            with self.assertRaises(SessionNotFoundError):
                self.prepare(user=user, chat_id=chat_id)

    def test_too_large_rejected_before_insert_key_title_or_occupancy(self):
        before = self.snapshot_chat()
        original = self.policy
        self.policy = replace(self.policy, app_context_tokens=self.policy.app_context_tokens - self.policy.input_limit + 1)
        with self.assertRaises(ContextTooLargeError), self.accept():
            self.fail("Must not accept")
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.snapshot_chat(), before)
        self.assertEqual(self.registry._entries, {})
        self.check.assert_not_called()
        self.policy = original
        with self.accept() as accepted:  # 同一个 key 没有被失败准入消耗。
            self.assertEqual(accepted.prepared_input.messages[-1].content, self.request.content)
        self.assertEqual(len(self.rows()), 1)

    def test_accepted_snapshot_does_not_reload_or_duplicate_current_question(self):
        old_id = self.pair("previous")
        self.memory()
        with self.accept() as accepted:
            prepared = accepted.prepared_input
            self.assertEqual([m.source_message_id for m in prepared.messages], [old_id, str(int(old_id) + 1), None])
            self.assertEqual(sum(m.content == self.request.content for m in prepared.messages), 1)
            self.assertEqual(len(self.rows()), 3)
            with self.factory.begin() as session:
                memory = session.scalar(select(AgentMemory).where(AgentMemory.user_id == int(self.user.user_id)))
                memory.summary = "Changed after acceptance"
            self.assertIn("喜欢中文", prepared.profile_memory)
            self.assertNotIn("Changed after acceptance", prepared.profile_memory)

    def test_duplicate_does_not_reassemble_with_smaller_budget(self):
        with self.accept() as first:
            self.policy = replace(self.policy, app_context_tokens=self.policy.app_context_tokens - self.policy.input_limit + 1)
            with self.accept() as duplicate:
                self.assertTrue(duplicate.duplicate)
                self.assertEqual(duplicate.attempt_id, first.attempt_id)
        self.check.assert_called_once()
        self.assertEqual(len(self.rows()), 1)

    def test_inconsistent_success_cannot_accept_new_question(self):
        self.pair("missing answer", missing_answer=True)
        before = self.snapshot_chat()
        with self.assertRaises(AgentInputUnavailableError), self.accept():
            self.fail("Must not accept")
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.snapshot_chat(), before)
        self.assertEqual(self.registry._entries, {})

    def test_budget_selects_recent_whole_pair_without_deleting_history(self):
        baseline = self.prepare().estimated_input_tokens
        self.pair("old large " + "文" * 300)
        newest = self.pair("new")
        self.policy = replace(self.policy, app_context_tokens=self.policy.app_context_tokens - self.policy.input_limit
                              + baseline + 100)
        result = self.prepare()
        self.assertEqual(result.history_rounds, 1)
        self.assertTrue(result.history_truncated)
        self.assertEqual(result.messages[0].source_message_id, newest)
        self.assertEqual(len(self.rows()), 4)
