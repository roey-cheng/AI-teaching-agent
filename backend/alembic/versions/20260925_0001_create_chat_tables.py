"""Create the five initial chat demo tables.

Revision ID: 20260925_0001
Revises: None

这是表结构的历史快照，不导入 app.models 或可变的记忆主题常量。
只创建表、约束及索引，不创建数据库、账号或演示数据。
"""

from alembic import context, op
from alembic.util import CommandError
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision: str = "20260925_0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """先建被引用的 users，再建其余表，最后建 messages。"""
    if not context.is_offline_mode():
        # 不用 IF NOT EXISTS 掩盖旧表结构不一致，也不接管已有同名表。
        inspector = sa.inspect(op.get_bind())
        existing = set(inspector.get_table_names()) | set(inspector.get_view_names())
        if existing & {"users", "auth_sessions", "chat_sessions", "agent_memory", "messages"}:
            raise CommandError(
                "Initial migration requires absent demo tables. Existing tables or views "
                "were found; inspect and reconcile them before migrating."
            )

    op.create_table(
        "users",
        sa.Column("user_id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("email", mysql.VARCHAR(320, charset="utf8mb4", collation="utf8mb4_bin"), nullable=False),
        sa.Column("password_hash", mysql.VARCHAR(255, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column("display_name", mysql.VARCHAR(255), nullable=False),
        sa.Column("status", mysql.VARCHAR(16, charset="ascii", collation="ascii_bin"), server_default="ACTIVE", nullable=False),
        sa.Column("system_role", mysql.VARCHAR(16, charset="ascii", collation="ascii_bin"), server_default="USER", nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("last_login_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.PrimaryKeyConstraint("user_id"),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.CheckConstraint("CHAR_LENGTH(email) > 0", name="ck_users_email_not_empty"),
        sa.CheckConstraint("CHAR_LENGTH(password_hash) > 0", name="ck_users_password_hash_not_empty"),
        sa.CheckConstraint("CHAR_LENGTH(display_name) > 0", name="ck_users_display_name_not_empty"),
        sa.CheckConstraint("status IN ('ACTIVE', 'DISABLED')", name="ck_users_status"),
        sa.CheckConstraint("system_role IN ('USER', 'ADMIN')", name="ck_users_system_role"),
        mysql_engine="InnoDB", mysql_charset="utf8mb4",
    )
    op.create_table(
        "auth_sessions",
        sa.Column("auth_session_id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("user_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("token_hash", mysql.CHAR(64, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("expires_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("revoked_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.PrimaryKeyConstraint("auth_session_id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], name="fk_auth_sessions_user", ondelete="RESTRICT", onupdate="RESTRICT"),
        sa.UniqueConstraint("token_hash", name="uq_auth_sessions_token_hash"),
        sa.CheckConstraint("expires_at > created_at", name="ck_auth_sessions_expiry"),
        sa.CheckConstraint("revoked_at IS NULL OR revoked_at >= created_at", name="ck_auth_sessions_revocation"),
        mysql_engine="InnoDB", mysql_charset="utf8mb4",
    )
    op.create_index("ix_auth_sessions_user_expiry", "auth_sessions", ["user_id", "expires_at"])

    op.create_table(
        "chat_sessions",
        sa.Column("chat_session_id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("user_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("title", mysql.VARCHAR(100), server_default="新对话", nullable=False),
        sa.Column("title_is_manual", sa.Boolean(create_constraint=False), server_default=sa.text("0"), nullable=False),
        sa.Column("last_activity_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.PrimaryKeyConstraint("chat_session_id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], name="fk_chat_sessions_user", ondelete="RESTRICT", onupdate="RESTRICT"),
        sa.CheckConstraint("CHAR_LENGTH(title) BETWEEN 1 AND 100", name="ck_chat_sessions_title"),
        sa.CheckConstraint("title_is_manual IN (0, 1)", name="ck_chat_sessions_manual_title"),
        mysql_engine="InnoDB", mysql_charset="utf8mb4",
    )
    op.create_index("ix_chat_sessions_user_activity", "chat_sessions", ["user_id", "last_activity_at", "chat_session_id"])

    op.create_table(
        "agent_memory",
        sa.Column("memory_id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("user_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("memory_key", mysql.VARCHAR(64, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column("memory_type", mysql.VARCHAR(32, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column("summary", mysql.TEXT(), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.PrimaryKeyConstraint("memory_id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], name="fk_agent_memory_user", ondelete="RESTRICT", onupdate="RESTRICT"),
        sa.UniqueConstraint("user_id", "memory_key", name="uq_agent_memory_user_key"),
        sa.CheckConstraint("CHAR_LENGTH(summary) BETWEEN 1 AND 500", name="ck_agent_memory_summary"),
        # 冻结当前的 25 个 key/type 配对；以后扩充主题需要新迁移。
        sa.CheckConstraint(
            "(memory_key = 'preference.language' AND memory_type = 'LEARNING_PREFERENCE') OR "
            "(memory_key = 'preference.explanation_style' AND memory_type = 'LEARNING_PREFERENCE') OR "
            "(memory_key = 'preference.detail_level' AND memory_type = 'LEARNING_PREFERENCE') OR "
            "(memory_key = 'preference.example_style' AND memory_type = 'LEARNING_PREFERENCE') OR "
            "(memory_key = 'preference.code_style' AND memory_type = 'LEARNING_PREFERENCE') OR "
            "(memory_key = 'preference.hint_style' AND memory_type = 'LEARNING_PREFERENCE') OR "
            "(memory_key = 'preference.pacing' AND memory_type = 'LEARNING_PREFERENCE') OR "
            "(memory_key = 'preference.knowledge_connections' AND memory_type = 'LEARNING_PREFERENCE') OR "
            "(memory_key = 'learning.goal' AND memory_type = 'LEARNING_GOAL') OR "
            "(memory_key = 'learning.current_topic' AND memory_type = 'LEARNING_GOAL') OR "
            "(memory_key = 'programming.level' AND memory_type = 'PROGRAMMING_BACKGROUND') OR "
            "(memory_key = 'programming.languages' AND memory_type = 'PROGRAMMING_BACKGROUND') OR "
            "(memory_key = 'programming.known_concepts' AND memory_type = 'PROGRAMMING_BACKGROUND') OR "
            "(memory_key = 'programming.tools' AND memory_type = 'PROGRAMMING_BACKGROUND') OR "
            "(memory_key = 'programming.environment' AND memory_type = 'PROGRAMMING_BACKGROUND') OR "
            "(memory_key = 'personal.preferred_name' AND memory_type = 'PERSONAL_BACKGROUND') OR "
            "(memory_key = 'personal.occupation' AND memory_type = 'PERSONAL_BACKGROUND') OR "
            "(memory_key = 'personal.interests' AND memory_type = 'PERSONAL_BACKGROUND') OR "
            "(memory_key = 'personal.long_term_goal' AND memory_type = 'PERSONAL_BACKGROUND') OR "
            "(memory_key = 'personal.timezone' AND memory_type = 'PERSONAL_BACKGROUND') OR "
            "(memory_key = 'personal.general_location' AND memory_type = 'PERSONAL_BACKGROUND') OR "
            "(memory_key = 'personal.daily_routine' AND memory_type = 'DAILY_PREFERENCE') OR "
            "(memory_key = 'preference.conversation_tone' AND memory_type = 'DAILY_PREFERENCE') OR "
            "(memory_key = 'lifestyle.food_preferences' AND memory_type = 'DAILY_PREFERENCE') OR "
            "(memory_key = 'lifestyle.activity_preferences' AND memory_type = 'DAILY_PREFERENCE')",
            name="ck_agent_memory_topic_type",
        ),
        mysql_engine="InnoDB", mysql_charset="utf8mb4",
    )
    op.create_index("ix_agent_memory_user_updated", "agent_memory", ["user_id", "updated_at", "memory_id"])

    op.create_table(
        "messages",
        sa.Column("message_id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("chat_session_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("role", mysql.VARCHAR(16, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column("content", mysql.LONGTEXT(), nullable=False),
        sa.Column("in_reply_to_message_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("client_message_key", mysql.VARCHAR(64, charset="ascii", collation="ascii_bin"), nullable=True),
        sa.Column("model_key", mysql.VARCHAR(128), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("attempt_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True),
        sa.Column("retry_of_attempt_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True),
        sa.Column("generation_status", mysql.VARCHAR(16, charset="ascii", collation="ascii_bin"), nullable=True),
        sa.Column("generation_error_code", mysql.VARCHAR(64, charset="ascii", collation="ascii_bin"), nullable=True),
        sa.Column("generation_error_message", mysql.VARCHAR(500), nullable=True),
        sa.PrimaryKeyConstraint("message_id"),
        sa.ForeignKeyConstraint(["chat_session_id"], ["chat_sessions.chat_session_id"], name="fk_messages_chat_session", ondelete="RESTRICT", onupdate="RESTRICT"),
        sa.ForeignKeyConstraint(["in_reply_to_message_id"], ["messages.message_id"], name="fk_messages_reply", ondelete="RESTRICT", onupdate="RESTRICT"),
        sa.UniqueConstraint("chat_session_id", "client_message_key", name="uq_messages_session_client_key"),
        sa.UniqueConstraint("in_reply_to_message_id", name="uq_messages_reply"),
        sa.UniqueConstraint("attempt_id", name="uq_messages_attempt"),
        sa.CheckConstraint("role IN ('USER', 'ASSISTANT')", name="ck_messages_role"),
        sa.CheckConstraint("CHAR_LENGTH(content) > 0", name="ck_messages_content_not_empty"),
        sa.CheckConstraint("role <> 'USER' OR CHAR_LENGTH(content) <= 20000", name="ck_messages_user_content_length"),
        sa.CheckConstraint(
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
        mysql_engine="InnoDB", mysql_charset="utf8mb4",
    )
    op.create_index("ix_messages_session_history", "messages", ["chat_session_id", "created_at", "message_id"])
    op.create_index("ix_messages_generation", "messages", ["generation_status", "message_id"])


def downgrade() -> None:
    """逆序删表，也会删除其中的数据；仅在明确允许丢弃数据时使用。"""
    op.drop_table("messages")
    op.drop_table("agent_memory")
    op.drop_table("chat_sessions")
    op.drop_table("auth_sessions")
    op.drop_table("users")
