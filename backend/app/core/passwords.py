"""密码哈希工具；不记录明文或哈希，不处理登录凭据。"""

from argon2 import PasswordHasher
from argon2.exceptions import HashingError, InvalidHashError, VerificationError
from argon2.profiles import RFC_9106_LOW_MEMORY
from pydantic import SecretStr

# Argon2id：64 MiB 内存、3 次迭代、并行度 4；库自动生成随机盐。
# 只创建工具，不读取配置或执行哈希。将来部署前需按资源测量吞吐并配限流。
_hasher = PasswordHasher.from_parameters(RFC_9106_LOW_MEMORY)

# 公开的占位哈希，不属于任何真实账号。未知邮箱也做一次验证，减少快速失败差异。
# 不在导入时执行昂贵哈希；即使输入恰好匹配此占位值，也不能登录不存在的账号。
DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$9WQVuVsjtMK5V+EmA7ZWPw$"
    "P9Yll0lruJ5JeVXv63UPq/1SP5ArYSweafMxB4t1prQ"
)


class PasswordHashError(RuntimeError):
    """安全的失败提示，不包含底层异常或密码。"""


def hash_password(password: SecretStr) -> str:
    """取出原密码计算哈希；不 strip、不改大小写，不对星号掩码做哈希。"""
    try:
        return _hasher.hash(password.get_secret_value())
    except (HashingError, UnicodeError):
        raise PasswordHashError("Password hashing failed.") from None


def verify_password(password: SecretStr, password_hash: str) -> bool:
    """用数据库中受信任的哈希核对原密码；不裁剪，不自己比较两次随机哈希。"""
    try:
        return _hasher.verify(password_hash, password.get_secret_value())
    except (VerificationError, InvalidHashError, UnicodeError):
        # 错密码、损坏哈希等一律不能通过认证，不向外暴露内部细节。
        return False
