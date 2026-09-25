"""Schema 共用的小函数，避免注册/登录、用户/会话的同类规则逐渐不一致。"""

import re
from datetime import UTC, datetime


def normalize_email_input(value: object) -> object:
    """只整理邮箱输入；合法性仍交给后续 EmailStr 检查。"""
    if isinstance(value, str):
        value = value.strip().lower()
        if len(value) > 320:
            raise ValueError("Email must be at most 320 characters")
        if "<" in value or ">" in value:
            raise ValueError("Enter only the email address, without a display name")
    return value


def format_database_id(value: object, label: str) -> str:
    """将数据库正整数编号变成 API 字符串；拒绝布尔值、小数等错误输入。"""
    if type(value) is int:
        number = value
    elif isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,19}", value):
        number = int(value)
    else:
        raise ValueError(f"{label} must be a positive integer or its decimal string")
    if not 1 <= number <= 18446744073709551615:
        raise ValueError(f"{label} is outside the BIGINT UNSIGNED range")
    return str(number)


def database_datetime_as_utc(value: datetime) -> datetime:
    """无时区数据库时间按项目约定解释为 UTC；有时区时间转换到 UTC。"""
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
