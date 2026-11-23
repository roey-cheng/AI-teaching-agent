"""准入检查新消息与画像；会话历史由 LangGraph checkpoint 管理。"""

from dataclasses import dataclass, field

from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.agent.input_policy import AgentInputPolicy, estimate_message_tokens, estimate_text_tokens
from app.agent.profile_store import PROFILE_MEMORY_PATH, ProfileStore
from app.models import ChatSession
from app.schemas import SendMessageRequest, UserResponse
from app.schemas._validation import format_database_id
from app.services.errors import (
    AgentInputUnavailableError,
    ContextTooLargeError,
    SessionNotFoundError,
)


@dataclass(frozen=True)
class InputMessage:
    role: str
    content: str = field(repr=False)
    source_message_id: str | None = None


@dataclass(frozen=True)
class ChatAdmissionSnapshot:
    """准入阶段的不可变快照，不是最终发给模型的完整输入。

    正式聊天的 messages 只含本条问题；完整会话状态由 checkpoint 恢复。
    兼容流程也可提供本地消息列表，下面的历史计数和裁剪标记只描述该列表，
    不描述 checkpoint 中保存的全部历史。正文不进入 repr 或公开 API 回执。
    """

    user_id: str
    session_id: str
    policy: AgentInputPolicy
    messages: tuple[InputMessage, ...] = field(repr=False)
    profile_memory: str = field(repr=False)
    # 准入/兼容流程最近一次计算的本地估算；刷新画像不自动重算，不含 checkpoint 历史。
    estimated_local_input_tokens: int
    included_history_turns: int
    input_history_trimmed: bool
    counting_method: str = "utf8_bytes_conservative_estimate"
    memory_path: str = "/memories/profile.md"

    def to_langchain_messages(self) -> list[HumanMessage | AIMessage]:
        """只转换本地消息列表；不恢复 checkpoint、不追加系统提示或画像。"""
        return [
            HumanMessage(content=item.content)
            if item.role == "user"
            else AIMessage(content=item.content)
            for item in self.messages
        ]


def _profile_memory(session: Session, user_id: int) -> str:
    try:
        file = ProfileStore(user_id, session).backend().download_files([PROFILE_MEMORY_PATH])[0]
        if file.content is None:
            raise AgentInputUnavailableError()
        return file.content.decode("utf-8")
    except ValueError:
        raise AgentInputUnavailableError() from None


def prepare_chat_admission(
    session: Session,
    current_user: UserResponse,
    session_id: str,
    request: SendMessageRequest,
    policy: AgentInputPolicy,
    *,
    retry_message_id: str | None = None,
) -> ChatAdmissionSnapshot:
    """复用准入事务，只检查本条消息，不裁剪或重建会话历史。

    不开启/提交事务、不写数据、不领取运行锁；调用者负责连接生命周期。
    新发送的问题尚未落库；重试的问题由准入层读取并验证为最后一个失败问题。
    重试快照仍只含原问题一次，并保留其消息编号；这里不读取或重建历史。
    """
    chat_id = int(format_database_id(session_id, "Session ID"))
    user_id = int(format_database_id(current_user.user_id, "User ID"))
    request = SendMessageRequest.model_validate(request.model_dump())
    retry_id = (
        None
        if retry_message_id is None
        else int(format_database_id(retry_message_id, "Message ID"))
    )
    try:
        owned = session.scalar(
            select(ChatSession.chat_session_id).where(
                ChatSession.chat_session_id == chat_id,
                ChatSession.user_id == user_id,
            )
        )
        if owned is None:
            raise SessionNotFoundError()
        memory = _profile_memory(session, user_id)
        used = (
            estimate_text_tokens(policy.system_prompt)
            + estimate_text_tokens(memory)
            + estimate_text_tokens(policy.tool_definitions_json)
            + estimate_message_tokens(request.content)
            + 256  # 外层序列化/路径等开销。
        )
        if used > policy.admission_input_budget_tokens:
            raise ContextTooLargeError()

        return ChatAdmissionSnapshot(
            user_id=str(user_id),
            session_id=str(chat_id),
            policy=policy,
            messages=(
                InputMessage("user", request.content, None if retry_id is None else str(retry_id)),
            ),
            profile_memory=memory,
            estimated_local_input_tokens=used,
            included_history_turns=0,
            input_history_trimmed=False,
        )
    except SQLAlchemyError:
        raise AgentInputUnavailableError() from None
