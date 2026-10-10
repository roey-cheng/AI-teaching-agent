"""启动清理真实 MySQL 验收；由临时容器脚本注入 Engine，不调用项目数据库或模型。"""

from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete, event, select
from sqlalchemy.exc import SQLAlchemyError

from app.agent.input_policy import policy_for_model
from app.core.runtime import open_backend_runtime
from app.core.runtime_lock import RuntimeAlreadyActiveError, RuntimeLock
from app.core.http_config import HTTPSettings
from app.db.session import build_session_factory
from app.main import create_app
from app.models import AgentMemory, AuthSession, ChatSession, Message, User
from app.schemas import RegisterRequest, RetryMessageRequest, SendMessageRequest
from app.services.auth import register_user
from app.services.chat_sessions import create_chat_session
from app.services.errors import StartupCleanupError
from app.services.generation_registry import GenerationRegistry
from app.services.generation_result import settle_generation
from app.services.message_history import get_message_history
from app.services.message_retry import accept_failed_message_retry
from app.services.message_submission import accept_user_message
from app.services.startup_cleanup import StartupCleanupResult, reconcile_interrupted_generations


class StartupCleanupMySQLTest(unittest.TestCase):
    engine = None

    def setUp(self):
        # 只接受验收脚本的明确临时目标。此组测试在其他业务用例之前运行。
        if self.engine.url.database != "migration_test" or self.engine.url.port == 3306:
            raise RuntimeError("Startup tests require the isolated migration database")
        self.factory = build_session_factory(self.engine)
        self.user = register_user(RegisterRequest(email=f"startup-{uuid4().hex}@example.com",
                                                  password="test-password", display_name="Startup test"), self.factory)
        self.chat = create_chat_session(self.user, self.factory)
        self.other_chat = create_chat_session(self.user, self.factory)
        self.registry = GenerationRegistry()
        self.policy = policy_for_model("deepseek-v4-pro")
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.lock_path = Path(folder.name) / "chat.lock"
        self.lock = RuntimeLock(self.lock_path)

    def tearDown(self):
        # 仅删除本测试创建的两段对话及账号，避免刻意构造的坏数据污染其他验收。
        chat_ids = [int(self.chat.session_id), int(self.other_chat.session_id)]
        with self.factory.begin() as session:
            session.execute(delete(Message).where(Message.chat_session_id.in_(chat_ids), Message.role == "ASSISTANT"))
            session.execute(delete(Message).where(Message.chat_session_id.in_(chat_ids)))
            session.execute(delete(ChatSession).where(ChatSession.chat_session_id.in_(chat_ids)))
            session.execute(delete(AgentMemory).where(AgentMemory.user_id == int(self.user.user_id)))
            session.execute(delete(AuthSession).where(AuthSession.user_id == int(self.user.user_id)))
            session.execute(delete(User).where(User.user_id == int(self.user.user_id)))

    def question(self, *, chat=None, status="RUNNING"):
        now = datetime.now(UTC).replace(tzinfo=None)
        with self.factory.begin() as session:
            q = Message(chat_session_id=int((chat or self.chat).session_id), role="USER", content="Saved question",
                        client_message_key=str(uuid4()), attempt_id=str(uuid4()), retry_of_attempt_id=str(uuid4()),
                        generation_status=status, created_at=now, updated_at=now)
            if status == "FAILED":
                q.generation_error_code = "MODEL_REQUEST_FAILED"
                q.generation_error_message = "Previous failure"
            session.add(q)
            session.flush()
        return q

    def answer(self, question, *, chat_id=None, content="Saved final answer"):
        now = datetime.now(UTC).replace(tzinfo=None)
        with self.factory.begin() as session:
            a = Message(chat_session_id=chat_id or question.chat_session_id, role="ASSISTANT", content=content,
                        in_reply_to_message_id=question.message_id, model_key="test-model",
                        created_at=now, updated_at=now)
            session.add(a)
            session.flush()
        return a

    def cleanup(self):
        with self.lock:
            return reconcile_interrupted_generations(self.factory, self.registry, runtime_lock=self.lock)

    def messages(self):
        with self.factory() as session:
            return session.execute(select(Message.__table__).where(Message.chat_session_id.in_(
                [int(self.chat.session_id), int(self.other_chat.session_id)],
            )).order_by(Message.message_id)).all()

    def side_data(self):
        with self.factory() as session:
            return {model.__tablename__: session.execute(select(model.__table__)).all()
                    for model in (User, AuthSession, ChatSession, AgentMemory)}

    def test_interrupts_all_abandoned_questions_preserves_identity_and_other_data(self):
        first = self.question()
        self.question(chat=self.other_chat)
        now = datetime.now(UTC).replace(tzinfo=None)
        with self.factory.begin() as session:
            session.add(AgentMemory(user_id=int(self.user.user_id), memory_key="preference.language",
                                    memory_type="LEARNING_PREFERENCE", summary="中文", created_at=now, updated_at=now))
        before, side_before = self.messages(), self.side_data()
        self.assertEqual(self.cleanup(), StartupCleanupResult(interrupted=2))
        after = self.messages()
        for old, new in zip(before, after, strict=True):
            for field in ("message_id", "content", "created_at", "client_message_key", "attempt_id", "retry_of_attempt_id"):
                self.assertEqual(getattr(old, field), getattr(new, field))
            self.assertEqual(new.generation_status, "FAILED")
            self.assertEqual(new.generation_error_code, "GENERATION_INTERRUPTED")
        self.assertEqual(self.side_data(), side_before)
        history = get_message_history(self.user, self.chat.session_id, self.factory, self.registry)
        self.assertFalse(history.is_generating)
        self.assertTrue(history.items[0].generation.can_retry)
        self.assertEqual(history.items[0].generation.attempt_id, first.attempt_id)
        # 重启清理之后，仍能用同一失败编号进入原消息重试。
        with accept_failed_message_retry(
            self.user, self.chat.session_id, str(first.message_id), RetryMessageRequest(failed_attempt_id=first.attempt_id),
            self.factory, self.registry, check_retry=lambda _: None, input_policy=self.policy,
        ) as accepted:
            settle_generation(accepted, self.factory, self.registry, answer="Retry after restart")
        self.assertEqual(get_message_history(self.user, self.chat.session_id, self.factory, self.registry)
                         .items[0].generation.status, "SUCCEEDED")

    def test_saved_answer_restored_and_second_start_is_noop(self):
        q = self.question()
        self.answer(q)
        answer_before = self.messages()[1]
        self.assertEqual(self.cleanup(), StartupCleanupResult(restored_success=1))
        self.assertEqual(self.messages()[1], answer_before)
        first = self.messages(), self.side_data()
        self.assertEqual(self.cleanup(), StartupCleanupResult())
        self.assertEqual((self.messages(), self.side_data()), first)
        history = get_message_history(self.user, self.chat.session_id, self.factory, self.registry)
        self.assertEqual(history.items[0].generation.status, "SUCCEEDED")
        self.assertFalse(history.items[0].generation.can_retry)

    def test_existing_final_statuses_are_not_touched(self):
        self.question(status="FAILED")
        good = self.question(status="SUCCEEDED")
        self.answer(good)
        before = self.messages(), self.side_data()
        self.assertEqual(self.cleanup(), StartupCleanupResult())
        self.assertEqual((self.messages(), self.side_data()), before)

    def test_cross_session_answer_stops_startup_and_rolls_back_all_repairs(self):
        self.question()  # 先修改这一行，再遇到坏关联；整笔事务必须回滚。
        invalid = self.question(chat=self.other_chat)
        self.answer(invalid, chat_id=int(self.chat.session_id))
        before = self.messages(), self.side_data()
        with self.assertRaises(StartupCleanupError):
            self.cleanup()
        self.assertEqual((self.messages(), self.side_data()), before)

    def test_blank_saved_answer_is_not_reported_as_success(self):
        self.answer(self.question(), content=" \n ")
        before = self.messages()
        with self.assertRaises(StartupCleanupError):
            self.cleanup()
        self.assertEqual(self.messages(), before)

    def test_precommit_failure_rolls_back_and_next_start_can_reconcile(self):
        self.question()
        before = self.messages()

        def fail(session):
            raise SQLAlchemyError("private-before-commit")

        event.listen(self.factory, "before_commit", fail)
        try:
            with self.assertRaises(StartupCleanupError) as caught:
                self.cleanup()
            self.assertNotIn("private", str(caught.exception))
        finally:
            event.remove(self.factory, "before_commit", fail)
        self.assertEqual(self.messages(), before)
        self.assertEqual(self.cleanup(), StartupCleanupResult(interrupted=1))

    def test_postcommit_ack_failure_blocks_startup_but_next_start_is_safe(self):
        self.question()

        def fail(session):
            raise SQLAlchemyError("private-after-commit")

        event.listen(self.factory, "after_commit", fail)
        try:
            with self.assertRaises(StartupCleanupError):
                self.cleanup()
        finally:
            event.remove(self.factory, "after_commit", fail)
        self.assertEqual(self.messages()[0].generation_status, "FAILED")
        before = self.messages()
        self.assertEqual(self.cleanup(), StartupCleanupResult())
        self.assertEqual(self.messages(), before)

    def test_live_registry_generation_cannot_be_cleaned(self):
        with accept_user_message(
            self.user, self.chat.session_id, SendMessageRequest(client_message_key=str(uuid4()), content="Live question"),
            self.factory, self.registry, check_new_message=lambda _: None, input_policy=self.policy,
        ):
            before = self.messages()
            with self.assertRaises(StartupCleanupError):
                self.cleanup()
            self.assertEqual(self.messages(), before)
            self.assertTrue(get_message_history(self.user, self.chat.session_id, self.factory, self.registry).is_generating)

    def test_empty_second_registry_cannot_bypass_process_lock(self):
        with open_backend_runtime(self.engine, lock=self.lock) as runtime:
            with accept_user_message(
                self.user, self.chat.session_id, SendMessageRequest(client_message_key=str(uuid4()), content="Live question"),
                runtime.session_factory, runtime.registry, check_new_message=lambda _: None, input_policy=self.policy,
            ):
                before = self.messages()
                with self.assertRaises(RuntimeAlreadyActiveError), open_backend_runtime(
                    self.engine, lock=RuntimeLock(self.lock_path),
                ):
                    self.fail("Second process must never clean the first one's live message")
                self.assertEqual(self.messages(), before)

    def test_real_fastapi_startup_cleans_before_serving_and_holds_runtime_lock(self):
        self.question()
        app = create_app(runtime_factory=lambda: open_backend_runtime(self.engine, lock=self.lock),
                         http_settings=HTTPSettings(_env_file=None))
        with TestClient(app) as client:
            self.assertEqual(client.get("/health").json(), {"status": "ok"})
            self.assertEqual(self.messages()[0].generation_status, "FAILED")
            self.assertEqual(app.state.runtime.cleanup.interrupted, 1)
            with self.assertRaises(RuntimeAlreadyActiveError), RuntimeLock(self.lock_path):
                self.fail("Web application must keep its lock while serving")
        with RuntimeLock(self.lock_path):
            pass
