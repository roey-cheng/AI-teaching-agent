"""${message}
#准备以后新增迁移的模版
Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

from typing import Sequence

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: str | Sequence[str] | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    """应用本次结构变化；执行前必须审阅生成内容。"""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """撤销本次结构变化；可能删除数据，不能当作无损撤销。"""
    ${downgrades if downgrades else "pass"}
