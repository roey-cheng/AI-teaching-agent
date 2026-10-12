import unittest
from datetime import UTC, datetime
from unittest.mock import MagicMock

from pydantic import ValidationError

from app.core.config import GenerationQuotaSettings
from app.models import GenerationAttempt
from app.services.errors import (
    GlobalConcurrencyLimitError,
    GlobalDailyQuotaExceededError,
    UserDailyQuotaExceededError,
)
from app.services.generation_quota import GenerationQuota


class GenerationQuotaTest(unittest.TestCase):
    def settings(self, **overrides):
        values = {
            "user_daily_limit": 3,
            "global_daily_limit": 5,
            "global_concurrency_limit": 2,
        }
        values.update(overrides)
        return GenerationQuotaSettings.from_sources(_env_file=None, **values)

    def test_admission_creates_one_persistent_running_attempt(self):
        session = MagicMock()
        session.scalar.side_effect = [object(), 1, 1, 0, 0]
        now = datetime(2026, 10, 12, 2, 0, tzinfo=UTC).replace(tzinfo=None)
        GenerationQuota(self.settings()).admit(
            session,
            user_id=7,
            message_id=9,
            attempt_id="8b4c91a4-772c-49e3-a9e3-e94c2c79d863",
            now=now,
        )
        attempt = session.add.call_args.args[0]
        self.assertIsInstance(attempt, GenerationAttempt)
        self.assertEqual(attempt.user_id, 7)
        self.assertEqual(attempt.message_id, 9)
        self.assertEqual(attempt.quota_date.isoformat(), "2026-10-12")
        self.assertEqual(attempt.status, "RUNNING")
        session.flush.assert_called_once_with()

    def test_each_limit_rejects_before_insert(self):
        cases = (
            ([object(), 5, 0, 0], GlobalDailyQuotaExceededError),
            ([object(), 1, 3, 0], UserDailyQuotaExceededError),
            ([object(), 1, 1, 2], GlobalConcurrencyLimitError),
        )
        for values, expected in cases:
            session = MagicMock()
            session.scalar.side_effect = [*values, 0]
            with self.subTest(expected=expected.__name__), self.assertRaises(expected):
                GenerationQuota(self.settings()).admit(
                    session,
                    user_id=7,
                    message_id=9,
                    attempt_id="8b4c91a4-772c-49e3-a9e3-e94c2c79d863",
                    now=datetime(2026, 10, 12, 2, 0),
                )
            session.add.assert_not_called()

    def test_finish_changes_only_running_attempt(self):
        attempt = MagicMock(status="RUNNING", finished_at=None)
        session = MagicMock()
        session.scalar.return_value = attempt
        now = datetime(2026, 10, 12, 2, 1)
        GenerationQuota(self.settings()).finish(session, "attempt", "SUCCEEDED", now)
        self.assertEqual(attempt.status, "SUCCEEDED")
        self.assertEqual(attempt.finished_at, now)
        session.flush.assert_called_once_with()

    def test_invalid_settings_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.settings(user_daily_limit=6, global_daily_limit=5)


if __name__ == "__main__":
    unittest.main()
