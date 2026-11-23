"""Opt-in full chat execution against a newly-created disposable MySQL schema.

Enable only with CHECKPOINT_TEST_MYSQL_PORT pointing at the dedicated test
container. Credentials are test fixtures; never read .env or application DB_*.
No model API/tracing calls are made. The unique test database is removed after.
"""

import os
import unittest
from datetime import UTC, datetime
from unittest.mock import patch
from uuid import uuid4

from alembic.config import Config
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langsmith import tracing_context
from pydantic import PrivateAttr
from sqlalchemy import URL, create_engine, event, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from test_chat_agent_factory import settings
from test_memory_orchestrator import MemoryModel, answer

from alembic import command
from app.agent.conversation_context import SUMMARY_INSTRUCTIONS
from app.agent.factory import SessionChatGraphCache
from app.agent.input_policy import policy_for_model
from app.core.config import TracingSettings
from app.db.checkpoints import open_checkpoint_saver, setup_checkpoint_schema
from app.models import ChatSession, Message, User
from app.models.chat_thread import PublishedChatCheckpoint
from app.schemas import (
    MessageDoneData,
    MessageErrorData,
    RetryMessageRequest,
    SendMessageRequest,
    UserResponse,
)
from app.services.chat_execution import execute_chat_retry, execute_chat_turn
from app.services.generation_registry import GenerationRegistry
from testing_support import require_value


class ExecutionModel(MemoryModel):
    _fail_next: bool = PrivateAttr(default=False)

    def _generate(self, messages, *args, **kwargs):
        if self._fail_next:
            self._fail_next = False
            raise RuntimeError("simulated model failure, no network")
        return super()._generate(messages, *args, **kwargs)


@unittest.skipUnless(os.environ.get("CHECKPOINT_TEST_MYSQL_PORT"), "isolated MySQL not enabled")
class CheckpointExecutionMySQLTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        url = URL.create(
            "mysql+pymysql",
            username="root",
            password="checkpoint-test-only",
            host="127.0.0.1",
            port=int(os.environ["CHECKPOINT_TEST_MYSQL_PORT"]),
            database="checkpoint_test",
            query={"charset": "utf8mb4"},
        )
        cls.database = "checkpoint_execution_test_" + uuid4().hex
        cls.admin = create_engine(url, hide_parameters=True)
        with cls.admin.begin() as connection:
            connection.execute(text(f"CREATE DATABASE `{cls.database}`"))
        cls.engine = create_engine(url.set(database=cls.database), hide_parameters=True)
        cls.factory = sessionmaker(cls.engine, expire_on_commit=False)
        with (
            patch("app.core.config.load_database_settings", return_value=object()),
            patch("app.db.engine.build_database_engine", return_value=cls.engine),
        ):
            command.upgrade(Config("alembic.ini"), "head")
        setup_checkpoint_schema(cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        with cls.admin.begin() as connection:
            connection.execute(text(f"DROP DATABASE `{cls.database}`"))
        cls.admin.dispose()

    def setUp(self):
        now = datetime.now(UTC).replace(tzinfo=None)
        with self.factory.begin() as session:
            user = User(
                email=f"{uuid4().hex}@example.com",
                password_hash="not-a-real-hash",
                display_name="checkpoint-test",
                status="ACTIVE",
                system_role="USER",
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            session.flush()
            chat = ChatSession(
                user_id=user.user_id,
                title="test",
                title_is_manual=False,
                last_activity_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(chat)
            session.flush()
            self.user = UserResponse.model_validate(user)
            self.session_id = str(chat.chat_session_id)
        self.registry = GenerationRegistry()
        self.policy = policy_for_model(settings().name)
        self.events = []
        self.model = ExecutionModel(responses=[answer("complete teaching answer")])
        self.patch_model = patch(
            "app.agent.deepseek_protocol.ReasoningReplayChatDeepSeek", return_value=self.model
        )
        self.patch_model.start()
        self.addCleanup(self.patch_model.stop)
        trace = tracing_context(enabled=False)
        trace.__enter__()
        self.addCleanup(trace.__exit__, None, None, None)

    async def emit(self, item):
        self.events.append(item)

    async def send(self, provider, content, *, factory=None):
        return await execute_chat_turn(
            self.user,
            self.session_id,
            SendMessageRequest(client_message_key=str(uuid4()), content=content),
            factory or self.factory,
            self.registry,
            model_settings=settings(),
            tracing_settings=TracingSettings.from_sources(_env_file=None, tracing=False),
            input_policy=self.policy,
            check_new_message=lambda _: None,
            on_event=self.emit,
            session_graph_cache=provider,
        )

    def head(self):
        with self.factory() as session:
            head = require_value(session.get(PublishedChatCheckpoint, int(self.session_id)))
            return head.checkpoint_id

    def config(self) -> RunnableConfig:
        return {
            "configurable": {
                "thread_id": f"teaching:v1:user:{self.user.user_id}:session:{self.session_id}",
                "checkpoint_id": self.head(),
            }
        }

    async def test_reopen_preserves_successful_context_and_excludes_failed_branch(self):
        async with open_checkpoint_saver(self.engine) as saver:
            provider = SessionChatGraphCache(saver)
            self.assertIsInstance(
                await self.send(provider, "first accepted question"), MessageDoneData
            )
            self.assertIsInstance(
                await self.send(provider, "second accepted question"), MessageDoneData
            )
            head = self.head()
            self.model._fail_next = True
            self.assertIsInstance(
                await self.send(provider, "FAILED-BRANCH-MUST-NOT-LEAK"), MessageErrorData
            )
            self.assertEqual(self.head(), head)

        async with open_checkpoint_saver(self.engine) as saver:
            provider = SessionChatGraphCache(saver)
            self.assertIsInstance(
                await self.send(provider, "third accepted question"), MessageDoneData
            )
            seen = "\n".join(message.text for message in self.model._seen[-1])
            self.assertIn("first accepted question", seen)
            self.assertIn("second accepted question", seen)
            self.assertEqual(seen.count("third accepted question"), 1)
            self.assertNotIn("FAILED-BRANCH-MUST-NOT-LEAK", seen)
            graph = provider.get(
                settings(), self.policy, user_id=self.user.user_id, session_id=self.session_id
            )
            state = await graph.aget_state(self.config())
            self.assertEqual(
                sum(isinstance(message, HumanMessage) for message in state.values["messages"]), 3
            )

    async def test_failed_question_retry_appends_once(self):
        async with open_checkpoint_saver(self.engine) as saver:
            provider = SessionChatGraphCache(saver)
            self.model._fail_next = True
            failed = await self.send(provider, "retry this exact question")
            self.assertIsInstance(failed, MessageErrorData)
            with self.factory() as session:
                question = session.scalar(
                    select(Message).where(
                        Message.chat_session_id == int(self.session_id), Message.role == "USER"
                    )
                )
                question = require_value(question)
                message_id = str(question.message_id)
                attempt_id = require_value(question.attempt_id)
            result = await execute_chat_retry(
                self.user,
                self.session_id,
                message_id,
                RetryMessageRequest(failed_attempt_id=attempt_id),
                self.factory,
                self.registry,
                model_settings=settings(),
                tracing_settings=TracingSettings.from_sources(_env_file=None, tracing=False),
                input_policy=self.policy,
                check_retry=lambda _: None,
                on_event=self.emit,
                session_graph_cache=provider,
            )
            self.assertIsInstance(result, MessageDoneData)
            seen = "\n".join(message.text for message in self.model._seen[-1])
            self.assertEqual(seen.count("retry this exact question"), 1)
            with self.factory() as session:
                questions = list(
                    session.scalars(
                        select(Message).where(
                            Message.chat_session_id == int(self.session_id), Message.role == "USER"
                        )
                    )
                )
                self.assertEqual(len(questions), 1)
                self.assertEqual(questions[0].generation_status, "SUCCEEDED")

    async def test_checkpoint_write_failure_keeps_published_history(self):
        async with open_checkpoint_saver(self.engine) as saver:
            provider = SessionChatGraphCache(saver)
            self.assertIsInstance(await self.send(provider, "confirmed baseline"), MessageDoneData)
            published = self.head()
            with patch.object(saver, "aput", side_effect=RuntimeError("simulated storage failure")):
                failed = await self.send(provider, "UNPERSISTED-QUESTION")
            self.assertIsInstance(failed, MessageErrorData)
            self.assertEqual(self.head(), published)
            with self.factory() as session:
                question = require_value(
                    session.scalar(
                        select(Message).where(
                            Message.chat_session_id == int(self.session_id),
                            Message.content == "UNPERSISTED-QUESTION",
                        )
                    )
                )
                self.assertEqual(question.generation_status, "FAILED")
                self.assertIsNone(
                    session.scalar(
                        select(Message).where(Message.in_reply_to_message_id == question.message_id)
                    )
                )
            self.assertIsInstance(await self.send(provider, "continue safely"), MessageDoneData)
            seen = "\n".join(message.text for message in self.model._seen[-1])
            self.assertIn("confirmed baseline", seen)
            self.assertIn("continue safely", seen)
            self.assertNotIn("UNPERSISTED-QUESTION", seen)

    async def test_legacy_import_summarizes_archives_and_survives_mysql_reopen(self):
        now = datetime.now(UTC).replace(tzinfo=None)
        with self.factory.begin() as session:
            for index in range(10):
                question = Message(
                    chat_session_id=int(self.session_id),
                    role="USER",
                    content=f"legacy question {index}: " + "x" * 4000,
                    client_message_key=str(uuid4()),
                    attempt_id=str(uuid4()),
                    generation_status="SUCCEEDED",
                    created_at=now,
                    updated_at=now,
                )
                session.add(question)
                session.flush()
                session.add(
                    Message(
                        chat_session_id=int(self.session_id),
                        role="ASSISTANT",
                        content="y" * 4000,
                        in_reply_to_message_id=question.message_id,
                        created_at=now,
                        updated_at=now,
                    )
                )
        async with open_checkpoint_saver(self.engine) as saver:
            provider = SessionChatGraphCache(saver)
            self.assertIsInstance(
                await self.send(provider, "continue from previous lessons"), MessageDoneData
            )
            graph = provider.get(
                settings(), self.policy, user_id=self.user.user_id, session_id=self.session_id
            )
            state = await graph.aget_state(self.config())
            summary = state.values["_summarization_event"]
            archive = summary["file_path"]
            self.assertTrue(archive.startswith("/conversation_history/"))
            self.assertIn(archive, state.values["files"])
            self.assertTrue(
                any(
                    messages[0].text == SUMMARY_INSTRUCTIONS
                    for messages in self.model._summary_seen
                )
            )
            self.assertNotIn(
                SUMMARY_INSTRUCTIONS, "\n".join(getattr(e, "text", "") for e in self.events)
            )
            self.assertNotIn(
                "Summary: teaching discussion.",
                "\n".join(getattr(e, "text", "") for e in self.events),
            )
        async with open_checkpoint_saver(self.engine) as saver:
            provider = SessionChatGraphCache(saver)
            self.assertIsInstance(await self.send(provider, "one more lesson"), MessageDoneData)
            graph = provider.get(
                settings(), self.policy, user_id=self.user.user_id, session_id=self.session_id
            )
            state = await graph.aget_state(self.config())
            self.assertIn(archive, state.values["files"])
            self.assertNotIn(
                "legacy question 0:", "\n".join(message.text for message in self.model._seen[-1])
            )

    async def test_answer_transaction_failure_does_not_publish_graph_branch(self):
        class FaultSession(Session):
            pass

        fail = [True]

        def before_flush(session, *_):
            if fail[0] and any(
                isinstance(row, Message) and row.role == "ASSISTANT" for row in session.new
            ):
                fail[0] = False
                raise SQLAlchemyError("simulated answer transaction rollback")

        event.listen(FaultSession, "before_flush", before_flush)
        factory = sessionmaker(self.engine, class_=FaultSession, expire_on_commit=False)
        async with open_checkpoint_saver(self.engine) as saver:
            provider = SessionChatGraphCache(saver)
            self.assertIsInstance(await self.send(provider, "baseline success"), MessageDoneData)
            baseline = self.head()
            result = await self.send(provider, "ANSWER-COMMIT-MUST-ROLLBACK", factory=factory)
            self.assertIsInstance(result, MessageErrorData)
            self.assertEqual(self.head(), baseline)
            self.assertIsInstance(
                await self.send(provider, "after failed answer commit"), MessageDoneData
            )
            self.assertNotIn(
                "ANSWER-COMMIT-MUST-ROLLBACK",
                "\n".join(message.text for message in self.model._seen[-1]),
            )

    async def test_lost_commit_ack_recovers_saved_answer_and_published_checkpoint(self):
        class AckLossSession(Session):
            pass

        fail = [True]

        def before_flush(session, *_):
            if fail[0] and any(
                isinstance(row, Message) and row.role == "ASSISTANT" for row in session.new
            ):
                session.info["simulate_lost_answer_ack"] = True

        def after_commit(session):
            if fail[0] and session.info.get("simulate_lost_answer_ack"):
                fail[0] = False
                raise SQLAlchemyError("simulated lost acknowledgement after durable commit")

        event.listen(AckLossSession, "before_flush", before_flush)
        event.listen(AckLossSession, "after_commit", after_commit)
        factory = sessionmaker(self.engine, class_=AckLossSession, expire_on_commit=False)
        async with open_checkpoint_saver(self.engine) as saver:
            provider = SessionChatGraphCache(saver)
            self.assertIsInstance(
                await self.send(provider, "baseline before ack loss"), MessageDoneData
            )
            baseline = self.head()
            result = await self.send(provider, "durable answer with lost ack", factory=factory)
            self.assertIsInstance(result, MessageDoneData)
            self.assertNotEqual(self.head(), baseline)
            self.assertIsInstance(
                await self.send(provider, "continue after ack recovery"), MessageDoneData
            )
            seen = "\n".join(message.text for message in self.model._seen[-1])
            self.assertEqual(seen.count("durable answer with lost ack"), 1)
            with self.factory() as session:
                questions = list(
                    session.scalars(
                        select(Message).where(
                            Message.chat_session_id == int(self.session_id),
                            Message.role == "USER",
                            Message.content == "durable answer with lost ack",
                        )
                    )
                )
                self.assertEqual(len(questions), 1)
                self.assertEqual(questions[0].generation_status, "SUCCEEDED")
