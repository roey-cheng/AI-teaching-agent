import unittest

from fastapi.testclient import TestClient

from app.main import app


class HealthTest(unittest.TestCase):
    def test_health_returns_ok_without_external_services(self):
        with TestClient(app) as client:
            response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertIn("application/json", response.headers["content-type"])


if __name__ == "__main__":
    unittest.main()
