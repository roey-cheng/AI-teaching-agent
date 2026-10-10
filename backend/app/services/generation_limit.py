"""单进程的生成频率检查；不是跨进程/分布式限流，也不包含注册登录限流。"""

from collections import deque
from collections.abc import Callable
from math import ceil
from threading import Lock
from time import monotonic

from app.services.errors import GenerationRateLimitError


class GenerationRateLimiter:
    def __init__(self, *, clock: Callable[[], float] = monotonic):
        self._clock = clock
        self._lock = Lock()
        self._accepted: dict[str, deque[float]] = {}

    def check(self, user_id: str) -> None:
        """新请求的准入钩子：滚动 60 秒最多 10 次；身份必须来自后端认证。

        查重/忙碌/预算拒绝发生在本钩子之前，不消耗额度。钩子通过后若提交失败，
        保守地保留计数，不用数据库故障绕过限制；进程重启后计数重置。
        """
        with self._lock:
            now = self._clock()
            times = self._accepted.setdefault(user_id, deque())
            while times and times[0] <= now - 60:
                times.popleft()
            if len(times) >= 10:
                raise GenerationRateLimitError(max(1, ceil(60 - (now - times[0]))))
            times.append(now)
