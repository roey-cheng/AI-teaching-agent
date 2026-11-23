"""Per-conversation graph caching and run-local isolation of reusable graphs."""

import asyncio
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from langsmith import tracing_context
from pydantic import PrivateAttr
from test_chat_agent_factory import prepared, settings
from test_memory_orchestrator import MemoryModel, answer

from app.agent.factory import SessionChatGraphCache, compile_chat_graph
from app.agent.orchestrator import ChatAttemptState, compact_profile
from app.agent.run_context import build_chat_run_dependencies
from app.agent.teaching_skill import TEACHING_SKILL_PATH
from app.services.agent_input import InputMessage
from app.services.message_submission import AcceptedMessage


class InterleavedModel(MemoryModel):
    """Responses are derived from each request, never a shared response cursor."""

    _barrier: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)
    _arrivals: set = PrivateAttr(default_factory=set)
    _interleave: bool = PrivateAttr(default=False)
    _held: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)
    _hold: bool = PrivateAttr(default=False)

    def _generate(self, messages, *args, **kwargs):
        self._seen.append(messages)
        last_user = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage))
        question = messages[last_user].text
        steps = sum(isinstance(m, ToolMessage) for m in messages[last_user + 1 :])
        if "fail-only" in question:
            raise RuntimeError("deliberate model failure")
        if steps == 0:
            response = answer(tool="read_file", args={"file_path": TEACHING_SKILL_PATH})
        elif steps == 1:
            response = answer(tool="read_profile_topics", args={"topics": ["learning.goal"]})
        elif steps == 2:
            response = answer(
                tool="save_profile_facts",
                args={
                    "facts": [
                        {
                            "memory_key": "learning.goal",
                            "summary": question,
                            "source_quote": question,
                        }
                    ]
                },
            )
        else:
            response = answer("done: " + question)
        return ChatResult(generations=[ChatGeneration(message=response)])

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        last_user = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage))
        question = messages[last_user].text
        first = not any(isinstance(m, ToolMessage) for m in messages[last_user + 1 :])
        if self._hold and "hold-only" in question and first:
            self._held.set()
            await asyncio.Event().wait()
        if self._interleave and first:
            self._arrivals.add(question)
            if len(self._arrivals) == 2:
                self._barrier.set()
            await asyncio.wait_for(self._barrier.wait(), timeout=5)
        async for chunk in super()._astream(messages, stop=stop, run_manager=run_manager, **kwargs):
            yield chunk


def run_context(uid, marker):
    question = "Please remember " + marker
    snapshot = replace(
        prepared(user_id=str(uid)),
        messages=(InputMessage("user", question),),
    )
    accepted = AcceptedMessage(str(uid), str(uid * 10), str(uuid4()), snapshot)
    attempt_state = ChatAttemptState(accepted, MagicMock(), MagicMock(), AsyncMock())
    attempt_state.profile_facts = {"learning.goal": marker}

    async def save(facts):
        attempt_state.profile_write_tool.status = "saved"
        attempt_state.profile_write_tool.current_facts = {"learning.goal": question}
        return "saved for " + str(uid)

    attempt_state.profile_write_tool.save = AsyncMock(side_effect=save)
    snapshot = replace(snapshot, profile_memory=compact_profile(attempt_state.profile_facts))
    return build_chat_run_dependencies(snapshot, attempt_state)


class ReusableGraphTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        trace = tracing_context(enabled=False)
        trace.__enter__()
        self.addCleanup(trace.__exit__, None, None, None)
        network = patch("socket.socket.connect", side_effect=AssertionError("No network"))
        network.start()
        self.addCleanup(network.stop)
        self.model = InterleavedModel(responses=[answer()])
        with patch(
            "app.agent.deepseek_protocol.ReasoningReplayChatDeepSeek", return_value=self.model
        ):
            self.graph = compile_chat_graph(settings(), checkpointer=InMemorySaver())

    async def invoke(self, context, graph=None):
        assert context.attempt_state is not None
        result = None
        async for kind, value in (graph if graph is not None else self.graph).astream(
            {"messages": [HumanMessage(content=context.admission_snapshot.messages[-1].content)]},
            {
                "configurable": {
                    "thread_id": f"user:{context.admission_snapshot.user_id}:session:{context.admission_snapshot.session_id}"
                }
            },
            context=context,
            stream_mode=["messages", "values"],
        ):
            if kind == "values":
                result = value
        assert isinstance(result, dict)
        return result

    async def test_one_graph_reused_sequentially_resets_counters_and_memory(self):
        contexts = [run_context(1, "violet-only"), run_context(1, "orange-only")]
        for context in contexts:
            assert context.attempt_state is not None
            await self.invoke(context)
            self.assertEqual(context.attempt_state.model_call_count, 4)
            self.assertEqual(context.attempt_state.profile_read_count, 1)
            cast(AsyncMock, context.attempt_state.profile_write_tool.save).assert_awaited_once()
            self.assertEqual(len(context.attempt_state.protocol_messages), 7)
        self.assertIn("violet-only", "\n".join(m.text for m in self.model._seen[-1]))
        self.assertIn("orange-only", self.model._seen[-1][0].text)
        self.assertNotIn("violet-only", self.model._seen[-1][0].text)

    async def test_provider_compiles_distinct_real_graphs_and_reuses_each_conversation(self):
        provider = SessionChatGraphCache(InMemorySaver())
        model_settings, policy = settings(), prepared().policy
        models = [InterleavedModel(responses=[answer()]) for _ in range(2)]
        with patch(
            "app.agent.deepseek_protocol.ReasoningReplayChatDeepSeek", side_effect=models
        ) as constructor:
            first = provider.get(model_settings, policy, user_id="1", session_id="1")
            second = provider.get(model_settings, policy, user_id="2", session_id="2")
            self.assertIsNot(first, second)
            for markers in [("violet-only", "orange-only"), ("blue-only", "green-only")]:
                contexts = [run_context(uid, marker) for uid, marker in enumerate(markers, start=1)]
                graphs = [
                    provider.get(model_settings, policy, user_id=str(uid), session_id=str(uid))
                    for uid in (1, 2)
                ]
                self.assertIs(graphs[0], first)
                self.assertIs(graphs[1], second)
                results = await asyncio.gather(
                    *(self.invoke(context, graph) for context, graph in zip(contexts, graphs))
                )
                for context, result, marker in zip(contexts, results, markers):
                    assert context.attempt_state is not None
                    self.assertIn(marker, result["messages"][-1].text)
                    self.assertEqual(context.attempt_state.model_call_count, 4)
                    cast(
                        AsyncMock, context.attempt_state.profile_write_tool.save
                    ).assert_awaited_once()
            self.assertEqual(constructor.call_count, 2)
        for model, excluded in zip(
            models, [("orange-only", "green-only"), ("violet-only", "blue-only")]
        ):
            self.assertEqual(len(model._seen), 8)
            for messages in model._seen:
                for marker in excluded:
                    self.assertNotIn(marker, "\n".join(m.text for m in messages))

    async def test_same_graph_overlapping_runs_route_tools_and_store_per_user(self):
        self.model._interleave = True
        contexts = [run_context(1, "violet-only"), run_context(2, "orange-only")]
        results = await asyncio.gather(*(self.invoke(context) for context in contexts))
        for context, result, marker in zip(contexts, results, ["violet-only", "orange-only"]):
            assert context.attempt_state is not None
            self.assertIn(marker, result["messages"][-1].text)
            self.assertEqual(context.attempt_state.model_call_count, 4)
            call = cast(AsyncMock, context.attempt_state.profile_write_tool.save).call_args
            assert call is not None
            saved = call.args[0]
            self.assertIn(marker, saved[0].summary)
            self.assertEqual(context.attempt_state.read_topic_keys, {"learning.goal"})
            other = "orange-only" if marker == "violet-only" else "violet-only"
            self.assertNotIn(other, "\n".join(m.text for m in result["messages"]))

    async def test_cancel_one_run_does_not_cancel_or_contaminate_another(self):
        self.model._hold = True
        held, healthy = run_context(1, "hold-only"), run_context(2, "healthy-only")
        assert held.attempt_state is not None and healthy.attempt_state is not None
        task = asyncio.create_task(self.invoke(held))
        await asyncio.wait_for(self.model._held.wait(), timeout=5)
        result = await self.invoke(healthy)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        cast(AsyncMock, held.attempt_state.profile_write_tool.save).assert_not_called()
        cast(AsyncMock, healthy.attempt_state.profile_write_tool.save).assert_awaited_once()
        self.assertIn("healthy-only", result["messages"][-1].text)
        self.assertEqual(healthy.attempt_state.model_call_count, 4)

    async def test_model_failure_does_not_poison_next_run(self):
        failed = run_context(1, "fail-only")
        with self.assertRaisesRegex(RuntimeError, "deliberate model failure"):
            await self.invoke(failed)
        healthy = run_context(2, "healthy-only")
        assert failed.attempt_state is not None and healthy.attempt_state is not None
        await self.invoke(healthy)
        self.assertEqual(healthy.attempt_state.model_call_count, 4)
        cast(AsyncMock, failed.attempt_state.profile_write_tool.save).assert_not_called()

    async def test_missing_mismatched_or_reused_context_fails_closed(self):
        with self.assertRaises(TypeError):
            await self.graph.ainvoke(
                {"messages": [HumanMessage(content="no context")]},
                {"configurable": {"thread_id": "missing-context"}},
            )
        context = run_context(1, "violet-only")
        bad = replace(
            context,
            admission_snapshot=replace(
                context.admission_snapshot,
                policy=replace(context.admission_snapshot.policy, output_tokens=1024),
            ),
        )
        with self.assertRaisesRegex(ValueError, "policy"):
            await self.invoke(bad)
        await self.invoke(context)
        count = len(self.model._seen)
        with self.assertRaisesRegex(ValueError, "fresh chat run dependencies"):
            await self.invoke(context)
        self.assertEqual(len(self.model._seen), count)


class GraphProviderTest(unittest.TestCase):
    def test_concurrent_first_access_compiles_once_and_config_is_not_silently_reused(self):
        saver = InMemorySaver()
        provider = SessionChatGraphCache(saver)
        model_settings, policy = settings(), prepared().policy
        with patch("app.agent.factory.compile_chat_graph", return_value=MagicMock()) as build:
            with ThreadPoolExecutor(max_workers=4) as pool:
                graphs = list(
                    pool.map(
                        lambda _: provider.get(
                            model_settings, policy, user_id="1", session_id="10"
                        ),
                        range(8),
                    )
                )
            build.assert_called_once_with(model_settings, policy, checkpointer=saver)
            self.assertTrue(all(graph is graphs[0] for graph in graphs))
            with self.assertRaisesRegex(ValueError, "restart"):
                provider.get(
                    model_settings,
                    replace(policy, output_tokens=1024),
                    user_id="1",
                    session_id="10",
                )
            other = SessionChatGraphCache(InMemorySaver())
            other.get(model_settings, policy, user_id="1", session_id="10")
            self.assertEqual(build.call_count, 2)

    def test_same_conversation_reuses_graph_but_other_conversations_do_not(self):
        provider = SessionChatGraphCache(InMemorySaver())
        model_settings, policy = settings(), prepared().policy
        with patch(
            "app.agent.factory.compile_chat_graph", side_effect=lambda *_, **__: MagicMock()
        ) as build:
            first = provider.get(model_settings, policy, user_id="1", session_id="10")
            second = provider.get(model_settings, policy, user_id="1", session_id="11")
            other_user = provider.get(model_settings, policy, user_id="2", session_id="10")
            self.assertIsNot(first, second)
            self.assertIsNot(first, other_user)
            self.assertIsNot(second, other_user)
            # Numeric and API string IDs refer to the same conversation.
            self.assertIs(first, provider.get(model_settings, policy, user_id=1, session_id=10))
            self.assertIs(
                second, provider.get(model_settings, policy, user_id="1", session_id="11")
            )
            self.assertEqual(build.call_count, 3)

    def test_invalid_identity_never_creates_a_cache_entry(self):
        provider = SessionChatGraphCache(InMemorySaver())
        with patch("app.agent.factory.compile_chat_graph") as build:
            for user_id, session_id in [("", "1"), ("1", "0"), (True, "1"), ("1", "01")]:
                with (
                    self.subTest(user_id=user_id, session_id=session_id),
                    self.assertRaises(ValueError),
                ):
                    provider.get(
                        settings(), prepared().policy, user_id=user_id, session_id=session_id
                    )
            build.assert_not_called()
