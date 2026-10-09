"""单进程的最小运行登记；不是任务执行器，不启动模型、不恢复任务。"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock

from pydantic import TypeAdapter

from app.schemas._types import UUIDString
from app.schemas._validation import format_database_id

_attempt_adapter = TypeAdapter(UUIDString)


@dataclass
class _RunSlot:
    # 仅允许在 GenerationRegistry.locked() 内访问或修改。
    attempt_id: str | None = None

    def claim(self, attempt_id: str) -> bool:
        attempt_id = _attempt_adapter.validate_python(attempt_id)
        if self.attempt_id is not None:
            return False
        self.attempt_id = attempt_id
        return True

    def release(self, attempt_id: str) -> bool:
        attempt_id = _attempt_adapter.validate_python(attempt_id)
        if self.attempt_id != attempt_id:
            return False  # 旧执行的清理不能释放新执行的占用。
        self.attempt_id = None
        return True


@dataclass
class _Entry:
    lock: Lock = field(default_factory=Lock)
    slot: _RunSlot = field(default_factory=_RunSlot)
    users: int = 0  # 包括正在等锁的调用者，防止删除仍被等待的锁。


class GenerationRegistry:
    """应用生命周期共享一份；不能每个请求新建，也不支持多个进程共享。

    locked 内只做短的同步数据库操作/状态切换，不能等待模型或跨 await 持锁。
    未来异步接口应在线程池中执行整个同步业务函数，而非在事件循环直接等锁。
    """

    def __init__(self):
        self._entries: dict[int, _Entry] = {}
        self._index_lock = Lock()

    @contextmanager
    def locked(self, session_id: int) -> Iterator[_RunSlot]:
        session_id = int(format_database_id(session_id, "Session ID"))
        with self._index_lock:
            entry = self._entries.setdefault(session_id, _Entry())
            entry.users += 1
        try:
            with entry.lock:
                yield entry.slot
        finally:
            with self._index_lock:
                entry.users -= 1
                if entry.users == 0 and entry.slot.attempt_id is None:
                    del self._entries[session_id]
