"""网页入口配置；与数据库、模型配置分开，读取配置不发网络请求。"""

from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import SettingsConfigDict

from app.core.config import BACKEND_ENV_FILE, EnvironmentSettings


class HTTPSettings(EnvironmentSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_ENV_FILE,
        env_file_encoding="utf-8",
        env_prefix="HTTP_",
        extra="ignore",
        hide_input_in_errors=True,
    )

    # 精确匹配浏览器页面的 Origin，不是后端代理转发的目标地址。
    allowed_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:8000",
            "http://127.0.0.1:8000",
        ]
    )
    cookie_secure: bool = False  # 本地 HTTP；部署 HTTPS 时必须设为 true。
    require_trusted_proxy: bool = False
    proxy_shared_secret: SecretStr | None = None

    @field_validator("proxy_shared_secret", mode="before")
    @classmethod
    def empty_proxy_secret(cls, value):
        return None if value == "" else value

    @model_validator(mode="after")
    def validate_proxy_configuration(self):
        if self.require_trusted_proxy and self.proxy_shared_secret is None:
            raise ValueError("HTTP_PROXY_SHARED_SECRET is required when trusted proxy is enabled")
        if self.proxy_shared_secret is not None:
            raw = self.proxy_shared_secret.get_secret_value()
            if len(raw) < 32 or any(character.isspace() for character in raw):
                raise ValueError(
                    "HTTP_PROXY_SHARED_SECRET must contain at least 32 non-whitespace characters"
                )
        return self

    @field_validator("allowed_origins")
    @classmethod
    def validate_origins(cls, origins: list[str]) -> list[str]:
        if not origins:
            raise ValueError("At least one trusted origin is required")
        for origin in origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
                or "*" in origin
                or any(c.isspace() for c in origin)
            ):
                raise ValueError("Use exact HTTP(S) origins without paths or wildcards")
            # 触发无效端口检查；不在启动失败提示中回显输入。
            _ = parsed.port
        return origins
