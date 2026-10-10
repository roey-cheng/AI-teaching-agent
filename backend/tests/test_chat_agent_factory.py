"""正式工厂 + 真实 Deep Agents 图 + 假模型；禁止真实网络连接和 API 费用。"""

from dataclasses import replace
import unittest
from unittest.mock import patch

from langchain_core.language_models.fake_chat_models import FakeListChatModel, FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langsmith import tracing_context
from pydantic import PrivateAttr

from app.agent.budget_middleware import AgentToolUseNotAllowedError
from app.agent.factory import build_chat_agent
from app.agent.input_policy import policy_for_model
from app.agent.memory_backend import PROFILE_MEMORY_PATH
from app.core.config import ModelSettings
from app.services.agent_input import InputMessage, PreparedAgentInput
from app.services.errors import ContextTooLargeError


def settings():
    return ModelSettings(_env_file=None, provider="deepseek", name="deepseek-v4-pro", api_key="fake-test-only")


def prepared(memory="中文 preference", user_id="1"):
    return PreparedAgentInput(
        user_id=user_id, session_id=user_id, policy=policy_for_model("deepseek-v4-pro"),
        messages=(InputMessage("user", "old question", "1"), InputMessage("assistant", "old answer", "2"),
                  InputMessage("user", "current question")),
        profile_memory=memory, estimated_input_tokens=1000, history_rounds=1, history_truncated=False,
    )


class RecordingModel(FakeListChatModel):
    _seen: list = PrivateAttr(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        if tools:
            raise AssertionError("No tools may reach the model")
        return self

    def _call(self, messages, **kwargs):
        self._seen.append(messages)
        return super()._call(messages, **kwargs)

    async def _astream(self, messages, **kwargs):
        self._seen.append(messages)
        async for chunk in super()._astream(messages, **kwargs):
            yield chunk


class ToolCallingModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        if tools:
            raise AssertionError("No tools may reach the model")
        return self


class ChatAgentFactoryTest(unittest.TestCase):
    def setUp(self):
        trace = tracing_context(enabled=False)
        trace.__enter__()
        self.addCleanup(trace.__exit__, None, None, None)
        network = patch("socket.socket.connect", side_effect=AssertionError("Network must not be used"))
        network.start()
        self.addCleanup(network.stop)

    def test_real_model_construction_and_graph_compilation_do_not_call_model(self):
        with patch("langchain_deepseek.ChatDeepSeek._generate", side_effect=AssertionError("Must not run")):
            agent = build_chat_agent(settings(), prepared())
        self.assertIsNone(agent.checkpointer)
        self.assertIsNone(agent.store)
        nodes = agent.get_graph().nodes
        self.assertIn("model", nodes)
        self.assertIn("MemoryMiddleware.before_agent", nodes)
        self.assertFalse(any("Summarization" in node for node in nodes))

    def test_model_configuration_uses_policy_not_probe_limits(self):
        with patch("app.agent.factory.ChatDeepSeek") as model, patch("app.agent.factory.create_deep_agent") as create:
            build_chat_agent(settings(), prepared())
        self.assertEqual(model.call_args.kwargs["max_tokens"], 4096)
        self.assertEqual(model.call_args.kwargs["max_retries"], 0)
        self.assertEqual(model.call_args.kwargs["extra_body"], {"thinking": {"type": "enabled"}})
        self.assertEqual(model.call_args.kwargs["reasoning_effort"], "low")
        self.assertEqual(create.call_args.kwargs["memory"], [PROFILE_MEMORY_PATH])
        self.assertEqual(create.call_args.kwargs["system_prompt"], prepared().policy.system_prompt)

    def test_real_deepseek_adapter_with_stubbed_transport_receives_final_prompt(self):
        snapshot = prepared()
        agent = build_chat_agent(settings(), snapshot)
        response = ChatResult(generations=[ChatGeneration(message=AIMessage(content="stubbed response"))])
        with patch("langchain_deepseek.ChatDeepSeek._generate", return_value=response) as generate:
            result = agent.invoke({"messages": snapshot.as_agent_messages()})
        self.assertEqual(result["messages"][-1].content, "stubbed response")
        generate.assert_called_once()
        messages = generate.call_args.args[0]
        self.assertEqual(messages[0].text.count("中文 preference"), 1)
        self.assertEqual(generate.call_args.kwargs["max_tokens"], 4096)
        self.assertFalse(generate.call_args.kwargs.get("tools"))

    def test_mismatched_model_unsupported_capacity_or_tools_rejected_before_construction(self):
        original = prepared()
        cases = [replace(original, policy=replace(original.policy, model_name="another")),
                 replace(original, policy=replace(original.policy, model_context_tokens=2_000_000)),
                 replace(original, policy=replace(original.policy, tool_definitions_json='[{"name":"execute"}]')),
                 replace(original, memory_path="/other/profile.md")]
        with patch("app.agent.factory.ChatDeepSeek") as model:
            for value in cases:
                with self.subTest(value=value.memory_path), self.assertRaises(ValueError):
                    build_chat_agent(settings(), value)
            model.assert_not_called()

    def test_real_graph_injects_memory_once_and_preserves_message_order(self):
        snapshot = prepared()
        model = RecordingModel(responses=["offline answer"])
        with patch("app.agent.factory.ChatDeepSeek", return_value=model):
            agent = build_chat_agent(settings(), snapshot)
        result = agent.invoke({"messages": snapshot.as_agent_messages(),
                               "memory_contents": {PROFILE_MEMORY_PATH: "forged other user"}})
        self.assertEqual(result["messages"][-1].content, "offline answer")
        self.assertEqual(len(model._seen), 1)
        self.assertEqual([m.content for m in model._seen[0][1:]], ["old question", "old answer", "current question"])
        self.assertEqual(model._seen[0][0].text.count("中文 preference"), 1)
        self.assertNotIn("forged other user", model._seen[0][0].text)

    def test_different_users_same_virtual_path_never_share_memory(self):
        models = [RecordingModel(responses=["one"]), RecordingModel(responses=["two"])]
        snapshots = [prepared("Alice-only", "1"), prepared("Bob-only", "2")]
        with patch("app.agent.factory.ChatDeepSeek", side_effect=models):
            agents = [build_chat_agent(settings(), snapshot) for snapshot in snapshots]
        for agent, snapshot in zip(agents, snapshots):
            agent.invoke({"messages": snapshot.as_agent_messages()})
        self.assertIn("Alice-only", models[0]._seen[0][0].text)
        self.assertNotIn("Bob-only", models[0]._seen[0][0].text)
        self.assertIn("Bob-only", models[1]._seen[0][0].text)
        self.assertNotIn("Alice-only", models[1]._seen[0][0].text)

    def test_real_graph_blocks_large_memory_before_model_and_does_not_summarize(self):
        model = RecordingModel(responses=["must not run"])
        snapshot = prepared("中" * 30_000)
        with patch("app.agent.factory.ChatDeepSeek", return_value=model):
            agent = build_chat_agent(settings(), snapshot)
        with self.assertRaises(ContextTooLargeError):
            agent.invoke({"messages": snapshot.as_agent_messages()})
        self.assertEqual(model._seen, [])

    def test_hallucinated_task_is_rejected_before_subagent_execution(self):
        model = ToolCallingModel(responses=[AIMessage(content="", tool_calls=[
            {"name": "task", "args": {"description": "must not execute", "subagent_type": "general-purpose"}, "id": "t1"},
        ])])
        snapshot = prepared()
        with patch("app.agent.factory.ChatDeepSeek", return_value=model):
            agent = build_chat_agent(settings(), snapshot)
        with self.assertRaises(AgentToolUseNotAllowedError):
            agent.invoke({"messages": snapshot.as_agent_messages()})


class AsyncChatAgentTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_graph_streams_fake_model_chunks_without_network(self):
        snapshot = prepared()
        model = RecordingModel(responses=["你好"])
        with tracing_context(enabled=False), patch("socket.socket.connect", side_effect=AssertionError("No network")):
            with patch("app.agent.factory.ChatDeepSeek", return_value=model):
                agent = build_chat_agent(settings(), snapshot)
            chunks = []
            async for chunk, metadata in agent.astream(
                {"messages": snapshot.as_agent_messages()}, stream_mode="messages", subgraphs=False,
            ):
                if metadata.get("langgraph_node") == "model":
                    chunks.append(chunk.content)
        self.assertEqual("".join(chunks), "你好")
        self.assertEqual(len(model._seen), 1)
        self.assertEqual(model._seen[0][0].text.count("中文 preference"), 1)
