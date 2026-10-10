"""临时 MySQL + 真 Deep Agents 图 + 假供应商流；不读取项目密钥、不发送云端请求。"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import unittest
import threading
from unittest.mock import MagicMock, patch
from uuid import uuid4

from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from sqlalchemy import event, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.agent.input_policy import policy_for_model
from app.core.config import ModelSettings, TracingSettings
from app.db.session import build_session_factory
from app.models import AgentMemory, Message
from app.schemas import DuplicateMessageResponse, RegisterRequest, SendMessageRequest
from app.services.auth import register_user
from app.services.chat_sessions import create_chat_session
from app.services.chat_execution import EventDeliveryError, execute_chat_turn
from app.services.errors import SessionBusyError, SessionNotFoundError
from app.services.generation_registry import GenerationRegistry
from app.services.generation_result import StaleGenerationError, settle_generation
from app.services.message_history import get_message_history
from app.services.message_submission import accept_user_message


class ChatExecutionMySQLTest(unittest.IsolatedAsyncioTestCase):
    engine = None

    def setUp(self):
        self.factory = build_session_factory(self.engine)
        registered = register_user(RegisterRequest(email=f"exec-{uuid4().hex}@example.com",
                                                   password="test-password", display_name="Execution tester"), self.factory)
        self.user = registered
        self.chat = create_chat_session(self.user, self.factory)
        self.registry = GenerationRegistry()
        self.request = SendMessageRequest(client_message_key=str(uuid4()), content="Explain Python")
        self.policy = policy_for_model("deepseek-v4-pro")
        self.settings = ModelSettings(_env_file=None, provider="deepseek", name=self.policy.model_name, api_key="fake-only")
        self.tracing = TracingSettings(_env_file=None, tracing=False, api_key=None)
        self.check = MagicMock()  # 仍不声称实现了生产限流。
        self.events, self.inputs = [], []
        self.reason = "stop"
        self.delay = 0
        self.started = asyncio.Event()
        self.model_closed = False

        async def provider(_model, messages, **kwargs):
            self.inputs.append(messages)
            self.assertFalse(kwargs.get("tools"))
            self.assertEqual(kwargs["max_tokens"], self.policy.output_tokens)
            self.started.set()
            try:
                if self.delay:
                    await asyncio.sleep(self.delay)
                yield ChatGenerationChunk(message=AIMessageChunk(content="", additional_kwargs={"reasoning_content": "visible reasoning"}))
                yield ChatGenerationChunk(message=AIMessageChunk(content="Final "))
                yield ChatGenerationChunk(message=AIMessageChunk(content="answer"))
                yield ChatGenerationChunk(message=AIMessageChunk(content="", response_metadata={"finish_reason": self.reason}))
            finally:
                self.model_closed = True

        mock = patch("langchain_deepseek.ChatDeepSeek._astream", provider)
        mock.start()
        self.addCleanup(mock.stop)

    async def emit(self, item):
        self.events.append(item)
        if item.event_name in ("message_done", "message_error"):
            self.assertTrue(self.model_closed)
            history = await asyncio.to_thread(self.history)
            self.assertFalse(history.is_generating)
            self.assertNotEqual(history.items[0].generation.status, "RUNNING")

    async def execute(self, **overrides):
        values = dict(current_user=self.user, session_id=self.chat.session_id, request=self.request,
                      session_factory=self.factory, registry=self.registry, model_settings=self.settings,
                      tracing_settings=self.tracing, input_policy=self.policy,
                      check_new_message=self.check, on_event=self.emit)
        values.update(overrides)
        return await execute_chat_turn(**values)

    def history(self):
        return get_message_history(self.user, self.chat.session_id, self.factory, self.registry)

    def rows(self):
        with self.factory() as session:
            return session.scalars(select(Message).where(Message.chat_session_id == int(self.chat.session_id))
                                   .order_by(Message.message_id)).all()

    async def test_complete_real_graph_memory_two_turns_and_duplicate(self):
        now = datetime.now(UTC).replace(tzinfo=None)
        with self.factory.begin() as session:
            session.add(AgentMemory(user_id=int(self.user.user_id), memory_key="preference.language",
                                    memory_type="LEARNING_PREFERENCE", summary="user-specific-memory",
                                    created_at=now, updated_at=now))
        result = await self.execute()
        self.assertEqual(result.event_name, "message_done")
        self.assertEqual(result.assistant_message.content, "Final answer")
        self.assertEqual([r.role for r in self.rows()], ["USER", "ASSISTANT"])
        self.assertFalse(any("visible reasoning" in r.content for r in self.rows()))
        self.assertEqual(self.inputs[0][0].text.count("user-specific-memory"), 1)
        self.assertTrue(any(e.event_name == "reasoning_delta" for e in self.events))
        count = len(self.events)
        duplicate = await self.execute()
        self.assertIsInstance(duplicate, DuplicateMessageResponse)
        self.assertEqual(duplicate.status, "SUCCEEDED")
        self.assertEqual(len(self.events), count)
        self.assertEqual(len(self.inputs), 1)
        second = SendMessageRequest(client_message_key=str(uuid4()), content="Next question")
        await self.execute(request=second)
        self.assertEqual([m.content for m in self.inputs[1][1:]], ["Explain Python", "Final answer", "Next question"])
        self.assertEqual(len(self.rows()), 4)

    async def test_truncated_output_saves_failure_only(self):
        self.reason = "length"
        result = await self.execute()
        self.assertEqual(result.error.code, "MODEL_REQUEST_FAILED")
        self.assertEqual(len(self.rows()), 1)
        self.assertTrue(self.history().items[0].generation.can_retry)

    async def test_timeout_closes_model_and_releases_conversation(self):
        self.delay = 10
        result = await self.execute(timeout_seconds=0.2)
        self.assertEqual(result.error.code, "GENERATION_TIMEOUT")
        self.assertEqual(self.rows()[0].generation_status, "FAILED")
        self.assertFalse(self.registry._entries)

    async def test_cancel_during_model_marks_interrupted_and_sends_no_terminal(self):
        self.delay = 10
        task = asyncio.create_task(self.execute())
        await self.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.model_closed)
        self.assertEqual(self.rows()[0].generation_error_code, "GENERATION_INTERRUPTED")
        self.assertFalse(self.history().is_generating)
        self.assertFalse(any(e.event_name in ("message_done", "message_error") for e in self.events))

    async def test_event_consumer_disconnect_does_not_save_partial_answer(self):
        async def disconnect(item):
            if item.event_name == "message_delta":
                raise ConnectionError("private connection detail")
        with self.assertRaises(EventDeliveryError):
            await self.execute(on_event=disconnect)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0].generation_error_code, "GENERATION_INTERRUPTED")
        self.assertFalse(self.history().is_generating)

    async def test_same_session_busy_while_another_session_can_run(self):
        holding, release = asyncio.Event(), asyncio.Event()

        async def paused(item):
            if item.event_name == "reasoning_delta":
                holding.set()
                await release.wait()

        first = asyncio.create_task(self.execute(on_event=paused))
        try:
            await asyncio.wait_for(holding.wait(), 5)
            other_request = SendMessageRequest(client_message_key=str(uuid4()), content="Other question")
            with self.assertRaises(SessionBusyError):
                await self.execute(request=other_request)
            other_chat = await asyncio.to_thread(create_chat_session, self.user, self.factory)
            async def sink(_item):
                pass
            other = await self.execute(session_id=other_chat.session_id, request=other_request, on_event=sink)
            self.assertEqual(other.event_name, "message_done")
        finally:
            release.set()
            await first

    async def test_cross_user_session_rejected_without_model(self):
        stranger = self.user.model_copy(update={"user_id": "18446744073709551614"})
        with self.assertRaises(SessionNotFoundError):
            await self.execute(current_user=stranger)
        self.assertFalse(self.inputs)
        self.assertFalse(self.rows())

    async def test_success_commit_ack_lost_is_reconciled_not_overwritten(self):
        fired = False
        def lost_ack(session):
            nonlocal fired
            if not fired and any(isinstance(r, Message) and r.role == "ASSISTANT" for r in session.identity_map.values()):
                fired = True
                raise SQLAlchemyError("private lost acknowledgement")
        event.listen(Session, "after_commit", lost_ack)
        try:
            result = await self.execute()
        finally:
            event.remove(Session, "after_commit", lost_ack)
        self.assertTrue(fired)
        self.assertEqual(result.event_name, "message_done")
        self.assertEqual(self.rows()[0].generation_status, "SUCCEEDED")
        self.assertEqual(len(self.rows()), 2)

    async def test_answer_transaction_rollback_does_not_leave_success_or_answer(self):
        fired = False
        def reject_commit(session):
            nonlocal fired
            if not fired and any(isinstance(r, Message) and r.role == "ASSISTANT" for r in session.identity_map.values()):
                fired = True
                raise SQLAlchemyError("private failed commit")
        event.listen(Session, "before_commit", reject_commit)
        try:
            result = await self.execute()
        finally:
            event.remove(Session, "before_commit", reject_commit)
        self.assertTrue(fired)
        self.assertEqual(result.event_name, "message_error")
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0].generation_status, "FAILED")

    async def test_old_failed_and_replaced_attempts_cannot_save_late_answer(self):
        with accept_user_message(self.user, self.chat.session_id, self.request, self.factory, self.registry,
                                 check_new_message=self.check, input_policy=self.policy) as accepted:
            settle_generation(accepted, self.factory, self.registry, error_code="GENERATION_TIMEOUT")
            with self.assertRaises(StaleGenerationError):
                settle_generation(accepted, self.factory, self.registry, answer="late answer")
            new_attempt = str(uuid4())
            with self.registry.locked(int(self.chat.session_id)) as slot:
                with self.factory.begin() as session:
                    question = session.get(Message, int(accepted.user_message_id))
                    question.attempt_id = new_attempt
                    question.generation_status = "RUNNING"
                    question.generation_error_code = question.generation_error_message = None
                slot.release(accepted.attempt_id)
                slot.claim(new_attempt)
            with self.assertRaises(StaleGenerationError):
                settle_generation(accepted, self.factory, self.registry, answer="late answer")
            settle_generation(replace(accepted, attempt_id=new_attempt), self.factory, self.registry,
                              error_code="GENERATION_INTERRUPTED")
            with self.registry.locked(int(self.chat.session_id)) as slot:
                slot.release(new_attempt)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0].attempt_id, new_attempt)

    async def test_cancel_while_answer_commit_waits_keeps_committed_success(self):
        committing, release = threading.Event(), threading.Event()
        fired = False
        def pause_commit(session):
            nonlocal fired
            if not fired and any(isinstance(r, Message) and r.role == "ASSISTANT" for r in session.identity_map.values()):
                fired = True
                committing.set()
                if not release.wait(5):
                    raise RuntimeError("Test release did not arrive")
        event.listen(Session, "before_commit", pause_commit)
        task = asyncio.create_task(self.execute())
        try:
            async with asyncio.timeout(5):
                while not committing.is_set():
                    await asyncio.sleep(0.005)
            task.cancel()
            await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            event.remove(Session, "before_commit", pause_commit)
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self.assertEqual(self.rows()[0].generation_status, "SUCCEEDED")
        self.assertEqual(len(self.rows()), 2)
        self.assertFalse(self.history().is_generating)
        self.assertFalse(any(e.event_name == "message_done" for e in self.events))

    async def test_cancel_during_admission_commit_is_cleaned_not_left_running(self):
        committing, release = threading.Event(), threading.Event()
        fired = False
        def pause_commit(session):
            nonlocal fired
            if not fired and any(isinstance(r, Message) and r.role == "USER" for r in session.identity_map.values()):
                fired = True
                committing.set()
                if not release.wait(5):
                    raise RuntimeError("Test release did not arrive")
        event.listen(Session, "before_commit", pause_commit)
        task = asyncio.create_task(self.execute())
        try:
            async with asyncio.timeout(5):
                while not committing.is_set():
                    await asyncio.sleep(0.005)
            task.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            event.remove(Session, "before_commit", pause_commit)
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self.assertEqual(self.rows()[0].generation_error_code, "GENERATION_INTERRUPTED")
        self.assertFalse(self.registry._entries)
        self.assertFalse(self.inputs)
