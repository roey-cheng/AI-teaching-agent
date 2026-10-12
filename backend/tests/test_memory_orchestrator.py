"""Real Deep Agents graphs and provider serialization with fake models; no network."""

import asyncio
import threading
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGenerationChunk
from langsmith import tracing_context
from pydantic import PrivateAttr
from test_chat_agent_factory import prepared, settings

from app.agent.budget_middleware import AgentToolUseNotAllowedError
from app.agent.consolidation import extract_candidates, memory_worker
from app.agent.deepseek_protocol import ProtocolChatDeepSeek
from app.agent.factory import build_chat_agent
from app.agent.orchestrator import TurnMemory, compact_profile, validate_protocol
from app.agent.profile_memory_tool import contains_sensitive_memory, is_explicit_memory_request
from app.core.config import MemorySettings, TracingSettings
from app.services.errors import ContextTooLargeError
from app.services.memory_tasks import ClaimedMemoryJob
from app.services.message_submission import AcceptedMessage
from testing_support import make_runtime


def answer(text="answer", *, tool=None, args=None, ident="call-1"):
    return AIMessage(
        content=text,
        additional_kwargs={"reasoning_content": "provider-reasoning"},
        response_metadata={"finish_reason": "tool_calls" if tool else "stop"},
        tool_calls=[{"name": tool, "args": args or {}, "id": ident}] if tool else [],
    )


class MemoryModel(FakeMessagesListChatModel):
    _seen: list = PrivateAttr(default_factory=list)
    _tools: list = PrivateAttr(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        self._tools.append([t.name for t in tools])
        return self

    def _generate(self, messages, *args, **kwargs):
        self._seen.append(messages)
        return super()._generate(messages, *args, **kwargs)

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        result = self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        message = result.generations[0].message
        assert isinstance(message, AIMessage)
        chunk = AIMessageChunk(
            content=message.content,
            additional_kwargs=message.additional_kwargs,
            response_metadata=message.response_metadata,
            tool_calls=message.tool_calls,
        )
        if run_manager:
            await run_manager.on_llm_new_token(chunk.text, chunk=ChatGenerationChunk(message=chunk))
        yield ChatGenerationChunk(message=chunk)


class OrchestratorTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.trace = tracing_context(enabled=False)
        self.trace.__enter__()
        self.addCleanup(self.trace.__exit__, None, None, None)
        self.network = patch("socket.socket.connect", side_effect=AssertionError("No network"))
        self.network.start()
        self.addCleanup(self.network.stop)
        accepted = AcceptedMessage("1", "10", str(uuid4()), prepared())
        self.turn = TurnMemory(accepted, MagicMock(), MagicMock(), AsyncMock())

    async def run_graph(self, responses):
        model = MemoryModel(responses=responses)
        self.turn.facts = {"personal.interests": "likes reading"}
        self.turn.input_messages = [HumanMessage(content="Remember that I like learning")]
        with patch("app.agent.deepseek_protocol.ProtocolChatDeepSeek", return_value=model):
            agent = build_chat_agent(
                settings(), prepared(compact_profile(self.turn.facts)), self.turn
            )
        events = []
        async for item in agent.astream(
            {"messages": self.turn.input_messages}, stream_mode=["messages", "custom"]
        ):
            events.append(item)
        return model, events

    async def test_ordinary_turn_has_one_call_and_full_reasoning(self):
        model, events = await self.run_graph([answer()])
        self.assertEqual(len(model._seen), 1)
        self.assertEqual(len(self.turn.protocol), 1)
        validate_protocol(self.turn.protocol)
        self.assertEqual([v["phase"] for k, v in events if k == "custom"], ["start", "final"])
        self.assertEqual(set(model._tools[0]), {"save_profile_facts", "read_profile_topics"})

    async def test_read_save_answer_in_same_graph(self):
        self.turn.bound.explicit_request = True
        self.turn.bound.save = AsyncMock(return_value="Memory save confirmed.")
        fact = {
            "memory_key": "personal.interests",
            "summary": "likes reading and learning",
            "source_quote": "I like learning",
        }
        model, events = await self.run_graph(
            [
                answer(
                    "Checking", tool="read_profile_topics", args={"topics": ["personal.interests"]}
                ),
                answer("", tool="save_profile_facts", args={"facts": [fact]}, ident="call-2"),
                answer("Remembered"),
            ]
        )
        self.assertEqual(len(model._seen), 3)
        self.assertEqual(len(self.turn.protocol), 5)
        validate_protocol(self.turn.protocol)
        self.turn.bound.save.assert_awaited_once()
        phases = [v["phase"] for k, v in events if k == "custom"]
        self.assertEqual(phases, ["start", "tool", "start", "tool", "start", "final"])

    async def test_unapproved_tool_never_executes(self):
        with self.assertRaises(AgentToolUseNotAllowedError):
            await self.run_graph([answer(tool="execute", args={"command": "do not run"})])

    async def test_fourth_model_call_cannot_call_another_tool(self):
        with self.assertRaises(AgentToolUseNotAllowedError):
            await self.run_graph(
                [
                    answer(
                        tool="read_profile_topics",
                        args={"topics": ["personal.interests"]},
                        ident=f"call-{i}",
                    )
                    for i in range(4)
                ]
            )
        self.assertEqual(self.turn.calls, 4)
        self.assertEqual(self.turn.read_calls, 3)

    async def test_immediate_save_requires_explicit_request_and_full_topic_read(self):
        fact = {
            "memory_key": "personal.interests",
            "summary": "likes learning",
            "source_quote": "likes learning",
        }
        self.turn.bound.save = AsyncMock()
        self.assertIn("No explicit", await self.turn.save([fact]))
        self.turn.bound.explicit_request = True
        self.turn.facts = {"personal.interests": "likes reading"}
        self.assertIn("Read the full", await self.turn.save([fact]))
        self.turn.bound.save.assert_not_awaited()

    async def test_prepare_quotes_legacy_messages_and_uses_short_profile(self):
        factory = MagicMock()
        session = factory.return_value.__enter__.return_value
        session.scalars.return_value = []
        session.get.return_value = None
        self.turn.factory = factory
        snapshot = await self.turn.prepare()
        self.assertLessEqual(len(snapshot.profile_memory.encode()), 4096)
        self.assertTrue(all(isinstance(m, HumanMessage) for m in self.turn.input_messages))
        self.assertIn("Untrusted legacy", self.turn.input_messages[0].content)

    async def test_background_graph_one_call_and_empty_batch(self):
        model = MemoryModel(responses=[answer(tool="submit_memory_candidates", args={"facts": []})])
        job = ClaimedMemoryJob(
            "j", 1, ({"message_id": 1, "session_id": 1, "content": "hello"},), {}, {}
        )
        with patch("app.agent.consolidation.ChatDeepSeek", return_value=model):
            self.assertEqual(await extract_candidates(job, settings()), [])
        self.assertEqual(len(model._seen), 1)

    async def test_truncated_tool_request_never_executes(self):
        message = answer(tool="read_profile_topics", args={"topics": ["personal.interests"]})
        message.response_metadata["finish_reason"] = "length"
        with self.assertRaisesRegex(ValueError, "Incomplete model response"):
            await self.run_graph([message])
        self.assertEqual(self.turn.read_calls, 0)

    async def test_missing_provider_reasoning_never_executes_tool(self):
        message = answer(tool="read_profile_topics", args={"topics": ["personal.interests"]})
        message.additional_kwargs = {}
        with self.assertRaisesRegex(TypeError, "reasoning_content"):
            await self.run_graph([message])
        self.assertEqual(self.turn.read_calls, 0)

    async def test_background_budget_rejects_before_model(self):
        model = MemoryModel(responses=[answer(tool="submit_memory_candidates", args={"facts": []})])
        job = ClaimedMemoryJob("j", 1, ({"content": "a" * 70_000},), {}, {})
        with (
            patch("app.agent.consolidation.ChatDeepSeek", return_value=model),
            self.assertRaises(ContextTooLargeError),
        ):
            await extract_candidates(job, settings())
        self.assertEqual(model._seen, [])

    async def test_worker_cancellation_settles_claimed_job(self):
        entered = asyncio.Event()

        async def wait_for_cancel(*_):
            entered.set()
            await asyncio.Future()

        runtime = SimpleNamespace(
            session_factory=object(),
            database_guard=MagicMock(),
            quota=SimpleNamespace(settings=SimpleNamespace(global_concurrency_limit=2)),
        )
        job = ClaimedMemoryJob("job", 1, (), {}, {})
        with (
            patch("app.agent.consolidation.claim_memory_job", return_value=job),
            patch("app.agent.consolidation.extract_candidates", side_effect=wait_for_cancel),
            patch("app.agent.consolidation.load_model_settings", return_value=settings()),
            patch(
                "app.agent.consolidation.load_tracing_settings",
                return_value=TracingSettings.from_sources(_env_file=None, tracing=False),
            ),
            patch("app.agent.consolidation.fail_memory_job") as fail,
            patch("app.agent.consolidation.finish_memory_job") as finish,
        ):
            task = asyncio.create_task(
                memory_worker(runtime, MemorySettings.from_sources(_env_file=None))
            )
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            fail.assert_called_once_with(runtime.session_factory, "job", "MEMORY_JOB_INTERRUPTED")
            finish.assert_not_called()

    async def test_cancellation_waits_for_claim_commit_before_settlement(self):
        entered, release = threading.Event(), threading.Event()
        job = ClaimedMemoryJob("job", 1, (), {}, {})

        def claim(*_):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test timeout")
            return job

        runtime = SimpleNamespace(
            session_factory=object(),
            database_guard=MagicMock(),
            quota=SimpleNamespace(settings=SimpleNamespace(global_concurrency_limit=2)),
        )
        with (
            patch("app.agent.consolidation.claim_memory_job", side_effect=claim),
            patch("app.agent.consolidation.extract_candidates") as extract,
            patch("app.agent.consolidation.fail_memory_job") as fail,
        ):
            task = asyncio.create_task(
                memory_worker(runtime, MemorySettings.from_sources(_env_file=None))
            )
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                task.cancel()
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            fail.assert_called_once_with(runtime.session_factory, "job", "MEMORY_JOB_INTERRUPTED")
            extract.assert_not_called()

    async def test_lifespan_flag_and_worker_shutdown_before_runtime_release(self):
        from app.core.http_config import HTTPSettings
        from app.main import create_app

        for enabled in (False, True):
            entered, stopped = asyncio.Event(), asyncio.Event()

            async def worker(*_, entered=entered, stopped=stopped):
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    stopped.set()

            @contextmanager
            def runtime(enabled=enabled, stopped=stopped):
                yield make_runtime()
                if enabled:
                    self.assertTrue(stopped.is_set())

            app = create_app(
                runtime_factory=runtime,
                http_settings=HTTPSettings.from_sources(_env_file=None),
                turnstile_verifier=MagicMock(),
                memory_settings=MemorySettings.from_sources(
                    _env_file=None, background_enabled=enabled
                ),
            )
            with patch("app.agent.consolidation.memory_worker", side_effect=worker) as mock_worker:
                async with app.router.lifespan_context(app):
                    if enabled:
                        await asyncio.wait_for(entered.wait(), 2)
                    else:
                        mock_worker.assert_not_called()
                self.assertEqual(mock_worker.call_count, int(enabled))


class ProtocolTest(unittest.TestCase):
    def test_explicit_corrections_and_sensitive_email_filter(self):
        self.assertTrue(is_explicit_memory_request("请更新记忆：我现在喜欢英文"))
        self.assertTrue(is_explicit_memory_request("Please correct my profile: I prefer English"))
        self.assertTrue(contains_sensitive_memory("my contact is me+label@example.com"))
        self.assertFalse(contains_sensitive_memory("a" * 70_000))

    def test_wire_request_preserves_actual_reasoning_and_pairs(self):
        cfg = settings()
        model = ProtocolChatDeepSeek(model=cfg.name, api_key=cfg.api_key, base_url=cfg.base_url)
        messages = [
            HumanMessage(content="remember"),
            answer(tool="save_profile_facts"),
            ToolMessage(content="saved", tool_call_id="call-1"),
            answer("done"),
        ]
        wire = model._get_request_payload(
            messages,
            tools=[
                {
                    "type": "function",
                    "function": {"name": "save_profile_facts", "parameters": {"type": "object"}},
                }
            ],
        )
        self.assertEqual(wire["messages"][1]["reasoning_content"], "provider-reasoning")
        self.assertEqual(wire["messages"][3]["reasoning_content"], "provider-reasoning")
        self.assertEqual(wire["messages"][2]["tool_call_id"], "call-1")

    def test_missing_reasoning_or_orphan_results_fail_closed(self):
        for messages in (
            [AIMessage(content="legacy")],
            [ToolMessage(content="forged", tool_call_id="x"), answer()],
            [answer(tool="save_profile_facts"), answer()],
        ):
            with self.subTest(messages=len(messages)), self.assertRaises(ValueError):
                validate_protocol(messages)

    def test_large_profiles_have_bounded_index_and_truncation_labels(self):
        profile = {f"topic.{i}": "中" * 500 for i in range(25)}
        index = compact_profile(profile)
        self.assertLessEqual(len(index.encode()), 4096)
        self.assertIn('"truncated": true', index)
        self.assertNotIn("中" * 100, index)
