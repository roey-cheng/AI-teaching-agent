"""在 backend 中运行：uv run python -m app.db.sqlalchemy_check。仅做只读连接验证。"""

from pydantic import ValidationError
from pydantic_settings import SettingsError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import load_database_settings
from app.db.engine import build_database_engine


def main() -> int:
    try:
        # 复用现有 DB_* 配置，不再要求填写一套账号密码。
        settings = load_database_settings()
    except (ValidationError, SettingsError, OSError):
        print("Invalid database configuration: check DB_* in backend/.env; configuration values are not displayed.")
        return 1

    try:
        engine = build_database_engine(settings)
        try:
            # Engine 是连接管理入口；connect() 才真正通过 PyMySQL 接通 MySQL。
            with engine.connect() as connection:
                result = connection.execute(text("SELECT 1")).scalar_one()
            # 离开 with 会归还连接到连接池，不等于立即关闭底层网络连接。
        finally:
            # 此命令只检查一次；结束时释放连接池里的连接，包括失败时的清理。
            engine.dispose()
    except (SQLAlchemyError, OSError):
        # 不输出原始异常，以免数据库地址、账号或敏感参数出现在终端。
        print("SQLAlchemy connection or query failed: check the MySQL service, network, and DB_* configuration.")
        return 1

    if result != 1:
        print("SQLAlchemy check failed: SELECT 1 did not return the expected value of 1.")
        return 1

    print("SQLAlchemy connection successful: SELECT 1 returned 1 via PyMySQL; connections released.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
