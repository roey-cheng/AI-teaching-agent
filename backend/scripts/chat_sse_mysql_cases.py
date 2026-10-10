"""HTTP + SSE + 真 Deep Agents 图 + 临时 MySQL；只替换供应商，不花 API 余额。"""

import asyncio
import json
from unittest.mock import patch
from uuid import uuid4

from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk

from app.agent.input_policy import policy_for_model
from app.api.chat_messages import ChatConfiguration
from app.core.config import ModelSettings, TracingSettings
from resource_http_mysql_cases import ResourceHTTPFixture


class ChatSSEMySQLTest(ResourceHTTPFixture):
    def setUp(self):
        super().setUp()
        model = ModelSettings(_env_file=None, provider="deepseek", name="deepseek-v4-pro", api_key="fixture-only")
        config = ChatConfiguration(model, TracingSettings(_env_file=None, tracing=False, api_key=None), policy_for_model(model.name))
        self.enterContext(patch("app.api.chat_messages.load_chat_configuration", return_value=config))
        self.calls = 0
        self.fail = False
        self.wait_after_delta = False
        self.closed = False

        async def provider(_model, messages, **kwargs):
            self.calls += 1
            if kwargs.get("tools"):
                yield ChatGenerationChunk(message=AIMessageChunk(content="No memory update",
                                                                 response_metadata={"finish_reason": "stop"}))
                return
            try:
                if self.fail:
                    raise RuntimeError("private-provider-secret")
                yield ChatGenerationChunk(message=AIMessageChunk(content="", additional_kwargs={"reasoning_content": "Visible reasoning"}))
                yield ChatGenerationChunk(message=AIMessageChunk(content="Hello "))
                if self.wait_after_delta:
                    await asyncio.Event().wait()
                yield ChatGenerationChunk(message=AIMessageChunk(content="世界"))
                yield ChatGenerationChunk(message=AIMessageChunk(content="", response_metadata={"finish_reason": "stop"}))
            finally:
                self.closed = True

        self.enterContext(patch("langchain_deepseek.ChatDeepSeek._astream", provider))
        self.chat = self.create()["session_id"]
        self.path = f"/api/v1/chat/sessions/{self.chat}/messages"
        self.body = {"client_message_key": str(uuid4()), "content": "Explain Python"}

    def send_message(self, **changes):
        return self.client.post(self.path, json={**self.body, **changes}, headers=self.headers)

    def events(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("text/event-stream", response.headers["content-type"])
        return [(block.splitlines()[0][7:], json.loads(block.split("data: ", 1)[1]))
                for block in response.text.strip().split("\n\n") if not block.startswith(":")]

    def test_real_agent_stream_saves_answer_then_duplicate_returns_json(self):
        response = self.send_message()
        events = self.events(response)
        self.assertEqual(events[0][0], "message_start")
        self.assertEqual(events[-1][0], "message_done")
        self.assertIn("reasoning_delta", [name for name, _ in events])
        self.assertEqual("".join(data["text"] for name, data in events if name == "message_delta"), "Hello 世界")
        history = self.client.get(self.path).json()
        self.assertFalse(history["is_generating"])
        self.assertEqual([i["role"] for i in history["items"]], ["USER", "ASSISTANT"])
        self.assertEqual(history["items"][1]["content"], "Hello 世界")
        self.assertNotIn("Visible reasoning", json.dumps(history))
        calls = self.calls
        duplicate = self.send_message()
        self.assertTrue(duplicate.json()["duplicate"])
        self.assertEqual(duplicate.json()["status"], "SUCCEEDED")
        self.assertEqual(self.calls, calls)
        self.assertEqual(self.send_message(content="Changed text").status_code, 409)

    def test_failed_stream_retry_reuses_user_message_and_deduplicates(self):
        self.fail = True
        failed = self.send_message()
        events = self.events(failed)
        self.assertEqual(events[-1][0], "message_error")
        self.assertNotIn("private-provider-secret", failed.text)
        self.assertEqual(events[-1][1]["error"]["request_id"], failed.headers["x-request-id"])
        question = self.client.get(self.path).json()["items"][0]
        self.assertTrue(question["generation"]["can_retry"])
        original_attempt = question["generation"]["attempt_id"]
        retry_path = self.path + "/" + question["message_id"] + "/retry"
        retry_body = {"failed_attempt_id": original_attempt}
        self.fail = False
        retried = self.client.post(retry_path, json=retry_body, headers=self.headers)
        self.assertEqual(self.events(retried)[-1][0], "message_done")
        history = self.client.get(self.path).json()
        self.assertEqual(len(history["items"]), 2)
        self.assertEqual(history["items"][0]["message_id"], question["message_id"])
        self.assertNotEqual(history["items"][0]["generation"]["attempt_id"], original_attempt)
        calls = self.calls
        duplicate = self.client.post(retry_path, json=retry_body, headers=self.headers)
        self.assertTrue(duplicate.json()["duplicate"])
        self.assertEqual(self.calls, calls)
        # 成功答案不支持重新生成；使用当前编号也不能绕过。
        denied = self.client.post(retry_path, json={"failed_attempt_id": history["items"][0]["generation"]["attempt_id"]}, headers=self.headers)
        self.assertEqual(denied.status_code, 409)

    def test_cross_user_send_and_retry_404_without_calling_model(self):
        question_id, attempt = self.question(self.chat)
        self.account()
        for response in (self.send_message(), self.client.post(self.path + f"/{question_id}/retry",
                            json={"failed_attempt_id": attempt}, headers=self.headers)):
            self.assertEqual(response.status_code, 404, response.text)
            self.assertEqual(response.json()["error"]["code"], "SESSION_NOT_FOUND")
        self.assertEqual(self.calls, 0)

    def test_rate_rejection_creates_no_message(self):
        limiter = self.client.app.state.runtime.limiter
        for _ in range(10):
            limiter.check(str(self.user_id))
        response = self.send_message()
        self.assertEqual(response.status_code, 429)
        self.assertIn("retry-after", response.headers)
        self.assertEqual(self.client.get(self.path).json()["items"], [])
        self.assertEqual(self.calls, 0)

    def test_http_disconnect_cancels_real_graph_marks_failed_and_releases(self):
        self.wait_after_delta = True

        async def run():
            incoming = asyncio.Queue()
            await incoming.put({"type": "http.request", "body": json.dumps(self.body).encode(), "more_body": False})
            output = []

            async def send(message):
                output.append(message)
                if b"event: message_delta" in message.get("body", b""):
                    await incoming.put({"type": "http.disconnect"})

            scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
                     "http_version": "1.1", "method": "POST", "scheme": "http", "path": self.path,
                     "raw_path": self.path.encode(), "query_string": b"", "root_path": "",
                     "server": ("testserver", 80), "client": ("127.0.0.1", 12345),
                     "headers": [(b"content-type", b"application/json"), (b"origin", b"http://localhost:5173"),
                                 (b"cookie", ("chat_session=" + self.token).encode())]}
            await asyncio.wait_for(self.client.app(scope, incoming.get, send), 5)
            self.assertTrue(any(b"Hello " in item.get("body", b"") for item in output))

        asyncio.run(run())
        self.assertTrue(self.closed)
        history = self.client.get(self.path).json()
        self.assertFalse(history["is_generating"])
        self.assertEqual(len(history["items"]), 1)
        self.assertEqual(history["items"][0]["generation"]["status"], "FAILED")
        self.assertEqual(history["items"][0]["generation"]["error"]["code"], "GENERATION_INTERRUPTED")
        self.assertTrue(history["items"][0]["generation"]["can_retry"])
