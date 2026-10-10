"""独立 MySQL + 真实 Deep Agents 图的失败重试验收；模型响应替身，不产生云端费用。"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import threading
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from sqlalchemy import event, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.agent.input_policy import policy_for_model
from app.core.config import ModelSettings, TracingSettings
from app.db.session import build_session_factory
from app.models import AgentMemory, ChatSession, Message
from app.schemas import (
    DuplicateMessageResponse, RegisterRequest, RetryMessageRequest, SendMessageRequest, UserResponse,
)
from app.services.auth import register_user
from app.services.chat_execution import EventDeliveryError, execute_chat_retry
from app.services.chat_sessions import create_chat_session
from app.services.errors import (
    ContextTooLargeError, GenerationRateLimitError, MessageNotFoundError, MessageSendUnavailableError,
    RetryNotAllowedError, SessionBusyError, SessionNotFoundError, StaleAttemptError,
)
from app.services.generation_limit import GenerationRateLimiter
from app.services.generation_registry import GenerationRegistry
from app.services.generation_result import StaleGenerationError, settle_generation
from app.services.message_history import get_message_history
from app.services.message_retry import accept_failed_message_retry
from app.services.message_submission import accept_user_message, _settle_interrupted
from app.services.profile_memory import ProfileFact, save_profile_facts


class MessageRetryMySQLTest(unittest.IsolatedAsyncioTestCase):
    engine = None

    def setUp(self):
        self.factory = build_session_factory(self.engine)
        self.user = register_user(RegisterRequest(
            email=f"retry-{uuid4().hex}@example.com", password="test-password", display_name="Retry tester",
        ), self.factory)
        self.chat = create_chat_session(self.user, self.factory)
        self.registry = GenerationRegistry()
        self.policy = policy_for_model("deepseek-v4-pro")
        self.settings = ModelSettings(_env_file=None, provider="deepseek", name=self.policy.model_name, api_key="fake-only")
        self.tracing = TracingSettings(_env_file=None, tracing=False, api_key=None)
        self.request = SendMessageRequest(client_message_key=str(uuid4()), content="以后请用中文解释")
        self.limiter = GenerationRateLimiter()
        self.check = MagicMock(side_effect=lambda _: self.limiter.check(self.user.user_id))
        self.events, self.inputs, self.memory_inputs = [], [], []
        self.reason, self.delay, self.save_memory = "stop", 0, False
        self.started, self.closed = asyncio.Event(), False

        async def provider(_model, messages, **kwargs):
            if kwargs.get("tools"):
                self.memory_inputs.append(messages)
                if self.save_memory:
                    facts = [{"memory_key": "preference.language", "summary": "喜欢中文解释",
                              "source_quote": self.request.content}]
                    yield ChatGenerationChunk(message=AIMessageChunk(content="", tool_call_chunks=[{
                        "name": "save_profile_facts", "args": json.dumps({"facts": facts}, ensure_ascii=False),
                        "id": "memory1", "index": 0,
                    }]))
                    reason = "tool_calls"
                else:
                    yield ChatGenerationChunk(message=AIMessageChunk(content="No update"))
                    reason = "stop"
                yield ChatGenerationChunk(message=AIMessageChunk(content="", response_metadata={"finish_reason": reason}))
                return
            self.inputs.append(messages)
            self.started.set()
            try:
                if self.delay:
                    await asyncio.sleep(self.delay)
                yield ChatGenerationChunk(message=AIMessageChunk(content="", additional_kwargs={"reasoning_content": "live reasoning"}))
                yield ChatGenerationChunk(message=AIMessageChunk(content="Final answer"))
                yield ChatGenerationChunk(message=AIMessageChunk(content="", response_metadata={"finish_reason": self.reason}))
            finally:
                self.closed = True

        stub = patch("langchain_deepseek.ChatDeepSeek._astream", provider)
        stub.start()
        self.addCleanup(stub.stop)

    def send_scope(self, request=None):
        return accept_user_message(
            self.user, self.chat.session_id, request or self.request, self.factory, self.registry,
            check_new_message=lambda _: self.limiter.check(self.user.user_id), input_policy=self.policy,
        )

    def seed_failure(self):
        with self.send_scope() as accepted:
            pass
        return accepted

    def retry_scope(self, original, **kwargs):
        return accept_failed_message_retry(
            kwargs.pop("user", self.user), kwargs.pop("session_id", self.chat.session_id),
            kwargs.pop("message_id", original.user_message_id),
            RetryMessageRequest(failed_attempt_id=kwargs.pop("attempt_id", original.attempt_id)),
            self.factory, self.registry, check_retry=self.check,
            input_policy=kwargs.pop("policy", self.policy), **kwargs,
        )

    async def emit(self, event):
        self.events.append(event)

    async def retry(self, original, **kwargs):
        return await execute_chat_retry(
            self.user, self.chat.session_id, original.user_message_id,
            RetryMessageRequest(failed_attempt_id=original.attempt_id), self.factory, self.registry,
            model_settings=self.settings, tracing_settings=self.tracing, input_policy=self.policy,
            check_retry=self.check, on_event=kwargs.pop("on_event", self.emit), **kwargs,
        )

    def rows(self):
        with self.factory() as session:
            return session.execute(select(Message.__table__).where(
                Message.chat_session_id == int(self.chat.session_id)).order_by(Message.message_id)).all()

    def chat_row(self):
        with self.factory() as session:
            return session.execute(select(ChatSession.__table__).where(
                ChatSession.chat_session_id == int(self.chat.session_id))).one()

    def history(self):
        return get_message_history(self.user, self.chat.session_id, self.factory, self.registry)

    async def test_retry_success_reuses_question_and_context_contains_it_once(self):
        with self.send_scope(SendMessageRequest(client_message_key=str(uuid4()), content="Earlier question")) as earlier:
            settle_generation(earlier, self.factory, self.registry, answer="Earlier answer")
        original = self.seed_failure()
        before, old_chat = self.rows()[-1], self.chat_row()
        result = await self.retry(original)
        rows = self.rows()
        question = rows[-2]
        self.assertEqual(result.event_name, "message_done")
        self.assertEqual(len(rows), 4)
        for name in ("message_id", "created_at", "client_message_key", "content"):
            self.assertEqual(getattr(question, name), getattr(before, name))
        self.assertNotEqual(question.attempt_id, before.attempt_id)
        self.assertEqual(question.retry_of_attempt_id, before.attempt_id)
        self.assertEqual(question.generation_status, "SUCCEEDED")
        self.assertIsNone(question.generation_error_code)
        self.assertEqual(rows[-1].in_reply_to_message_id, before.message_id)
        self.assertEqual(self.chat_row().title, old_chat.title)
        self.assertGreaterEqual(self.chat_row().last_activity_at, old_chat.last_activity_at)
        self.assertEqual([m.content for m in self.inputs[0][1:]], ["Earlier question", "Earlier answer", self.request.content])
        self.assertTrue(any(e.event_name == "reasoning_delta" for e in self.events))
        self.assertFalse(any("live reasoning" in r.content for r in rows))
        self.assertFalse(self.history().is_generating)
        self.assertFalse(self.history().items[-2].generation.can_retry)
        with self.assertRaises(RetryNotAllowedError), self.retry_scope(original, attempt_id=question.attempt_id):
            self.fail("Successful answer is not regeneration")

    async def test_failed_retry_replay_next_retry_and_old_request_conflict(self):
        original = self.seed_failure()
        self.reason = "length"
        result = await self.retry(original)
        self.assertEqual(result.event_name, "message_error")
        self.assertEqual(len(self.rows()), 1)  # 半截回答不落库。
        self.assertTrue(self.history().items[0].generation.can_retry)
        before = (self.rows(), len(self.events), len(self.inputs), len(self.memory_inputs))
        duplicate = await self.retry(original)
        self.assertEqual(duplicate.status, "FAILED")
        self.assertEqual((self.rows(), len(self.events), len(self.inputs), len(self.memory_inputs)), before)
        self.check.assert_called_once()
        failed_again = replace(original, attempt_id=duplicate.attempt_id)
        self.reason = "stop"
        result = await self.retry(failed_again)
        self.assertEqual(result.event_name, "message_done")
        with self.assertRaises(StaleAttemptError):
            await self.retry(original)
        duplicate = await self.retry(failed_again)
        self.assertEqual(duplicate.status, "SUCCEEDED")
        self.assertEqual(duplicate.assistant_message_id, result.assistant_message.message_id)
        self.assertEqual(self.check.call_count, 2)

    async def test_access_target_and_older_failed_question_restrictions(self):
        original = self.seed_failure()
        other_user = UserResponse(user_id="18446744073709551615", email="other@example.com", display_name="Other")
        with self.assertRaises(SessionNotFoundError), self.retry_scope(original, user=other_user):
            self.fail("Must not yield")
        with self.assertRaises(MessageNotFoundError), self.retry_scope(original, message_id="18446744073709551615"):
            self.fail("Must not yield")
        other_chat = create_chat_session(self.user, self.factory)
        with self.assertRaises(MessageNotFoundError), self.retry_scope(original, session_id=other_chat.session_id):
            self.fail("Cross-session message must not be accepted")
        with self.send_scope(SendMessageRequest(client_message_key=str(uuid4()), content="New question")) as newer:
            with self.assertRaises(SessionBusyError), self.retry_scope(original):
                self.fail("Must not yield")
            outcome = settle_generation(newer, self.factory, self.registry, answer="New answer")
        with self.assertRaises(RetryNotAllowedError), self.retry_scope(original):
            self.fail("Older failed question must not run")
        with self.assertRaises(RetryNotAllowedError), self.retry_scope(original, message_id=outcome.assistant.message_id):
            self.fail("Assistant is not a question")
        self.assertFalse(self.history().items[0].generation.can_retry)
        self.check.assert_not_called()

    async def test_two_concurrent_retries_execute_once_and_block_new_send(self):
        original = self.seed_failure()
        self.delay = 0.2
        task = asyncio.create_task(self.retry(original))
        try:
            await asyncio.wait_for(self.started.wait(), 5)
            duplicate = await self.retry(original)
            self.assertIsInstance(duplicate, DuplicateMessageResponse)
            self.assertEqual(duplicate.status, "RUNNING")
            with self.assertRaises(SessionBusyError), self.send_scope(
                SendMessageRequest(client_message_key=str(uuid4()), content="Must wait"),
            ):
                self.fail("Must not yield")
            with self.send_scope() as send_receipt:
                self.assertEqual(send_receipt.attempt_id, duplicate.attempt_id)
                self.assertEqual(send_receipt.status, "RUNNING")
            result = await task
            self.assertEqual(result.event_name, "message_done")
            self.assertEqual(len(self.inputs), 1)
            self.assertEqual(len(self.memory_inputs), 1)
            self.assertEqual(len([e for e in self.events if e.event_name == "message_start"]), 1)
            self.check.assert_called_once()
        finally:
            if not task.done():
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

    async def test_late_old_result_memory_and_cleanup_cannot_touch_retry(self):
        original = self.seed_failure()
        with self.retry_scope(original) as accepted:
            before = self.rows()
            with self.assertRaises(StaleGenerationError):
                settle_generation(original, self.factory, self.registry, answer="Late old answer")
            with self.assertRaises(StaleGenerationError):
                save_profile_facts(original, [ProfileFact(memory_key="preference.language", summary="English")],
                                   self.factory, self.registry)
            _settle_interrupted(int(self.user.user_id), int(self.chat.session_id), self.request.client_message_key,
                                original.attempt_id, self.factory, self.registry)
            self.assertEqual(self.rows(), before)
            self.assertTrue(self.history().is_generating)
            self.assertEqual(self.history().items[0].generation.attempt_id, accepted.attempt_id)
            settle_generation(accepted, self.factory, self.registry, error_code="MODEL_REQUEST_FAILED")
            # 最终状态已写入但仍在清理时，禁止新重试。
            self.assertFalse(self.history().items[0].generation.can_retry)
            with self.assertRaises(SessionBusyError), self.retry_scope(original, attempt_id=accepted.attempt_id):
                self.fail("Cleanup still owns the slot")
            with self.assertRaises(StaleGenerationError):
                settle_generation(accepted, self.factory, self.registry, answer="Late same attempt")
        self.assertTrue(self.history().items[0].generation.can_retry)

    async def test_retry_timeout_and_disconnect_leave_failed_not_running(self):
        original = self.seed_failure()
        self.delay = 10
        result = await self.retry(original, timeout_seconds=0.15)
        self.assertEqual(result.error.code, "GENERATION_TIMEOUT")
        self.assertFalse(self.registry._entries)
        current = replace(original, attempt_id=self.rows()[0].attempt_id)

        async def disconnect(event):
            raise OSError("private-network-error")

        with self.assertRaises(EventDeliveryError):
            await self.retry(current, on_event=disconnect)
        self.assertEqual(self.rows()[0].generation_error_code, "GENERATION_INTERRUPTED")
        self.assertFalse(self.registry._entries)
        self.assertEqual(len(self.rows()), 1)

    async def test_cancel_running_retry_closes_model_and_releases_after_cleanup(self):
        original = self.seed_failure()
        self.delay = 10
        task = asyncio.create_task(self.retry(original))
        await asyncio.wait_for(self.started.wait(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.closed)
        self.assertFalse(self.registry._entries)
        self.assertEqual(self.rows()[0].generation_error_code, "GENERATION_INTERRUPTED")
        self.assertFalse(any(e.event_name in ("message_done", "message_error") for e in self.events))

    async def test_cancel_while_retry_commit_waits_for_commit_then_cleans_up(self):
        original = self.seed_failure()
        entered, release = threading.Event(), threading.Event()

        class PausedSession(Session):
            pass

        def pause_once(session):
            if not entered.is_set():
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("Test did not release commit")

        event.listen(PausedSession, "before_commit", pause_once)
        self.factory = sessionmaker(bind=self.engine, class_=PausedSession, expire_on_commit=False)
        task = asyncio.create_task(self.retry(original))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 4))
            task.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
        finally:
            release.set()
            try:
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                event.remove(PausedSession, "before_commit", pause_once)
        self.assertNotEqual(self.rows()[0].attempt_id, original.attempt_id)
        self.assertEqual(self.rows()[0].generation_error_code, "GENERATION_INTERRUPTED")
        self.assertEqual(self.inputs, [])
        self.assertEqual(self.events, [])
        self.assertFalse(self.registry._entries)

    async def test_retry_precommit_failure_preserves_previous_failure_and_frees_slot(self):
        original = self.seed_failure()
        before = self.rows(), self.chat_row()

        def fail_once(session):
            if not getattr(fail_once, "done", False):
                fail_once.done = True
                raise SQLAlchemyError("private-before-commit")

        event.listen(self.factory, "before_commit", fail_once)
        try:
            with self.assertRaises(MessageSendUnavailableError), self.retry_scope(original):
                self.fail("Must not yield")
        finally:
            event.remove(self.factory, "before_commit", fail_once)
        self.assertEqual((self.rows(), self.chat_row()), before)
        self.assertFalse(self.registry._entries)
        self.assertTrue(self.history().items[0].generation.can_retry)

    async def test_retry_postcommit_error_reconciles_new_attempt_and_replay(self):
        original = self.seed_failure()

        def fail_once(session):
            if not getattr(fail_once, "done", False):
                fail_once.done = True
                raise SQLAlchemyError("private-after-commit")

        event.listen(self.factory, "after_commit", fail_once)
        try:
            with self.assertRaises(MessageSendUnavailableError), self.retry_scope(original):
                self.fail("Must not yield")
        finally:
            event.remove(self.factory, "after_commit", fail_once)
        self.assertEqual(len(self.rows()), 1)
        self.assertNotEqual(self.rows()[0].attempt_id, original.attempt_id)
        self.assertEqual(self.rows()[0].generation_error_code, "GENERATION_INTERRUPTED")
        self.assertFalse(self.registry._entries)
        with self.retry_scope(original) as receipt:
            self.assertEqual(receipt.status, "FAILED")
        self.check.assert_called_once()

    async def test_failed_cleanup_keeps_busy_until_explicit_reconciliation(self):
        original = self.seed_failure()

        def fail(session):
            raise SQLAlchemyError("private-cleanup-error")

        try:
            with self.assertRaises(MessageSendUnavailableError):
                with self.retry_scope(original) as accepted:
                    event.listen(self.factory, "before_commit", fail)
        finally:
            event.remove(self.factory, "before_commit", fail)
        self.assertEqual(self.rows()[0].generation_status, "RUNNING")
        self.assertTrue(self.history().is_generating)
        # 仅测试模拟数据库恢复后显式核对；自动启动清理不属于这一步。
        _settle_interrupted(int(self.user.user_id), int(self.chat.session_id), self.request.client_message_key,
                            accepted.attempt_id, self.factory, self.registry,
                            previous_attempt_id=original.attempt_id)
        self.assertFalse(self.registry._entries)
        self.assertTrue(self.history().items[0].generation.can_retry)

    async def test_budget_and_shared_user_rate_limit_preserve_old_failure(self):
        original = self.seed_failure()
        before = self.rows(), self.chat_row()
        tiny = replace(self.policy, app_context_tokens=self.policy.app_context_tokens - self.policy.input_limit + 1)
        with self.assertRaises(ContextTooLargeError), self.retry_scope(original, policy=tiny):
            self.fail("Must not yield")
        self.check.assert_not_called()
        for _ in range(9):
            self.limiter.check(self.user.user_id)
        with self.assertRaises(GenerationRateLimitError), self.retry_scope(original):
            self.fail("New sends and retries share the same 10-generation quota")
        self.assertEqual((self.rows(), self.chat_row()), before)
        self.assertFalse(self.registry._entries)

    async def test_memory_upsert_survives_failed_answer_and_retry_without_duplicates(self):
        original = self.seed_failure()
        self.save_memory, self.reason = True, "length"
        result = await self.retry(original)
        self.assertEqual(result.event_name, "message_error")
        with self.factory() as session:
            first = session.scalar(select(AgentMemory).where(AgentMemory.user_id == int(self.user.user_id)))
        current = replace(original, attempt_id=self.rows()[0].attempt_id)
        self.reason = "stop"
        result = await self.retry(current)
        self.assertEqual(result.event_name, "message_done")
        with self.factory() as session:
            memories = session.scalars(select(AgentMemory).where(AgentMemory.user_id == int(self.user.user_id))).all()
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0].memory_id, first.memory_id)
        self.assertEqual(memories[0].summary, "喜欢中文解释")
        self.assertIn("喜欢中文解释", self.inputs[-1][0].text)
        self.assertEqual(len(self.rows()), 2)

    async def test_recent_retry_receipt_still_available_after_sending_new_question(self):
        original = self.seed_failure()
        await self.retry(original)
        with self.send_scope(SendMessageRequest(client_message_key=str(uuid4()), content="Later question")):
            receipt = await self.retry(original)
            self.assertTrue(receipt.duplicate)
            self.assertEqual(receipt.status, "SUCCEEDED")
            self.assertTrue(self.history().is_generating)
        self.check.assert_called_once()
        self.assertEqual(len(self.inputs), 1)

    async def test_simultaneous_retry_admissions_claim_only_one_attempt(self):
        original = self.seed_failure()
        barrier, duplicate_seen = threading.Barrier(2), threading.Event()

        def submit():
            barrier.wait(timeout=5)
            with self.retry_scope(original) as result:
                if isinstance(result, DuplicateMessageResponse):
                    duplicate_seen.set()
                else:
                    self.assertTrue(duplicate_seen.wait(timeout=5))
                return result

        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(submit) for _ in range(2)]
            results = [job.result(timeout=10) for job in jobs]
        self.assertEqual(sum(isinstance(result, DuplicateMessageResponse) for result in results), 1)
        self.assertEqual(results[0].attempt_id, results[1].attempt_id)
        self.assertEqual(len(self.rows()), 1)
        self.check.assert_called_once()
        self.assertFalse(self.registry._entries)
