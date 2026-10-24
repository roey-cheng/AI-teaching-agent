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
from langsmith import tracing_context
from pydantic import PrivateAttr
from test_chat_agent_factory import prepared, settings
from test_memory_orchestrator import MemoryModel, answer

from app.agent.factory import ChatAgentProvider, build_chat_agent
from app.agent.orchestrator import TurnMemory, compact_profile
from app.agent.run_context import prepare_run_context
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
        question = next(m.text for m in messages if isinstance(m, HumanMessage))
        steps = sum(isinstance(m, ToolMessage) for m in messages)
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
        question = next(m.text for m in messages if isinstance(m, HumanMessage))
        first = not any(isinstance(m, ToolMessage) for m in messages)
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
    turn = TurnMemory(accepted, MagicMock(), MagicMock(), AsyncMock())
    turn.facts = {"learning.goal": marker}
    turn.input_messages = [HumanMessage(content=question)]

    async def save(facts):
        turn.bound.status = "saved"
        return "saved for " + str(uid)

    turn.bound.save = AsyncMock(side_effect=save)
    snapshot = replace(snapshot, profile_memory=compact_profile(turn.facts))
    return prepare_run_context(snapshot, turn)


class ReusableGraphTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        trace = tracing_context(enabled=False)
        trace.__enter__()
        self.addCleanup(trace.__exit__, None, None, None)
        network = patch("socket.socket.connect", side_effect=AssertionError("No network"))
        network.start()
        self.addCleanup(network.stop)
        self.model = InterleavedModel(responses=[answer()])
        with patch("app.agent.deepseek_protocol.ProtocolChatDeepSeek", return_value=self.model):
            self.graph = build_chat_agent(settings())

    async def invoke(self, context, graph=None):
        assert context.turn is not None
        result = None
        async for kind, value in (graph if graph is not None else self.graph).astream(
            {"messages": context.turn.input_messages},
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
            assert context.turn is not None
            await self.invoke(context)
            self.assertEqual(context.turn.calls, 4)
            self.assertEqual(context.turn.read_calls, 1)
            cast(AsyncMock, context.turn.bound.save).assert_awaited_once()
            self.assertEqual(len(context.turn.protocol), 7)
        for messages in self.model._seen:
            question = next(m.text for m in messages if isinstance(m, HumanMessage))
            forbidden = "orange-only" if "violet-only" in question else "violet-only"
            self.assertNotIn(forbidden, "\n".join(m.text for m in messages))

    async def test_provider_compiles_distinct_real_graphs_and_reuses_each_conversation(self):
        provider = ChatAgentProvider()
        model_settings, policy = settings(), prepared().policy
        models = [InterleavedModel(responses=[answer()]) for _ in range(2)]
        with patch(
            "app.agent.deepseek_protocol.ProtocolChatDeepSeek", side_effect=models
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
                    assert context.turn is not None
                    self.assertIn(marker, result["messages"][-1].text)
                    self.assertEqual(context.turn.calls, 4)
                    cast(AsyncMock, context.turn.bound.save).assert_awaited_once()
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
            assert context.turn is not None
            self.assertIn(marker, result["messages"][-1].text)
            self.assertEqual(context.turn.calls, 4)
            call = cast(AsyncMock, context.turn.bound.save).call_args
            assert call is not None
            saved = call.args[0]
            self.assertIn(marker, saved[0].summary)
            self.assertEqual(context.turn.read_keys, {"learning.goal"})
            other = "orange-only" if marker == "violet-only" else "violet-only"
            self.assertNotIn(other, "\n".join(m.text for m in result["messages"]))

    async def test_cancel_one_run_does_not_cancel_or_contaminate_another(self):
        self.model._hold = True
        held, healthy = run_context(1, "hold-only"), run_context(2, "healthy-only")
        assert held.turn is not None and healthy.turn is not None
        task = asyncio.create_task(self.invoke(held))
        await asyncio.wait_for(self.model._held.wait(), timeout=5)
        result = await self.invoke(healthy)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        cast(AsyncMock, held.turn.bound.save).assert_not_called()
        cast(AsyncMock, healthy.turn.bound.save).assert_awaited_once()
        self.assertIn("healthy-only", result["messages"][-1].text)
        self.assertEqual(healthy.turn.calls, 4)

    async def test_model_failure_does_not_poison_next_run(self):
        failed = run_context(1, "fail-only")
        with self.assertRaisesRegex(RuntimeError, "deliberate model failure"):
            await self.invoke(failed)
        healthy = run_context(2, "healthy-only")
        assert failed.turn is not None and healthy.turn is not None
        await self.invoke(healthy)
        self.assertEqual(healthy.turn.calls, 4)
        cast(AsyncMock, failed.turn.bound.save).assert_not_called()

    async def test_missing_mismatched_or_reused_context_fails_closed(self):
        with self.assertRaises(TypeError):
            await self.graph.ainvoke({"messages": [HumanMessage(content="no context")]})
        context = run_context(1, "violet-only")
        bad = replace(
            context,
            prepared=replace(
                context.prepared, policy=replace(context.prepared.policy, output_tokens=1024)
            ),
        )
        with self.assertRaisesRegex(ValueError, "policy"):
            await self.invoke(bad)
        await self.invoke(context)
        count = len(self.model._seen)
        with self.assertRaisesRegex(ValueError, "fresh chat context"):
            await self.invoke(context)
        self.assertEqual(len(self.model._seen), count)


class GraphProviderTest(unittest.TestCase):
    def test_concurrent_first_access_compiles_once_and_config_is_not_silently_reused(self):
        provider = ChatAgentProvider()
        model_settings, policy = settings(), prepared().policy
        with patch("app.agent.factory.build_chat_agent", return_value=MagicMock()) as build:
            with ThreadPoolExecutor(max_workers=4) as pool:
                graphs = list(
                    pool.map(
                        lambda _: provider.get(
                            model_settings, policy, user_id="1", session_id="10"
                        ),
                        range(8),
                    )
                )
            build.assert_called_once_with(model_settings, policy)
            self.assertTrue(all(graph is graphs[0] for graph in graphs))
            with self.assertRaisesRegex(ValueError, "restart"):
                provider.get(
                    model_settings,
                    replace(policy, output_tokens=1024),
                    user_id="1",
                    session_id="10",
                )
            other = ChatAgentProvider()
            other.get(model_settings, policy, user_id="1", session_id="10")
            self.assertEqual(build.call_count, 2)

    def test_same_conversation_reuses_graph_but_other_conversations_do_not(self):
        provider = ChatAgentProvider()
        model_settings, policy = settings(), prepared().policy
        with patch(
            "app.agent.factory.build_chat_agent", side_effect=lambda *_: MagicMock()
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
        provider = ChatAgentProvider()
        with patch("app.agent.factory.build_chat_agent") as build:
            for user_id, session_id in [("", "1"), ("1", "0"), (True, "1"), ("1", "01")]:
                with (
                    self.subTest(user_id=user_id, session_id=session_id),
                    self.assertRaises(ValueError),
                ):
                    provider.get(
                        settings(), prepared().policy, user_id=user_id, session_id=session_id
                    )
            build.assert_not_called()
