"""登录凭据工具；随机凭据不是用户密码，也不是数据库自增登录编号。"""

import hashlib
import re
import secrets

from pydantic import SecretStr


def create_session_token() -> SecretStr:
    """32 字节安全随机数，编码为 43 字符 URL-safe 字符串。"""
    return SecretStr(secrets.token_urlsafe(32))


def hash_session_token(token: SecretStr) -> str | None:
    """合法凭据返回 SHA-256 十六进制哈希；畸形旧 Cookie 视为无效凭据。"""
    value = token.get_secret_value()
    if re.fullmatch(r"[A-Za-z0-9_-]{43}", value) is None:
        return None
    return hashlib.sha256(value.encode("ascii")).hexdigest()
