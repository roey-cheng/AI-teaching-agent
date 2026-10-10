"""记忆业务的真实 MySQL 验收；由隔离容器脚本注入 Engine，不读取项目 .env。"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from threading import Barrier
import unittest
from uuid import uuid4

from sqlalchemy import event, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.agent.input_policy import policy_for_model
from app.db.session import build_session_factory
from app.models import AgentMemory, Message
from app.schemas import RegisterRequest, SendMessageRequest
from app.services.agent_input import prepare_agent_input
from app.services.auth import register_user
from app.services.chat_sessions import create_chat_session
from app.services.errors import MemoryUnavailableError, SessionNotFoundError
from app.services.generation_registry import GenerationRegistry
from app.services.generation_result import StaleGenerationError, settle_generation
from app.services.message_submission import accept_user_message
from app.services.profile_memory import ProfileFact, list_profile_memory, save_profile_facts


class ProfileMemoryMySQLTest(unittest.TestCase):
    engine = None

    def setUp(self):
        self.factory = build_session_factory(self.engine)
        self.user = self.new_user()
        self.chat = create_chat_session(self.user, self.factory)
        self.registry = GenerationRegistry()
        self.policy = policy_for_model("deepseek-v4-pro")

    def new_user(self):
        return register_user(RegisterRequest(email=f"memory-{uuid4().hex}@example.com",
                                              password="test-password", display_name="Memory tester"), self.factory)

    def accepted(self, chat=None):
        return accept_user_message(
            self.user, (chat or self.chat).session_id,
            SendMessageRequest(client_message_key=str(uuid4()), content="以后请用中文解释，我也使用 Git 和 Docker。"),
            self.factory, self.registry, check_new_message=lambda _: None, input_policy=self.policy,
        )

    def save(self, accepted, summary="喜欢中文", key="preference.language", factory=None):
        return save_profile_facts(accepted, [ProfileFact(memory_key=key, summary=summary)],
                                  factory or self.factory, self.registry)

    def rows(self):
        with self.factory() as session:
            return session.scalars(select(AgentMemory).where(AgentMemory.user_id == int(self.user.user_id))
                                   .order_by(AgentMemory.memory_key)).all()

    def test_insert_update_deduplicate_and_preserve_unmentioned_topics(self):
        with self.accepted() as accepted:
            self.save(accepted)
            self.save(accepted, "使用 Git", "programming.tools")
            first = {item.memory_key: item for item in self.rows()}
            self.save(accepted, "使用 Git 和 Docker", "programming.tools")
            self.save(accepted, "使用 Git 和 Docker", "programming.tools")
            current = {item.memory_key: item for item in self.rows()}
            self.assertEqual(len(current), 2)
            self.assertEqual(current["programming.tools"].summary, "使用 Git 和 Docker")
            self.assertEqual(current["programming.tools"].memory_id, first["programming.tools"].memory_id)
            self.assertEqual(current["programming.tools"].created_at, first["programming.tools"].created_at)
            self.assertEqual(current["preference.language"].updated_at, first["preference.language"].updated_at)
            self.assertEqual(current["programming.tools"].memory_type, "PROGRAMMING_BACKGROUND")

    def test_list_filters_owner_has_stable_order_and_no_read_side_effects(self):
        other = self.new_user()
        self.assertEqual(list_profile_memory(self.user, self.factory).items, [])
        with self.accepted() as accepted:
            self.save(accepted)
            self.save(accepted, "使用 Git", "programming.tools")
        same_time = datetime(2026, 10, 11)
        with self.factory.begin() as session:
            rows = session.scalars(select(AgentMemory).where(AgentMemory.user_id == int(self.user.user_id))).all()
            for item in rows:
                item.updated_at = same_time
        before = {item.memory_id: item.updated_at for item in self.rows()}
        result = list_profile_memory(self.user, build_session_factory(self.engine))
        self.assertEqual([int(item.memory_id) for item in result.items], sorted(before, reverse=True))
        self.assertEqual({item.memory_id: item.updated_at for item in self.rows()}, before)
        self.assertEqual(list_profile_memory(other, self.factory).items, [])
        self.assertNotIn("user_id", result.model_dump_json())
        self.assertNotIn("memory_key", result.model_dump_json())

    def test_saved_memory_survives_failed_answer_and_enters_new_chat_input(self):
        with self.accepted() as accepted:
            self.save(accepted)
            settle_generation(accepted, self.factory, self.registry, error_code="MODEL_REQUEST_FAILED")
        second = create_chat_session(self.user, self.factory)
        with self.factory() as session:
            prepared = prepare_agent_input(session, self.user, second.session_id,
                                           SendMessageRequest(client_message_key=str(uuid4()), content="你好"), self.policy)
        self.assertIn("喜欢中文", prepared.profile_memory)
        self.assertEqual(prepared.history_rounds, 0)
        self.assertEqual(len(prepared.messages), 1)

    def test_foreign_identity_and_finished_attempt_cannot_write(self):
        other = self.new_user()
        with self.accepted() as accepted:
            forged = replace(accepted, prepared_input=replace(accepted.prepared_input, user_id=other.user_id))
            with self.assertRaises(SessionNotFoundError):
                self.save(forged)
            settle_generation(accepted, self.factory, self.registry, answer="Final answer")
            with self.assertRaises(StaleGenerationError):
                self.save(accepted)
        with self.assertRaises(StaleGenerationError):
            self.save(accepted)
        self.assertEqual(self.rows(), [])

    def test_database_attempt_mismatch_and_failed_status_cannot_write(self):
        with self.accepted() as accepted:
            with self.factory.begin() as session:
                question = session.get(Message, int(accepted.user_message_id))
                question.attempt_id = str(uuid4())
            try:
                with self.assertRaises(StaleGenerationError):
                    self.save(accepted)
            finally:
                with self.factory.begin() as session:
                    session.get(Message, int(accepted.user_message_id)).attempt_id = accepted.attempt_id
            settle_generation(accepted, self.factory, self.registry, error_code="GENERATION_TIMEOUT")
            with self.assertRaises(StaleGenerationError):
                self.save(accepted)
        self.assertEqual(self.rows(), [])

    def test_batch_failure_before_commit_rolls_back_update_and_insert(self):
        class FailingSession(Session):
            pass

        def fail(_session):
            raise SQLAlchemyError("private SQL error")

        event.listen(FailingSession, "before_commit", fail)
        failing_factory = sessionmaker(bind=self.engine, class_=FailingSession, expire_on_commit=False)
        with self.accepted() as accepted:
            self.save(accepted)
            facts = [ProfileFact(memory_key="preference.language", summary="喜欢英文"),
                     ProfileFact(memory_key="programming.tools", summary="使用 Git")]
            with self.assertRaises(MemoryUnavailableError) as error:
                save_profile_facts(accepted, facts, failing_factory, self.registry)
            self.assertNotIn("private", str(error.exception))
            self.assertEqual([(row.memory_key, row.summary) for row in self.rows()], [("preference.language", "喜欢中文")])
            self.save(accepted, "喜欢英文")  # 故障不释放生成占用，正常存储仍可使用。

    def test_commit_acknowledgment_failure_is_not_reported_as_saved(self):
        class AckLostSession(Session):
            pass

        def fail(_session):
            raise SQLAlchemyError("connection failed after commit")

        event.listen(AckLostSession, "after_commit", fail)
        failing_factory = sessionmaker(bind=self.engine, class_=AckLostSession, expire_on_commit=False)
        with self.accepted() as accepted:
            with self.assertRaises(MemoryUnavailableError):
                self.save(accepted, factory=failing_factory)
            # 模拟确认丢失：确实提交过，但函数不能谎报成功或自动再写一遍。
            self.assertEqual([row.summary for row in self.rows()], ["喜欢中文"])

    def concurrent_saves(self, pairs):
        chats = [create_chat_session(self.user, self.factory) for _ in pairs]
        barrier = Barrier(len(pairs))

        def worker(pair):
            chat, (key, summary) = pair
            with self.accepted(chat) as accepted:
                barrier.wait(timeout=10)
                self.save(accepted, summary, key)

        with ThreadPoolExecutor(max_workers=len(pairs)) as pool:
            list(pool.map(worker, zip(chats, pairs)))

    def test_concurrent_different_topics_are_both_kept(self):
        self.concurrent_saves([("preference.language", "喜欢中文"), ("programming.tools", "使用 Git")])
        self.assertEqual({row.memory_key for row in self.rows()}, {"preference.language", "programming.tools"})

    def test_concurrent_same_topic_has_one_row_and_later_commit_can_replace(self):
        self.concurrent_saves([("preference.language", "喜欢中文"), ("preference.language", "喜欢英文")])
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertIn(rows[0].summary, ("喜欢中文", "喜欢英文"))
        with self.accepted() as accepted:
            self.save(accepted, "喜欢双语")
        self.assertEqual(self.rows()[0].memory_id, rows[0].memory_id)
        self.assertEqual(self.rows()[0].summary, "喜欢双语")
