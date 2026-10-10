from concurrent.futures import ThreadPoolExecutor
import unittest

from app.services.errors import GenerationRateLimitError
from app.services.generation_limit import GenerationRateLimiter


class GenerationLimitTest(unittest.TestCase):
    def test_ten_generations_per_user_in_rolling_window(self):
        now = [100.0]
        limiter = GenerationRateLimiter(clock=lambda: now[0])
        for _ in range(10):
            limiter.check("1")
        with self.assertRaises(GenerationRateLimitError) as error:
            limiter.check("1")
        self.assertEqual(error.exception.retry_after_seconds, 60)
        limiter.check("2")
        now[0] = 159.1
        with self.assertRaises(GenerationRateLimitError) as error:
            limiter.check("1")
        self.assertEqual(error.exception.retry_after_seconds, 1)
        now[0] = 160
        limiter.check("1")
        self.assertEqual(list(limiter._accepted["1"]), [160])

    def test_rejection_does_not_extend_window_and_restart_resets(self):
        limiter = GenerationRateLimiter(clock=lambda: 10)
        for _ in range(10):
            limiter.check("1")
        for _ in range(3):
            with self.assertRaises(GenerationRateLimitError):
                limiter.check("1")
        self.assertEqual(len(limiter._accepted["1"]), 10)
        GenerationRateLimiter(clock=lambda: 10).check("1")

    def test_concurrent_checks_do_not_exceed_ten(self):
        limiter = GenerationRateLimiter(clock=lambda: 100)
        def check(_):
            try:
                limiter.check("1")
                return True
            except GenerationRateLimitError:
                return False
        with ThreadPoolExecutor(max_workers=12) as pool:
            self.assertEqual(sum(pool.map(check, range(30))), 10)
