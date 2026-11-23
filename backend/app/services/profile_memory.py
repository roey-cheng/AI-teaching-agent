"""个人记忆存取业务；不提取事实、不调用模型，也不新增公开写入接口。"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.agent.profile_store import ProfileStore, save_profile_files
from app.models import MemoryWriteReceipt, Message
from app.models.agent_memory import MEMORY_TOPIC_TYPES
from app.schemas import MemoryListResponse, MemoryResponse, UserResponse
from app.services.errors import MemoryUnavailableError
from app.services.generation_registry import GenerationRegistry
from app.services.generation_result import StaleGenerationError
from app.services.message_submission import AcceptedMessage, _owned_chat


class ProfileFact(BaseModel):
    """内部存储参数，不是 HTTP 请求或已接通的模型工具 Schema。

    summary 是调用方审核、合并后的完整主题摘要，不是待追加的原话。
    只验证结构，不能靠这个类判断事实真假、长期价值或敏感信息。
    """

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )

    memory_key: str
    summary: str = Field(repr=False)

    @field_validator("memory_key")
    @classmethod
    def known_topic(cls, value: str) -> str:
        if value not in MEMORY_TOPIC_TYPES:
            raise ValueError("Unknown memory topic")
        return value

    @field_validator("summary")
    @classmethod
    def valid_summary(cls, value: str) -> str:
        value = value.strip()
        if not 1 <= len(value) <= 500:
            raise ValueError("Memory summary must contain 1 to 500 characters")
        return value


_facts_adapter = TypeAdapter(Annotated[list[ProfileFact], Field(min_length=1, max_length=12)])


@dataclass(frozen=True)
class MemorySaveResult:
    """Confirmed transaction result; replay is not a new successful write."""

    response: MemoryListResponse = field(repr=False)
    saved_facts: dict[str, str] = field(repr=False)
    current_facts: dict[str, str] = field(repr=False)
    replayed: bool = False


def list_profile_memory(
    current_user: UserResponse,
    session_factory: sessionmaker[Session],
) -> MemoryListResponse:
    """先由调用方认证；只读本人记忆，按更新时间/编号倒序返回公开字段。"""
    try:
        with session_factory() as session:
            rows = ProfileStore(int(current_user.user_id), session).rows()
            return MemoryListResponse.model_validate(
                {"items": [MemoryResponse.model_validate(row) for row in rows]}
            )
    except SQLAlchemyError, ValueError:
        # 数据库不可用不能伪装成空列表，也不输出 SQL/凭据/原始记忆。
        raise MemoryUnavailableError() from None


def save_profile_facts(
    accepted: AcceptedMessage,
    facts: list[ProfileFact],
    session_factory: sessionmaker[Session],
    registry: GenerationRegistry,
    *,
    expected_revisions: dict | None = None,
) -> MemorySaveResult:
    """在有效生成的作用域内保存审核后的摘要；只返回本次涉及的记忆。

    身份/问题/attempt 来自后端准入结果，不能让前端或模型构造 accepted。
    本次仅提供存储原语：调用方必须先按记忆策略排除敏感信息、判断明确陈述，
    并把同主题仍有效的旧事实合并进摘要。这里不猜测、不自动拼接自然语言。
    按原问题编号保存一次性回执，和画像更新同事务；失败回答重试不重复写入。
    异步工具在线程中调用整个函数，取消后不再启动新写入，并等待在途事务结束。
    """
    # 全批次先校验；不要写到一半才发现后面的主题或摘要无效。
    checked = _facts_adapter.validate_python(facts, strict=True)
    keys = [fact.memory_key for fact in checked]
    if len(set(keys)) != len(keys):
        raise ValueError("Each memory topic may appear only once per save")
    if accepted.admission_snapshot.session_id != accepted.session_id:
        raise StaleGenerationError()
    chat_id, user_id = int(accepted.session_id), int(accepted.admission_snapshot.user_id)
    try:
        # 与消息结算采用相同锁顺序：本地会话锁 -> 父会话行 -> 原问题行。
        with registry.locked(chat_id) as slot:
            if slot.attempt_id != accepted.attempt_id:
                raise StaleGenerationError()
            with session_factory.begin() as session:
                _owned_chat(session, chat_id, user_id)
                question = session.scalar(
                    select(Message)
                    .where(
                        Message.message_id == int(accepted.user_message_id),
                        Message.chat_session_id == chat_id,
                    )
                    .with_for_update()
                )
                if (
                    question is None
                    or question.role != "USER"
                    or question.attempt_id != accepted.attempt_id
                    or question.generation_status != "RUNNING"
                ):
                    raise StaleGenerationError()
                if (
                    session.scalar(
                        select(Message.message_id).where(
                            Message.in_reply_to_message_id == question.message_id,
                        )
                    )
                    is not None
                ):
                    raise StaleGenerationError()
                # The parent question row is already locked. This lookup and the
                # insert below therefore share the same serialization boundary,
                # including retries that have a new attempt_id but the same ID.
                receipt = session.get(MemoryWriteReceipt, question.message_id)
                replayed = receipt is not None
                if receipt is not None:
                    if receipt.user_id != user_id:
                        raise ValueError("Memory receipt ownership mismatch")
                    confirmed = _facts_adapter.validate_python(receipt.facts, strict=True)
                    if len({fact.memory_key for fact in confirmed}) != len(confirmed):
                        raise ValueError("Invalid memory receipt")
                else:
                    confirmed = sorted(checked, key=lambda item: item.memory_key)
                    now = datetime.now(UTC).replace(tzinfo=None)
                    if expected_revisions is not None:
                        from app.services.memory_tasks import check_and_bump_revisions

                        check_and_bump_revisions(
                            session,
                            user_id,
                            keys,
                            {key: question.message_id for key in keys},
                            expected_revisions,
                        )
                    save_profile_files(session, user_id, confirmed, now)
                    session.add(
                        MemoryWriteReceipt(
                            question_id=question.message_id,
                            user_id=user_id,
                            facts=[fact.model_dump() for fact in confirmed],
                            created_at=now,
                        )
                    )
                # Another conversation may have updated these facts since the
                # receipt was committed. Return actual current values, never the
                # retry's un-applied proposal or an old receipt as current memory.
                rows = ProfileStore(user_id, session).rows()
                confirmed_keys = {fact.memory_key for fact in confirmed}
                result = MemorySaveResult(
                    response=MemoryListResponse.model_validate(
                        {
                            "items": [
                                MemoryResponse.model_validate(row)
                                for row in rows
                                if row.memory_key in confirmed_keys
                            ]
                        }
                    ),
                    saved_facts={fact.memory_key: fact.summary for fact in confirmed},
                    current_facts={row.memory_key: row.summary for row in rows},
                    replayed=replayed,
                )
            # begin 正常退出、COMMIT 得到确认后才返回成功；不释放生成占用。
            return result
    except SQLAlchemyError, ValueError:
        # 提交断线可能已落库；不声称一定回滚、不盲目自动重试、不返回“已保存”。
        raise MemoryUnavailableError() from None
