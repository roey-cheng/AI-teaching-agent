"""Cloudflare Turnstile 服务端验证；令牌和密钥不进入日志或返回正文。"""

from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr

from app.core.config import TurnstileSettings
from app.services.errors import (
    TurnstileFailedError,
    TurnstileRequiredError,
    TurnstileUnavailableError,
)

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


class TurnstileResult(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, hide_input_in_errors=True)

    success: bool
    hostname: str | None = None
    action: str | None = None


class TurnstileVerifierProtocol(Protocol):
    async def verify(self, token: SecretStr | None, client_ip: str) -> None: ...


class TurnstileVerifier:
    def __init__(
        self,
        settings: TurnstileSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.settings = settings
        self.transport = transport

    async def verify(self, token: SecretStr | None, client_ip: str) -> None:
        if not self.settings.enabled:
            return
        if token is None or not token.get_secret_value().strip():
            raise TurnstileRequiredError()
        secret = self.settings.secret_key
        if secret is None:  # Settings 正常校验后不会发生；仍保持失败关闭。
            raise TurnstileUnavailableError()
        try:
            async with httpx.AsyncClient(timeout=5, transport=self.transport) as client:
                response = await client.post(
                    SITEVERIFY_URL,
                    json={
                        "secret": secret.get_secret_value(),
                        "response": token.get_secret_value(),
                        "remoteip": client_ip,
                    },
                )
                response.raise_for_status()
                result = TurnstileResult.model_validate(response.json())
        except httpx.HTTPError, ValueError:
            raise TurnstileUnavailableError() from None
        if (
            not result.success
            or result.hostname != self.settings.expected_hostname
            or result.action != self.settings.expected_action
        ):
            raise TurnstileFailedError()
