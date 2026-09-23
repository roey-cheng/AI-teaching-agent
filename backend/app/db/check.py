"""手动运行：在 backend 目录执行 uv run python -m app.db.check。"""

from contextlib import closing

import pymysql
from pydantic import ValidationError
from pydantic_settings import SettingsError

from app.core.config import load_database_settings
from app.db.connection import open_database_connection


def main() -> int:
    try:
        # 第一步：读取 backend/.env 的数据库配置。
        settings = load_database_settings()
    except (ValidationError, SettingsError, OSError):
        print("配置读取失败：请检查 backend/.env 的 DB_* 字段；密码不能为空，端口必须合法。")
        return 1

    try:
        # 第二步：建立连接。closing 保证离开这段代码时关闭已建立的连接，
        # 无论后续查询成功还是抛出异常，都不把连接一直留着。
        with closing(open_database_connection(settings)) as connection:
            # cursor（游标）在这里是执行 SQL、读取结果的对象，
            # 不是之前 API 分页设计里的 cursor 分页标记。
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")  # 只读查询，不创建表、不写入业务数据。
                result = cursor.fetchone()
        if result != (1,):
            print("数据库检查失败：SELECT 1 没有返回预期结果。")
            return 1
    except (pymysql.MySQLError, OSError):
        # 不输出原始异常、连接参数或密码。真实故障可在下一步单独排查。
        print("数据库连接或查询失败：请检查 MySQL 是否启动，以及地址、端口、账号、密码和权限。")
        return 1

    print("数据库连接成功：SELECT 1 返回 1，连接已关闭。")
    return 0


# 只有明确运行本模块时才执行检查；import 本模块不会自动访问数据库。
if __name__ == "__main__":
    raise SystemExit(main())
