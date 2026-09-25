"""接口数据格式的统一入口；不是 SQLAlchemy 表模型，不连接数据库。"""

from app.schemas.auth import LoginRequest, LoginResponse, RegisterRequest, RegisterResponse
from app.schemas.chat import (
    CreateSessionRequest,
    RenameSessionRequest,
    SessionListResponse,
    SessionResponse,
)
from app.schemas.error import ErrorDetail, ErrorResponse
from app.schemas.memory import MemoryListResponse, MemoryResponse, MemoryType
from app.schemas.message import (
    AssistantMessageResponse,
    DuplicateMessageResponse,
    GenerationError,
    GenerationResponse,
    MessageHistoryResponse,
    MessageResponse,
    RetryMessageRequest,
    SendMessageRequest,
    UserMessageResponse,
)
from app.schemas.stream import (
    MessageDeltaData,
    MessageDoneData,
    MessageErrorData,
    MessageStartData,
    StreamError,
)
from app.schemas.user import UserResponse

__all__ = [
    "AssistantMessageResponse",
    "CreateSessionRequest",
    "DuplicateMessageResponse",
    "ErrorDetail",
    "ErrorResponse",
    "GenerationError",
    "GenerationResponse",
    "LoginRequest",
    "LoginResponse",
    "MemoryListResponse",
    "MemoryResponse",
    "MemoryType",
    "MessageDeltaData",
    "MessageDoneData",
    "MessageErrorData",
    "MessageHistoryResponse",
    "MessageResponse",
    "MessageStartData",
    "RegisterRequest",
    "RegisterResponse",
    "RenameSessionRequest",
    "RetryMessageRequest",
    "SendMessageRequest",
    "SessionListResponse",
    "SessionResponse",
    "StreamError",
    "UserMessageResponse",
    "UserResponse",
]
