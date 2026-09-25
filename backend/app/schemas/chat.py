from datetime import datetime

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from app.schemas._validation import database_datetime_as_utc, format_database_id


class CreateSessionRequest(BaseModel):
    """新建会话的请求体只能是 {}；所属用户与默认标题由后端决定。"""

    # 没有声明任何字段，再加 forbid，意味着不能传入 user_id、title 等字段。
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class RenameSessionRequest(BaseModel):
    """只允许改标题，不接受归档状态、用户编号或活动时间。"""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    title: str = Field(min_length=1, max_length=100)

    @field_validator("title", mode="before")
    @classmethod
    def trim_title(cls, value: object) -> object:
        # 去掉首尾空白，再检查长度；中间的空格保留。
        return value.strip() if isinstance(value, str) else value


class SessionResponse(BaseModel):
    """新建、重命名及会话列表共用的单个会话输出结构。"""

    model_config = ConfigDict(
        from_attributes=True, extra="ignore", strict=True, hide_input_in_errors=True
    )

    # 数据库字段叫 chat_session_id，API 字段叫 session_id。
    # validation_alias 只管“从哪里取值”；输出始终叫 session_id。
    session_id: str = Field(validation_alias=AliasChoices("session_id", "chat_session_id"))
    title: str = Field(min_length=1, max_length=100)
    created_at: datetime
    updated_at: datetime
    last_activity_at: datetime

    @field_validator("session_id", mode="before")
    @classmethod
    def format_session_id(cls, value: object) -> str:
        return format_database_id(value, "Session ID")

    @field_validator("created_at", "updated_at", "last_activity_at")
    @classmethod
    def use_utc(cls, value: datetime) -> datetime:
        return database_datetime_as_utc(value)


class SessionListResponse(BaseModel):
    """左侧栏列表：{"items": [...]}，无会话时显式传入空列表。"""

    model_config = ConfigDict(extra="ignore", strict=True, hide_input_in_errors=True)

    items: list[SessionResponse]
    # 不查询数据库、不筛选用户、不排序；这些是后续业务/查询层的职责。
    # 不声明 cursor 等分页字段，也不会自动创建新会话或修改标题。
