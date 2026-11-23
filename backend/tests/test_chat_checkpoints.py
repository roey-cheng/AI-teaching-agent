"""Publication rules exercised with actual LangGraph checkpoints, no DB/network."""

import unittest
from dataclasses import replace
from datetime import datetime
from unittest.mock import MagicMock
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, messages_to_dict
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from test_chat_agent_factory import prepared

from app.models import AgentTurnRecord, ChatSession, Message, PublishedChatCheckpoint
from app.services.agent_input import InputMessage
from app.services.chat_checkpoints import (
    completed_checkpoint_id,
    current_user_message,
    prepare_checkpoint_run,
    thread_config,
)
from app.services.message_submission import AcceptedMessage


class FakeBusinessDB:
    """Only the business SQL surface is fake; checkpoints use the native saver."""

    def __init__(self):
        self.head: PublishedChatCheckpoint | None = None
        self.questions = []
        self.records = {}
        self.factory = MagicMock()
        self.read = self.factory.return_value.__enter__.return_value
        self.write = self.factory.begin.return_value.__enter__.return_value
        for session in (self.read, self.write):
            session.scalar.return_value = ChatSession(chat_session_id=1, user_id=1)
            session.get.side_effect = self.get
        self.read.scalars.return_value.all.side_effect = lambda: self.questions
        self.write.add.side_effect = self.publish

    def get(self, model, ident):
        if model is PublishedChatCheckpoint:
            return self.head
        if model is AgentTurnRecord:
            return self.records.get(ident)
        raise AssertionError("Unexpected business table lookup")

    def publish(self, row):
        if not isinstance(row, PublishedChatCheckpoint):
            raise TypeError("Unexpected business insert")
        self.head = row

    @property
    def published(self) -> PublishedChatCheckpoint:
        assert self.head is not None
        return self.head


class CheckpointPublicationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = FakeBusinessDB()
        self.saver = InMemorySaver()
        self.seen = []
        self.fail_next = False

        async def answer(state):
            self.seen.append(list(state["messages"]))
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("simulated provider failure")
            return {
                "messages": [
                    AIMessage(
                        content="Complete answer", response_metadata={"finish_reason": "stop"}
                    )
                ]
            }

        builder = StateGraph(MessagesState)
        builder.add_node("answer", answer)
        builder.add_edge(START, "answer")
        builder.add_edge("answer", END)
        self.graph = builder.compile(checkpointer=self.saver)

    def accepted(self, question_id="10", text="Current question"):
        snapshot = replace(prepared(), messages=(InputMessage("user", text),))
        return AcceptedMessage("1", question_id, str(uuid4()), snapshot)

    async def run_turn(self, accepted, config):
        return await self.graph.ainvoke(
            {"messages": [current_user_message(accepted)]}, config, durability="sync"
        )

    async def test_empty_baseline_is_published_once_before_execution(self):
        accepted = self.accepted()
        first = await prepare_checkpoint_run(self.graph, accepted, self.db.factory)
        second = await prepare_checkpoint_run(self.graph, accepted, self.db.factory)
        self.assertEqual(first.get("configurable"), second.get("configurable"))
        self.db.write.add.assert_called_once()
        self.db.read.scalars.assert_called_once()
        state = await self.graph.aget_state(first)
        self.assertEqual(state.values["messages"], [])
        self.assertEqual(self.seen, [])

    async def test_legacy_protocol_is_bootstrapped_once_then_only_new_message_is_added(self):
        self.db.questions = [
            Message(message_id=1, content="Legacy question", created_at=datetime(2026, 10, 1))
        ]
        self.db.records[1] = AgentTurnRecord(
            question_id=1,
            user_id=1,
            messages=messages_to_dict(
                [AIMessage(content="Legacy answer", additional_kwargs={"reasoning_content": "r"})]
            ),
        )
        accepted = self.accepted()
        config = await prepare_checkpoint_run(self.graph, accepted, self.db.factory)
        result = await self.run_turn(accepted, config)
        self.assertEqual(
            [m.content for m in result["messages"] if isinstance(m, HumanMessage)],
            ["Legacy question", "Current question"],
        )
        self.db.published.checkpoint_id = await completed_checkpoint_id(self.graph, accepted)
        next_turn = self.accepted("11", "Next question")
        config = await prepare_checkpoint_run(self.graph, next_turn, self.db.factory)
        result = await self.run_turn(next_turn, config)
        self.assertEqual(
            [m.content for m in result["messages"] if isinstance(m, HumanMessage)],
            ["Legacy question", "Current question", "Next question"],
        )
        self.db.read.scalars.assert_called_once()

    async def test_missing_published_checkpoint_is_not_silently_replaced(self):
        self.db.head = PublishedChatCheckpoint(
            chat_session_id=1, user_id=1, checkpoint_id="lost-checkpoint"
        )
        with self.assertRaisesRegex(ValueError, "Published checkpoint is missing"):
            await prepare_checkpoint_run(self.graph, self.accepted(), self.db.factory)
        self.db.write.add.assert_not_called()
        self.db.read.scalars.assert_not_called()
        self.assertEqual(self.seen, [])

    async def test_abandoned_first_attempt_never_becomes_bootstrap_history(self):
        abandoned = self.accepted("9", "Unpublished stale question")
        await self.run_turn(abandoned, thread_config(abandoned))
        accepted = self.accepted()
        config = await prepare_checkpoint_run(self.graph, accepted, self.db.factory)
        result = await self.run_turn(accepted, config)
        self.assertEqual(
            [m.content for m in result["messages"] if isinstance(m, HumanMessage)],
            ["Current question"],
        )

    async def test_failed_attempt_retry_forks_from_published_success(self):
        first = self.accepted()
        config = await prepare_checkpoint_run(self.graph, first, self.db.factory)
        await self.run_turn(first, config)
        self.db.published.checkpoint_id = await completed_checkpoint_id(self.graph, first)
        published = self.db.published.checkpoint_id
        failed = self.accepted("11", "Question whose first attempt fails")
        config = await prepare_checkpoint_run(self.graph, failed, self.db.factory)
        self.fail_next = True
        with self.assertRaisesRegex(RuntimeError, "simulated provider failure"):
            await self.run_turn(failed, config)
        with self.assertRaisesRegex(ValueError, "not complete"):
            await completed_checkpoint_id(self.graph, failed)
        self.assertEqual(self.db.published.checkpoint_id, published)
        retry = replace(failed, attempt_id=str(uuid4()))
        config = await prepare_checkpoint_run(self.graph, retry, self.db.factory)
        self.assertEqual(config.get("configurable", {}).get("checkpoint_id"), published)
        result = await self.run_turn(retry, config)
        humans = [m for m in result["messages"] if isinstance(m, HumanMessage)]
        self.assertEqual([m.id for m in humans], ["question:10", "question:11"])
        self.assertNotEqual(await completed_checkpoint_id(self.graph, retry), published)

    async def test_complete_but_sql_unpublished_turn_is_excluded_from_next_turn(self):
        failed = self.accepted("10", "Answer never committed to business SQL")
        config = await prepare_checkpoint_run(self.graph, failed, self.db.factory)
        await self.run_turn(failed, config)
        unpublished = await completed_checkpoint_id(self.graph, failed)
        self.assertNotEqual(unpublished, self.db.published.checkpoint_id)
        next_turn = self.accepted("11", "New question after the failed save")
        config = await prepare_checkpoint_run(self.graph, next_turn, self.db.factory)
        result = await self.run_turn(next_turn, config)
        self.assertEqual(
            [m.id for m in result["messages"] if isinstance(m, HumanMessage)],
            ["question:11"],
        )

    async def test_foreign_head_is_rejected_before_any_graph_run(self):
        self.db.head = PublishedChatCheckpoint(
            chat_session_id=1, user_id=2, checkpoint_id="foreign"
        )
        with self.assertRaisesRegex(ValueError, "owner mismatch"):
            await prepare_checkpoint_run(self.graph, self.accepted(), self.db.factory)
        self.assertEqual(self.seen, [])

    async def test_terminal_checkpoint_cannot_be_claimed_by_another_attempt(self):
        accepted = self.accepted()
        config = await prepare_checkpoint_run(self.graph, accepted, self.db.factory)
        await self.run_turn(accepted, config)
        self.assertIsInstance(await completed_checkpoint_id(self.graph, accepted), str)
        other_attempt = replace(accepted, attempt_id=str(uuid4()))
        with self.assertRaisesRegex(ValueError, "another attempt"):
            await completed_checkpoint_id(self.graph, other_attempt)

    async def test_terminal_checkpoint_must_contain_the_accepted_question(self):
        accepted = self.accepted()
        config = await prepare_checkpoint_run(self.graph, accepted, self.db.factory)
        await self.run_turn(accepted, config)
        wrong_question = replace(accepted, user_message_id="11")
        with self.assertRaisesRegex(ValueError, "accepted question"):
            await completed_checkpoint_id(self.graph, wrong_question)

    def test_user_message_id_is_stable_across_attempts_and_threads_include_owner(self):
        accepted = self.accepted()
        retry = replace(accepted, attempt_id=str(uuid4()))
        self.assertEqual(current_user_message(accepted).id, current_user_message(retry).id)
        self.assertEqual(
            thread_config(accepted).get("configurable", {}).get("thread_id"),
            "teaching:v1:user:1:session:1",
        )
        with self.assertRaisesRegex(ValueError, "ownership mismatch"):
            thread_config(replace(accepted, session_id="2"))
