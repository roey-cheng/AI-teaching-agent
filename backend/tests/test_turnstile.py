import unittest

import httpx
from pydantic import SecretStr, ValidationError

from app.core.config import TurnstileSettings
from app.services.errors import (
    TurnstileFailedError,
    TurnstileRequiredError,
    TurnstileUnavailableError,
)
from app.services.turnstile import TurnstileVerifier


class TurnstileVerifierTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def settings(**overrides):
        values = {
            "enabled": True,
            "secret_key": "test-secret",
            "expected_hostname": "ai.example.com",
        }
        values.update(overrides)
        return TurnstileSettings.from_sources(
            _env_file=None,
            **values,
        )

    async def test_disabled_verification_never_calls_network(self):
        async def fail(_request):
            raise AssertionError("Disabled Turnstile must not call the network")

        verifier = TurnstileVerifier(
            TurnstileSettings.from_sources(_env_file=None),
            transport=httpx.MockTransport(fail),
        )
        await verifier.verify(None, "127.0.0.1")

    async def test_success_sends_secret_token_and_ip(self):
        async def respond(request):
            data = request.read().decode()
            self.assertIn("test-secret", data)
            self.assertIn("browser-token", data)
            self.assertIn("203.0.113.8", data)
            return httpx.Response(
                200,
                json={"success": True, "hostname": "ai.example.com", "action": "register"},
            )

        verifier = TurnstileVerifier(self.settings(), transport=httpx.MockTransport(respond))
        await verifier.verify(SecretStr("browser-token"), "203.0.113.8")

    async def test_missing_bad_hostname_or_bad_action_fails_closed(self):
        verifier = TurnstileVerifier(self.settings())
        with self.assertRaises(TurnstileRequiredError):
            await verifier.verify(None, "127.0.0.1")

        for payload in (
            {"success": False},
            {"success": True, "hostname": "other.example.com", "action": "register"},
            {"success": True, "hostname": "ai.example.com", "action": "other"},
        ):

            async def respond(_request, value=payload):
                return httpx.Response(200, json=value)

            with self.subTest(payload=payload), self.assertRaises(TurnstileFailedError):
                verifier = TurnstileVerifier(
                    self.settings(), transport=httpx.MockTransport(respond)
                )
                await verifier.verify(SecretStr("browser-token"), "127.0.0.1")

    async def test_network_and_invalid_response_are_unavailable(self):
        async def fail(_request):
            raise httpx.ConnectError("private network details")

        verifier = TurnstileVerifier(self.settings(), transport=httpx.MockTransport(fail))
        with self.assertRaises(TurnstileUnavailableError) as error:
            await verifier.verify(SecretStr("browser-token"), "127.0.0.1")
        self.assertNotIn("private", str(error.exception))

    def test_enabled_settings_require_secret_and_exact_hostname(self):
        with self.assertRaises(ValidationError):
            TurnstileSettings.from_sources(_env_file=None, enabled=True)
        with self.assertRaises(ValidationError):
            self.settings(expected_hostname="https://ai.example.com")


if __name__ == "__main__":
    unittest.main()
