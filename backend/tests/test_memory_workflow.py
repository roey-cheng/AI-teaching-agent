"""真实 Deep Agents 图 + 假供应商流；不访问数据库、网络或真实密钥。"""

import asyncio
from dataclasses import replace
from datetime import datetime
import json
import threading
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from langsmith import tracing_context
from pydantic import ValidationError

from app.agent.memory_workflow import prepare_memory_for_turn, with_memory_result
from app.agent.profile_memory_tool import BoundMemoryTool, MemoryCandidate, SaveMemoryInput, contains_sensitive_memory
from app.schemas import MemoryListResponse
from app.services.agent_input import InputMessage
from app.services.chat_execution import EventDeliveryError
from app.services.errors import MemoryUnavailableError
from app.services.generation_registry import GenerationRegistry
from app.services.message_submission import AcceptedMessage
from test_chat_agent_factory import prepared, settings


class MemoryToolSchemaTest(unittest.TestCase):
    def test_exact_topics_extras_length_and_duplicate_topics(self):
        fact = dict(memory_key="preference.language", summary="喜欢中文", source_quote="请记住我喜欢中文")
        self.assertEqual(SaveMemoryInput(facts=[fact]).facts[0].summary, "喜欢中文")
        for changed in (fact | {"user_id": "2"}, fact | {"memory_key": "new.topic"},
                        fact | {"summary": "x" * 501}, fact | {"source_quote": " "}):
            with self.assertRaises(ValidationError):
                SaveMemoryInput(facts=[changed])
        with self.assertRaises(ValidationError):
            SaveMemoryInput(facts=[fact, fact])
        self.assertNotIn("喜欢中文", repr(MemoryCandidate(**fact)))

    def test_sensitive_examples_are_rejected_but_normal_preferences_are_not(self):
        for text in ("我的密码 abc123", "my API key is secret", "我的邮箱 a@example.com", "我有花生过敏",
                     "my religion is Christian", "我住在中山路12号", "my address is 12 Main Street",
                     "护照号码", "sexual orientation", "银行卡号1234567890"):
            with self.subTest(text=text):
                self.assertTrue(contains_sensitive_memory(text))
        for text in ("我喜欢中文解释", "我会用 Git，也在学 Python", "我住在奥克兰"):
            self.assertFalse(contains_sensitive_memory(text))

    def test_memory_growth_trims_whole_pairs_without_losing_current_question(self):
        original = prepared()
        budget = replace(original.policy, app_context_tokens=1600, output_tokens=10,
                         framework_reserve_tokens=0, tool_result_reserve_tokens=0, safety_tokens=0)
        original = replace(original, policy=budget, messages=(InputMessage("user", "x" * 800),
                           InputMessage("assistant", "y" * 100), InputMessage("user", "current")))
        updated = with_memory_result(original, "profile" * 50, "saved", ("preference.language",))
        self.assertEqual([m.content for m in updated.messages], ["current"])
        self.assertTrue(updated.history_truncated)
        self.assertIn("confirmed saving", updated.policy.system_prompt)
        self.assertEqual(updated.policy.tool_definitions_json, "[]")


class MemoryWorkflowTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        trace = tracing_context(enabled=False)
        trace.__enter__()
        self.addCleanup(trace.__exit__, None, None, None)
        network = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        self.fact = dict(memory_key="preference.language", summary="喜欢中文", source_quote="以后请用中文解释")
        self.question = "以后请用中文解释"
        snapshot = replace(prepared(), messages=(InputMessage("user", self.question),), history_rounds=0)
        self.accepted = AcceptedMessage("1", "10", str(uuid4()), snapshot)
        self.factory, self.registry = MagicMock(), GenerationRegistry()
        self.calls, self.stages = [], []
        self.tools = [dict(name="save_profile_facts", args={"facts": [self.fact]}, id="memory-call")]
        self.finish = "tool_calls"
        self.delay = False
        self.started = asyncio.Event()
        self.closed = False

        async def provider(_model, messages, **kwargs):
            self.calls.append((messages, kwargs, _model))
            self.started.set()
            try:
                if self.delay:
                    await asyncio.sleep(20)
                if self.tools:
                    for index, call in enumerate(self.tools):
                        yield ChatGenerationChunk(message=AIMessageChunk(content="", tool_call_chunks=[{
                            "name": call["name"], "args": json.dumps(call["args"], ensure_ascii=False),
                            "id": call["id"], "index": index,
                        }]))
                else:
                    yield ChatGenerationChunk(message=AIMessageChunk(content="No update"))
                yield ChatGenerationChunk(message=AIMessageChunk(content="", response_metadata={"finish_reason": self.finish}))
            finally:
                self.closed = True

        for path, value in (("langchain_deepseek.ChatDeepSeek._astream", provider),
                            ("app.agent.memory_workflow._profile_memory", lambda *a: "updated-profile")):
            patcher = patch(path, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        save = patch("app.agent.profile_memory_tool.save_profile_facts", return_value=MemoryListResponse(items=[]))
        self.save = save.start()
        self.addCleanup(save.stop)

    async def progress(self, stage):
        self.stages.append(stage)

    async def run_memory(self, progress=None):
        return await prepare_memory_for_turn(self.accepted, self.factory, self.registry, settings(), progress or self.progress)

    async def test_real_graph_exposes_only_bound_tool_and_stops_after_one_call(self):
        updated = await self.run_memory()
        self.assertEqual(len(self.calls), 1)
        messages, kwargs, model = self.calls[0]
        self.assertEqual(model.extra_body, {"thinking": {"type": "disabled"}})
        payload = model._get_request_payload(messages, **kwargs)
        self.assertEqual(payload["extra_body"]["thinking"], {"type": "disabled"})
        self.assertEqual([message["role"] for message in payload["messages"]], ["system", "user"])
        self.assertEqual([item["function"]["name"] for item in kwargs["tools"]], ["save_profile_facts"])
        schema = kwargs["tools"][0]["function"]["parameters"]
        self.assertEqual(set(schema["properties"]), {"facts"})
        self.assertNotIn("old answer", str(messages))
        self.save.assert_called_once()
        self.assertIs(self.save.call_args.args[0], self.accepted)
        self.assertEqual(self.save.call_args.args[1][0].summary, "喜欢中文")
        self.assertEqual(updated.profile_memory, "updated-profile")
        self.assertEqual(self.stages, ["memory_checking", "memory_saving", "memory_saved"])
        self.assertTrue(self.closed)

    async def test_no_tool_does_not_write_and_does_not_claim_success(self):
        self.tools, self.finish = [], "stop"
        updated = await self.run_memory()
        self.save.assert_not_called()
        self.assertIn("No new memory save", updated.policy.system_prompt)
        self.assertEqual(self.stages[-1], "memory_skipped")

    async def test_sensitive_current_message_skips_memory_model_entirely(self):
        self.accepted = replace(self.accepted, prepared_input=replace(self.accepted.prepared_input,
                                messages=(InputMessage("user", "我的密码是abc123"),)))
        await self.run_memory()
        self.assertFalse(self.calls)
        self.save.assert_not_called()

    async def test_fabricated_source_and_sensitive_summary_do_not_write(self):
        for fact in (self.fact | {"source_quote": "another user's text"},
                     self.fact | {"summary": "user password abc123"}):
            self.tools[0]["args"] = {"facts": [fact]}
            await self.run_memory()
        self.save.assert_not_called()

    async def test_unknown_tool_multiple_calls_and_truncated_generation_never_write(self):
        self.tools[0]["name"] = "task"
        await self.run_memory()
        self.tools[0]["name"] = "save_profile_facts"
        self.tools = [self.tools[0], self.tools[0] | {"id": "second"}]
        await self.run_memory()
        self.tools = self.tools[:1]
        self.finish = "length"
        await self.run_memory()
        self.save.assert_not_called()

    async def test_memory_failure_is_safe_and_answer_input_is_still_returned(self):
        self.save.side_effect = MemoryUnavailableError()
        updated = await self.run_memory()
        self.assertIn("could not be confirmed", updated.policy.system_prompt)
        self.assertEqual(self.stages[-1], "memory_unavailable")
        self.assertEqual(len(self.calls), 1)

    async def test_model_failure_falls_back_without_retry(self):
        with patch("langchain_deepseek.ChatDeepSeek._astream", side_effect=RuntimeError("secret-provider-error")):
            updated = await self.run_memory()
        self.assertNotIn("secret-provider-error", updated.policy.system_prompt)
        self.assertIn("could not be confirmed", updated.policy.system_prompt)
        self.save.assert_not_called()

    async def test_memory_stage_timeout_still_prepares_normal_answer(self):
        self.delay = True
        with patch("app.agent.memory_workflow.MEMORY_STAGE_TIMEOUT_SECONDS", 0.1):
            updated = await self.run_memory()
        self.assertTrue(self.closed)
        self.assertIn("could not be confirmed", updated.policy.system_prompt)
        self.save.assert_not_called()

    async def test_actual_prompt_and_tool_schema_budget_checked_before_model(self):
        original = self.accepted.prepared_input
        small = replace(original.policy, app_context_tokens=2100, output_tokens=10,
                        framework_reserve_tokens=0, tool_result_reserve_tokens=0, safety_tokens=0)
        self.accepted = replace(self.accepted, prepared_input=replace(original, policy=small))
        await self.run_memory()
        self.assertFalse(self.calls)
        self.save.assert_not_called()

    async def test_cancel_during_model_closes_stream_and_never_writes(self):
        self.delay = True
        task = asyncio.create_task(self.run_memory())
        await self.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.closed)
        self.save.assert_not_called()

    async def test_broken_progress_consumer_propagates_without_write(self):
        async def broken(stage):
            if stage == "memory_saving":
                raise EventDeliveryError()
        with self.assertRaises(EventDeliveryError):
            await self.run_memory(broken)
        self.save.assert_not_called()

    async def test_cancel_during_write_waits_for_worker_before_returning(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        def write(*args):
            started.set()
            release.wait(timeout=5)
            finished.set()
            return MemoryListResponse(items=[])
        self.save.side_effect = write
        task = asyncio.create_task(self.run_memory())
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 3))
            task.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(finished.is_set())

    async def test_closed_binding_cannot_start_tool(self):
        bound = BoundMemoryTool(self.accepted, self.factory, self.registry, self.progress)
        bound.close()
        with self.assertRaises(asyncio.CancelledError):
            await bound.save([MemoryCandidate(**self.fact)])
        self.save.assert_not_called()
