"""只定义 SSE 的 data 数据；不启动流、不调用 Agent，也不负责发送事件。"""

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from app.schemas._types import DatabaseID, UUIDString
from app.schemas.message import AssistantMessageResponse, GenerationError


class _EventData(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, hide_input_in_errors=True)


class MessageStartData(_EventData):
    # ClassVar 是类上的标签，不是 JSON 字段。事件名放在 SSE 的 event: 行。
    event_name: ClassVar[str] = "message_start"

    session_id: DatabaseID
    user_message_id: DatabaseID
    attempt_id: UUIDString


class MessageDeltaData(_EventData):
    event_name: ClassVar[str] = "message_delta"

    attempt_id: UUIDString
    # 空片段不用发送；但空格、换行可能是正文的一部分，不能 strip 后丢掉。
    text: str = Field(min_length=1)


class MessageDoneData(_EventData):
    event_name: ClassVar[str] = "message_done"

    attempt_id: UUIDString
    assistant_message: AssistantMessageResponse
    # “回答已保存”必须由业务代码保证：事务成功提交后才能发送 done。


class StreamError(GenerationError):
    """SSE 错误比历史错误摘要多一个 request_id，用于定位本次请求。"""

    request_id: str = Field(min_length=1)


class MessageErrorData(_EventData):
    event_name: ClassVar[str] = "message_error"

    attempt_id: UUIDString
    error: StreamError
