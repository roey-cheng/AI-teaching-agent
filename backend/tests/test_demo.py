import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient

from app.demo import create_demo_app
from app.services.startup_cleanup import StartupCleanupResult
from testing_support import make_runtime


class DemoTest(unittest.TestCase):
    def test_public_origin_static_boundary_and_api(self):
        @contextmanager
        def runtime():
            yield make_runtime(cleanup=StartupCleanupResult())

        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            dist = parent / "dist"
            dist.mkdir()
            (dist / "index.html").write_text("<h1>Demo</h1>")
            (parent / ".env").write_text("PRIVATE_SENTINEL")
            app = create_demo_app("https://demo.example.com", runtime_factory=runtime, dist=dist)
            with TestClient(app, base_url="https://demo.example.com") as client:
                self.assertEqual(client.get("/").text, "<h1>Demo</h1>")
                self.assertEqual(client.get("/health").json(), {"status": "ok"})
                self.assertTrue(app.state.http_settings.cookie_secure)
                self.assertEqual(
                    app.state.http_settings.allowed_origins, ["https://demo.example.com"]
                )
                for path in ("/.env", "/backend/.env", "/%2e%2e/.env"):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 404)
                    self.assertNotIn("PRIVATE_SENTINEL", response.text)
                self.assertEqual(
                    client.get("/", headers={"Host": "untrusted.example.com"}).status_code, 400
                )
                self.assertEqual(
                    client.post(
                        "/api/v1/auth/login",
                        json={},
                        headers={"Origin": "https://untrusted.example.com"},
                    ).status_code,
                    403,
                )
                self.assertEqual(
                    client.post(
                        "/api/v1/auth/login",
                        json={},
                        headers={"Origin": "https://demo.example.com"},
                    ).status_code,
                    422,
                )

    def test_rejects_plain_http(self):
        with self.assertRaises(ValueError):
            create_demo_app("http://demo.example.com")
