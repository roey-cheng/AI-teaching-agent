from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, UniqueConstraint
from sqlalchemy.dialects.mysql import BIGINT, CHAR, DATETIME, LONGTEXT, VARCHAR
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Message(Base):
    """一条 USER 问题或最终 ASSISTANT 回复；流式片段不各占一行。"""

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("chat_session_id", "client_message_key", name="uq_messages_session_client_key"),
        UniqueConstraint("in_reply_to_message_id", name="uq_messages_reply"),
        UniqueConstraint("attempt_id", name="uq_messages_attempt"),
        CheckConstraint("role IN ('USER', 'ASSISTANT')", name="ck_messages_role"),
        CheckConstraint("CHAR_LENGTH(content) > 0", name="ck_messages_content_not_empty"),
        CheckConstraint(
            "role <> 'USER' OR CHAR_LENGTH(content) <= 20000", name="ck_messages_user_content_length"
        ),
        # 两个角色共用一张表，因此一些列允许 NULL，再按角色检查哪些必须有值。
        # 必须显式写 IS NOT NULL：SQL 的 CHECK 对 UNKNOWN 也可能放行。
        CheckConstraint(
            """
            (role = 'USER'
                AND client_message_key IS NOT NULL
                AND attempt_id IS NOT NULL
                AND generation_status IS NOT NULL
                AND generation_status IN ('RUNNING', 'SUCCEEDED', 'FAILED')
                AND in_reply_to_message_id IS NULL
                AND model_key IS NULL
                AND (retry_of_attempt_id IS NULL OR retry_of_attempt_id <> attempt_id)
                AND (
                    (generation_status = 'FAILED'
                        AND generation_error_code IS NOT NULL
                        AND CHAR_LENGTH(generation_error_code) > 0
                        AND generation_error_message IS NOT NULL
                        AND CHAR_LENGTH(generation_error_message) > 0)
                    OR (generation_status IN ('RUNNING', 'SUCCEEDED')
                        AND generation_error_code IS NULL
                        AND generation_error_message IS NULL)
                )
            )
            OR (role = 'ASSISTANT'
                AND in_reply_to_message_id IS NOT NULL
                AND client_message_key IS NULL
                AND attempt_id IS NULL
                AND retry_of_attempt_id IS NULL
                AND generation_status IS NULL
                AND generation_error_code IS NULL
                AND generation_error_message IS NULL
            )
            """,
            name="ck_messages_role_fields",
        ),
        Index("ix_messages_session_history", "chat_session_id", "created_at", "message_id"),
        Index("ix_messages_generation", "generation_status", "message_id"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    message_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), primary_key=True, autoincrement=True, nullable=False
    )
    chat_session_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True),
        ForeignKey(
            "chat_sessions.chat_session_id", name="fk_messages_chat_session",
            ondelete="RESTRICT", onupdate="RESTRICT",
        ),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(
        VARCHAR(16, charset="ascii", collation="ascii_bin"), nullable=False
    )
    # 保留正文格式与代码缩进。纯空白、token 预算及回复完整性由业务层检查。
    content: Mapped[str] = mapped_column(LONGTEXT, nullable=False)
    # 自引用外键：回答指向同表中的原问题。是否同会话、是否 USER 仍需业务层验证。
    in_reply_to_message_id: Mapped[int | None] = mapped_column(
        BIGINT(unsigned=True),
        ForeignKey(
            "messages.message_id", name="fk_messages_reply",
            ondelete="RESTRICT", onupdate="RESTRICT",
        ),
        nullable=True,
    )
    client_message_key: Mapped[str | None] = mapped_column(
        VARCHAR(64, charset="ascii", collation="ascii_bin"), nullable=True
    )
    # 模型配置标识，不是密钥，也不能写入带凭据的 URL。
    model_key: Mapped[str | None] = mapped_column(VARCHAR(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)

    # 只在 USER 行保存最近一次生成的状态，不创建独立尝试历史表。
    attempt_id: Mapped[str | None] = mapped_column(
        CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
    )
    # 本次重试所针对的失败编号；旧编号可能已被替换，所以这里没有外键。
    retry_of_attempt_id: Mapped[str | None] = mapped_column(
        CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
    )
    generation_status: Mapped[str | None] = mapped_column(
        VARCHAR(16, charset="ascii", collation="ascii_bin"), nullable=True
    )
    generation_error_code: Mapped[str | None] = mapped_column(
        VARCHAR(64, charset="ascii", collation="ascii_bin"), nullable=True
    )
    generation_error_message: Mapped[str | None] = mapped_column(VARCHAR(500), nullable=True)
