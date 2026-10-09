"""检查已有登录凭据；只读数据库，不续期、不处理 Cookie 或 HTTP。"""

from datetime import UTC, datetime

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.tokens import hash_session_token
from app.models import AuthSession, User
from app.schemas import UserResponse
from app.services.errors import AuthenticationRequiredError, AuthenticationUnavailableError


def get_current_user(
    token: SecretStr | None, session_factory: sessionmaker[Session],
) -> UserResponse:
    """身份只由凭据关联的数据库记录决定，不接收前端指定的 user_id。"""
    # 没带凭据或格式不对，直接拒绝，不占用数据库连接。
    token_hash = hash_session_token(token) if token is not None else None
    if token_hash is None:
        raise AuthenticationRequiredError()

    try:
        with session_factory() as session:
            # JOIN 把登录记录与所属用户一起查出；不读取密码哈希或完整 ORM 对象。
            record = session.execute(
                select(User.user_id, User.email, User.display_name, User.status,
                       AuthSession.expires_at, AuthSession.revoked_at)
                .join(AuthSession, AuthSession.user_id == User.user_id)
                .where(AuthSession.token_hash == token_hash)
            ).one_or_none()
            # 查询完成后取当前 UTC 时间；到期时刻本身也算失效，不自动续期。
            now = datetime.now(UTC).replace(tzinfo=None)
            if (record is None or record.revoked_at is not None
                    or record.expires_at <= now or record.status != "ACTIVE"):
                raise AuthenticationRequiredError()
            result = UserResponse.model_validate(record)
        return result
    except SQLAlchemyError:
        # 查询故障不是“你没登录”；不泄露 SQL、连接信息或凭据，也不自动重试。
        raise AuthenticationUnavailableError() from None
