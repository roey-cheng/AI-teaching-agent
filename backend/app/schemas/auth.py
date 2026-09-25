from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator

from app.schemas._validation import database_datetime_as_utc, normalize_email_input
from app.schemas.user import UserResponse


class RegisterRequest(BaseModel):
    """注册请求的格式规则；不会查重、计算密码哈希、保存用户或自动登录。"""

    # 不接受 user_id、system_role 等额外字段，防止客户端指定身份和权限。
    # strict=True 不把数字、布尔值自动当作字符串；校验报错文本不展示输入值。
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    email: EmailStr = Field(max_length=320)
    # SecretStr 用于显示时遮住密码，不是密码哈希；原值仍用于后续哈希计算。
    # 不 strip、不转小写：密码中的空格和大小写也是密码的一部分。
    password: SecretStr = Field(min_length=8, max_length=128, repr=False)
    display_name: str = Field(min_length=1, max_length=100)

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value: object) -> object:
        # before 表示先做规范化，再让 EmailStr 验证邮箱格式。
        # 注册与登录共用同一函数，保证大小写和空白处理完全一致。
        return normalize_email_input(value)

    @field_validator("display_name", mode="before")
    @classmethod
    def trim_display_name(cls, value: object) -> object:
        # 只去两端空白，保留名字中间的空格。处理完才检查 1～100 字符。
        return value.strip() if isinstance(value, str) else value


class RegisterResponse(UserResponse):
    """注册成功响应：公共用户信息之外，还返回创建时间。"""

    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def use_utc(cls, value: datetime) -> datetime:
        # 本项目的 MySQL DATETIME 没有时区标记，但按设计约定保存的是 UTC。
        # 因此对数据库读出的无时区 datetime 补 UTC，不把它当作本地时间转换。
        return database_datetime_as_utc(value)


class LoginRequest(BaseModel):
    """登录只接收邮箱和密码；Schema 不查账号，也不知道密码是否正确。"""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    email: EmailStr = Field(max_length=320)
    # 登录不是设置新密码：只校验非空和最大长度，不套用注册的 8 字符下限。
    # 合规格式但不正确的密码，由后续认证业务统一返回 INVALID_CREDENTIALS。
    password: SecretStr = Field(min_length=1, max_length=128, repr=False)

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value: object) -> object:
        return normalize_email_input(value)


class LoginResponse(BaseModel):
    """登录成功的 JSON 正文；Cookie 另由接口在 HTTP 响应头中设置。"""

    model_config = ConfigDict(extra="ignore", strict=True, hide_input_in_errors=True)

    # 嵌套已有响应模型，输出结构为 {"user": {"user_id": ..., ...}}。
    user: UserResponse
