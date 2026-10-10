"""网页入口配置；与数据库、模型配置分开，读取配置不发网络请求。"""

from urllib.parse import urlsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.config import BACKEND_ENV_FILE


class HTTPSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_ENV_FILE, env_file_encoding="utf-8", env_prefix="HTTP_",
        extra="ignore", hide_input_in_errors=True,
    )

    # 精确匹配浏览器页面的 Origin，不是后端代理转发的目标地址。
    allowed_origins: list[str] = [
        "http://localhost:5173", "http://127.0.0.1:5173",
        "http://localhost:8000", "http://127.0.0.1:8000",
    ]
    cookie_secure: bool = False  # 本地 HTTP；部署 HTTPS 时必须设为 true。

    @field_validator("allowed_origins")
    @classmethod
    def validate_origins(cls, origins: list[str]) -> list[str]:
        if not origins:
            raise ValueError("At least one trusted origin is required")
        for origin in origins:
            parsed = urlsplit(origin)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or parsed.path or parsed.query or parsed.fragment
                    or "*" in origin or any(c.isspace() for c in origin)):
                raise ValueError("Use exact HTTP(S) origins without paths or wildcards")
            # 触发无效端口检查；不在启动失败提示中回显输入。
            _ = parsed.port
        return origins
