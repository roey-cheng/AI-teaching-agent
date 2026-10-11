import unittest
from contextlib import contextmanager

from fastapi.testclient import TestClient

from app.core.http_config import HTTPSettings
from app.main import create_app
from testing_support import make_runtime


class TrustedProxyTest(unittest.TestCase):
    def setUp(self):
        @contextmanager
        def runtime():
            yield make_runtime()

        self.secret = "0123456789abcdef0123456789abcdef"
        settings = HTTPSettings.from_sources(
            _env_file=None,
            require_trusted_proxy=True,
            proxy_shared_secret=self.secret,
        )
        self.client = TestClient(create_app(runtime_factory=runtime, http_settings=settings))

    def test_direct_and_spoofed_requests_are_rejected(self):
        with self.client as client:
            direct = client.get("/api/v1/users/me")
            spoofed = client.get(
                "/api/v1/users/me",
                headers={"x-client-ip": "203.0.113.8", "x-ai-proxy-secret": "wrong"},
            )
        self.assertEqual(direct.status_code, 403)
        self.assertEqual(spoofed.status_code, 403)

    def test_valid_proxy_headers_reach_normal_authentication(self):
        with self.client as client:
            response = client.get(
                "/api/v1/users/me",
                headers={
                    "x-client-ip": "203.0.113.8",
                    "x-ai-proxy-secret": self.secret,
                },
            )
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
