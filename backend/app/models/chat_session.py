from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, text
from sqlalchemy.dialects.mysql import BIGINT, DATETIME, VARCHAR
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ChatSession(Base):
    """左侧栏中的一段对话；数据库 chat_session_id 对外映射成 session_id。"""

    __tablename__ = "chat_sessions"
    __table_args__ = (
        CheckConstraint("CHAR_LENGTH(title) BETWEEN 1 AND 100", name="ck_chat_sessions_title"),
        CheckConstraint("title_is_manual IN (0, 1)", name="ck_chat_sessions_manual_title"),
        # 后续查询按 last_activity_at、chat_session_id 倒序排列；索引本身不执行排序查询。
        Index("ix_chat_sessions_user_activity", "user_id", "last_activity_at", "chat_session_id"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    chat_session_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), primary_key=True, autoincrement=True, nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True),
        ForeignKey(
            "users.user_id", name="fk_chat_sessions_user",
            ondelete="RESTRICT", onupdate="RESTRICT",
        ),
        nullable=False,
    )
    # 允许不同会话同名；纯空白检查和首条问题自动起标题留给业务层。
    title: Mapped[str] = mapped_column(VARCHAR(100), nullable=False, server_default="新对话")
    # Python 使用 True/False；MySQL 存为布尔对应的 1/0，由上面的 CHECK 限制。
    title_is_manual: Mapped[bool] = mapped_column(
        Boolean(create_constraint=False), nullable=False, server_default=text("0")
    )
    # 新建时三个时间相同；改名只改 updated_at，不改 last_activity_at。
    # 不用 onupdate 自动更新，避免改变已约定的侧栏排序语义。
    last_activity_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
