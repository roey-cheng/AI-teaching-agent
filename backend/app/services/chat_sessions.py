"""聊天会话业务：新建、列表和重命名；不处理 HTTP、消息或模型调用。"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.models import ChatSession
from app.schemas import RenameSessionRequest, SessionListResponse, SessionResponse, UserResponse
from app.schemas._validation import format_database_id
from app.services.errors import SessionNotFoundError, SessionUnavailableError


def create_chat_session(
    current_user: UserResponse, session_factory: sessionmaker[Session],
) -> SessionResponse:
    """调用方先用 get_current_user 验证身份；不能从请求正文构造 current_user。"""
    try:
        with session_factory.begin() as session:
            # 三个时间使用同一个值。MySQL 存无时区 UTC，响应 Schema 转成带时区 UTC。
            now = datetime.now(UTC).replace(tzinfo=None)
            chat = ChatSession(
                user_id=int(current_user.user_id),
                title="new chat session",
                title_is_manual=False,
                created_at=now,
                updated_at=now,
                last_activity_at=now,
            )
            session.add(chat)
            session.flush()  # 执行 INSERT、取得自增编号；此时还没有提交。
            response = SessionResponse.model_validate(chat)
        # 退出事务且提交成功后才返回。每次成功调用创建一个独立会话。
        return response
    except SQLAlchemyError:
        # 提交断线可能无法确认结果，不自动重试，避免重复创建。
        # 不泄露 SQL 或凭据；未来接口将业务错误转换成安全的 HTTP 响应。
        raise SessionUnavailableError() from None


def list_chat_sessions(
    current_user: UserResponse, session_factory: sessionmaker[Session],
) -> SessionListResponse:
    """调用方先验证身份；只读本人的全部会话，不查询消息、不分页。"""
    try:
        with session_factory() as session:
            chats = session.scalars(
                select(ChatSession)
                .where(ChatSession.user_id == int(current_user.user_id))
                .order_by(ChatSession.last_activity_at.desc(), ChatSession.chat_session_id.desc())
            ).all()
            # 排序和用户筛选由数据库完成；Schema 仅整理公开字段。
            response = SessionListResponse(
                items=[SessionResponse.model_validate(chat) for chat in chats]
            )
        # 读取结束释放 Session；不更新活动时间，也不创建空会话。
        return response
    except SQLAlchemyError:
        # 数据库故障不能假装成“没有会话”；沿用安全的业务错误，不自动重试。
        raise SessionUnavailableError() from None


def rename_chat_session(
    current_user: UserResponse, session_id: str,
    request: RenameSessionRequest, session_factory: sessionmaker[Session],
) -> SessionResponse:
    """调用方先验证身份和标题 Schema；不存在与不属于本人使用同一种错误。"""
    chat_id = int(format_database_id(session_id, "Session ID"))
    try:
        with session_factory.begin() as session:
            # 同时检查编号和主人，锁定这条记录直到提交，串行处理同一会话的改名。
            chat = session.scalar(
                select(ChatSession)
                .where(ChatSession.chat_session_id == chat_id,
                       ChatSession.user_id == int(current_user.user_id))
                .with_for_update()
            )
            if chat is None:
                raise SessionNotFoundError()
            # 即使新旧标题相同，也标记为手动命名；以后自动标题不能覆盖它。
            chat.title = request.title
            chat.title_is_manual = True
            chat.updated_at = datetime.now(UTC).replace(tzinfo=None)
            # 不改变 created_at 或 last_activity_at，改名不影响侧栏排序。
            # 不检查生成状态：第一版允许 Agent 回复过程中改名。
            session.flush()
            response = SessionResponse.model_validate(chat)
        return response
    except SQLAlchemyError:
        raise SessionUnavailableError() from None
