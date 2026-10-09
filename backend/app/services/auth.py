"""注册业务：查重、密码哈希、保存用户；没有 HTTP 路由或自动登录。"""

from datetime import UTC, datetime
import re

from pymysql.err import IntegrityError as MySQLIntegrityError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.passwords import PasswordHashError, hash_password
from app.models import User
from app.schemas import RegisterRequest, RegisterResponse
from app.services.errors import EmailAlreadyRegisteredError, RegistrationUnavailableError


def _is_email_conflict(error: IntegrityError) -> bool:
    """只识别 MySQL 邮箱唯一约束，不能把所有数据库错误都说成邮箱重复。"""
    original = error.orig
    return (
        isinstance(original, MySQLIntegrityError)
        and len(original.args) >= 2
        and original.args[0] == 1062
        and isinstance(original.args[1], str)
        and re.search(r"for key ['`](?:users\.)?uq_users_email['`]\s*$", original.args[1]) is not None
    )


def register_user(
    request: RegisterRequest, session_factory: sessionmaker[Session]
) -> RegisterResponse:
    """接收已校验的注册资料；独立管理读取与写入 Session，提交成功后才返回。"""
    try:
        # 第一段短查询：已注册时提前给出业务错误；退出 with 后释放这次会话。
        with session_factory() as session:
            existing_id = session.scalar(select(User.user_id).where(User.email == request.email))
            if existing_id is not None:
                raise EmailAlreadyRegisteredError()

        # 密码计算较耗时，放在数据库事务外；原密码不进入 User 对象和 SQL 参数。
        password_hash = hash_password(request.password)
        # MySQL DATETIME(6) 不存时区，按项目约定写入无时区的 UTC 值。
        now = datetime.now(UTC).replace(tzinfo=None)
        user = User(
            email=str(request.email), password_hash=password_hash,
            display_name=request.display_name, status="ACTIVE", system_role="USER",
            created_at=now, updated_at=now, last_login_at=None,
        )

        # 查重后仍可能有另一请求抢先注册，真正的兜底是数据库唯一约束。
        with session_factory.begin() as session:
            session.add(user)
            session.flush()  # 执行 INSERT，拿到数据库生成的 user_id；此时还没提交。
            response = RegisterResponse.model_validate(user)
            # 先确认公开响应能正常生成，再退出上下文提交事务。
        return response  # 只有提交成功才走到这里；不创建 auth_sessions 或 Cookie。
    except IntegrityError as error:
        # begin() 上下文已处理回滚/关闭；不自行重试，避免未知提交结果引发重复操作。
        if _is_email_conflict(error):
            raise EmailAlreadyRegisteredError() from None
        raise RegistrationUnavailableError() from None
    except (SQLAlchemyError, PasswordHashError):
        # 不向调用方暴露 SQL、连接信息、原始异常或密码哈希。
        raise RegistrationUnavailableError() from None
