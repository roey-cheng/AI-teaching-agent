"""Offline acceptance tests for native compaction, checkpoint continuation and cache eviction."""

import unittest
from typing import Any, cast
from unittest.mock import MagicMock, patch

from deepagents.backends import CompositeBackend, StateBackend
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langsmith import tracing_context
from pydantic import PrivateAttr
from test_chat_agent_factory import prepared, settings

from app.agent.conversation_context import SUMMARY_INSTRUCTIONS, build_summary_middleware
from app.agent.factory import SessionChatGraphCache, compile_chat_graph
from app.agent.run_context import build_chat_run_dependencies


class ContextModel(FakeListChatModel):
    _seen: list = PrivateAttr(default_factory=list)
    _summary_kwargs: list = PrivateAttr(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        if tools:
            raise AssertionError("This fixture has no tools")
        return self

    def _call(self, messages, **kwargs) -> str:
        self._seen.append(messages)
        if messages[0].text == SUMMARY_INSTRUCTIONS:
            self._summary_kwargs.append(kwargs)
            return "SUMMARY: learner is studying loops; preserve the earlier worked example."
        return "final answer"

    def _generate(self, *args, **kwargs):
        result = super()._generate(*args, **kwargs)
        result.generations[0].message.response_metadata["finish_reason"] = "stop"
        return result


class TruncatedSummaryModel(ContextModel):
    def _generate(self, *args, **kwargs):
        result = super()._generate(*args, **kwargs)
        result.generations[0].message.response_metadata["finish_reason"] = "length"
        return result


class FailingSummaryModel(ContextModel):
    def _call(self, messages, **kwargs) -> str:
        self._seen.append(messages)
        if messages[0].text == SUMMARY_INSTRUCTIONS:
            raise RuntimeError("summary unavailable")
        return "must not answer"


class ConversationContextTest(unittest.TestCase):
    def setUp(self):
        trace = tracing_context(enabled=False)
        trace.__enter__()
        self.addCleanup(trace.__exit__, None, None, None)
        network = patch("socket.socket.connect", side_effect=AssertionError("No network"))
        network.start()
        self.addCleanup(network.stop)

    def test_native_summary_archives_history_and_survives_graph_rebuild(self):
        model = ContextModel(responses=["unused"])
        saver = InMemorySaver()
        snapshot = prepared()
        config: RunnableConfig = {"configurable": {"thread_id": "user:1:conversation:1"}}
        history = []
        for i in range(10):
            history.extend(
                [
                    HumanMessage(content=f"question {i}: " + "x" * 4000),
                    AIMessage(content="y" * 4000),
                ]
            )
        history.append(HumanMessage(content="please continue"))
        with patch("app.agent.factory.ChatDeepSeek", return_value=model):
            graph = compile_chat_graph(settings(), checkpointer=saver, enable_chat_tools=False)
        graph.invoke({"messages": history}, config, context=build_chat_run_dependencies(snapshot))
        state = graph.get_state(config).values
        self.assertIn("_summarization_event", state)
        self.assertIn("SUMMARY:", state["_summarization_event"]["summary_message"].text)
        archive = state["_summarization_event"]["file_path"]
        self.assertTrue(archive.startswith("/conversation_history/"))
        self.assertIn(archive, state["files"])
        self.assertIn("question 0", state["files"][archive]["content"])
        self.assertEqual(len(model._summary_kwargs), 1)
        self.assertEqual(model._summary_kwargs[0]["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertEqual(model._summary_kwargs[0]["max_tokens"], 2048)

        # Rebuilding the process-local graph does not lose checkpointed history.
        with patch("app.agent.factory.ChatDeepSeek", return_value=model):
            graph = compile_chat_graph(settings(), checkpointer=saver, enable_chat_tools=False)
        graph.invoke(
            {"messages": [HumanMessage(content="next question", id="question-next")]},
            config,
            context=build_chat_run_dependencies(snapshot),
        )
        seen = "\n".join(message.text for message in model._seen[-1])
        self.assertIn("SUMMARY:", seen)
        self.assertIn("next question", seen)
        self.assertNotIn("question 0:", seen)
        self.assertEqual(seen.count("next question"), 1)
        self.assertEqual(len(model._summary_kwargs), 1)
        other_config: RunnableConfig = {"configurable": {"thread_id": "user:2:conversation:2"}}
        graph.invoke(
            {"messages": [HumanMessage(content="independent question")]},
            other_config,
            context=build_chat_run_dependencies(prepared(user_id="2")),
        )
        other_seen = "\n".join(message.text for message in model._seen[-1])
        self.assertNotIn("SUMMARY:", other_seen)
        self.assertNotIn("question 0:", other_seen)
        self.assertFalse(graph.get_state(other_config).values.get("files"))

    def test_summary_threshold_uses_application_budget_not_provider_profile(self):
        policy = prepared().policy
        model = ContextModel(responses=["unused"])
        middleware = build_summary_middleware(
            model, CompositeBackend(default=StateBackend(), routes={}), policy
        )
        self.assertFalse(middleware._should_summarize([], policy.admission_input_budget_tokens - 1))
        self.assertTrue(middleware._should_summarize([], policy.admission_input_budget_tokens))
        self.assertEqual(policy.admission_input_budget_tokens, 45056)

    def test_compaction_preserves_complete_tool_pairs(self):
        model = ContextModel(responses=["unused"])
        snapshot = prepared()
        history = []
        for i in range(8):
            history.extend(
                [
                    HumanMessage(content=f"question {i}: " + "x" * 4000),
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "read_file",
                                "id": f"call-{i}",
                                "args": {"file_path": "/example"},
                            }
                        ],
                        additional_kwargs={"reasoning_content": "provider protocol"},
                    ),
                    ToolMessage(content="y" * 4000, tool_call_id=f"call-{i}"),
                    AIMessage(content="answered"),
                ]
            )
        history.append(HumanMessage(content="continue"))
        with patch("app.agent.factory.ChatDeepSeek", return_value=model):
            graph = compile_chat_graph(
                settings(), checkpointer=InMemorySaver(), enable_chat_tools=False
            )
        graph.invoke(
            {"messages": history},
            {"configurable": {"thread_id": "paired-tools"}},
            context=build_chat_run_dependencies(snapshot),
        )
        pending = set()
        for message in model._seen[-1]:
            if isinstance(message, AIMessage):
                self.assertFalse(pending)
                pending = {tool["id"] for tool in message.tool_calls}
            elif isinstance(message, ToolMessage):
                self.assertIn(message.tool_call_id, pending)
                pending.remove(message.tool_call_id)
        self.assertFalse(pending)

    def test_failed_summary_is_not_silently_retried_or_replaced_with_answer(self):
        model = FailingSummaryModel(responses=["unused"])
        with patch("app.agent.factory.ChatDeepSeek", return_value=model):
            graph = compile_chat_graph(
                settings(), checkpointer=InMemorySaver(), enable_chat_tools=False
            )
        history = [HumanMessage(content="x" * 10000) for _ in range(8)]
        with self.assertRaisesRegex(RuntimeError, "summary unavailable"):
            graph.invoke(
                {"messages": history},
                {"configurable": {"thread_id": "failed-summary"}},
                context=build_chat_run_dependencies(prepared()),
            )
        self.assertEqual(len(model._seen), 1)

    def test_truncated_summary_cannot_be_published_as_compaction(self):
        model = TruncatedSummaryModel(responses=["unused"])
        with patch("app.agent.factory.ChatDeepSeek", return_value=model):
            graph = compile_chat_graph(
                settings(), checkpointer=InMemorySaver(), enable_chat_tools=False
            )
        config: RunnableConfig = {"configurable": {"thread_id": "truncated-summary"}}
        with self.assertRaisesRegex(ValueError, "summary did not finish"):
            graph.invoke(
                {"messages": [HumanMessage(content="x" * 10000) for _ in range(8)]},
                config,
                context=build_chat_run_dependencies(prepared()),
            )
        self.assertEqual(len(model._seen), 1)
        self.assertNotIn("_summarization_event", graph.get_state(config).values)

    def test_no_implicit_saver_fallback(self):
        with self.assertRaises(TypeError):
            compile_chat_graph(settings(), checkpointer=cast(Any, None))
        with self.assertRaises(TypeError):
            SessionChatGraphCache(cast(Any, None))

    def test_cache_eviction_and_idle_expiry_do_not_close_held_graph(self):
        now = [0.0]
        saver = InMemorySaver()
        provider = SessionChatGraphCache(
            saver, max_cached_sessions=2, idle_ttl_seconds=10, clock=lambda: now[0]
        )
        model_settings, policy = settings(), prepared().policy
        with patch(
            "app.agent.factory.compile_chat_graph", side_effect=lambda *_, **__: MagicMock()
        ) as build:
            first = provider.get(model_settings, policy, user_id=1, session_id=1)
            provider.get(model_settings, policy, user_id=1, session_id=2)
            provider.get(model_settings, policy, user_id=1, session_id=3)
            self.assertEqual(len(provider._session_graphs), 2)
            cast(MagicMock, first).close.assert_not_called()
            rebuilt = provider.get(model_settings, policy, user_id=1, session_id=1)
            self.assertIsNot(rebuilt, first)
            now[0] = 11
            refreshed = provider.get(model_settings, policy, user_id=1, session_id=1)
            self.assertIsNot(refreshed, rebuilt)
            cast(MagicMock, rebuilt).close.assert_not_called()
            self.assertEqual(len(provider._session_graphs), 1)
            self.assertTrue(
                all(call.kwargs["checkpointer"] is saver for call in build.call_args_list)
            )
