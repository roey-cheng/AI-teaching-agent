from pathlib import Path
from typing import Any, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 根据代码的位置找到 backend/.env，不依赖终端当前在哪个目录。
BACKEND_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class EnvironmentSettings(BaseSettings):
    @classmethod
    def from_sources(cls, **overrides: Any) -> Self:
        """读取环境配置并校验；入参是尚未校验的数据，允许 Settings 的 _env_file 等选项。"""
        return cls(**overrides)


class DatabaseSettings(EnvironmentSettings):
    """把本地 DB_* 配置读取为 Python 可使用的数据，并检查格式。"""

    model_config = SettingsConfigDict(
        env_file=BACKEND_ENV_FILE,
        env_file_encoding="utf-8",
        env_prefix="DB_",
        extra="ignore",  # .env 中还预留了模型等配置，这个类只负责数据库。
        hide_input_in_errors=True,  # 校验报错时不展示输入值，避免泄露密码。
    )

    # env_prefix="DB_" 使 host 对应 DB_HOST、port 对应 DB_PORT，以此类推。
    host: str = Field(default="127.0.0.1", min_length=1)
    port: int = Field(default=3306, ge=1, le=65535)
    name: str = Field(min_length=1)
    user: str = Field(min_length=1)
    password: SecretStr  # 通常打印此对象时会掩码；不是加密本地 .env 文件。

    @field_validator("password")
    @classmethod
    def password_must_not_be_blank(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("DB_PASSWORD must not be blank")
        return value


def load_database_settings() -> DatabaseSettings:
    # 调用时才读取配置；仅仅 import 本文件不会读取密码或连接数据库。
    # 同名环境变量优先于 .env，便于之后部署；正常本地开发无需额外设置。
    return DatabaseSettings.from_sources()


class ModelSettings(EnvironmentSettings):
    """只读取 MODEL_*；填写配置本身不会发送模型请求。"""

    model_config = SettingsConfigDict(
        env_file=BACKEND_ENV_FILE,
        env_file_encoding="utf-8",
        env_prefix="MODEL_",
        extra="ignore",
        hide_input_in_errors=True,
    )

    provider: Literal["deepseek"]
    name: str = Field(min_length=1)
    api_key: SecretStr
    # 这个探针只允许官方地址，避免拼错地址时把密钥送到别的网站。
    base_url: Literal["https://api.deepseek.com", "https://api.deepseek.com/v1"] = (
        "https://api.deepseek.com"
    )

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError(
                "MODEL_NAME must not be blank or contain leading or trailing whitespace"
            )
        return value

    @field_validator("api_key")
    @classmethod
    def key_must_have_no_whitespace(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not raw or any(character.isspace() for character in raw):
            raise ValueError("MODEL_API_KEY must not be blank or contain whitespace")
        return value


def load_model_settings() -> ModelSettings:
    return ModelSettings.from_sources()


class TracingSettings(EnvironmentSettings):
    """LangSmith tracing 配置；读取配置不上传记录，默认关闭。"""

    model_config = SettingsConfigDict(
        env_file=BACKEND_ENV_FILE,
        env_file_encoding="utf-8",
        env_prefix="LANGSMITH_",
        extra="ignore",
        hide_input_in_errors=True,
    )

    tracing: bool = False
    api_key: SecretStr | None = None
    project: str = Field(default="ai-teaching-agent-dev", min_length=1)
    endpoint: Literal["https://api.smith.langchain.com", "https://eu.api.smith.langchain.com"] = (
        "https://api.smith.langchain.com"
    )
    workspace_id: str | None = None

    @field_validator("api_key", "workspace_id", mode="before")
    @classmethod
    def empty_optional_value(cls, value):
        return None if value == "" else value

    @field_validator("project")
    @classmethod
    def project_must_not_be_blank(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("LANGSMITH_PROJECT must not be blank or padded with whitespace")
        return value

    @model_validator(mode="after")
    def validate_tracing_key(self):
        if self.tracing and self.api_key is None:
            raise ValueError("LANGSMITH_API_KEY is required when tracing is enabled")
        if self.api_key is not None:
            raw = self.api_key.get_secret_value()
            if not raw or any(character.isspace() for character in raw):
                raise ValueError("LANGSMITH_API_KEY must not contain whitespace")
        return self


def load_tracing_settings() -> TracingSettings:
    return TracingSettings()


class TurnstileSettings(EnvironmentSettings):
    """公开注册的人机验证配置；本地开发默认关闭。"""

    model_config = SettingsConfigDict(
        env_file=BACKEND_ENV_FILE,
        env_file_encoding="utf-8",
        env_prefix="TURNSTILE_",
        extra="ignore",
        hide_input_in_errors=True,
    )

    enabled: bool = False
    secret_key: SecretStr | None = None
    expected_hostname: str | None = None
    expected_action: str = "register"

    @field_validator("secret_key", "expected_hostname", mode="before")
    @classmethod
    def empty_optional_value(cls, value):
        return None if value == "" else value

    @model_validator(mode="after")
    def validate_enabled_configuration(self):
        if self.enabled and (self.secret_key is None or self.expected_hostname is None):
            raise ValueError(
                "TURNSTILE_SECRET_KEY and TURNSTILE_EXPECTED_HOSTNAME are required when enabled"
            )
        if self.secret_key is not None and not self.secret_key.get_secret_value().strip():
            raise ValueError("TURNSTILE_SECRET_KEY must not be blank")
        if self.expected_hostname is not None and (
            not self.expected_hostname.strip()
            or self.expected_hostname != self.expected_hostname.strip()
            or "://" in self.expected_hostname
            or "/" in self.expected_hostname
        ):
            raise ValueError("TURNSTILE_EXPECTED_HOSTNAME must be a hostname without a scheme")
        if not self.expected_action.strip() or self.expected_action != self.expected_action.strip():
            raise ValueError("TURNSTILE_EXPECTED_ACTION must not be blank or padded")
        return self


def load_turnstile_settings() -> TurnstileSettings:
    return TurnstileSettings.from_sources()


class GenerationQuotaSettings(EnvironmentSettings):
    """公开模型调用的持久化额度；一次聊天轮次最多包含两次模型请求。"""

    model_config = SettingsConfigDict(
        env_file=BACKEND_ENV_FILE,
        env_file_encoding="utf-8",
        env_prefix="GENERATION_",
        extra="ignore",
        hide_input_in_errors=True,
    )

    user_daily_limit: int = Field(default=30, ge=1, le=10000)
    global_daily_limit: int = Field(default=500, ge=1, le=1000000)
    global_concurrency_limit: int = Field(default=2, ge=1, le=100)

    @model_validator(mode="after")
    def user_limit_cannot_exceed_global_limit(self):
        if self.user_daily_limit > self.global_daily_limit:
            raise ValueError("GENERATION_USER_DAILY_LIMIT cannot exceed the global daily limit")
        return self


def load_generation_quota_settings() -> GenerationQuotaSettings:
    return GenerationQuotaSettings.from_sources()
