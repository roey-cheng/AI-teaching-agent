"""真实 HTTP 编码与 ASGI 流生命周期测试；不连接数据库或供应商。"""

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.agent.input_policy import policy_for_model
from app.api.chat_messages import ChatConfiguration, load_chat_configuration
from app.api.errors import APIError
from app.api.sse import ChatStreamResponse, encode_event
from app.core.config import ModelSettings, TracingSettings
from app.core.http_config import HTTPSettings
from app.main import create_app
from app.schemas import (
    AgentProgressData, AssistantMessageResponse, DuplicateMessageResponse, MessageDeltaData,
    MessageDoneData, MessageErrorData, MessageStartData, ReasoningDeltaData, StreamError, UserResponse,
)
from app.services import errors
from app.services.generation_limit import GenerationRateLimiter
from app.services.generation_registry import GenerationRegistry
from app.services.startup_cleanup import StartupCleanupResult

ATTEMPT = str(uuid4())
START = MessageStartData(session_id="42", user_message_id="1", attempt_id=ATTEMPT)
DONE = MessageDoneData(attempt_id=ATTEMPT, assistant_message=AssistantMessageResponse(
    message_id="2", role="ASSISTANT", content="你好\nWorld", in_reply_to_message_id="1", created_at=datetime.now(UTC)))


def configuration():
    model = ModelSettings(_env_file=None, provider="deepseek", name="deepseek-v4-pro", api_key="fixture-only")
    return ChatConfiguration(model, TracingSettings(_env_file=None, tracing=False, api_key=None), policy_for_model(model.name))


class ChatHTTPTest(unittest.TestCase):
    def setUp(self):
        self.runtime = SimpleNamespace(session_factory=object(), registry=GenerationRegistry(),
                                       limiter=GenerationRateLimiter(), cleanup=StartupCleanupResult())

        @contextmanager
        def runtime():
            yield self.runtime

        self.client = self.enterContext(TestClient(create_app(runtime_factory=runtime,
            http_settings=HTTPSettings(_env_file=None, allowed_origins=["http://localhost:5173"]))))
        self.headers = {"Origin": "http://localhost:5173"}
        self.user = UserResponse(user_id="123", email="student@example.com", display_name="Student")
        self.auth = self.enterContext(patch("app.api.dependencies.get_current_user", return_value=self.user))
        self.config = self.enterContext(patch("app.api.chat_messages.load_chat_configuration", return_value=configuration()))
        self.send_path = "/api/v1/chat/sessions/42/messages"
        self.retry_path = self.send_path + "/1/retry"
        self.body = {"client_message_key": str(uuid4()), "content": "Explain Python"}

    def post(self, path=None, **kwargs):
        kwargs.setdefault("json", self.body)
        kwargs.setdefault("headers", self.headers)
        return self.client.post(path or self.send_path, **kwargs)

    async def execute(self, *args, **kwargs):
        hook = kwargs.get("check_new_message", kwargs.get("check_retry"))
        hook(args[2])
        for event in (START, AgentProgressData(attempt_id=ATTEMPT, stage="memory_checking"),
                      ReasoningDeltaData(attempt_id=ATTEMPT, text="API reasoning"),
                      MessageDeltaData(attempt_id=ATTEMPT, text="你好\nWorld"), DONE):
            await kwargs["on_event"](event)
        return DONE

    def test_send_sse_headers_event_order_and_shared_runtime(self):
        with patch("app.api.chat_messages.execute_chat_turn", side_effect=self.execute) as execute:
            response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        self.assertEqual(response.headers["cache-control"], "no-cache, no-store")
        self.assertEqual(response.headers["x-accel-buffering"], "no")
        self.assertNotIn("content-length", response.headers)
        blocks = response.text.strip().split("\n\n")
        self.assertEqual([b.splitlines()[0] for b in blocks], ["event: " + e for e in (
            "message_start", "agent_progress", "reasoning_delta", "message_delta", "message_done")])
        self.assertEqual(json.loads(blocks[3].split("data: ")[1])["text"], "你好\nWorld")
        args = execute.call_args.args
        self.assertEqual(args[0:2], (self.user, "42"))
        self.assertIs(args[3], self.runtime.session_factory)
        self.assertIs(args[4], self.runtime.registry)

    def test_retry_uses_original_message_and_same_rate_limiter(self):
        with patch("app.api.chat_messages.execute_chat_retry", side_effect=self.execute) as execute:
            response = self.post(self.retry_path, json={"failed_attempt_id": ATTEMPT})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(execute.call_args.args[1:3], ("42", "1"))
        self.assertEqual(execute.call_args.args[3].failed_attempt_id, ATTEMPT)
        for _ in range(9):
            self.runtime.limiter.check("123")
        with patch("app.api.chat_messages.execute_chat_turn", side_effect=self.execute):
            limited = self.post()
        self.assertEqual(limited.status_code, 429)
        self.assertIn("retry-after", limited.headers)

    def test_duplicate_is_json_not_sse_and_does_not_require_event(self):
        receipt = DuplicateMessageResponse(duplicate=True, session_id="42", user_message_id="1",
            attempt_id=ATTEMPT, status="SUCCEEDED", assistant_message_id="2")
        for path, function, body in ((self.send_path, "execute_chat_turn", self.body),
                                      (self.retry_path, "execute_chat_retry", {"failed_attempt_id": ATTEMPT})):
            with patch("app.api.chat_messages." + function, new=AsyncMock(return_value=receipt)):
                response = self.post(path, json=body)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.headers["content-type"].startswith("application/json"))
            self.assertEqual(response.json(), receipt.model_dump())

    def test_pre_stream_errors_use_correct_http_status_and_safe_json(self):
        cases = [(errors.SessionNotFoundError(), 404), (errors.MessageNotFoundError(), 404),
                 (errors.SessionBusyError(), 409), (errors.IdempotencyConflictError(), 409),
                 (errors.RetryNotAllowedError(), 409), (errors.StaleAttemptError(), 409),
                 (errors.ContextTooLargeError(), 422), (errors.GenerationRateLimitError(12), 429),
                 (errors.MessageSendUnavailableError(), 503), (errors.AgentInputUnavailableError(), 503)]
        for error, status in cases:
            with patch("app.api.chat_messages.execute_chat_turn", new=AsyncMock(side_effect=error)):
                response = self.post()
            self.assertEqual(response.status_code, status, response.text)
            self.assertEqual(response.json()["error"]["code"], error.code)
            self.assertNotIn("event:", response.text)

    def test_model_failure_event_uses_http_request_id(self):
        async def failure(*args, on_event, **kwargs):
            await on_event(START)
            result = MessageErrorData(attempt_id=ATTEMPT, error=StreamError(
                code="MODEL_REQUEST_FAILED", message="Failed to generate a response.", request_id="old-id"))
            await on_event(result)
            return result
        with patch("app.api.chat_messages.execute_chat_turn", side_effect=failure):
            response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: message_error", response.text)
        self.assertIn(response.headers["x-request-id"], response.text)
        self.assertNotIn("old-id", response.text)

    def test_uncertain_settlement_closes_without_false_terminal_or_private_error(self):
        async def uncertain(*args, on_event, **kwargs):
            await on_event(START)
            raise RuntimeError("private-database-password")
        with patch("app.api.chat_messages.execute_chat_turn", side_effect=uncertain), self.assertLogs("app.api.sse"):
            response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: message_start", response.text)
        self.assertNotIn("private", response.text)
        self.assertNotIn("event: message_error", response.text)
        self.assertNotIn("event: message_done", response.text)

    def test_auth_origin_schema_queries_and_body_limits_before_execution(self):
        with patch("app.api.chat_messages.execute_chat_turn") as execute:
            self.assertEqual(self.post(headers={}).status_code, 403)
            self.assertEqual(self.post(json={"content": "missing-key"}).status_code, 422)
            self.assertEqual(self.post(json={**self.body, "model": "untrusted"}).status_code, 422)
            self.assertEqual(self.post(json={**self.body, "content": "  "}).status_code, 422)
            self.assertEqual(self.post(self.send_path + "?user_id=9").status_code, 422)
            self.assertEqual(self.post(self.retry_path, json={"failed_attempt_id": "bad"}).status_code, 422)
            self.assertEqual(self.post(self.send_path.replace("42", "0")).status_code, 422)
            response = self.client.post(self.send_path, content="x" * 262145,
                headers={**self.headers, "Content-Type": "application/json"})
            self.assertEqual(response.status_code, 413)
            self.auth.side_effect = errors.AuthenticationRequiredError()
            self.assertEqual(self.post().status_code, 401)
            self.assertEqual(self.post(self.retry_path, json={"failed_attempt_id": ATTEMPT}).status_code, 401)
        execute.assert_not_called()
        self.config.assert_not_called()

    def test_20000_unicode_characters_not_blocked_by_old_16k_limit(self):
        with patch("app.api.chat_messages.execute_chat_turn", side_effect=self.execute):
            response = self.post(json={**self.body, "content": "🙂" * 20000})
        self.assertEqual(response.status_code, 200)

    def test_bad_configuration_is_503_before_saving_or_streaming(self):
        self.config.side_effect = APIError(503, "MODEL_CONFIGURATION_UNAVAILABLE", "Chat model configuration is unavailable.")
        with patch("app.api.chat_messages.execute_chat_turn") as execute:
            response = self.post()
        self.assertEqual(response.status_code, 503)
        execute.assert_not_called()
        with patch("app.api.chat_messages.load_model_settings", side_effect=ValueError("secret-key")):
            with self.assertRaises(APIError) as caught:
                load_chat_configuration()
        self.assertNotIn("secret-key", str(caught.exception))

    def test_openapi_documents_both_sse_and_duplicate_json(self):
        spec = self.client.get("/openapi.json").json()
        for path in ("/api/v1/chat/sessions/{session_id}/messages",
                     "/api/v1/chat/sessions/{session_id}/messages/{message_id}/retry"):
            content = spec["paths"][path]["post"]["responses"]["200"]["content"]
            self.assertIn("text/event-stream", content)
            self.assertIn("application/json", content)


class SSETransportTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.incoming = asyncio.Queue()
        self.output = []

    async def send(self, message):
        self.output.append(message)

    async def run_response(self, execute, **kwargs):
        response = ChatStreamResponse(execute, request_id="req_test", **kwargs)
        await response({"type": "http"}, self.incoming.get, self.send)

    async def test_incremental_delivery_and_ping_before_execution_finishes(self):
        received = asyncio.Event()
        original = self.send
        async def send(message):
            await original(message)
            if message.get("body") == b": ping\n\n":
                received.set()
        self.send = send
        async def execute(emit):
            await emit(START)
            await emit(MessageDeltaData(attempt_id=ATTEMPT, text="First part"))
            await asyncio.wait_for(received.wait(), 1)
            self.assertTrue(any(b"First part" in m.get("body", b"") for m in self.output))
            await emit(DONE)
            return DONE
        await self.run_response(execute, heartbeat_seconds=0.01)

    async def test_disconnect_cancels_execution_and_waits_for_cleanup(self):
        cleanup_started, allow_cleanup = asyncio.Event(), asyncio.Event()
        async def execute(emit):
            try:
                await emit(START)
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await allow_cleanup.wait()
        response = asyncio.create_task(self.run_response(execute))
        await self.incoming.put({"type": "http.disconnect"})
        await asyncio.wait_for(cleanup_started.wait(), 1)
        self.assertFalse(response.done())
        allow_cleanup.set()
        await asyncio.wait_for(response, 1)

    async def test_cancellation_repeated_during_cleanup_still_waits(self):
        active, cleanup_started, allow_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def execute(emit):
            active.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await allow_cleanup.wait()
        response = asyncio.create_task(self.run_response(execute))
        await active.wait()
        response.cancel()
        await asyncio.wait_for(cleanup_started.wait(), 1)
        response.cancel()
        await asyncio.sleep(0)
        self.assertFalse(response.done())
        allow_cleanup.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(response, 1)

    async def test_backpressure_bounded_and_send_timeout_cleans_producer(self):
        count = 0
        closed = asyncio.Event()
        async def blocked_send(message):
            await asyncio.Event().wait()
        self.send = blocked_send
        async def execute(emit):
            nonlocal count
            try:
                await emit(START)
                for _ in range(1000):
                    await emit(MessageDeltaData(attempt_id=ATTEMPT, text="piece"))
                    count += 1
            finally:
                closed.set()
        await asyncio.wait_for(self.run_response(execute, queue_size=2, send_timeout_seconds=0.02), 1)
        self.assertTrue(closed.is_set())
        self.assertLessEqual(count, 2)

    async def test_event_text_cannot_inject_another_event(self):
        payload = "\n\nevent: message_done\ndata: evil\r\n"
        frame = encode_event(MessageDeltaData(attempt_id=ATTEMPT, text=payload), "req_test")
        self.assertEqual(frame.count(b"\n\n"), 1)
        self.assertEqual(json.loads(frame.decode().split("data: ", 1)[1])["text"], payload)
        with self.assertRaises(ValueError):
            encode_event(SimpleNamespace(event_name="tool_call"), "req_test")

    async def test_unexpected_executor_cancellation_does_not_hang(self):
        async def execute(emit):
            raise asyncio.CancelledError
        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(self.run_response(execute), 1)

    async def test_shutdown_cleans_stream_before_releasing_runtime(self):
        order = []
        active = asyncio.Event()

        @contextmanager
        def runtime():
            try:
                yield SimpleNamespace(cleanup=StartupCleanupResult())
            finally:
                order.append("runtime_closed")

        app = create_app(runtime_factory=runtime, http_settings=HTTPSettings(_env_file=None))
        async def execute(emit):
            active.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                order.append("execution_cleaned")
        async with app.router.lifespan_context(app):
            response = ChatStreamResponse(execute, request_id="req_test",
                                          active_requests=app.state.active_stream_requests)
            task = asyncio.create_task(response({"type": "http"}, self.incoming.get, self.send))
            await active.wait()
            self.assertEqual(len(app.state.active_stream_requests), 1)
        self.assertTrue(task.done())
        self.assertEqual(order, ["execution_cleaned", "runtime_closed"])
        self.assertFalse(app.state.active_stream_requests)
