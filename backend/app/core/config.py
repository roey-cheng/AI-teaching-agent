from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 根据代码的位置找到 backend/.env，不依赖终端当前在哪个目录。
BACKEND_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class DatabaseSettings(BaseSettings):
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
            raise ValueError("DB_PASSWORD 不能为空")
        return value


def load_database_settings() -> DatabaseSettings:
    # 调用时才读取配置；仅仅 import 本文件不会读取密码或连接数据库。
    # 同名环境变量优先于 .env，便于之后部署；正常本地开发无需额外设置。
    return DatabaseSettings()


class ModelSettings(BaseSettings):
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
    base_url: Literal["https://api.deepseek.com", "https://api.deepseek.com/v1"] = "https://api.deepseek.com"

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("MODEL_NAME 不能为空或包含首尾空格")
        return value

    @field_validator("api_key")
    @classmethod
    def key_must_have_no_whitespace(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not raw or any(character.isspace() for character in raw):
            raise ValueError("MODEL_API_KEY 不能为空或包含空白字符")
        return value


def load_model_settings() -> ModelSettings:
    return ModelSettings()
