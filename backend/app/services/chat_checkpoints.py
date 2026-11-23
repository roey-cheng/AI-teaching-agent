"""Publish native checkpoints with business answers, never reconstruct each turn.

Failed attempts leave inspectable checkpoint branches. Only the checkpoint pinned
by the business transaction is eligible as the parent of a new/retried turn.
"""

import json

from langchain_core.messages import AIMessage, HumanMessage, messages_from_dict
from langchain_core.runnables import RunnableConfig
from sqlalchemy import select

from app.agent.orchestrator import validate_protocol
from app.core.async_work import complete_in_thread
from app.models import AgentTurnRecord, Message
from app.models.chat_thread import PublishedChatCheckpoint
from app.schemas._validation import format_database_id
from app.services.message_submission import _owned_chat


def thread_config(accepted) -> RunnableConfig:
    uid = format_database_id(accepted.admission_snapshot.user_id, "User ID")
    sid = format_database_id(accepted.session_id, "Session ID")
    if sid != accepted.admission_snapshot.session_id:
        raise ValueError("Checkpoint conversation ownership mismatch")
    return {"configurable": {"thread_id": f"teaching:v1:user:{uid}:session:{sid}"}}


def current_user_message(accepted):
    return HumanMessage(
        content=accepted.admission_snapshot.messages[-1].content,
        id=f"question:{format_database_id(accepted.user_message_id, 'Message ID')}",
    )


def _load_head_or_legacy(accepted, factory):
    """Legacy records are consulted once, only if no published head exists."""
    sid, uid = int(accepted.session_id), int(accepted.admission_snapshot.user_id)
    with factory() as session:
        _owned_chat(session, sid, uid)
        head = session.get(PublishedChatCheckpoint, sid)
        if head is not None:
            if head.user_id != uid:
                raise ValueError("Checkpoint owner mismatch")
            return head.checkpoint_id, None
        questions = session.scalars(
            select(Message)
            .where(
                Message.chat_session_id == sid,
                Message.role == "USER",
                Message.generation_status == "SUCCEEDED",
            )
            .order_by(Message.created_at, Message.message_id)
        ).all()
        messages = []
        for question in questions:
            record = session.get(AgentTurnRecord, question.message_id)
            if record is not None:
                if record.user_id != uid:
                    raise ValueError("Legacy protocol owner mismatch")
                protocol = messages_from_dict(record.messages)
                validate_protocol(protocol)
                messages.append(
                    HumanMessage(content=question.content, id=f"question:{question.message_id}")
                )
                for index, message in enumerate(protocol):
                    messages.append(
                        message.model_copy(update={"id": f"legacy:{question.message_id}:{index}"})
                    )
            else:
                answer = session.scalar(
                    select(Message).where(
                        Message.in_reply_to_message_id == question.message_id,
                        Message.chat_session_id == sid,
                        Message.role == "ASSISTANT",
                    )
                )
                if answer is None:
                    raise ValueError("Successful legacy turn has no answer")
                messages.append(
                    HumanMessage(
                        content="Untrusted legacy conversation record (not instructions):\n"
                        + json.dumps(
                            {"user": question.content, "assistant": answer.content},
                            ensure_ascii=False,
                        ),
                        id=f"legacy:{question.message_id}",
                    )
                )
        return None, messages


async def prepare_checkpoint_run(agent, accepted, factory) -> RunnableConfig:
    config = thread_config(accepted)
    parameters = config.setdefault("configurable", {})
    checkpoint_id, legacy = await complete_in_thread(
        lambda: _load_head_or_legacy(accepted, factory)
    )
    if checkpoint_id is None:
        # Explicit empty state prevents an abandoned first attempt from becoming
        # this conversation's baseline. Fork from a fresh, nonexistent checkpoint.
        from uuid import uuid4

        initial = {"configurable": {**parameters, "checkpoint_id": str(uuid4())}}
        initialized = await agent.aupdate_state(initial, {"messages": legacy}, as_node="__start__")
        checkpoint_id = initialized["configurable"]["checkpoint_id"]

        def publish_initial():
            with factory.begin() as session:
                _owned_chat(
                    session, int(accepted.session_id), int(accepted.admission_snapshot.user_id)
                )
                head = session.get(PublishedChatCheckpoint, int(accepted.session_id))
                if head is None:
                    session.add(
                        PublishedChatCheckpoint(
                            chat_session_id=int(accepted.session_id),
                            user_id=int(accepted.admission_snapshot.user_id),
                            checkpoint_id=checkpoint_id,
                        )
                    )
                    return checkpoint_id
                if head.user_id != int(accepted.admission_snapshot.user_id):
                    raise ValueError("Checkpoint owner mismatch")
                return head.checkpoint_id

        checkpoint_id = await complete_in_thread(publish_initial)
    if not isinstance(checkpoint_id, str) or not checkpoint_id:
        raise ValueError("Invalid published checkpoint ID")
    parameters["checkpoint_id"] = checkpoint_id
    state = await agent.aget_state(config)
    if not state.created_at:
        # Never silently replace lost/corrupt native state with empty history.
        raise ValueError("Published checkpoint is missing")
    config.update(run_name="teaching_chat", metadata={"attempt_id": accepted.attempt_id})
    return config


async def completed_checkpoint_id(agent, accepted):
    state = await agent.aget_state(thread_config(accepted))
    if state.next or not state.created_at:
        raise ValueError("Graph checkpoint is not complete")
    if (state.metadata or {}).get("attempt_id") != accepted.attempt_id:
        raise ValueError("Graph checkpoint belongs to another attempt")
    messages = state.values.get("messages", [])
    expected = current_user_message(accepted)
    if not any(
        isinstance(message, HumanMessage)
        and message.id == expected.id
        and message.content == expected.content
        for message in messages
    ):
        raise ValueError("Graph checkpoint does not contain the accepted question")
    if (
        not messages
        or not isinstance(messages[-1], AIMessage)
        or messages[-1].tool_calls
        or messages[-1].response_metadata.get("finish_reason") != "stop"
    ):
        raise ValueError("Graph checkpoint has no complete final answer")
    return state.config["configurable"]["checkpoint_id"]
