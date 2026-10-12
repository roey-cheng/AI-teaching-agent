"""Durable memory work and provider protocol history; no historical backfill."""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision = "20261012_0003"
down_revision = "20261012_0002"
branch_labels = None
depends_on = None


def upgrade():
    def uid(name, primary=False):
        return sa.Column(
            "user_id",
            mysql.BIGINT(unsigned=True),
            sa.ForeignKey("users.user_id", name=name, ondelete="RESTRICT", onupdate="RESTRICT"),
            primary_key=primary,
            autoincrement=False,
            nullable=False,
        )

    op.create_table(
        "agent_turn_records",
        sa.Column(
            "question_id",
            mysql.BIGINT(unsigned=True),
            sa.ForeignKey("messages.message_id", ondelete="RESTRICT", onupdate="RESTRICT"),
            primary_key=True,
            autoincrement=False,
        ),
        uid("fk_turn_record_user"),
        sa.Column("messages", mysql.JSON(), nullable=False),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_table(
        "memory_consolidation_state",
        uid("fk_memory_state_user", True),
        sa.Column("cursor", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("pending_ids", mysql.JSON(), nullable=False),
        sa.Column("dirty", mysql.BOOLEAN(), nullable=False),
        sa.Column("last_activity_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("due_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("failures", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("paused", mysql.BOOLEAN(), nullable=False),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_table(
        "memory_consolidation_jobs",
        sa.Column("job_id", mysql.VARCHAR(36), primary_key=True),
        uid("fk_memory_job_user"),
        sa.Column("quota_date", mysql.DATE(), nullable=False),
        sa.Column("status", mysql.VARCHAR(16), nullable=False),
        sa.Column("source_ids", mysql.JSON(), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("finished_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("error_code", mysql.VARCHAR(64), nullable=True),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_memory_jobs_daily", "memory_consolidation_jobs", ["quota_date", "user_id"])
    op.create_index("ix_memory_jobs_status", "memory_consolidation_jobs", ["status"])
    op.create_table(
        "memory_topic_revisions",
        uid("fk_memory_revision_user", True),
        sa.Column("memory_key", mysql.VARCHAR(128), primary_key=True),
        sa.Column("revision", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("source_message_id", mysql.BIGINT(unsigned=True), nullable=False),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    # Existing users begin at the migration boundary, not at message zero.
    op.execute(
        "INSERT INTO memory_consolidation_state "
        "(user_id, `cursor`, pending_ids, dirty, last_activity_at, due_at, failures, paused) "
        "SELECT user_id, (SELECT COALESCE(MAX(message_id), 0) FROM messages), "
        "JSON_ARRAY(), 0, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6), 0, 0 FROM users"
    )


def downgrade():
    for table in (
        "memory_topic_revisions",
        "memory_consolidation_jobs",
        "memory_consolidation_state",
        "agent_turn_records",
    ):
        op.drop_table(table)
