from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas._types import DatabaseID, UTCDateTime, UUIDString

GenerationStatus = Literal["RUNNING", "SUCCEEDED", "FAILED"]


class SendMessageRequest(BaseModel):
    """接口 9：只接收发送键和正文，不接受用户、模型或工具配置。"""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    client_message_key: UUIDString
    content: str = Field(min_length=1, max_length=20000)

    @field_validator("content")
    @classmethod
    def reject_blank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Message content must not be blank")
        # 只用 strip() 判断是否全为空白；返回原文，不能破坏代码缩进或换行。
        return value


class RetryMessageRequest(BaseModel):
    """接口 10：重试哪个失败编号；是否有权限、是否仍可重试由业务层判断。"""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    failed_attempt_id: UUIDString


class _MessageOutput(BaseModel):
    """输出只保留声明的字段，防止内部模型标识、发送键等意外返回。"""

    model_config = ConfigDict(
        from_attributes=True, extra="ignore", strict=True, hide_input_in_errors=True
    )


class GenerationError(_MessageOutput):
    """历史中的简短错误摘要，不是原始异常；后端必须先过滤敏感信息。"""

    code: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=500)


class GenerationResponse(_MessageOutput):
    """每条 USER 问题的最近一次生成状态，不是会话级 latest_generation。"""

    attempt_id: UUIDString
    status: GenerationStatus
    assistant_message_id: DatabaseID | None
    error: GenerationError | None
    can_retry: bool

    @model_validator(mode="after")
    def check_status_fields(self) -> Self:
        # model_validator 同时检查多个字段，避免“成功却没有答案编号”等矛盾。
        if self.status == "SUCCEEDED":
            if self.assistant_message_id is None or self.error is not None or self.can_retry:
                raise ValueError("Successful generation requires an answer ID, no error, and can_retry=false")
        elif self.status == "FAILED":
            if self.assistant_message_id is not None or self.error is None:
                raise ValueError("Failed generation requires an error and no answer ID")
        elif self.assistant_message_id is not None or self.error is not None or self.can_retry:
            raise ValueError("Running generation must have no answer ID or error, and can_retry=false")
        return self


class UserMessageResponse(_MessageOutput):
    """USER 必须包含 generation；查询层需把数据库的平铺状态字段组装进去。"""

    message_id: DatabaseID
    role: Literal["USER"]
    content: str = Field(min_length=1, max_length=20000)
    in_reply_to_message_id: None
    created_at: UTCDateTime
    generation: GenerationResponse


class AssistantMessageResponse(_MessageOutput):
    """完整回答的公开字段；不声明 generation，因此不会输出 generation:null。"""

    message_id: DatabaseID
    role: Literal["ASSISTANT"]
    content: str = Field(min_length=1)
    in_reply_to_message_id: DatabaseID
    created_at: UTCDateTime

    @model_validator(mode="after")
    def reject_self_reply(self) -> Self:
        if self.message_id == self.in_reply_to_message_id:
            raise ValueError("An assistant message cannot reply to itself")
        return self


# 同一个历史列表容纳两种消息。Pydantic 根据 role 选择对应规则，不靠猜测。
MessageResponse = Annotated[
    UserMessageResponse | AssistantMessageResponse, Field(discriminator="role")
]


class MessageHistoryResponse(_MessageOutput):
    """接口 8：一个会话的全部消息；只校验给定快照，不查库、不排序、不鉴权。"""

    session_id: DatabaseID
    is_generating: bool
    items: list[MessageResponse]

    @model_validator(mode="after")
    def check_snapshot_consistency(self) -> Self:
        # 这里只能检查传入列表内部是否矛盾。服务层仍负责查询完整历史、锁和用户隔离。
        by_id = {item.message_id: item for item in self.items}
        if len(by_id) != len(self.items):
            raise ValueError("History must not contain duplicate message IDs")
        order = [(item.created_at, int(item.message_id)) for item in self.items]
        if order != sorted(order):
            raise ValueError("History must be ordered by created_at and message_id ascending")
        users = [item for item in self.items if isinstance(item, UserMessageResponse)]
        running_count = sum(item.generation.status == "RUNNING" for item in users)
        if running_count > 1 or (running_count and not self.is_generating):
            raise ValueError("History contains inconsistent running generation state")
        for user in users:
            if user.generation.can_retry and (self.is_generating or user is not users[-1]):
                raise ValueError("Only the last failed question in an idle session may be retried")
            if user.generation.status == "SUCCEEDED":
                answer_id = user.generation.assistant_message_id
                answer = by_id.get(answer_id) if answer_id is not None else None
                if not isinstance(answer, AssistantMessageResponse) or answer.in_reply_to_message_id != user.message_id:
                    raise ValueError("Successful generation must reference its saved assistant message")
        for item in self.items:
            if isinstance(item, AssistantMessageResponse):
                question = by_id.get(item.in_reply_to_message_id)
                if not isinstance(question, UserMessageResponse) or question.generation.assistant_message_id != item.message_id:
                    raise ValueError("Assistant message must match a successful question in this history")
        # 清理过程中可能已写 FAILED 但仍占用会话，所以允许 busy=true 且没有 RUNNING。
        return self


class DuplicateMessageResponse(_MessageOutput):
    """接口 9/10 的重复请求 JSON 回执；不包含正文，前端随后查询历史。"""

    duplicate: Literal[True]
    session_id: DatabaseID
    user_message_id: DatabaseID
    attempt_id: UUIDString
    status: GenerationStatus
    assistant_message_id: DatabaseID | None

    @field_validator("duplicate", mode="before")
    @classmethod
    def require_boolean_true(cls, value: object) -> object:
        # Python 中 1 == True，但 JSON 的数字 1 不能代替布尔 true。
        if value is not True:
            raise ValueError("duplicate must be the boolean true")
        return value

    @model_validator(mode="after")
    def check_answer_id(self) -> Self:
        if (self.status == "SUCCEEDED") != (self.assistant_message_id is not None):
            raise ValueError("An answer ID is required only for successful generation")
        if self.assistant_message_id == self.user_message_id:
            raise ValueError("The answer ID must differ from the question ID")
        return self
