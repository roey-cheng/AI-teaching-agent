"""新消息准入的真实 MySQL 测试；只用临时容器和模拟执行，不调用模型。"""

import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier, Event
from typing import ClassVar
from unittest.mock import MagicMock
from uuid import uuid4

from sqlalchemy import Engine, Table, event, select
from sqlalchemy.exc import SQLAlchemyError

from app.agent.input_policy import policy_for_model
from app.db.session import build_session_factory
from app.models import ChatSession, Message
from app.schemas import (
    DuplicateMessageResponse,
    LoginRequest,
    RegisterRequest,
    RenameSessionRequest,
    SendMessageRequest,
    UserMessageResponse,
)
from app.services.auth import register_user
from app.services.authentication import get_current_user
from app.services.chat_sessions import create_chat_session, rename_chat_session
from app.services.errors import (
    IdempotencyConflictError,
    MessageSendUnavailableError,
    SessionBusyError,
    SessionNotFoundError,
)
from app.services.generation_registry import GenerationRegistry
from app.services.login import login_user
from app.services.message_history import get_message_history
from app.services.message_submission import (
    AcceptedMessage,
    _settle_interrupted,
    accept_user_message,
)
from testing_support import require_type, require_value


class MessageSubmissionMySQLTest(unittest.TestCase):
    engine: ClassVar[Engine]  # 由临时 MySQL 验收入口注入。

    def setUp(self):
        self.factory = build_session_factory(self.engine)
        email = f"submission-{uuid4().hex}@example.com"
        register_user(
            RegisterRequest.model_validate(
                {"email": email, "password": "test-password", "display_name": "Submit tester"}
            ),
            self.factory,
        )
        login = login_user(
            LoginRequest.model_validate({"email": email, "password": "test-password"}), self.factory
        )
        self.user = get_current_user(login.token, self.factory)
        self.chat = create_chat_session(self.user, self.factory)
        self.registry = GenerationRegistry()
        self.request = SendMessageRequest.model_validate(
            {"client_message_key": str(uuid4()), "content": "  Explain\nPython🙂  "}
        )
        self.check = MagicMock()  # 限流仍为替身；输入组装和预算使用真实业务代码。
        self.policy = policy_for_model("deepseek-v4-pro")

    def accept(self, request=None, chat_id=None, user=None):
        return accept_user_message(
            user or self.user,
            chat_id or self.chat.session_id,
            request or self.request,
            self.factory,
            self.registry,
            check_new_message=self.check,
            input_policy=self.policy,
        )

    def rows(self):
        with self.factory() as session:
            return session.execute(
                select(require_type(Message.__table__, Table))
                .where(Message.chat_session_id == int(self.chat.session_id))
                .order_by(Message.message_id)
            ).all()

    def history(self):
        return get_message_history(self.user, self.chat.session_id, self.factory, self.registry)

    def snapshot_chat(self):
        with self.factory() as session:
            return session.execute(
                select(require_type(ChatSession.__table__, Table)).where(
                    ChatSession.chat_session_id == int(self.chat.session_id)
                )
            ).one()

    def save_simulated_answer(self, accepted):
        with self.registry.locked(int(self.chat.session_id)), self.factory.begin() as session:
            question = session.get(Message, int(accepted.user_message_id))
            require_value(question).generation_status = "SUCCEEDED"
            answer = Message(
                chat_session_id=int(self.chat.session_id),
                role="ASSISTANT",
                content="Test answer",
                in_reply_to_message_id=require_value(question).message_id,
                created_at=datetime.now(UTC).replace(tzinfo=None),
                updated_at=datetime.now(UTC).replace(tzinfo=None),
            )
            session.add(answer)
            session.flush()
            return answer.message_id

    def test_new_message_visible_only_after_commit_and_scope_exit_marks_interrupted(self):
        with self.accept() as accepted:
            assert isinstance(accepted, AcceptedMessage)
            row = self.rows()[0]
            self.assertEqual(
                (row.content, row.role, row.generation_status),
                (self.request.content, "USER", "RUNNING"),
            )
            self.assertEqual(str(row.message_id), accepted.user_message_id)
            self.assertEqual(row.attempt_id, require_value(accepted).attempt_id)
            chat = self.snapshot_chat()
            self.assertEqual(chat.title, "Explain Python🙂")
            self.assertEqual(chat.last_activity_at, row.created_at)
            self.assertTrue(self.history().is_generating)
        self.assertEqual(self.rows()[0].generation_error_code, "GENERATION_INTERRUPTED")
        self.assertFalse(self.history().is_generating)
        self.assertTrue(
            require_type(self.history().items[0], UserMessageResponse).generation.can_retry
        )
        self.assertEqual(self.snapshot_chat().last_activity_at, chat.last_activity_at)

    def test_duplicate_during_busy_and_content_conflict_do_not_touch_original(self):
        with self.accept() as first:
            before = (self.rows(), self.snapshot_chat())
            with self.accept() as duplicate:
                assert isinstance(duplicate, DuplicateMessageResponse)
                self.assertEqual(duplicate.attempt_id, first.attempt_id)
            conflict = SendMessageRequest.model_validate(
                {"client_message_key": self.request.client_message_key, "content": "different"}
            )
            with self.assertRaises(IdempotencyConflictError), self.accept(conflict):
                self.fail("Must not yield")
            self.assertEqual((self.rows(), self.snapshot_chat()), before)
            self.assertTrue(self.history().is_generating)
            self.check.assert_called_once()

    def test_failed_duplicate_returns_receipt_without_new_attempt(self):
        with self.accept() as first:
            pass
        before = (self.rows(), self.snapshot_chat())
        with self.accept() as duplicate:
            self.assertEqual(require_type(duplicate, DuplicateMessageResponse).status, "FAILED")
            self.assertEqual(duplicate.attempt_id, first.attempt_id)
        self.assertEqual((self.rows(), self.snapshot_chat()), before)
        self.check.assert_called_once()

    def test_successful_duplicate_and_scope_cleanup_preserve_answer(self):
        with self.accept() as first:
            answer_id = self.save_simulated_answer(first)
            self.assertTrue(self.history().is_generating)  # 已保存结果但作用域尚未结束。
        with self.accept() as duplicate:
            self.assertEqual(require_type(duplicate, DuplicateMessageResponse).status, "SUCCEEDED")
            self.assertEqual(
                require_type(duplicate, DuplicateMessageResponse).assistant_message_id,
                str(answer_id),
            )
        self.assertEqual(len(self.rows()), 2)
        self.assertFalse(self.history().is_generating)

    def test_key_is_scoped_to_session_and_owner_is_checked_first(self):
        other_chat = create_chat_session(self.user, self.factory)
        with self.accept(), self.accept(chat_id=other_chat.session_id) as other:
            assert isinstance(other, AcceptedMessage)
        fake_user = self.user.model_copy(update={"user_id": "18446744073709551615"})
        for chat_id in (self.chat.session_id, "18446744073709551615"):
            with (
                self.assertRaises(SessionNotFoundError),
                self.accept(chat_id=chat_id, user=fake_user),
            ):
                self.fail("Must not yield")

    def test_busy_rejection_does_not_reserve_new_key(self):
        new = SendMessageRequest.model_validate(
            {"client_message_key": str(uuid4()), "content": "Second question"}
        )
        with self.accept():
            with self.assertRaises(SessionBusyError), self.accept(new):
                self.fail("Must not yield")
            self.assertEqual(len(self.rows()), 1)
        with self.accept(new) as accepted:
            assert isinstance(accepted, AcceptedMessage)
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.snapshot_chat().title, "Explain Python🙂")

    def test_preflight_rejection_does_not_change_title_activity_or_messages(self):
        before = self.snapshot_chat()
        self.check.side_effect = ValueError("Input budget exceeded")
        with self.assertRaises(ValueError), self.accept():
            self.fail("Must not yield")
        self.assertEqual(self.snapshot_chat(), before)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.registry._entries, {})

    def test_manual_title_and_body_exception_are_preserved_and_settled(self):
        rename_chat_session(
            self.user,
            self.chat.session_id,
            RenameSessionRequest.model_validate({"title": "Keep"}),
            self.factory,
        )
        with self.assertRaisesRegex(RuntimeError, "stopped"), self.accept():
            self.assertEqual(self.snapshot_chat().title, "Keep")
            raise RuntimeError("stopped")
        self.assertEqual(self.rows()[0].generation_status, "FAILED")
        self.assertEqual(self.snapshot_chat().title, "Keep")
        self.assertEqual(self.registry._entries, {})

    def test_precommit_failure_rolls_back_and_reconciles_empty_record(self):
        before = self.snapshot_chat()

        failed_once = False

        def fail_once(session):
            nonlocal failed_once
            if not failed_once:
                failed_once = True
                raise SQLAlchemyError("private-before-commit")

        event.listen(self.factory, "before_commit", fail_once)
        try:
            with self.assertRaises(MessageSendUnavailableError), self.accept():
                self.fail("Must not yield")
        finally:
            event.remove(self.factory, "before_commit", fail_once)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.snapshot_chat(), before)
        self.assertEqual(self.registry._entries, {})

    def test_postcommit_error_reconciles_persisted_message_without_duplicate(self):
        failed_once = False

        def fail_once(session):
            nonlocal failed_once
            if not failed_once:
                failed_once = True
                raise SQLAlchemyError("private-after-commit")

        event.listen(self.factory, "after_commit", fail_once)
        try:
            with self.assertRaises(MessageSendUnavailableError), self.accept():
                self.fail("Must not yield")
        finally:
            event.remove(self.factory, "after_commit", fail_once)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0].generation_status, "FAILED")
        self.assertEqual(self.registry._entries, {})
        with self.accept() as receipt:
            self.assertTrue(require_type(receipt, DuplicateMessageResponse).duplicate)

    def test_cleanup_failure_keeps_occupancy_until_explicit_reconciliation(self):
        def fail(session):
            raise SQLAlchemyError("private-cleanup-error")

        accepted = None
        try:
            with self.assertRaises(MessageSendUnavailableError), self.accept() as accepted:
                event.listen(self.factory, "before_commit", fail)
        finally:
            event.remove(self.factory, "before_commit", fail)
        self.assertEqual(self.rows()[0].generation_status, "RUNNING")
        with self.registry.locked(int(self.chat.session_id)) as slot:
            self.assertEqual(slot.attempt_id, require_value(accepted).attempt_id)
        # 测试显式模拟数据库恢复后的核对；生产恢复入口尚未实现。
        _settle_interrupted(
            int(self.user.user_id),
            int(self.chat.session_id),
            self.request.client_message_key,
            require_value(accepted).attempt_id,
            self.factory,
            self.registry,
        )
        self.assertEqual(self.rows()[0].generation_status, "FAILED")
        self.assertEqual(self.registry._entries, {})

    def test_simultaneous_same_key_produces_one_message_and_one_duplicate(self):
        barrier, duplicate_seen = Barrier(2), Event()

        def send():
            barrier.wait(timeout=5)
            with self.accept() as result:
                if isinstance(result, DuplicateMessageResponse):
                    duplicate_seen.set()
                else:
                    self.assertTrue(duplicate_seen.wait(timeout=10))
                return type(result)

        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(send) for _ in range(2)]
            self.assertCountEqual(
                [job.result(timeout=15) for job in jobs],
                [AcceptedMessage, DuplicateMessageResponse],
            )
        self.assertEqual(len(self.rows()), 1)
        self.check.assert_called_once()

    def test_orphan_running_blocks_new_message_without_claim_or_write(self):
        now = datetime.now(UTC).replace(tzinfo=None)
        with self.factory.begin() as session:
            session.add(
                Message(
                    chat_session_id=int(self.chat.session_id),
                    role="USER",
                    content="Old",
                    client_message_key=str(uuid4()),
                    attempt_id=str(uuid4()),
                    generation_status="RUNNING",
                    created_at=now,
                    updated_at=now,
                )
            )
        with self.assertRaises(MessageSendUnavailableError), self.accept():
            self.fail("Must not yield")
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.registry._entries, {})

    def test_old_scope_cleanup_cannot_modify_new_attempt(self):
        with self.accept() as first:
            new_attempt = str(uuid4())
            with self.registry.locked(int(self.chat.session_id)) as slot:
                with self.factory.begin() as session:
                    q = session.get(Message, int(first.user_message_id))
                    require_value(q).attempt_id = new_attempt
                slot.release(first.attempt_id)
                slot.claim(new_attempt)
        self.assertEqual(self.rows()[0].attempt_id, new_attempt)
        self.assertEqual(self.rows()[0].generation_status, "RUNNING")
        _settle_interrupted(
            int(self.user.user_id),
            int(self.chat.session_id),
            self.request.client_message_key,
            new_attempt,
            self.factory,
            self.registry,
        )
