from sqlalchemy import URL, Engine, create_engine

from app.core.config import DatabaseSettings


def build_database_engine(settings: DatabaseSettings) -> Engine:
    """准备 SQLAlchemy 连接入口；调用 connect() 时才实际访问 MySQL。"""
    # mysql+pymysql：使用 MySQL 数据库，交给 PyMySQL 驱动通信。
    # 分项构造 URL，不手动拼字符串，密码包含 @、/ 等字符也能正确处理。
    # 这里不打印 URL、配置或明文密码。
    url = URL.create(
        drivername="mysql+pymysql",
        username=settings.user,
        password=settings.password.get_secret_value(),
        host=settings.host,
        port=settings.port,
        database=settings.name,
        query={"charset": "utf8mb4"},
    )
    return create_engine(
        url,
        echo=False,  # 不主动打印 SQL 日志。
        hide_parameters=True,  # SQLAlchemy 的日志/错误展示不附带 SQL 参数值。
        pool_pre_ping=True,  # 复用连接前检查它是否仍可用。
        connect_args={
            "connect_timeout": 5,
            "read_timeout": 5,
            "write_timeout": 5,
            "autocommit": False,
        },
    )
