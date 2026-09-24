from datetime import datetime
from types import MappingProxyType

from sqlalchemy import CheckConstraint, ForeignKey, Index, UniqueConstraint
from sqlalchemy.dialects.mysql import BIGINT, DATETIME, TEXT, VARCHAR
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# 25 个主题的共同允许列表。后续 Schema/工具/服务复用它，不分别维护不同版本。
# MappingProxyType 把字典变成只读映射，避免运行时误改；内容全部是项目常量。
MEMORY_TOPIC_TYPES = MappingProxyType({
    "preference.language": "LEARNING_PREFERENCE",
    "preference.explanation_style": "LEARNING_PREFERENCE",
    "preference.detail_level": "LEARNING_PREFERENCE",
    "preference.example_style": "LEARNING_PREFERENCE",
    "preference.code_style": "LEARNING_PREFERENCE",
    "preference.hint_style": "LEARNING_PREFERENCE",
    "preference.pacing": "LEARNING_PREFERENCE",
    "preference.knowledge_connections": "LEARNING_PREFERENCE",
    "learning.goal": "LEARNING_GOAL",
    "learning.current_topic": "LEARNING_GOAL",
    "programming.level": "PROGRAMMING_BACKGROUND",
    "programming.languages": "PROGRAMMING_BACKGROUND",
    "programming.known_concepts": "PROGRAMMING_BACKGROUND",
    "programming.tools": "PROGRAMMING_BACKGROUND",
    "programming.environment": "PROGRAMMING_BACKGROUND",
    "personal.preferred_name": "PERSONAL_BACKGROUND",
    "personal.occupation": "PERSONAL_BACKGROUND",
    "personal.interests": "PERSONAL_BACKGROUND",
    "personal.long_term_goal": "PERSONAL_BACKGROUND",
    "personal.timezone": "PERSONAL_BACKGROUND",
    "personal.general_location": "PERSONAL_BACKGROUND",
    "personal.daily_routine": "DAILY_PREFERENCE",
    "preference.conversation_tone": "DAILY_PREFERENCE",
    "lifestyle.food_preferences": "DAILY_PREFERENCE",
    "lifestyle.activity_preferences": "DAILY_PREFERENCE",
})

# 从固定常量生成“key 和 type 必须配对”的检查表达式，不拼接任何用户输入。
# 未来的迁移文件应保存当时的完整约束，不能导入此常量使旧迁移随代码改变。
_TOPIC_PAIR_CHECK = " OR ".join(
    f"(memory_key = '{key}' AND memory_type = '{memory_type}')"
    for key, memory_type in MEMORY_TOPIC_TYPES.items()
)


class AgentMemory(Base):
    """用户级个人记忆；一名用户的一个主题最多一行，不单独保存 Profile 文件。"""

    __tablename__ = "agent_memory"
    __table_args__ = (
        UniqueConstraint("user_id", "memory_key", name="uq_agent_memory_user_key"),
        CheckConstraint("CHAR_LENGTH(summary) BETWEEN 1 AND 500", name="ck_agent_memory_summary"),
        CheckConstraint(_TOPIC_PAIR_CHECK, name="ck_agent_memory_topic_type"),
        Index("ix_agent_memory_user_updated", "user_id", "updated_at", "memory_id"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    memory_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), primary_key=True, autoincrement=True, nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True),
        ForeignKey(
            "users.user_id", name="fk_agent_memory_user",
            ondelete="RESTRICT", onupdate="RESTRICT",
        ),
        nullable=False,
    )
    memory_key: Mapped[str] = mapped_column(
        VARCHAR(64, charset="ascii", collation="ascii_bin"), nullable=False
    )
    memory_type: Mapped[str] = mapped_column(
        VARCHAR(32, charset="ascii", collation="ascii_bin"), nullable=False
    )
    # 字数由 CHECK 限制；是否含敏感信息、是否值得长期保留不能由列类型判断。
    summary: Mapped[str] = mapped_column(TEXT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
