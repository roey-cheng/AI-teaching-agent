from datetime import date, datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index
from sqlalchemy.dialects.mysql import BIGINT, CHAR, DATE, DATETIME, TINYINT, VARCHAR
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class GenerationControl(Base):
    """固定一行的数据库准入互斥点；不保存额度计数。"""

    __tablename__ = "generation_control"
    __table_args__ = (
        CheckConstraint("control_id = 1", name="ck_generation_control_singleton"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    control_id: Mapped[int] = mapped_column(
        TINYINT(unsigned=True), primary_key=True, autoincrement=False, nullable=False
    )


class GenerationAttempt(Base):
    """一次真正获准的新发送或失败重试；网络重复发送不新增记录。"""

    __tablename__ = "generation_attempts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('RUNNING', 'SUCCEEDED', 'FAILED')",
            name="ck_generation_attempts_status",
        ),
        CheckConstraint(
            "(status = 'RUNNING' AND finished_at IS NULL) OR "
            "(status IN ('SUCCEEDED', 'FAILED') AND finished_at IS NOT NULL)",
            name="ck_generation_attempts_finished",
        ),
        Index("ix_generation_attempts_daily_user", "quota_date", "user_id", "attempt_id"),
        Index("ix_generation_attempts_status", "status", "attempt_id"),
        Index("ix_generation_attempts_message", "message_id", "created_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    attempt_id: Mapped[str] = mapped_column(
        CHAR(36, charset="ascii", collation="ascii_bin"), primary_key=True, nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True),
        ForeignKey(
            "users.user_id",
            name="fk_generation_attempts_user",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        nullable=False,
    )
    message_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True),
        ForeignKey(
            "messages.message_id",
            name="fk_generation_attempts_message",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        nullable=False,
    )
    quota_date: Mapped[date] = mapped_column(DATE, nullable=False)
    status: Mapped[str] = mapped_column(
        VARCHAR(16, charset="ascii", collation="ascii_bin"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)
