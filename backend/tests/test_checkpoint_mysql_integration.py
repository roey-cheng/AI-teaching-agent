"""Opt-in integration against an isolated disposable MySQL 8.4 test container.

Only CHECKPOINT_TEST_MYSQL_PORT enables this test. It never reads .env, DB_*
variables, production credentials, or the application database. The target is
always 127.0.0.1/checkpoint_test with disposable test-only credentials.
"""

import os
import unittest
from typing import Annotated, NotRequired, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from sqlalchemy import URL, create_engine

from app.db.checkpoints import (
    check_checkpoint_schema,
    open_checkpoint_saver,
    setup_checkpoint_schema,
)


class ConversationState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    files: NotRequired[dict]


def build_test_graph(saver):
    def answer(state):
        count = sum(isinstance(message, HumanMessage) for message in state["messages"])
        return {
            "messages": [AIMessage(content=f"Seen {count} questions")],
            "files": {"/history/test.md": {"content": ["Earlier conversation"]}},
        }

    builder = StateGraph(ConversationState)
    builder.add_node("answer", answer)
    builder.add_edge(START, "answer")
    builder.add_edge("answer", END)
    return builder.compile(checkpointer=saver)


@unittest.skipUnless(os.environ.get("CHECKPOINT_TEST_MYSQL_PORT"), "isolated MySQL not enabled")
class MySQLCheckpointIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_migrations_reopen_thread_isolation_and_idempotent_input(self):
        engine = create_engine(
            URL.create(
                "mysql+pymysql",
                username="root",
                password="checkpoint-test-only",
                host="127.0.0.1",
                port=int(os.environ["CHECKPOINT_TEST_MYSQL_PORT"]),
                database="checkpoint_test",
                query={"charset": "utf8mb4"},
            ),
            hide_parameters=True,
        )
        try:
            setup_checkpoint_schema(engine)
            setup_checkpoint_schema(engine)  # Native setup must be repeatable.
            check_checkpoint_schema(engine)
            first: RunnableConfig = {"configurable": {"thread_id": "storage-integration-session-1"}}
            second: RunnableConfig = {
                "configurable": {"thread_id": "storage-integration-session-2"}
            }
            async with open_checkpoint_saver(engine) as saver:
                # Only remove the two fixture threads in this disposable test DB.
                await saver.adelete_thread(first["configurable"]["thread_id"])
                await saver.adelete_thread(second["configurable"]["thread_id"])
                graph = build_test_graph(saver)
                result = await graph.ainvoke(
                    {"messages": [HumanMessage(content="First", id="question-1")]},
                    first,
                    durability="sync",
                )
                self.assertEqual(result["messages"][-1].content, "Seen 1 questions")

            # Recreate both pool/saver and graph: no in-process state survives.
            async with open_checkpoint_saver(engine) as saver:
                graph = build_test_graph(saver)
                previous = await graph.aget_state(first)
                self.assertEqual(len(previous.values["messages"]), 2)
                self.assertIn("/history/test.md", previous.values["files"])
                result = await graph.ainvoke(
                    {"messages": [HumanMessage(content="Second", id="question-2")]},
                    first,
                    durability="sync",
                )
                self.assertEqual(result["messages"][-1].content, "Seen 2 questions")
                # Stable framework message IDs replace, rather than duplicate,
                # an input. Business/tool idempotency still belongs to the app.
                result = await graph.ainvoke(
                    {"messages": [HumanMessage(content="Second", id="question-2")]},
                    first,
                    durability="sync",
                )
                self.assertEqual(result["messages"][-1].content, "Seen 2 questions")
                other = await graph.ainvoke(
                    {"messages": [HumanMessage(content="Other", id="question-3")]},
                    second,
                    durability="sync",
                )
                self.assertEqual(other["messages"][-1].content, "Seen 1 questions")
        finally:
            engine.dispose()
