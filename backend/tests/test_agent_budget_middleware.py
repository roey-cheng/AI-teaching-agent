"""使用假的 handler 测试调用前检查；不调用任何供应商。"""

from dataclasses import replace
import unittest
from unittest.mock import AsyncMock, Mock

from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agent.budget_middleware import AgentToolUseNotAllowedError, ChatMemoryBudgetMiddleware
from app.agent.input_policy import policy_for_model
from app.agent.memory_backend import PROFILE_MEMORY_PATH, ProfileMemoryBackend
from app.services.errors import ContextTooLargeError


def middleware_and_request(policy=None, memory="喜欢中文"):
    policy = policy or policy_for_model("deepseek-v4-pro")
    middleware = ChatMemoryBudgetMiddleware(ProfileMemoryBackend(memory), policy)
    request = ModelRequest(
        model=FakeListChatModel(responses=["unused"]), messages=[HumanMessage(content="question")],
        system_message=SystemMessage(content=policy.system_prompt),
        tools=[{"name": "execute"}, {"name": "task"}],
        state={"messages": [], "memory_contents": {PROFILE_MEMORY_PATH: memory}},
        model_settings={"max_tokens": 99},
    )
    return middleware, request


class ChatBudgetTest(unittest.TestCase):
    def test_memory_included_once_tools_hidden_output_limit_preserved(self):
        middleware, request = middleware_and_request()
        handler = Mock(return_value=ModelResponse(result=[AIMessage(content="answer")]))
        response = middleware.wrap_model_call(request, handler)
        actual = handler.call_args.args[0]
        self.assertEqual(actual.system_message.text.count("喜欢中文"), 1)
        self.assertIn("untrusted", actual.system_message.text)
        self.assertEqual(actual.tools, [])
        self.assertEqual(actual.model_settings["max_tokens"], 4096)
        self.assertEqual(len(request.tools), 2)  # override 不修改原请求。
        self.assertEqual(response.result[0].content, "answer")

    def test_oversized_memory_is_checked_after_injection_before_handler(self):
        middleware, request = middleware_and_request(memory="中" * 30_000)
        handler = Mock()
        with self.assertRaises(ContextTooLargeError):
            middleware.wrap_model_call(request, handler)
        handler.assert_not_called()

    def test_each_call_checks_framework_prompt_and_messages(self):
        for where in ("system", "messages"):
            middleware, request = middleware_and_request()
            handler = Mock(return_value=ModelResponse(result=[AIMessage(content="ok")]))
            middleware.wrap_model_call(request, handler)
            if where == "system":
                request = request.override(system_message=SystemMessage(content="framework" * 10_000))
            else:
                request = request.override(messages=[HumanMessage(content="user" * 20_000)])
            with self.subTest(where=where), self.assertRaises(ContextTooLargeError):
                middleware.wrap_model_call(request, handler)
            self.assertEqual(handler.call_count, 1)

    def test_fake_model_tool_call_is_rejected(self):
        middleware, request = middleware_and_request()
        answer = AIMessage(content="", tool_calls=[{"name": "task", "args": {"private": "data"}, "id": "call1"}])
        with self.assertRaises(AgentToolUseNotAllowedError) as caught:
            middleware.wrap_model_call(request, Mock(return_value=ModelResponse(result=[answer])))
        self.assertNotIn("private", str(caught.exception))

    def test_tool_execution_handler_is_never_called(self):
        middleware, _ = middleware_and_request()
        handler = Mock()
        with self.assertRaises(AgentToolUseNotAllowedError):
            middleware.wrap_tool_call(Mock(), handler)
        handler.assert_not_called()

    def test_caller_cannot_supply_another_memory(self):
        middleware, _ = middleware_and_request(memory="bound profile")
        result = middleware.before_agent({"memory_contents": {PROFILE_MEMORY_PATH: "forged"}}, None, {})
        self.assertEqual(result["memory_contents"], {PROFILE_MEMORY_PATH: "bound profile"})

    def test_small_valid_policy_rejects_real_request(self):
        policy = replace(policy_for_model("deepseek-v4-pro"), app_context_tokens=300,
                         output_tokens=20, framework_reserve_tokens=0, tool_result_reserve_tokens=0, safety_tokens=0)
        middleware, request = middleware_and_request(policy)
        with self.assertRaises(ContextTooLargeError):
            middleware.modify_request(request)


class AsyncChatBudgetTest(unittest.IsolatedAsyncioTestCase):
    async def test_async_read_budget_and_tool_checks(self):
        middleware, request = middleware_and_request()
        loaded = await middleware.abefore_agent({"memory_contents": {}}, None, {})
        self.assertIn("喜欢中文", loaded["memory_contents"][PROFILE_MEMORY_PATH])
        handler = AsyncMock(return_value=ModelResponse(result=[AIMessage(content="ok")]))
        await middleware.awrap_model_call(request, handler)
        self.assertEqual(handler.call_args.args[0].tools, [])
        with self.assertRaises(ContextTooLargeError):
            await middleware.awrap_model_call(request.override(messages=[HumanMessage(content="x" * 70_000)]), handler)
        self.assertEqual(handler.await_count, 1)
        tool_handler = AsyncMock()
        with self.assertRaises(AgentToolUseNotAllowedError):
            await middleware.awrap_tool_call(Mock(), tool_handler)
        tool_handler.assert_not_awaited()
