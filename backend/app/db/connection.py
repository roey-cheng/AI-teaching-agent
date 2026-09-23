import pymysql

from app.core.config import DatabaseSettings


def open_database_connection(settings: DatabaseSettings) -> pymysql.connections.Connection:
    """用项目账号建立连接；使用者负责在结束后关闭连接。"""
    # 只有执行这个函数才会访问 MySQL；安装、导入驱动都不会自动连接。
    # 单独传递各项参数，不拼接包含密码的连接 URL，也不打印这些参数。
    return pymysql.connect(
        host=settings.host,
        port=settings.port,
        user=settings.user,
        password=settings.password.get_secret_value(),
        database=settings.name,
        charset="utf8mb4",
        connect_timeout=5,
        read_timeout=5,
        write_timeout=5,
        autocommit=False,
    )
