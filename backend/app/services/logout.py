"""退出当前登录；只撤销所持凭据，不删除数据，不处理浏览器 Cookie。"""

from datetime import UTC, datetime

from pydantic import SecretStr
from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.tokens import hash_session_token
from app.models import AuthSession
from app.services.errors import LogoutUnavailableError


def logout_user(token: SecretStr | None, session_factory: sessionmaker[Session]) -> None:
    """成功无返回正文；无有效凭据也成功，不要求先通过 get_current_user。"""
    token_hash = hash_session_token(token) if token is not None else None
    if token_hash is None:
        # 未登录或畸形凭据不必查库；未来接口仍应清除浏览器 Cookie。
        return

    try:
        with session_factory.begin() as session:
            now = datetime.now(UTC).replace(tzinfo=None)
            # 一条有条件的 UPDATE：只改匹配凭据，重复退出不覆盖原撤销时间。
            # 不按 user_id 撤销全部设备，也不检查账号状态，禁用账号仍可退出。
            session.execute(
                update(AuthSession)
                .where(AuthSession.token_hash == token_hash,
                       AuthSession.revoked_at.is_(None), AuthSession.expires_at > now)
                .values(revoked_at=now)
            )
        # 即使影响 0 行（不存在、过期、已撤销），也视为成功。
        # 离开 begin 后提交完成，才向调用方报告成功。
    except SQLAlchemyError:
        # 数据库故障不能冒充成功；提交时断线可能无法确定结果，不自动重试。
        raise LogoutUnavailableError() from None
