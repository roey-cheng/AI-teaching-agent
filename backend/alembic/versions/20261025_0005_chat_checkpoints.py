"""Published native checkpoint heads and idempotent explicit memory writes.

Native saver tables are versioned by langgraph-checkpoint-mysql.setup() in the
explicit deployment migration entrypoint, not recreated by application models.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision = "20261025_0005"
down_revision = "20261025_0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "chat_thread_heads",
        sa.Column(
            "chat_session_id",
            mysql.BIGINT(unsigned=True),
            sa.ForeignKey(
                "chat_sessions.chat_session_id", ondelete="RESTRICT", onupdate="RESTRICT"
            ),
            primary_key=True,
            autoincrement=False,
        ),
        sa.Column(
            "user_id",
            mysql.BIGINT(unsigned=True),
            sa.ForeignKey("users.user_id", ondelete="RESTRICT", onupdate="RESTRICT"),
            nullable=False,
        ),
        sa.Column("checkpoint_id", mysql.VARCHAR(64), nullable=False),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_table(
        "memory_write_receipts",
        sa.Column(
            "question_id",
            mysql.BIGINT(unsigned=True),
            sa.ForeignKey("messages.message_id", ondelete="RESTRICT", onupdate="RESTRICT"),
            primary_key=True,
            autoincrement=False,
        ),
        sa.Column(
            "user_id",
            mysql.BIGINT(unsigned=True),
            sa.ForeignKey("users.user_id", ondelete="RESTRICT", onupdate="RESTRICT"),
            nullable=False,
        ),
        sa.Column("facts", mysql.JSON(), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )


def downgrade():
    # Never implicitly erase durable conversation state or write receipts.
    raise RuntimeError("Checkpoint publication migration requires an explicit backup/rollback plan")
