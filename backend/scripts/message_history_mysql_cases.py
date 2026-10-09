"""历史读取的隔离 MySQL 验收；模拟写入消息，不调用 Agent。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event
import unittest
from uuid import uuid4

from sqlalchemy import select

from app.db.session import build_session_factory
from app.models import AgentMemory, AuthSession, ChatSession, Message, User
from app.schemas import LoginRequest, RegisterRequest
from app.services.auth import register_user
from app.services.authentication import get_current_user
from app.services.chat_sessions import create_chat_session
from app.services.errors import MessageHistoryUnavailableError, SessionNotFoundError
from app.services.generation_registry import GenerationRegistry
from app.services.login import login_user
from app.services.message_history import get_message_history


class MessageHistoryMySQLTest(unittest.TestCase):
    engine = None

    def setUp(self):
        self.factory = build_session_factory(self.engine)
        self.registry = GenerationRegistry()
        self.user = self.new_user()
        self.chat = create_chat_session(self.user, self.factory)
        self.now = datetime.now(UTC).replace(tzinfo=None)

    def new_user(self):
        email = f"history-{uuid4().hex}@example.com"
        register_user(RegisterRequest(email=email, password="test-password", display_name="History tester"), self.factory)
        login = login_user(LoginRequest(email=email, password="test-password"), self.factory)
        return get_current_user(login.token, self.factory)

    def question(self, session, status="FAILED", chat_id=None):
        message = Message(chat_session_id=int(chat_id or self.chat.session_id), role="USER", content="  Question\n    code🙂",
                          client_message_key=str(uuid4()), attempt_id=str(uuid4()), generation_status=status,
                          generation_error_code="GENERATION_TIMEOUT" if status == "FAILED" else None,
                          generation_error_message="private-api-key-url" if status == "FAILED" else None,
                          created_at=self.now, updated_at=self.now)
        session.add(message)
        session.flush()
        return message

    def answer(self, session, question, chat_id=None):
        answer = Message(chat_session_id=int(chat_id or self.chat.session_id), role="ASSISTANT", content="Answer🙂",
                         in_reply_to_message_id=question.message_id, model_key="private-model",
                         created_at=self.now, updated_at=self.now)
        session.add(answer)
        session.flush()
        return answer

    def read(self):
        return get_message_history(self.user, self.chat.session_id, self.factory, self.registry)

    def test_empty_and_other_users_or_missing_session_are_isolated(self):
        self.assertEqual(self.read().model_dump(), {"session_id": self.chat.session_id, "is_generating": False, "items": []})
        other = self.new_user()
        other_chat = create_chat_session(other, self.factory)
        with self.factory.begin() as session:
            self.question(session, chat_id=other_chat.session_id)
        failures = []
        for chat_id in (other_chat.session_id, "18446744073709551615"):
            with self.assertRaises(SessionNotFoundError) as caught:
                get_message_history(self.user, chat_id, self.factory, self.registry)
            failures.append(str(caught.exception))
        self.assertEqual(failures[0], failures[1])
        self.assertEqual(self.read().items, [])

    def test_old_failure_then_success_stays_failed_and_latest_failure_can_retry(self):
        with self.factory.begin() as session:
            first = self.question(session)
            second = self.question(session, "SUCCEEDED")
            reply = self.answer(session, second)
        result = self.read()
        self.assertEqual([int(item.message_id) for item in result.items], [first.message_id, second.message_id, reply.message_id])
        self.assertEqual(result.items[0].generation.status, "FAILED")
        self.assertFalse(result.items[0].generation.can_retry)
        self.assertEqual(result.items[1].generation.assistant_message_id, str(reply.message_id))
        self.assertNotIn("generation", result.items[2].model_dump())
        with self.factory.begin() as session:
            last = self.question(session)
        result = self.read()
        self.assertEqual(result.items[-1].message_id, str(last.message_id))
        self.assertTrue(result.items[-1].generation.can_retry)
        self.assertEqual(result.items[0].content, "  Question\n    code🙂")

    def test_running_and_cleanup_stay_busy_then_release_enables_retry(self):
        with self.registry.locked(int(self.chat.session_id)) as slot:
            with self.factory.begin() as session:
                q = self.question(session, "RUNNING")
            slot.claim(q.attempt_id)
        self.assertTrue(self.read().is_generating)
        with self.registry.locked(int(self.chat.session_id)):
            with self.factory.begin() as session:
                row = session.get(Message, q.message_id)
                row.generation_status = "FAILED"
                row.generation_error_code = "GENERATION_INTERRUPTED"
                row.generation_error_message = "private-tool-error"
        result = self.read()
        self.assertTrue(result.is_generating)
        self.assertFalse(result.items[0].generation.can_retry)
        with self.registry.locked(int(self.chat.session_id)) as slot:
            slot.release(q.attempt_id)
        self.assertTrue(self.read().items[0].generation.can_retry)

    def test_orphan_running_is_rejected_without_repairing_database(self):
        with self.factory.begin() as session:
            q = self.question(session, "RUNNING")
        with self.assertRaises(MessageHistoryUnavailableError):
            self.read()
        with self.factory() as session:
            self.assertEqual(session.get(Message, q.message_id).generation_status, "RUNNING")

    def test_cross_session_reply_is_rejected_not_loaded_from_other_session(self):
        other_chat = create_chat_session(self.user, self.factory)
        with self.factory.begin() as session:
            q = self.question(session, "SUCCEEDED", chat_id=other_chat.session_id)
            self.answer(session, q)
        # 单列外键不阻止跨会话引用，业务读取必须拒绝这种不一致，而不是查别的会话补齐。
        with self.assertRaises(MessageHistoryUnavailableError):
            self.read()
        with self.assertRaises(MessageHistoryUnavailableError):
            get_message_history(self.user, other_chat.session_id, self.factory, self.registry)

    def test_unknown_error_and_internal_fields_never_escape(self):
        with self.factory.begin() as session:
            q = self.question(session)
            q.generation_error_code = "private-provider-token"
        result = self.read()
        self.assertEqual(result.items[0].generation.error.code, "GENERATION_FAILED")
        payload = result.model_dump_json()
        for hidden in ("private", "client_message_key", "model_key", "generation_error_message", "retry_of_attempt_id"):
            self.assertNotIn(hidden, payload)

    def test_all_messages_read_without_pagination_or_data_changes(self):
        with self.factory.begin() as session:
            for _ in range(105):
                self.question(session)
        def snapshot():
            with self.factory() as session:
                return [session.execute(select(model.__table__).order_by(*model.__table__.primary_key.columns)).all()
                        for model in (User, AuthSession, ChatSession, Message, AgentMemory)]
        before = snapshot()
        result = self.read()
        self.assertEqual(len(result.items), 105)
        ids = [int(item.message_id) for item in result.items]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(sum(item.generation.can_retry for item in result.items), 1)
        self.assertEqual(snapshot(), before)

    def test_history_waits_for_atomic_final_save_and_reads_consistent_snapshot(self):
        chat_id = int(self.chat.session_id)
        with self.registry.locked(chat_id) as slot:
            with self.factory.begin() as session:
                q = self.question(session, "RUNNING")
            slot.claim(q.attempt_id)
        started = Event()
        def read():
            started.set()
            return self.read()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.registry.locked(chat_id) as slot:
                future = pool.submit(read)
                self.assertTrue(started.wait(timeout=5))
                self.assertFalse(future.done())
                with self.factory.begin() as session:
                    question = session.get(Message, q.message_id)
                    question.generation_status = "SUCCEEDED"
                    reply = self.answer(session, question)
                slot.release(q.attempt_id)
            result = future.result(timeout=10)
        self.assertFalse(result.is_generating)
        self.assertEqual(result.items[0].generation.assistant_message_id, str(reply.message_id))
        self.assertEqual(len(result.items), 2)
