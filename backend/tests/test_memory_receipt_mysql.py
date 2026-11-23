"""Opt-in transaction tests in an isolated disposable MySQL container only.

CHECKPOINT_TEST_MYSQL_PORT is the explicit opt-in. Credentials are fixed test-only
values; never reads .env, DB_* variables or an application database. Each run
creates and drops only its own randomly named checkpoint_receipt_* test schema.
"""

import os
import unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import URL, create_engine, event, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from test_chat_agent_factory import prepared

from app.db.base import Base
from app.models import (
    AgentMemory,
    ChatSession,
    GenerationControl,
    MemoryTopicRevision,
    MemoryWriteReceipt,
    Message,
    PublishedChatCheckpoint,
    User,
)
from app.services.errors import MemoryUnavailableError, MessageSendUnavailableError
from app.services.generation_registry import GenerationRegistry
from app.services.generation_result import settle_generation
from app.services.message_submission import AcceptedMessage
from app.services.profile_memory import ProfileFact, save_profile_facts
from testing_support import require_value


@unittest.skipUnless(os.environ.get("CHECKPOINT_TEST_MYSQL_PORT"), "isolated MySQL not enabled")
class MySQLMemoryReceiptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database_name = "checkpoint_receipt_" + uuid4().hex
        url = URL.create(
            "mysql+pymysql",
            username="root",
            password="checkpoint-test-only",
            host="127.0.0.1",
            port=int(os.environ["CHECKPOINT_TEST_MYSQL_PORT"]),
            database="checkpoint_test",
            query={"charset": "utf8mb4"},
        )
        cls.admin = create_engine(url, hide_parameters=True)
        with cls.admin.begin() as connection:
            connection.execute(text(f"CREATE DATABASE `{cls.database_name}`"))
        cls.engine = create_engine(url.set(database=cls.database_name), hide_parameters=True)
        cls.factory = sessionmaker(cls.engine, expire_on_commit=False)
        Base.metadata.create_all(
            cls.engine,
            tables=[
                Base.metadata.tables[model.__tablename__]
                for model in (
                    User,
                    ChatSession,
                    Message,
                    AgentMemory,
                    GenerationControl,
                    MemoryTopicRevision,
                    MemoryWriteReceipt,
                    PublishedChatCheckpoint,
                )
            ],
        )
        with cls.factory.begin() as session:
            session.add(GenerationControl(control_id=1))

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        assert cls.database_name.startswith("checkpoint_receipt_")
        assert len(cls.database_name.removeprefix("checkpoint_receipt_")) == 32
        with cls.admin.begin() as connection:
            connection.execute(text(f"DROP DATABASE `{cls.database_name}`"))
        cls.admin.dispose()

    def setUp(self):
        now = datetime(2026, 10, 25)
        attempt = str(uuid4())
        with self.factory.begin() as session:
            user = User(
                email=f"{uuid4().hex}@example.test",
                password_hash="test-not-an-authentication-secret",
                display_name="Test learner",
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            session.flush()
            chat = ChatSession(
                user_id=user.user_id,
                title="Test",
                title_is_manual=False,
                last_activity_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(chat)
            session.flush()
            question = Message(
                chat_session_id=chat.chat_session_id,
                role="USER",
                content="Please remember I prefer English.",
                client_message_key=str(uuid4()),
                attempt_id=attempt,
                generation_status="RUNNING",
                created_at=now,
                updated_at=now,
            )
            session.add(question)
            session.flush()
            session.add(
                PublishedChatCheckpoint(
                    chat_session_id=chat.chat_session_id,
                    user_id=user.user_id,
                    checkpoint_id="published-parent",
                )
            )
            self.uid, self.sid, self.qid = user.user_id, chat.chat_session_id, question.message_id
        snapshot = replace(prepared(user_id=str(self.uid)), session_id=str(self.sid))
        self.accepted = AcceptedMessage(str(self.sid), str(self.qid), attempt, snapshot)
        self.registry = GenerationRegistry()
        with self.registry.locked(self.sid) as slot:
            slot.claim(attempt)
        self.facts = [ProfileFact(memory_key="preference.language", summary="English")]

    def save(self, *, factory=None, facts=None):
        return save_profile_facts(
            self.accepted,
            facts or self.facts,
            factory or self.factory,
            self.registry,
            expected_revisions={},
        )

    def ambiguous_commit_factory(self):
        @contextmanager
        def begin():
            with self.factory.begin() as session:
                yield session
            raise SQLAlchemyError("simulated lost commit acknowledgement")

        return cast(sessionmaker[Session], SimpleNamespace(begin=begin))

    def retry_attempt(self):
        old = self.accepted.attempt_id
        new = str(uuid4())
        with self.factory.begin() as session:
            question = require_value(session.get(Message, self.qid))
            question.attempt_id, question.retry_of_attempt_id = new, old
        with self.registry.locked(self.sid) as slot:
            slot.release(old)
            slot.claim(new)
        self.accepted = replace(self.accepted, attempt_id=new)

    def test_retry_reads_receipt_not_rewriting_newer_profile_or_revision(self):
        result = self.save()
        self.assertFalse(result.replayed)
        with self.factory.begin() as session:
            memory = require_value(
                session.scalar(select(AgentMemory).where(AgentMemory.user_id == self.uid))
            )
            memory.summary = "Chinese"
        self.retry_attempt()
        result = self.save(
            facts=[ProfileFact(memory_key="preference.language", summary="Unapplied retry")]
        )
        self.assertTrue(result.replayed)
        self.assertEqual(result.saved_facts, {"preference.language": "English"})
        self.assertEqual(result.current_facts, {"preference.language": "Chinese"})
        with self.factory() as session:
            receipt = require_value(session.get(MemoryWriteReceipt, self.qid))
            self.assertEqual(receipt.facts, [self.facts[0].model_dump()])
            revision = require_value(
                session.get(MemoryTopicRevision, (self.uid, "preference.language"))
            )
            self.assertEqual(revision.revision, 1)

    def test_receipt_insert_failure_rolls_back_profile_and_revision(self):
        def fail_receipt(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith("INSERT INTO memory_write_receipts"):
                raise SQLAlchemyError("simulated receipt insert failure")

        event.listen(self.engine, "before_cursor_execute", fail_receipt)
        try:
            with self.assertRaises(MemoryUnavailableError):
                self.save()
        finally:
            event.remove(self.engine, "before_cursor_execute", fail_receipt)
        with self.factory() as session:
            self.assertIsNone(session.get(MemoryWriteReceipt, self.qid))
            self.assertIsNone(session.get(MemoryTopicRevision, (self.uid, "preference.language")))
            self.assertIsNone(
                session.scalar(select(AgentMemory).where(AgentMemory.user_id == self.uid))
            )

    def test_commit_ack_loss_is_confirmed_by_receipt_without_second_write(self):
        with self.assertRaises(MemoryUnavailableError):
            self.save(factory=self.ambiguous_commit_factory())
        self.retry_attempt()
        with patch("app.services.profile_memory.save_profile_files") as writer:
            result = self.save()
        writer.assert_not_called()
        self.assertTrue(result.replayed)

    def test_answer_failure_rolls_back_checkpoint_publication(self):
        def fail_answer(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith("INSERT INTO messages"):
                raise SQLAlchemyError("simulated answer insert failure")

        event.listen(self.engine, "before_cursor_execute", fail_answer)
        try:
            with self.assertRaises(MessageSendUnavailableError):
                settle_generation(
                    self.accepted,
                    self.factory,
                    self.registry,
                    answer="Final answer",
                    checkpoint_id="new-terminal-checkpoint",
                    checkpoint_parent_id="published-parent",
                )
        finally:
            event.remove(self.engine, "before_cursor_execute", fail_answer)
        with self.factory() as session:
            self.assertEqual(
                require_value(session.get(PublishedChatCheckpoint, self.sid)).checkpoint_id,
                "published-parent",
            )
            self.assertEqual(
                require_value(session.get(Message, self.qid)).generation_status, "RUNNING"
            )
            self.assertIsNone(
                session.scalar(select(Message).where(Message.in_reply_to_message_id == self.qid))
            )

    def test_answer_commit_ack_loss_preserves_published_success(self):
        with self.assertRaises(MessageSendUnavailableError):
            settle_generation(
                self.accepted,
                self.ambiguous_commit_factory(),
                self.registry,
                answer="Final answer",
                checkpoint_id="new-terminal-checkpoint",
                checkpoint_parent_id="published-parent",
            )
        outcome = settle_generation(
            self.accepted, self.factory, self.registry, error_code="MODEL_REQUEST_FAILED"
        )
        self.assertEqual(require_value(outcome.assistant).content, "Final answer")
        with self.factory() as session:
            head = require_value(session.get(PublishedChatCheckpoint, self.sid))
            self.assertEqual(head.checkpoint_id, "new-terminal-checkpoint")
            self.assertEqual(
                require_value(session.get(Message, self.qid)).generation_status, "SUCCEEDED"
            )
