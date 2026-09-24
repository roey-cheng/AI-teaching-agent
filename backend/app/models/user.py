from datetime import datetime

from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.dialects.mysql import BIGINT, DATETIME, VARCHAR
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class User(Base):
    """users 表的 Python 描述；定义或实例化这个类都不会自动保存数据。"""

    # Python 类叫 User，真正的数据库表名叫 users。
    __tablename__ = "users"

    # 表级规则：UK 防止重复邮箱；CHECK 限制数据库允许保存的值。
    # name 是约束的名字，便于以后在迁移和报错中定位。
    __table_args__ = (
        UniqueConstraint("email", name="uq_users_email"),
        CheckConstraint("CHAR_LENGTH(email) > 0", name="ck_users_email_not_empty"),
        CheckConstraint(
            "CHAR_LENGTH(password_hash) > 0", name="ck_users_password_hash_not_empty"
        ),
        CheckConstraint(
            "CHAR_LENGTH(display_name) > 0", name="ck_users_display_name_not_empty"
        ),
        CheckConstraint("status IN ('ACTIVE', 'DISABLED')", name="ck_users_status"),
        CheckConstraint("system_role IN ('USER', 'ADMIN')", name="ck_users_system_role"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    # Mapped[int]：对应整数属性；mapped_column(...)：描述数据库中的列。
    # 数据库负责生成自增主键；编号不保证连续。
    user_id: Mapped[int] = mapped_column(
        BIGINT(unsigned=True), primary_key=True, autoincrement=True, nullable=False
    )

    # nullable=False 表示不允许 NULL。邮箱规范化和格式校验留给后续业务/Schema。
    email: Mapped[str] = mapped_column(
        VARCHAR(320, charset="utf8mb4", collation="utf8mb4_bin"), nullable=False
    )
    # 此列只接收已经算好的密码哈希；列名本身不会自动替我们加密或哈希。
    password_hash: Mapped[str] = mapped_column(
        VARCHAR(255, charset="ascii", collation="ascii_bin"), nullable=False
    )
    # 数据库容量为 255；接口仍需另外限制去空白后 1～100 字符。昵称允许重名。
    display_name: Mapped[str] = mapped_column(VARCHAR(255), nullable=False)

    # server_default 是未来建表时声明的数据库默认值，不是创建 Python 对象时赋值。
    # 公开注册的业务逻辑仍必须固定 USER，不接受前端指定 ADMIN。
    status: Mapped[str] = mapped_column(
        VARCHAR(16, charset="ascii", collation="ascii_bin"),
        nullable=False,
        server_default="ACTIVE",
    )
    system_role: Mapped[str] = mapped_column(
        VARCHAR(16, charset="ascii", collation="ascii_bin"),
        nullable=False,
        server_default="USER",
    )

    # DATETIME(6) 保留微秒，但不存时区；后端以后统一写入 UTC、读出按 UTC 解释。
    # 不设置自动时间或 onupdate：创建、修改、登录时由业务逻辑明确写入。
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), nullable=False)
    # datetime | None 表示可以是时间，也可以为空：刚注册还没有登录。
    last_login_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6), nullable=True)
