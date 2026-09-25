from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.schemas._validation import format_database_id


class UserResponse(BaseModel):
    """只允许向前端展示的用户字段；对应 GET /users/me 的响应正文。"""

    # from_attributes：可读取 SQLAlchemy User 对象的属性，不需要先转成字典。
    # extra=ignore：输入的后端记录即使有其他字段，也不带进这个响应。
    # 这与请求的 extra=forbid 不同：未知的前端输入要拒绝，后端多余字段要过滤。
    model_config = ConfigDict(
        from_attributes=True, extra="ignore", strict=True, hide_input_in_errors=True
    )

    # API 的 ID 始终输出字符串，避免浏览器丢失大整数精度。
    user_id: str
    email: EmailStr = Field(max_length=320)
    display_name: str = Field(min_length=1, max_length=100)

    @field_validator("user_id", mode="before")
    @classmethod
    def format_user_id(cls, value: object) -> str:
        # MySQL 给的是整数；接口也可以传入已格式化的十进制 ID 字符串。
        # 不能用 str(value) 无条件转换，否则 None、True 等也会变成“合法字符串”。
        return format_database_id(value, "User ID")

    # 故意不定义 password、password_hash、system_role、status 或登录凭据。
    # 后续接口必须实际使用此响应 Schema，不能绕过它直接返回原始 ORM 数据。
