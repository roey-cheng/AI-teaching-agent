import unittest
from contextlib import contextmanager
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.main import create_app
from app.services.startup_cleanup import StartupCleanupResult


class HealthTest(unittest.TestCase):
    def test_health_returns_ok_without_external_services(self):
        @contextmanager
        def fake_runtime():
            yield SimpleNamespace(cleanup=StartupCleanupResult())

        app = create_app(runtime_factory=fake_runtime)
        with TestClient(app) as client:
            response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertIn("application/json", response.headers["content-type"])


if __name__ == "__main__":
    unittest.main()
