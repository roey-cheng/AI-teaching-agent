"""ORM 数据库操作会话；不是登录 Session 或聊天会话。"""

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    """按需创建独立 Session；这里只做配置，不连接、不查询或建表。"""
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    # 调用方复用 Engine 和 factory，但不能跨请求/线程共享一个 Session。
    # with factory() 管理读取；with factory.begin() 管理提交/失败回滚和关闭。
