from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """所有表模型的共同基础；metadata 收集表结构，不连接或创建数据库。"""

