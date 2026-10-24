"""Opt-in disposable MySQL integration tests. Never reads .env or an application DB."""

import asyncio
import os
import unittest
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from alembic.config import Config
from langchain_core.messages import ToolMessage
from langsmith import tracing_context
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from test_chat_agent_factory import settings as model_settings
from test_memory_orchestrator import MemoryModel, answer

from alembic import command
from app.agent.consolidation import ConsolidatedFact
from app.agent.factory import ChatAgentProvider
from app.agent.input_policy import policy_for_model
from app.agent.profile_store import PROFILE_MEMORY_PATH, ProfileStore, topic_path
from app.core.config import GenerationQuotaSettings, MemorySettings, TracingSettings
from app.models import (
    AgentMemory,
    AgentTurnRecord,
    ChatSession,
    MemoryJob,
    MemoryState,
    MemoryTopicRevision,
    Message,
    User,
)
from app.schemas import SendMessageRequest, UserResponse
from app.services.chat_execution import execute_chat_turn
from app.services.errors import GlobalConcurrencyLimitError
from app.services.generation_quota import GenerationQuota
from app.services.generation_registry import GenerationRegistry
from app.services.memory_tasks import (
    MemoryConflict,
    check_and_bump_revisions,
    claim_memory_job,
    fail_memory_job,
    finish_memory_job,
    recover_memory_jobs,
    touch_memory,
    utcnow,
)
from testing_support import require_value

TEST_URL = os.environ.get("MEMORY_TEST_MYSQL_URL")


@unittest.skipUnless(TEST_URL, "Requires explicit disposable MEMORY_TEST_MYSQL_URL")
class MemoryMySQLTest(unittest.TestCase):
    def test_native_storebackend_persists_across_new_sessions_and_isolates_users(self):
        with self.factory.begin() as session:
            user = self.make_user(session)
            other = self.make_user(session)
            uid, other_id = user.user_id, other.user_id
            backend = ProfileStore(uid, session, writable=True).backend()
            backend.write(topic_path("preference.language"), "喜欢中文")
        with self.factory() as session:
            backend = ProfileStore(uid, session).backend()
            file = backend.download_files([PROFILE_MEMORY_PATH])[0]
            self.assertIn("喜欢中文", require_value(file.content).decode())
            self.assertEqual(ProfileStore(other_id, session).facts(), {})
            original_id = ProfileStore(uid, session).rows()[0].memory_id
        with self.factory.begin() as session:
            ProfileStore(uid, session, writable=True).backend().edit(
                topic_path("preference.language"), "中文", "英文"
            )
        with self.factory() as session:
            store = ProfileStore(uid, session)
            self.assertEqual(store.facts()["preference.language"], "喜欢英文")
            self.assertEqual(store.rows()[0].memory_id, original_id)

    def test_store_write_and_revision_rollback_together(self):
        from app.models import MemoryTopicRevision

        with self.factory.begin() as session:
            uid = self.make_user(session).user_id
        with (
            self.assertRaisesRegex(RuntimeError, "deliberate rollback"),
            self.factory.begin() as session,
        ):
            check_and_bump_revisions(
                session, uid, ["preference.language"], {"preference.language": 0}, {}
            )
            ProfileStore(uid, session, writable=True).backend().write(
                topic_path("preference.language"), "must roll back"
            )
            raise RuntimeError("deliberate rollback")
        with self.factory() as session:
            self.assertEqual(ProfileStore(uid, session).facts(), {})
            self.assertIsNone(session.get(MemoryTopicRevision, (uid, "preference.language")))

    @classmethod
    def setUpClass(cls):
        assert TEST_URL is not None
        url = make_url(TEST_URL)
        if (
            url.host != "127.0.0.1"
            or url.database != "memory_test"
            or url.password != "test-only-memory"
        ):
            raise ValueError("Only the explicitly isolated test container is permitted")
        cls.database_name = "memory_test_" + uuid4().hex
        cls.admin_engine = create_engine(url, hide_parameters=True)
        with cls.admin_engine.begin() as connection:
            connection.execute(text(f"CREATE DATABASE `{cls.database_name}`"))
        cls.engine = create_engine(url.set(database=cls.database_name), hide_parameters=True)
        cls.factory = sessionmaker(cls.engine, expire_on_commit=False)
        with (
            patch("app.core.config.load_database_settings", return_value=object()),
            patch("app.db.engine.build_database_engine", return_value=cls.engine),
        ):
            command.upgrade(Config("alembic.ini"), "20261012_0002")
            with cls.factory.begin() as session:
                old = cls.make_user(session)
                cls.legacy_user_id = old.user_id
                chat = cls.make_chat(session, old.user_id)
                q = cls.make_question(session, chat.chat_session_id, "old fact")
                cls.legacy_message_id = q.message_id
            command.upgrade(Config("alembic.ini"), "20261012_0003")
            with cls.factory.begin() as session:
                old = session.get(User, cls.legacy_user_id)
                assert old is not None
                q = session.get(Message, cls.legacy_message_id)
                assert q is not None
                now = utcnow()
                session.add_all(
                    [
                        AgentMemory(
                            user_id=old.user_id,
                            memory_key="personal.long_term_goal",
                            memory_type="PERSONAL_BACKGROUND",
                            summary="完成一个长期项目",
                            created_at=now,
                            updated_at=now,
                        ),
                        AgentMemory(
                            user_id=old.user_id,
                            memory_key="preference.conversation_tone",
                            memory_type="DAILY_PREFERENCE",
                            summary="自然一点",
                            created_at=now,
                            updated_at=now,
                        ),
                        AgentMemory(
                            user_id=old.user_id,
                            memory_key="personal.interests",
                            memory_type="PERSONAL_BACKGROUND",
                            summary="摄影",
                            created_at=now,
                            updated_at=now,
                        ),
                        MemoryTopicRevision(
                            user_id=old.user_id,
                            memory_key="personal.long_term_goal",
                            revision=3,
                            source_message_id=q.message_id,
                        ),
                    ]
                )
            command.upgrade(Config("alembic.ini"), "head")

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        with cls.admin_engine.begin() as connection:
            connection.execute(text(f"DROP DATABASE `{cls.database_name}`"))
        cls.admin_engine.dispose()

    @staticmethod
    def make_user(session):
        now = utcnow() - timedelta(minutes=20)
        user = User(
            email=f"{uuid4().hex}@example.com",
            password_hash="test-not-a-real-hash",
            display_name="test",
            status="ACTIVE",
            system_role="USER",
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        session.flush()
        return user

    @staticmethod
    def make_chat(session, uid):
        now = utcnow() - timedelta(minutes=20)
        chat = ChatSession(
            user_id=uid,
            title="test",
            title_is_manual=False,
            last_activity_at=now,
            created_at=now,
            updated_at=now,
        )
        session.add(chat)
        session.flush()
        return chat

    @staticmethod
    def make_question(session, chatid, content, status="SUCCEEDED"):
        now = utcnow() - timedelta(minutes=20)
        q = Message(
            chat_session_id=chatid,
            role="USER",
            content=content,
            client_message_key=str(uuid4()),
            attempt_id=str(uuid4()),
            generation_status=status,
            created_at=now,
            updated_at=now,
        )
        session.add(q)
        session.flush()
        if status == "SUCCEEDED":
            session.add(
                Message(
                    chat_session_id=chatid,
                    role="ASSISTANT",
                    content="test answer",
                    in_reply_to_message_id=q.message_id,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.flush()
        return q

    def setUp(self):
        # Tests leave rows for inspection. Finish/disable previous test jobs under the test-only DB.
        with self.factory.begin() as session:
            for state in session.scalars(select(MemoryState)):
                state.dirty = False
            for job in session.scalars(select(MemoryJob).where(MemoryJob.status == "RUNNING")):
                job.status, job.finished_at = "FAILED", utcnow()
            self.user = self.make_user(session)
            self.chat = self.make_chat(session, self.user.user_id)
        self.settings = MemorySettings.from_sources(_env_file=None, global_daily_limit=1000)

    def test_topic_migration_maps_teaching_preferences_and_removes_personal_topics(self):
        with self.factory() as session:
            rows = {
                row.memory_key: row
                for row in session.scalars(
                    select(AgentMemory).where(AgentMemory.user_id == self.legacy_user_id)
                )
            }
            self.assertEqual(rows["learning.goal"].summary, "完成一个长期项目")
            self.assertEqual(rows["preference.explanation_style"].summary, "自然一点")
            self.assertNotIn("personal.interests", rows)
            revisions = {
                row.memory_key: row
                for row in session.scalars(
                    select(MemoryTopicRevision).where(
                        MemoryTopicRevision.user_id == self.legacy_user_id
                    )
                )
            }
            self.assertEqual(revisions["learning.goal"].revision, 3)
            self.assertNotIn("personal.long_term_goal", revisions)

    def enqueue(self, content="我喜欢中文解释"):
        with self.factory.begin() as session:
            q = self.make_question(session, self.chat.chat_session_id, content)
            touch_memory(
                session,
                self.user.user_id,
                utcnow() - timedelta(minutes=20),
                dirty=True,
                message_id=q.message_id,
            )
            return q.message_id

    def claim(self, **kwargs):
        return claim_memory_job(self.factory, self.settings, 2, **kwargs)

    def candidate(self, mid, text="喜欢中文解释", quote="我喜欢中文解释"):
        return ConsolidatedFact(
            memory_key="preference.language",
            summary=text,
            source_quote=quote,
            source_message_id=mid,
        )

    def test_migration_does_not_queue_old_history(self):
        with self.factory() as session:
            state = require_value(session.get(MemoryState, self.legacy_user_id))
            self.assertGreaterEqual(state.cursor, self.legacy_message_id)
            self.assertEqual(state.pending_ids, [])
            self.assertFalse(state.dirty)

    def test_commit_is_idempotent_and_queue_drains(self):
        mid = self.enqueue()
        job = require_value(self.claim())
        finish_memory_job(self.factory, job, [self.candidate(mid)])
        finish_memory_job(self.factory, job, [self.candidate(mid)])
        with self.factory() as session:
            rows = session.scalars(
                select(AgentMemory).where(AgentMemory.user_id == self.user.user_id)
            ).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].summary, "喜欢中文解释")
            state = require_value(session.get(MemoryState, self.user.user_id))
            self.assertEqual(state.pending_ids, [])
            self.assertFalse(state.dirty)

    def test_newer_immediate_write_cannot_be_overwritten(self):
        mid = self.enqueue()
        job = require_value(self.claim())
        with self.factory.begin() as session:
            check_and_bump_revisions(
                session,
                self.user.user_id,
                ["preference.language"],
                {"preference.language": mid + 100},
                {},
            )
        with self.assertRaises(MemoryConflict):
            finish_memory_job(self.factory, job, [self.candidate(mid)])
        with self.factory() as session:
            self.assertIn(
                mid, require_value(session.get(MemoryState, self.user.user_id)).pending_ids
            )

    def test_exact_source_and_privacy_validation(self):
        mid = self.enqueue()
        job = require_value(self.claim())
        for fact in (
            self.candidate(mid + 1),
            self.candidate(mid, quote="invented"),
            self.candidate(mid, text="password secret"),
        ):
            with self.assertRaises(ValueError):
                finish_memory_job(self.factory, job, [fact])

    def test_new_activity_and_active_generation_defer(self):
        self.enqueue()
        with self.factory.begin() as session:
            state = require_value(session.get(MemoryState, self.user.user_id))
            state.last_activity_at = utcnow()
        self.assertIsNone(self.claim())
        with self.factory.begin() as session:
            state = require_value(session.get(MemoryState, self.user.user_id))
            state.last_activity_at = utcnow() - timedelta(minutes=20)
            running = self.make_question(session, self.chat.chat_session_id, "new", "RUNNING")
            running.created_at = utcnow()
        self.assertIsNone(self.claim())
        with self.factory.begin() as session:
            q = require_value(session.get(Message, running.message_id))
            q.generation_status = "FAILED"
            q.generation_error_code, q.generation_error_message = "TEST", "test"

    def test_recovery_retries_twice_then_pauses_without_losing_data(self):
        mid = self.enqueue()
        now = utcnow()
        first = require_value(self.claim(now=now))
        recover_memory_jobs(self.factory)
        self.assertIsNone(self.claim(now=now + timedelta(seconds=30)))
        second = require_value(self.claim(now=now + timedelta(minutes=2)))
        self.assertIsNotNone(second)
        fail_memory_job(self.factory, second.job_id, now=now + timedelta(minutes=2))
        third = require_value(self.claim(now=now + timedelta(minutes=8)))
        self.assertIsNotNone(third)
        fail_memory_job(self.factory, third.job_id, now=now + timedelta(minutes=8))
        self.assertIsNone(self.claim(now=now + timedelta(minutes=20)))
        with self.factory() as session:
            state = require_value(session.get(MemoryState, self.user.user_id))
            self.assertTrue(state.paused)
            self.assertIn(mid, state.pending_ids)
            self.assertEqual(require_value(session.get(MemoryJob, first.job_id)).status, "FAILED")

    def test_user_quota_counts_failed_calls(self):
        self.enqueue()
        self.settings = self.settings.model_copy(update={"user_daily_limit": 1})
        job = require_value(self.claim())
        fail_memory_job(self.factory, job.job_id)
        self.assertIsNone(self.claim(now=utcnow() + timedelta(minutes=10)))

    def test_new_pending_message_survives_inflight_commit(self):
        first = self.enqueue()
        job = require_value(self.claim())
        second = self.enqueue("我喜欢详细解释")
        finish_memory_job(self.factory, job, [self.candidate(first)])
        with self.factory() as session:
            self.assertEqual(
                require_value(session.get(MemoryState, self.user.user_id)).pending_ids, [second]
            )

    def test_running_background_job_reserves_foreground_capacity(self):
        mid = self.enqueue()
        self.assertIsNotNone(self.claim())
        self.assertIsNone(self.claim())
        quota = GenerationQuota(
            GenerationQuotaSettings.from_sources(_env_file=None, global_concurrency_limit=1)
        )
        with self.factory.begin() as session, self.assertRaises(GlobalConcurrencyLimitError):
            quota.admit(
                session,
                user_id=self.user.user_id,
                message_id=mid,
                attempt_id=str(uuid4()),
                now=utcnow(),
            )

    def test_global_quota_counts_failed_jobs(self):
        self.enqueue()
        job = require_value(self.claim())
        fail_memory_job(self.factory, job.job_id)
        self.settings = self.settings.model_copy(update={"global_daily_limit": 1})
        self.assertIsNone(self.claim(now=utcnow() + timedelta(minutes=10)))

    def test_conflict_retry_drains_old_evidence_without_overwriting_newer_fact(self):
        mid = self.enqueue()
        job = require_value(self.claim())
        with self.factory.begin() as session:
            check_and_bump_revisions(
                session,
                self.user.user_id,
                ["preference.language"],
                {"preference.language": mid + 100},
                {},
            )
        fail_memory_job(self.factory, job.job_id)
        retry = require_value(self.claim(now=utcnow() + timedelta(minutes=2)))
        finish_memory_job(self.factory, retry, [self.candidate(mid)])
        with self.factory() as session:
            self.assertFalse(require_value(session.get(MemoryState, self.user.user_id)).dirty)
            self.assertIsNone(
                session.scalar(select(AgentMemory).where(AgentMemory.user_id == self.user.user_id))
            )

    def test_oversized_source_retained_with_persistent_diagnostic(self):
        mid = self.enqueue("中" * 9000)
        self.assertIsNone(self.claim())
        with self.factory() as session:
            state = require_value(session.get(MemoryState, self.user.user_id))
            self.assertTrue(state.paused)
            self.assertEqual(state.pending_ids, [mid])
            job = session.scalar(select(MemoryJob).where(MemoryJob.user_id == self.user.user_id))
            assert job is not None
            self.assertEqual(job.error_code, "MEMORY_SOURCE_TOO_LARGE")

    def test_execution_caches_one_graph_per_conversation_for_same_user(self):
        with self.factory.begin() as session:
            other_chat_id = str(self.make_chat(session, self.user.user_id).chat_session_id)

        async def run():
            provider = ChatAgentProvider()
            registry = GenerationRegistry()
            models = [
                MemoryModel(responses=[answer("A answer one"), answer("A answer two")]),
                MemoryModel(responses=[answer("B answer one")]),
            ]

            async def emit(event):
                pass

            user = UserResponse.model_validate(self.user)
            with (
                tracing_context(enabled=False),
                patch(
                    "app.agent.deepseek_protocol.ProtocolChatDeepSeek", side_effect=models
                ) as constructor,
            ):
                for chat_id, content in [
                    (str(self.chat.chat_session_id), "session-A-first"),
                    (other_chat_id, "session-B-first"),
                    (str(self.chat.chat_session_id), "session-A-second"),
                ]:
                    result = await execute_chat_turn(
                        user,
                        chat_id,
                        SendMessageRequest(content=content, client_message_key=str(uuid4())),
                        self.factory,
                        registry,
                        model_settings=model_settings(),
                        tracing_settings=TracingSettings.from_sources(
                            _env_file=None, tracing=False
                        ),
                        input_policy=policy_for_model("deepseek-v4-pro"),
                        check_new_message=lambda _: None,
                        on_event=emit,
                        agent_provider=provider,
                    )
                    from app.schemas import MessageDoneData

                    self.assertIsInstance(result, MessageDoneData)
                self.assertEqual(constructor.call_count, 2)
            self.assertEqual(len(models[0]._seen), 2)
            self.assertEqual(len(models[1]._seen), 1)
            self.assertIn("session-A-first", str(models[0]._seen[-1]))
            self.assertNotIn("session-B-first", str(models[0]._seen))
            self.assertNotIn("session-A-first", str(models[1]._seen))
            self.assertNotIn("A answer one", str(models[1]._seen))

        asyncio.run(run())

    def test_real_execution_persists_protocol_and_replays_it(self):
        async def run():
            fact = {
                "memory_key": "learning.goal",
                "summary": "热爱学习",
                "source_quote": "我热爱学习",
            }
            model = MemoryModel(
                responses=[
                    answer("Checking", tool="save_profile_facts", args={"facts": [fact]}),
                    answer("已记住"),
                    answer("follow up"),
                ]
            )
            registry = GenerationRegistry()
            provider = ChatAgentProvider()
            quota = GenerationQuota(GenerationQuotaSettings.from_sources(_env_file=None))
            events = []

            async def emit(event):
                events.append(event)

            user = UserResponse.model_validate(self.user)
            with (
                tracing_context(enabled=False),
                patch(
                    "app.agent.deepseek_protocol.ProtocolChatDeepSeek", return_value=model
                ) as constructor,
            ):
                for text in ("请记住我热爱学习", "what do you remember?"):
                    result = await execute_chat_turn(
                        user,
                        str(self.chat.chat_session_id),
                        SendMessageRequest(content=text, client_message_key=str(uuid4())),
                        self.factory,
                        registry,
                        model_settings=model_settings(),
                        tracing_settings=TracingSettings.from_sources(
                            _env_file=None, tracing=False
                        ),
                        input_policy=policy_for_model("deepseek-v4-pro"),
                        check_new_message=lambda _: None,
                        on_event=emit,
                        quota=quota,
                        agent_provider=provider,
                    )
                    from app.schemas import MessageDoneData

                    self.assertIsInstance(result, MessageDoneData)
                    assert isinstance(result, MessageDoneData)
                    from app.services.message_reasoning import get_message_reasoning

                    reasoning = get_message_reasoning(
                        user,
                        str(self.chat.chat_session_id),
                        result.assistant_message.message_id,
                        self.factory,
                    )
                    self.assertTrue(reasoning.available)
                    self.assertEqual(len(reasoning.steps), 2 if text.startswith("请") else 1)
                    self.assertTrue(all(s.text == "provider-reasoning" for s in reasoning.steps))
                    from app.services.errors import MessageNotFoundError

                    with self.factory.begin() as session:
                        other_user = UserResponse.model_validate(self.make_user(session))
                        other_chat = self.make_chat(session, self.user.user_id)
                        other_chat_id = str(other_chat.chat_session_id)
                    with self.assertRaises(MessageNotFoundError):
                        get_message_reasoning(
                            other_user,
                            str(self.chat.chat_session_id),
                            result.assistant_message.message_id,
                            self.factory,
                        )
                    with self.assertRaises(MessageNotFoundError):
                        get_message_reasoning(
                            user,
                            other_chat_id,
                            result.assistant_message.message_id,
                            self.factory,
                        )
            constructor.assert_called_once()
            self.assertEqual(len(model._seen), 3)
            self.assertNotIn("热爱学习", model._seen[0][0].text)
            self.assertIn("热爱学习", model._seen[-1][0].text)
            self.assertEqual(
                [e.step for e in events if e.event_name == "model_step" and e.phase == "start"],
                [1, 2, 1],
            )
            self.assertTrue(any(isinstance(m, ToolMessage) for m in model._seen[-1]))
            self.assertTrue(any(e.event_name == "model_step" and e.phase == "tool" for e in events))
            with self.factory() as session:
                records = session.scalars(
                    select(AgentTurnRecord).where(AgentTurnRecord.user_id == self.user.user_id)
                ).all()
                self.assertEqual(len(records), 2)
                self.assertEqual(len(records[0].messages), 3)
                self.assertIn("provider-reasoning", str(records[0].messages))
                saved = session.scalar(
                    select(AgentMemory).where(AgentMemory.user_id == self.user.user_id)
                )
                self.assertEqual(require_value(saved).summary, "热爱学习")
                self.assertEqual(
                    len(require_value(session.get(MemoryState, self.user.user_id)).pending_ids), 2
                )

        asyncio.run(run())
