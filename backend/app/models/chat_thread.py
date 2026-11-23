"""Business publication pointer; checkpoint contents belong to the native saver."""

from typing import ClassVar

from sqlalchemy import ForeignKey
from sqlalchemy.dialects.mysql import BIGINT, VARCHAR
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PublishedChatCheckpoint(Base):
    """业务确认可用于下一次运行的存档指针；不一定是 saver 中最新的记录。

    表/列名是已有持久化契约，Python 类改名不重命名数据库对象。
    """

    __tablename__ = "chat_thread_heads"
    __table_args__: ClassVar = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}

    chat_session_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True),
        ForeignKey("chat_sessions.chat_session_id", ondelete="RESTRICT", onupdate="RESTRICT"),
        primary_key=True,
        autoincrement=False,
    )
    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True),
        ForeignKey("users.user_id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )
    checkpoint_id: Mapped[str] = mapped_column(VARCHAR(64), nullable=False)
