"""登录业务：验证身份、持久化登录状态；尚不设置 Cookie 或处理 HTTP 请求。"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.passwords import DUMMY_PASSWORD_HASH, verify_password
from app.core.tokens import create_session_token, hash_session_token
from app.models import AuthSession, User
from app.schemas import LoginRequest, LoginResponse, UserResponse
from app.services.errors import InvalidCredentialsError, LoginUnavailableError


@dataclass(frozen=True)
class LoginResult:
    """内部返回值，不是 HTTP 正文；以后接口只将 response 作为 JSON 返回。"""

    response: LoginResponse
    token: SecretStr = field(repr=False)  # 仅供未来接口设置 Cookie，不记录或放进 JSON。
    expires_at: datetime  # 有时区的 UTC，供未来 Cookie 的过期设置使用。


def login_user(
    request: LoginRequest,
    session_factory: sessionmaker[Session],
    *,
    current_token: SecretStr | None = None,
) -> LoginResult:
    """current_token 来自未来接口读取的 Cookie，不属于 LoginRequest JSON 字段。"""
    try:
        # 只读取验证所需数据；密码核对在关闭读取 Session 后执行。
        with session_factory() as session:
            snapshot = session.execute(
                select(User.user_id, User.password_hash, User.status).where(User.email == request.email)
            ).one_or_none()

        stored_hash = snapshot.password_hash if snapshot is not None else DUMMY_PASSWORD_HASH
        password_matches = verify_password(request.password, stored_hash)
        if snapshot is None or not password_matches or snapshot.status != "ACTIVE":
            raise InvalidCredentialsError()

        # 生成全新的随机凭据，绝不沿用前端给的旧值；SQL 只接触哈希。
        token = create_session_token()
        token_hash = hash_session_token(token)
        if token_hash is None:
            raise LoginUnavailableError()
        old_hash = hash_session_token(current_token) if current_token is not None else None

        with session_factory.begin() as session:
            # 密码验证耗时期间，账号可能被禁用或改密。短事务内锁定并复核快照。
            user = session.scalar(select(User).where(User.user_id == snapshot.user_id).with_for_update())
            if user is None or user.status != "ACTIVE" or user.password_hash != stored_hash:
                raise InvalidCredentialsError()
            now = datetime.now(UTC).replace(tzinfo=None)
            expires_at = now + timedelta(days=7)
            if old_hash is not None:
                # 只撤销当前请求携带且尚未过期/撤销的凭据，不撤销其他设备。
                # 凭据本身证明持有权；切换账号时也可以撤销该浏览器原账号的凭据。
                session.execute(
                    update(AuthSession)
                    .where(AuthSession.token_hash == old_hash, AuthSession.revoked_at.is_(None),
                           AuthSession.expires_at > now)
                    .values(revoked_at=now)
                )
            user.last_login_at = now
            user.updated_at = now
            session.add(AuthSession(user_id=user.user_id, token_hash=token_hash,
                                    created_at=now, expires_at=expires_at, revoked_at=None))
            session.flush()
            response = LoginResponse(user=UserResponse.model_validate(user))
        # 只有上述修改一起提交成功，才把 Cookie 所需的原凭据交给接口层。
        return LoginResult(response=response, token=token, expires_at=expires_at.replace(tzinfo=UTC))
    except SQLAlchemyError:
        # 不自动重试、不返回尚未确认提交的凭据；不输出 SQL/原始异常/哈希。
        raise LoginUnavailableError() from None
