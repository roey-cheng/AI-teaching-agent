"""正式执行器 + 两阶段 Deep Agents + 临时 MySQL；模型网络响应为替身。"""

import asyncio
import json
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from sqlalchemy import event, select
from sqlalchemy.orm import Session, sessionmaker

from app.agent.input_policy import policy_for_model
from app.core.config import ModelSettings, TracingSettings
from app.db.session import build_session_factory
from app.models import AgentMemory, Message
from app.schemas import RegisterRequest, SendMessageRequest
from app.services.auth import register_user
from app.services.chat_execution import execute_chat_turn
from app.services.chat_sessions import create_chat_session
from app.services.errors import MemoryUnavailableError
from app.services.generation_registry import GenerationRegistry
from app.services.profile_memory import list_profile_memory


class MemoryWorkflowMySQLTest(unittest.IsolatedAsyncioTestCase):
    engine = None

    def setUp(self):
        self.factory = build_session_factory(self.engine)
        self.user = register_user(RegisterRequest(email=f"workflow-{uuid4().hex}@example.com",
                                                  password="test-password", display_name="Memory workflow"), self.factory)
        self.chat = create_chat_session(self.user, self.factory)
        self.registry = GenerationRegistry()
        self.policy = policy_for_model("deepseek-v4-pro")
        self.settings = ModelSettings(_env_file=None, provider="deepseek", name=self.policy.model_name, api_key="fake-only")
        self.tracing = TracingSettings(_env_file=None, tracing=False, api_key=None)
        self.question = "以后请用中文解释"
        self.request = SendMessageRequest(client_message_key=str(uuid4()), content=self.question)
        self.facts = [dict(memory_key="preference.language", summary="喜欢用中文解释", source_quote=self.question)]
        self.memory_inputs, self.answer_inputs, self.events = [], [], []
        self.answer_fails = False

        async def provider(model, messages, **kwargs):
            if kwargs.get("tools"):
                self.assertEqual([t["function"]["name"] for t in kwargs["tools"]], ["save_profile_facts"])
                self.assertEqual(model.extra_body, {"thinking": {"type": "disabled"}})
                self.memory_inputs.append(messages)
                if self.facts:
                    yield ChatGenerationChunk(message=AIMessageChunk(content="", tool_call_chunks=[{
                        "name": "save_profile_facts", "args": json.dumps({"facts": self.facts}, ensure_ascii=False),
                        "id": "memory1", "index": 0,
                    }]))
                    yield ChatGenerationChunk(message=AIMessageChunk(content="", response_metadata={"finish_reason": "tool_calls"}))
                else:
                    yield ChatGenerationChunk(message=AIMessageChunk(content="No update", response_metadata={"finish_reason": "stop"}))
                return
            self.assertEqual(model.extra_body, {"thinking": {"type": "enabled"}})
            self.answer_inputs.append(messages)
            if self.answer_fails:
                raise RuntimeError("private-provider-error")
            yield ChatGenerationChunk(message=AIMessageChunk(content="", additional_kwargs={"reasoning_content": "live reasoning"}))
            yield ChatGenerationChunk(message=AIMessageChunk(content="正式回答", response_metadata={"finish_reason": "stop"}))

        stub = patch("langchain_deepseek.ChatDeepSeek._astream", provider)
        stub.start()
        self.addCleanup(stub.stop)

    async def emit(self, item):
        self.events.append(item)

    async def execute(self):
        return await execute_chat_turn(
            self.user, self.chat.session_id, self.request, self.factory, self.registry,
            model_settings=self.settings, tracing_settings=self.tracing, input_policy=self.policy,
            check_new_message=lambda _: None, on_event=self.emit,
        )

    def rows(self):
        with self.factory() as session:
            return session.scalars(select(AgentMemory).where(AgentMemory.user_id == int(self.user.user_id))).all()

    async def test_tool_persists_and_new_chat_uses_profile_without_other_chat_messages(self):
        result = await self.execute()
        self.assertEqual(result.event_name, "message_done")
        self.assertEqual([row.summary for row in self.rows()], ["喜欢用中文解释"])
        self.assertIn("喜欢用中文解释", self.answer_inputs[0][0].text)
        self.assertEqual(len(self.memory_inputs), 1)
        self.assertTrue(any(getattr(item, "stage", None) == "memory_saved" for item in self.events))
        self.assertFalse(any("source_quote" in getattr(item, "text", "") for item in self.events))
        self.chat = create_chat_session(self.user, self.factory)
        self.request = SendMessageRequest(client_message_key=str(uuid4()), content="介绍一下列表")
        self.facts = []
        await self.execute()
        self.assertEqual(len(self.answer_inputs[1]), 2)
        self.assertIn("喜欢用中文解释", self.answer_inputs[1][0].text)
        with self.factory() as session:
            stored = session.scalars(select(Message).where(Message.chat_session_id == int(self.chat.session_id))
                                     .order_by(Message.message_id)).all()
        self.assertEqual([row.content for row in stored], ["介绍一下列表", "正式回答"])

    async def test_same_topic_updates_same_row_and_supplies_old_summary_to_extractor(self):
        await self.execute()
        first_id = self.rows()[0].memory_id
        self.request = SendMessageRequest(client_message_key=str(uuid4()), content="以后改成英文解释")
        self.facts = [dict(memory_key="preference.language", summary="喜欢英文解释", source_quote=self.request.content)]
        await self.execute()
        self.assertIn("喜欢用中文解释", self.memory_inputs[-1][-1].content)
        self.assertNotIn("正式回答", self.memory_inputs[-1][-1].content)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0].memory_id, first_id)
        self.assertEqual(self.rows()[0].summary, "喜欢英文解释")

    async def test_model_cannot_choose_another_user_in_tool_arguments(self):
        self.facts[0]["user_id"] = "999"
        result = await self.execute()
        self.assertEqual(result.event_name, "message_done")
        self.assertEqual(self.rows(), [])

    async def test_sensitive_question_does_not_call_extraction_or_store_memory(self):
        self.request = SendMessageRequest(client_message_key=str(uuid4()), content="我的密码是example-only")
        await self.execute()
        self.assertEqual(self.memory_inputs, [])
        self.assertEqual(self.rows(), [])
        self.assertEqual(len(self.answer_inputs), 1)

    async def test_duplicate_message_does_not_call_either_model_again(self):
        await self.execute()
        result = await self.execute()
        self.assertTrue(result.duplicate)
        self.assertEqual((len(self.memory_inputs), len(self.answer_inputs)), (1, 1))
        self.assertEqual(len(self.rows()), 1)

    async def test_saved_memory_survives_later_answer_failure(self):
        self.answer_fails = True
        result = await self.execute()
        self.assertEqual(result.event_name, "message_error")
        self.assertEqual(len(self.rows()), 1)
        self.assertNotIn("private-provider-error", result.model_dump_json())

    async def test_memory_write_failure_still_allows_normal_answer(self):
        with patch("app.agent.profile_memory_tool.save_profile_facts", side_effect=MemoryUnavailableError()):
            result = await self.execute()
        self.assertEqual(result.event_name, "message_done")
        self.assertEqual(self.rows(), [])
        self.assertIn("could not be confirmed", self.answer_inputs[-1][0].text)

    async def test_other_user_cannot_list_saved_memory(self):
        await self.execute()
        other = register_user(RegisterRequest(email=f"other-{uuid4().hex}@example.com",
                                             password="test-password", display_name="Other"), self.factory)
        self.assertEqual(list_profile_memory(other, self.factory).items, [])

    async def test_cancellation_waits_for_memory_commit_before_releasing_generation(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        class MemorySession(Session):
            pass

        def track(state):
            if getattr(getattr(state.statement, "table", None), "name", None) == "agent_memory" and state.is_insert:
                state.session.info["memory_write"] = True

        def pause(session):
            if session.info.get("memory_write"):
                started.set()
                if not release.wait(5):
                    raise RuntimeError("Test release did not arrive")
                finished.set()

        event.listen(MemorySession, "do_orm_execute", track)
        event.listen(MemorySession, "before_commit", pause)
        self.factory = sessionmaker(bind=self.engine, class_=MemorySession, expire_on_commit=False)
        task = asyncio.create_task(self.execute())
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 4))
            task.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(finished.is_set())
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.answer_inputs, [])
        with self.registry.locked(int(self.chat.session_id)) as slot:
            self.assertIsNone(slot.attempt_id)
        with self.factory() as session:
            question = session.scalar(select(Message).where(Message.chat_session_id == int(self.chat.session_id)))
        self.assertEqual(question.generation_status, "FAILED")
        self.assertEqual(question.generation_error_code, "GENERATION_INTERRUPTED")
