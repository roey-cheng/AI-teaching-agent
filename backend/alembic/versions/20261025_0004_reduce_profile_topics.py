"""Keep only teaching-relevant profile memory topics."""

from alembic import op

revision = "20261025_0004"
down_revision = "20261012_0003"
branch_labels = None
depends_on = None


_TOPIC_CHECK = (
    "(memory_key = 'preference.language' AND memory_type = 'LEARNING_PREFERENCE') OR "
    "(memory_key = 'preference.explanation_style' AND memory_type = 'LEARNING_PREFERENCE') OR "
    "(memory_key = 'preference.detail_level' AND memory_type = 'LEARNING_PREFERENCE') OR "
    "(memory_key = 'preference.example_style' AND memory_type = 'LEARNING_PREFERENCE') OR "
    "(memory_key = 'preference.code_style' AND memory_type = 'LEARNING_PREFERENCE') OR "
    "(memory_key = 'preference.hint_style' AND memory_type = 'LEARNING_PREFERENCE') OR "
    "(memory_key = 'preference.pacing' AND memory_type = 'LEARNING_PREFERENCE') OR "
    "(memory_key = 'preference.knowledge_connections' AND memory_type = 'LEARNING_PREFERENCE') OR "
    "(memory_key = 'learning.goal' AND memory_type = 'LEARNING_GOAL') OR "
    "(memory_key = 'programming.level' AND memory_type = 'PROGRAMMING_BACKGROUND') OR "
    "(memory_key = 'programming.languages' AND memory_type = 'PROGRAMMING_BACKGROUND') OR "
    "(memory_key = 'programming.known_concepts' AND memory_type = 'PROGRAMMING_BACKGROUND')"
)


def _move_if_missing(old_key: str, new_key: str, new_type: str) -> None:
    op.execute(
        "INSERT INTO agent_memory "
        "(user_id, memory_key, memory_type, summary, created_at, updated_at) "
        "SELECT old.user_id, '"
        + new_key
        + "', '"
        + new_type
        + "', old.summary, old.created_at, old.updated_at "
        "FROM agent_memory AS old "
        "LEFT JOIN agent_memory AS current ON current.user_id = old.user_id "
        "AND current.memory_key = '"
        + new_key
        + "' WHERE old.memory_key = '"
        + old_key
        + "' AND current.memory_id IS NULL"
    )
    op.execute(
        "INSERT INTO memory_topic_revisions (user_id, memory_key, revision, source_message_id) "
        "SELECT old.user_id, '"
        + new_key
        + "', old.revision, old.source_message_id FROM memory_topic_revisions AS old "
        "LEFT JOIN memory_topic_revisions AS current ON current.user_id = old.user_id "
        "AND current.memory_key = '"
        + new_key
        + "' WHERE old.memory_key = '"
        + old_key
        + "' AND current.user_id IS NULL"
    )


def upgrade() -> None:
    # Existing target values win. If a legacy source has no target, copy it first.
    _move_if_missing("personal.long_term_goal", "learning.goal", "LEARNING_GOAL")
    _move_if_missing(
        "preference.conversation_tone", "preference.explanation_style", "LEARNING_PREFERENCE"
    )

    removed = (
        "'personal.preferred_name', 'personal.occupation', 'personal.interests', "
        "'personal.long_term_goal', 'personal.timezone', 'personal.general_location', "
        "'personal.daily_routine', 'preference.conversation_tone', "
        "'lifestyle.food_preferences', 'lifestyle.activity_preferences', "
        "'learning.current_topic', 'programming.tools', 'programming.environment'"
    )
    op.execute(f"DELETE FROM agent_memory WHERE memory_key IN ({removed})")
    op.execute(f"DELETE FROM memory_topic_revisions WHERE memory_key IN ({removed})")

    op.execute("ALTER TABLE agent_memory DROP CHECK ck_agent_memory_topic_type")
    op.execute(
        "ALTER TABLE agent_memory ADD CONSTRAINT ck_agent_memory_topic_type CHECK ("
        + _TOPIC_CHECK
        + ")"
    )


def downgrade() -> None:
    # Removed rows are intentionally not recreated; their values were excluded by policy.
    op.execute("ALTER TABLE agent_memory DROP CHECK ck_agent_memory_topic_type")
    op.execute(
        "ALTER TABLE agent_memory ADD CONSTRAINT ck_agent_memory_topic_type CHECK ("
        "(memory_key = 'preference.language' AND memory_type = 'LEARNING_PREFERENCE') OR "
        "(memory_key = 'preference.explanation_style' AND memory_type = 'LEARNING_PREFERENCE') OR "
        "(memory_key = 'preference.detail_level' AND memory_type = 'LEARNING_PREFERENCE') OR "
        "(memory_key = 'preference.example_style' AND memory_type = 'LEARNING_PREFERENCE') OR "
        "(memory_key = 'preference.code_style' AND memory_type = 'LEARNING_PREFERENCE') OR "
        "(memory_key = 'preference.hint_style' AND memory_type = 'LEARNING_PREFERENCE') OR "
        "(memory_key = 'preference.pacing' AND memory_type = 'LEARNING_PREFERENCE') OR "
        "(memory_key = 'preference.knowledge_connections' AND memory_type = 'LEARNING_PREFERENCE') OR "
        "(memory_key = 'learning.goal' AND memory_type = 'LEARNING_GOAL') OR "
        "(memory_key = 'programming.level' AND memory_type = 'PROGRAMMING_BACKGROUND') OR "
        "(memory_key = 'programming.languages' AND memory_type = 'PROGRAMMING_BACKGROUND') OR "
        "(memory_key = 'programming.known_concepts' AND memory_type = 'PROGRAMMING_BACKGROUND') OR "
        "(memory_key = 'learning.current_topic' AND memory_type = 'LEARNING_GOAL') OR "
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
        "(memory_key = 'lifestyle.activity_preferences' AND memory_type = 'DAILY_PREFERENCE')"
        ")"
    )
