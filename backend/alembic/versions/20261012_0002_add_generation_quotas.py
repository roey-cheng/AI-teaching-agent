"""Persist public chat quotas and active generation attempts.

Revision ID: 20261012_0002
Revises: 20260925_0001
"""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "20261012_0002"
down_revision: str | None = "20260925_0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "generation_control",
        sa.Column(
            "control_id", mysql.TINYINT(unsigned=True), autoincrement=False, nullable=False
        ),
        sa.PrimaryKeyConstraint("control_id"),
        sa.CheckConstraint("control_id = 1", name="ck_generation_control_singleton"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.execute(sa.text("INSERT INTO generation_control (control_id) VALUES (1)"))

    op.create_table(
        "generation_attempts",
        sa.Column(
            "attempt_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column("user_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("message_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("quota_date", sa.Date(), nullable=False),
        sa.Column(
            "status",
            mysql.VARCHAR(16, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("finished_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.PrimaryKeyConstraint("attempt_id"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
            name="fk_generation_attempts_user",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.message_id"],
            name="fk_generation_attempts_message",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('RUNNING', 'SUCCEEDED', 'FAILED')",
            name="ck_generation_attempts_status",
        ),
        sa.CheckConstraint(
            "(status = 'RUNNING' AND finished_at IS NULL) OR "
            "(status IN ('SUCCEEDED', 'FAILED') AND finished_at IS NOT NULL)",
            name="ck_generation_attempts_finished",
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_generation_attempts_daily_user",
        "generation_attempts",
        ["quota_date", "user_id", "attempt_id"],
    )
    op.create_index(
        "ix_generation_attempts_status", "generation_attempts", ["status", "attempt_id"]
    )
    op.create_index(
        "ix_generation_attempts_message",
        "generation_attempts",
        ["message_id", "created_at"],
    )

    # 旧版本只保留每条问题最近一次 attempt_id，因此升级时只能从现状回填这些尝试。
    # 空生产库不会产生回填；本地已有数据则至少不会把 RUNNING 并发计数遗漏。
    op.execute(
        sa.text(
            """
            INSERT INTO generation_attempts
                (attempt_id, user_id, message_id, quota_date, status, created_at, finished_at)
            SELECT
                m.attempt_id,
                cs.user_id,
                m.message_id,
                DATE(m.updated_at),
                m.generation_status,
                m.updated_at,
                CASE WHEN m.generation_status = 'RUNNING' THEN NULL ELSE m.updated_at END
            FROM messages AS m
            INNER JOIN chat_sessions AS cs
                ON cs.chat_session_id = m.chat_session_id
            WHERE m.role = 'USER'
            """
        )
    )


def downgrade() -> None:
    op.drop_table("generation_attempts")
    op.drop_table("generation_control")
