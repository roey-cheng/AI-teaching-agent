"""Internal execution data; only reasoning text has an owner-scoped read projection."""

from datetime import date, datetime
from typing import ClassVar

from sqlalchemy import ForeignKey, Index
from sqlalchemy.dialects.mysql import BIGINT, BOOLEAN, DATE, DATETIME, JSON, VARCHAR
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def user_fk(name):
    return ForeignKey("users.user_id", name=name, ondelete="RESTRICT", onupdate="RESTRICT")


class AgentTurnRecord(Base):
    __tablename__ = "agent_turn_records"
    __table_args__: ClassVar = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}
    question_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True),
        ForeignKey("messages.message_id", ondelete="RESTRICT", onupdate="RESTRICT"),
        primary_key=True,
        autoincrement=False,
    )
    user_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), user_fk("fk_turn_record_user"))
    # Serialized LangChain messages, including provider reasoning and paired tool results.
    messages: Mapped[list] = mapped_column(JSON)


class MemoryState(Base):
    __tablename__ = "memory_consolidation_state"
    __table_args__: ClassVar = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}
    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True),
        user_fk("fk_memory_state_user"),
        primary_key=True,
        autoincrement=False,
    )
    cursor: Mapped[int] = mapped_column(BIGINT(unsigned=True), default=0)
    pending_ids: Mapped[list] = mapped_column(JSON, default=list)
    dirty: Mapped[bool] = mapped_column(BOOLEAN, default=False)
    last_activity_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6))
    due_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6))
    failures: Mapped[int] = mapped_column(BIGINT(unsigned=True), default=0)
    paused: Mapped[bool] = mapped_column(BOOLEAN, default=False)


class MemoryJob(Base):
    __tablename__ = "memory_consolidation_jobs"
    __table_args__ = (
        Index("ix_memory_jobs_daily", "quota_date", "user_id"),
        Index("ix_memory_jobs_status", "status"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )
    job_id: Mapped[str] = mapped_column(VARCHAR(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), user_fk("fk_memory_job_user"))
    quota_date: Mapped[date] = mapped_column(DATE)
    status: Mapped[str] = mapped_column(VARCHAR(16))
    source_ids: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6))
    finished_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)
    error_code: Mapped[str | None] = mapped_column(VARCHAR(64), nullable=True)


class MemoryTopicRevision(Base):
    __tablename__ = "memory_topic_revisions"
    __table_args__: ClassVar = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}
    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True),
        user_fk("fk_memory_revision_user"),
        primary_key=True,
    )
    memory_key: Mapped[str] = mapped_column(VARCHAR(128), primary_key=True)
    revision: Mapped[int] = mapped_column(BIGINT(unsigned=True), default=0)
    source_message_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), default=0)
