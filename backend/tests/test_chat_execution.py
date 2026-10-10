"""执行器离线测试：假的数据库作用域/模型，不加载 .env，不发送 tracing。"""

import asyncio
from contextlib import contextmanager
from datetime import datetime
import threading
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from langchain_core.messages import AIMessageChunk

from app.core.async_work import complete_in_thread
from app.core.config import TracingSettings
from app.schemas import AssistantMessageResponse, DuplicateMessageResponse, GenerationError, SendMessageRequest, UserResponse
from app.services.chat_execution import EventDeliveryError, execute_chat_turn
from app.services.generation_registry import GenerationRegistry
from app.services.generation_result import GenerationOutcome
from app.services.message_submission import AcceptedMessage
from test_chat_agent_factory import prepared, settings


class FakeStreamAgent:
    def __init__(self, *, reason="stop", delay=0, error=False, no_answer=False, post_error=False):
        self.reason, self.delay, self.error = reason, delay, error
        self.no_answer, self.post_error = no_answer, post_error
        self.closed = False
        self.started = asyncio.Event()

    async def astream(self, *args, **kwargs):
        self.started.set()
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield AIMessageChunk(content="", additional_kwargs={"reasoning_content": "think"}), {"langgraph_node": "model"}
            if self.error:
                raise RuntimeError("private-model-secret")
            if not self.no_answer:
                yield AIMessageChunk(content="answer"), {"langgraph_node": "model"}
            yield AIMessageChunk(content="", response_metadata={"finish_reason": self.reason}), {"langgraph_node": "model"}
            if self.post_error:
                raise RuntimeError("Graph failed after model stopped")
        finally:
            self.closed = True


class ChatExecutionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.accepted = AcceptedMessage("1", "10", str(uuid4()), prepared())
        self.events = []
        self.cleaned = False
        self.worker_ids = []
        self.agent = FakeStreamAgent()
        self.answer = AssistantMessageResponse(message_id="11", role="ASSISTANT", content="answer",
                                               in_reply_to_message_id="10", created_at=datetime(2026, 10, 11))
        self.settlements = []

        @contextmanager
        def accept(*args, **kwargs):
            self.worker_ids.append(threading.get_ident())
            try:
                yield self.accepted
            finally:
                self.worker_ids.append(threading.get_ident())
                self.cleaned = True

        def settle(*args, **kwargs):
            self.worker_ids.append(threading.get_ident())
            self.settlements.append(kwargs)
            if "answer" in kwargs:
                return GenerationOutcome(assistant=self.answer)
            return GenerationOutcome(error=GenerationError(code=kwargs["error_code"], message="Safe failure"))

        for path, replacement in (("accept_user_message", accept), ("settle_generation", settle)):
            p = patch(f"app.services.chat_execution.{path}", side_effect=replacement)
            p.start()
            self.addCleanup(p.stop)
        self.build_patch = patch("app.services.chat_execution.build_chat_agent", side_effect=lambda *a: self.agent)
        self.build = self.build_patch.start()
        self.addCleanup(self.build_patch.stop)

    async def emit(self, item):
        if item.event_name in ("message_done", "message_error"):
            self.assertTrue(self.cleaned)
            self.assertTrue(self.agent.closed)
        self.events.append(item)

    async def run_turn(self, **kwargs):
        return await execute_chat_turn(
            UserResponse(user_id="1", email="a@example.com", display_name="A"), "1",
            SendMessageRequest(client_message_key=str(uuid4()), content="question"), MagicMock(), GenerationRegistry(),
            model_settings=settings(), tracing_settings=TracingSettings(_env_file=None, tracing=False, api_key=None),
            input_policy=prepared().policy, check_new_message=MagicMock(), on_event=kwargs.pop("on_event", self.emit), **kwargs,
        )

    async def test_success_event_order_and_only_answer_persisted(self):
        result = await self.run_turn()
        self.assertEqual(result.event_name, "message_done")
        self.assertEqual([getattr(e, "stage", e.event_name) for e in self.events], [
            "message_start", "context_ready", "agent_running", "thinking", "reasoning_delta",
            "answering", "message_delta", "saving", "message_done",
        ])
        self.assertEqual(self.settlements, [{"answer": "answer"}])
        self.assertTrue(all(i != threading.get_ident() for i in self.worker_ids))

    async def test_length_missing_finish_reason_and_reasoning_only_are_failures(self):
        for values in ({"reason": "length"}, {"reason": None}, {"no_answer": True}, {"post_error": True}):
            self.agent = FakeStreamAgent(**values)
            self.settlements.clear()
            result = await self.run_turn()
            self.assertEqual(result.event_name, "message_error")
            self.assertEqual(self.settlements, [{"error_code": "MODEL_REQUEST_FAILED"}])

    async def test_model_failure_is_safe_and_partial_output_not_saved(self):
        self.agent = FakeStreamAgent(error=True)
        result = await self.run_turn()
        self.assertNotIn("private-model-secret", result.model_dump_json())
        self.assertEqual(self.settlements, [{"error_code": "MODEL_REQUEST_FAILED"}])

    async def test_timeout_closes_stream_before_settling_and_terminal(self):
        self.agent = FakeStreamAgent(delay=10)
        result = await self.run_turn(timeout_seconds=0.03)
        self.assertEqual(result.error.code, "GENERATION_TIMEOUT")
        self.assertTrue(self.agent.closed)

    async def test_cancellation_cleans_without_terminal_or_success_save(self):
        self.agent = FakeStreamAgent(delay=10)
        task = asyncio.create_task(self.run_turn())
        await self.agent.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.agent.closed)
        self.assertTrue(self.cleaned)
        self.assertFalse(self.settlements)
        self.assertFalse(any(e.event_name in ("message_done", "message_error") for e in self.events))

    async def test_failed_event_delivery_aborts_and_cleans(self):
        async def broken(item):
            if item.event_name == "reasoning_delta":
                raise RuntimeError("private-sink-secret")
        with self.assertRaises(EventDeliveryError) as caught:
            await self.run_turn(on_event=broken)
        self.assertNotIn("private", str(caught.exception))
        self.assertTrue(self.agent.closed)
        self.assertTrue(self.cleaned)
        self.assertFalse(self.settlements)

    async def test_duplicate_has_no_model_or_events(self):
        self.accepted = DuplicateMessageResponse(duplicate=True, session_id="1", user_message_id="10",
                                                attempt_id=str(uuid4()), status="RUNNING", assistant_message_id=None)
        self.assertIs(await self.run_turn(), self.accepted)
        self.build.assert_not_called()
        self.assertFalse(self.events)

    async def test_uncertain_commit_reconciled_as_success(self):
        from app.services.errors import MessageSendUnavailableError
        with patch("app.services.chat_execution.settle_generation", side_effect=[
            MessageSendUnavailableError(), GenerationOutcome(assistant=self.answer),
        ]) as settle:
            result = await self.run_turn()
        self.assertEqual(result.event_name, "message_done")
        self.assertEqual(settle.call_count, 2)

    async def test_invalid_timeout_rejected_before_admission(self):
        with self.assertRaises(ValueError):
            await self.run_turn(timeout_seconds=float("inf"))
        self.assertFalse(self.cleaned)
        self.build.assert_not_called()

    async def test_no_reasoning_means_no_fake_thinking_stage(self):
        class AnswerOnly:
            closed = False
            async def astream(inner, *args, **kwargs):
                try:
                    yield AIMessageChunk(content="answer", response_metadata={"finish_reason": "stop"}), {"langgraph_node": "model"}
                finally:
                    inner.closed = True
        self.agent = AnswerOnly()
        await self.run_turn()
        self.assertFalse(any(e.event_name == "reasoning_delta" or getattr(e, "stage", None) == "thinking" for e in self.events))

    async def test_oversized_output_rejected_without_saving_answer(self):
        class Oversized:
            closed = False
            async def astream(inner, *args, **kwargs):
                try:
                    yield AIMessageChunk(content="x" * 131073), {"langgraph_node": "model"}
                finally:
                    inner.closed = True
        self.agent = Oversized()
        result = await self.run_turn()
        self.assertEqual(result.event_name, "message_error")
        self.assertEqual(self.settlements, [{"error_code": "MODEL_REQUEST_FAILED"}])

    async def test_repeated_cancel_waits_for_stream_close_before_scope_cleanup(self):
        closing, release, started = asyncio.Event(), asyncio.Event(), asyncio.Event()
        class SlowClose:
            closed = False
            async def astream(inner, *args, **kwargs):
                try:
                    yield AIMessageChunk(content="answer"), {"langgraph_node": "model"}
                finally:
                    closing.set()
                    await release.wait()
                    inner.closed = True
        self.agent = SlowClose()
        async def sink(item):
            if item.event_name == "message_delta":
                started.set()
                await asyncio.sleep(10)
        task = asyncio.create_task(self.run_turn(on_event=sink))
        await started.wait()
        task.cancel()
        await closing.wait()
        task.cancel()
        await asyncio.sleep(0.01)
        self.assertFalse(self.cleaned)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.agent.closed)
        self.assertTrue(self.cleaned)


class AsyncWorkTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_waits_for_thread_before_propagating_even_when_cancelled_twice(self):
        started, release, ended = threading.Event(), threading.Event(), threading.Event()

        def work():
            started.set()
            release.wait(2)
            ended.set()

        task = asyncio.create_task(complete_in_thread(work))
        while not started.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.sleep(0.005)
        task.cancel()
        await asyncio.sleep(0.005)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(ended.is_set())
