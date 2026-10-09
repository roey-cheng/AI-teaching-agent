"""单进程短锁/运行登记测试；没有真实模型或数据库任务。"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
import unittest
from uuid import uuid4

from pydantic import ValidationError

from app.services.generation_registry import GenerationRegistry


class GenerationRegistryTest(unittest.TestCase):
    def test_busy_entry_persists_and_old_cleanup_cannot_release_new_attempt(self):
        registry = GenerationRegistry()
        first, second = str(uuid4()), str(uuid4())
        with registry.locked(1) as slot:
            self.assertTrue(slot.claim(first))
            self.assertFalse(slot.claim(second))
        with registry.locked(1) as slot:
            self.assertEqual(slot.attempt_id, first)
            self.assertTrue(slot.release(first))
            self.assertTrue(slot.claim(second))
            self.assertFalse(slot.release(first))
            self.assertEqual(slot.attempt_id, second)
            self.assertTrue(slot.release(second))
        self.assertEqual(registry._entries, {})

    def test_invalid_attempt_and_exception_release_idle_lock(self):
        registry = GenerationRegistry()
        with self.assertRaises(ValidationError), registry.locked(1) as slot:
            slot.claim("bad")
        self.assertEqual(registry._entries, {})
        with registry.locked(1) as slot:
            self.assertIsNone(slot.attempt_id)

    def test_concurrent_claims_only_one_wins(self):
        registry = GenerationRegistry()
        barrier = Barrier(2)
        def claim():
            barrier.wait(timeout=5)
            with registry.locked(1) as slot:
                return slot.claim(str(uuid4()))
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(claim) for _ in range(2)]
            self.assertEqual(sorted(job.result(timeout=5) for job in jobs), [False, True])

    def test_same_session_waits_but_other_session_is_independent(self):
        registry = GenerationRegistry()
        waiting, acquired = Event(), Event()
        def reader():
            waiting.set()
            with registry.locked(1) as slot:
                acquired.set()
                return slot.attempt_id
        with ThreadPoolExecutor(max_workers=1) as pool:
            with registry.locked(1) as slot:
                future = pool.submit(reader)
                self.assertTrue(waiting.wait(timeout=5))
                self.assertFalse(acquired.is_set())
                with registry.locked(2) as independent:
                    self.assertIsNone(independent.attempt_id)
                active = str(uuid4())
                slot.claim(active)
            self.assertEqual(future.result(timeout=5), active)
