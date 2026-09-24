from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, UniqueConstraint
from sqlalchemy.dialects.mysql import BIGINT, CHAR, DATETIME
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AuthSession(Base):
    """一次登录的状态；不是左侧栏的聊天会话，也不是 ORM 的 Session。"""

    __tablename__ = "auth_sessions"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_auth_sessions_token_hash"),
        CheckConstraint("expires_at > created_at", name="ck_auth_sessions_expiry"),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="ck_auth_sessions_revocation",
        ),
        Index("ix_auth_sessions_user_expiry", "user_id", "expires_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    auth_session_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), primary_key=True, autoincrement=True, nullable=False
    )
    # FK：这条登录记录必须属于一个真实存在的用户。
    # RESTRICT：仍有引用时，拒绝删除用户或修改用户主键，不自动级联删除。
    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True),
        ForeignKey(
            "users.user_id", name="fk_auth_sessions_user",
            ondelete="RESTRICT", onupdate="RESTRICT",
        ),
        nullable=False,
    )
    # 只存随机登录凭据的哈希；不存 Cookie 原始凭据，也不是用户密码哈希。
    # SHA-256 计算、64 位十六进制格式检查由后续业务代码负责。
    token_hash: Mapped[str] = mapped_column(
        CHAR(64, charset="ascii", collation="ascii_bin"), nullable=False
    )
    # 都由业务代码显式提供 UTC；expires_at = created_at + 7 天，不自动续期。
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)
