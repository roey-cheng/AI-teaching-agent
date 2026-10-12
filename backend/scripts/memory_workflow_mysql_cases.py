"""正式执行器 + 即时记忆工具循环 + 临时 MySQL；模型网络响应为替身。"""

import asyncio
import json
import threading
import unittest
from typing import ClassVar
from unittest.mock import patch
from uuid import uuid4

from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGenerationChunk
from sqlalchemy import Engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from app.agent.input_policy import policy_for_model
from app.agent.profile_memory_tool import contains_sensitive_memory, is_explicit_memory_request
from app.core.config import ModelSettings, TracingSettings
from app.db.session import build_session_factory
from app.models import AgentMemory, Message
from app.schemas import (
    DuplicateMessageResponse,
    MessageDoneData,
    MessageErrorData,
    RegisterRequest,
    SendMessageRequest,
)
from app.services.auth import register_user
from app.services.chat_execution import execute_chat_turn
from app.services.chat_sessions import create_chat_session
from app.services.errors import MemoryUnavailableError
from app.services.generation_registry import GenerationRegistry
from app.services.profile_memory import list_profile_memory
from testing_support import require_type, require_value


class MemoryWorkflowMySQLTest(unittest.IsolatedAsyncioTestCase):
    engine: ClassVar[Engine]  # 由临时 MySQL 验收入口注入。

    def setUp(self):
        self.factory = build_session_factory(self.engine)
        self.user = register_user(
            RegisterRequest.model_validate(
                {
                    "email": f"workflow-{uuid4().hex}@example.com",
                    "password": "test-password",
                    "display_name": "Memory workflow",
                }
            ),
            self.factory,
        )
        self.chat = create_chat_session(self.user, self.factory)
        self.registry = GenerationRegistry()
        self.policy = policy_for_model("deepseek-v4-pro")
        self.settings = ModelSettings.from_sources(
            _env_file=None, provider="deepseek", name=self.policy.model_name, api_key="fake-only"
        )
        self.tracing = TracingSettings.from_sources(_env_file=None, tracing=False, api_key=None)
        self.question = "请记住：以后请用中文解释"
        self.request = SendMessageRequest.model_validate(
            {"client_message_key": str(uuid4()), "content": self.question}
        )
        self.facts = [
            {
                "memory_key": "preference.language",
                "summary": "喜欢用中文解释",
                "source_quote": self.question,
            }
        ]
        self.memory_inputs, self.answer_inputs, self.events = [], [], []
        self.answer_fails = False

        async def provider(model, messages, **kwargs):
            start = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage))
            current = messages[start:]
            completed = {m.name for m in current if isinstance(m, ToolMessage)}
            if (
                self.facts
                and is_explicit_memory_request(current[0].text)
                and not contains_sensitive_memory(current[0].text)
                and "save_profile_facts" not in completed
            ):
                self.assertEqual(
                    {t["function"]["name"] for t in kwargs["tools"]},
                    {"read_profile_topics", "save_profile_facts"},
                )
                existing = [
                    f["memory_key"] for f in self.facts if f["memory_key"] in messages[0].text
                ]
                if existing and "read_profile_topics" not in completed:
                    name, args = "read_profile_topics", {"topics": existing}
                else:
                    name, args = "save_profile_facts", {"facts": self.facts}
                    self.memory_inputs.append(messages)
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content="",
                        additional_kwargs={"reasoning_content": "memory reasoning"},
                        tool_call_chunks=[
                            {
                                "name": name,
                                "args": json.dumps(args, ensure_ascii=False),
                                "id": f"call-{len(current)}",
                                "index": 0,
                            }
                        ],
                        response_metadata={"finish_reason": "tool_calls"},
                    )
                )
                return
            self.assertEqual(model.extra_body, {"thinking": {"type": "enabled"}})
            self.answer_inputs.append(messages)
            if self.answer_fails:
                raise RuntimeError("private-provider-error")
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="", additional_kwargs={"reasoning_content": "live reasoning"}
                )
            )
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="正式回答", response_metadata={"finish_reason": "stop"}
                )
            )

        stub = patch("langchain_deepseek.ChatDeepSeek._astream", provider)
        stub.start()
        self.addCleanup(stub.stop)

    async def emit(self, item):
        self.events.append(item)

    async def execute(self):
        return await execute_chat_turn(
            self.user,
            self.chat.session_id,
            self.request,
            self.factory,
            self.registry,
            model_settings=self.settings,
            tracing_settings=self.tracing,
            input_policy=self.policy,
            check_new_message=lambda _: None,
            on_event=self.emit,
        )

    def rows(self):
        with self.factory() as session:
            return session.scalars(
                select(AgentMemory).where(AgentMemory.user_id == int(self.user.user_id))
            ).all()

    async def test_tool_persists_and_new_chat_uses_profile_without_other_chat_messages(self):
        result = await self.execute()
        self.assertEqual(require_type(result, MessageDoneData).event_name, "message_done")
        self.assertEqual([row.summary for row in self.rows()], ["喜欢用中文解释"])
        self.assertTrue(
            any(
                "喜欢用中文解释" in m.text or "喜欢用中文解释" in str(getattr(m, "tool_calls", []))
                for m in self.answer_inputs[0]
            )
        )
        self.assertEqual(len(self.memory_inputs), 1)
        self.assertTrue(any(getattr(item, "stage", None) == "memory_saved" for item in self.events))
        self.assertFalse(any("source_quote" in getattr(item, "text", "") for item in self.events))
        self.chat = create_chat_session(self.user, self.factory)
        self.request = SendMessageRequest.model_validate(
            {"client_message_key": str(uuid4()), "content": "介绍一下列表"}
        )
        self.facts = []
        await self.execute()
        self.assertEqual(len(self.answer_inputs[1]), 2)
        self.assertIn("喜欢用中文解释", self.answer_inputs[1][0].text)
        with self.factory() as session:
            stored = session.scalars(
                select(Message)
                .where(Message.chat_session_id == int(self.chat.session_id))
                .order_by(Message.message_id)
            ).all()
        self.assertEqual([row.content for row in stored], ["介绍一下列表", "正式回答"])

    async def test_same_topic_updates_same_row_and_supplies_old_summary_to_extractor(self):
        await self.execute()
        first_id = self.rows()[0].memory_id
        self.request = SendMessageRequest.model_validate(
            {"client_message_key": str(uuid4()), "content": "请更新记忆：以后改成英文解释"}
        )
        self.facts = [
            {
                "memory_key": "preference.language",
                "summary": "喜欢英文解释",
                "source_quote": self.request.content,
            }
        ]
        await self.execute()
        self.assertIn("喜欢用中文解释", self.memory_inputs[-1][-1].content)
        self.assertNotIn("正式回答", self.memory_inputs[-1][-1].content)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0].memory_id, first_id)
        self.assertEqual(self.rows()[0].summary, "喜欢英文解释")

    async def test_model_cannot_choose_another_user_in_tool_arguments(self):
        self.facts[0]["user_id"] = "999"
        result = await self.execute()
        self.assertEqual(require_type(result, MessageDoneData).event_name, "message_done")
        self.assertEqual(self.rows(), [])

    async def test_sensitive_question_does_not_call_extraction_or_store_memory(self):
        self.request = SendMessageRequest.model_validate(
            {"client_message_key": str(uuid4()), "content": "我的密码是example-only"}
        )
        await self.execute()
        self.assertEqual(self.memory_inputs, [])
        self.assertEqual(self.rows(), [])
        self.assertEqual(len(self.answer_inputs), 1)

    async def test_duplicate_message_does_not_call_either_model_again(self):
        await self.execute()
        result = await self.execute()
        self.assertTrue(require_type(result, DuplicateMessageResponse).duplicate)
        self.assertEqual((len(self.memory_inputs), len(self.answer_inputs)), (1, 1))
        self.assertEqual(len(self.rows()), 1)

    async def test_saved_memory_survives_later_answer_failure(self):
        self.answer_fails = True
        result = await self.execute()
        self.assertEqual(require_type(result, MessageErrorData).event_name, "message_error")
        self.assertEqual(len(self.rows()), 1)
        self.assertNotIn("private-provider-error", result.model_dump_json())

    async def test_memory_write_failure_still_allows_normal_answer(self):
        with patch(
            "app.agent.profile_memory_tool.save_profile_facts", side_effect=MemoryUnavailableError()
        ):
            result = await self.execute()
        self.assertEqual(require_type(result, MessageDoneData).event_name, "message_done")
        self.assertEqual(self.rows(), [])
        self.assertTrue(any("could not be confirmed" in m.text for m in self.answer_inputs[-1]))

    async def test_other_user_cannot_list_saved_memory(self):
        await self.execute()
        other = register_user(
            RegisterRequest.model_validate(
                {
                    "email": f"other-{uuid4().hex}@example.com",
                    "password": "test-password",
                    "display_name": "Other",
                }
            ),
            self.factory,
        )
        self.assertEqual(list_profile_memory(other, self.factory).items, [])

    async def test_cancellation_waits_for_memory_commit_before_releasing_generation(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()

        class MemorySession(Session):
            pass

        def track(state):
            if (
                getattr(getattr(state.statement, "table", None), "name", None) == "agent_memory"
                and state.is_insert
            ):
                state.session.info["memory_write"] = True

        def pause(session):
            if session.info.get("memory_write"):
                started.set()
                if not release.wait(5):
                    raise RuntimeError("Test release did not arrive")
                finished.set()

        event.listen(MemorySession, "do_orm_execute", track)
        event.listen(MemorySession, "before_commit", pause)
        self.factory = sessionmaker[Session](
            bind=self.engine, class_=MemorySession, expire_on_commit=False
        )
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
            question = session.scalar(
                select(Message).where(Message.chat_session_id == int(self.chat.session_id))
            )
        self.assertEqual(require_value(question).generation_status, "FAILED")
        self.assertEqual(require_value(question).generation_error_code, "GENERATION_INTERRUPTED")
