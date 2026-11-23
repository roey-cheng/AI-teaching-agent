"""An explicit memory batch is applied at most once per logical user message."""

from datetime import datetime
from typing import ClassVar

from sqlalchemy import ForeignKey
from sqlalchemy.dialects.mysql import BIGINT, DATETIME, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MemoryWriteReceipt(Base):
    __tablename__ = "memory_write_receipts"
    __table_args__: ClassVar = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}

    question_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True),
        ForeignKey("messages.message_id", ondelete="RESTRICT", onupdate="RESTRICT"),
        primary_key=True,
        autoincrement=False,
    )
    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True),
        ForeignKey("users.user_id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )
    # Only validated summaries are stored, not model arguments or evidence quotes.
    facts: Mapped[list] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
