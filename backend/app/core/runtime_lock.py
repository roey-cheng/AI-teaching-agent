"""同一项目目录的单主机进程锁；一直持有到服务/终端退出，不是分布式租约。"""

import os
from pathlib import Path
import stat

DEFAULT_LOCK_PATH = Path(__file__).resolve().parents[2] / ".runtime" / "chat.lock"


class RuntimeAlreadyActiveError(Exception):
    def __init__(self):
        super().__init__("Another chat process is using this project. Stop it before starting this one.")


class RuntimeLockError(Exception):
    def __init__(self):
        super().__init__("The single-process runtime lock could not be acquired or verified.")


class RuntimeLock:
    """macOS/Linux 的 flock；进程退出由操作系统释放，空锁文件保留且不能运行时删除。

    仅保证同一主机、同一项目锁路径的合作入口互斥。多目录副本、其他机器、绕过入口
    的旧程序不受保护；本版禁止它们同时访问同一聊天数据库。不能 fork 后共享此对象。
    """

    def __init__(self, path: Path = DEFAULT_LOCK_PATH):
        self.path = path
        self._fd: int | None = None
        self._pid: int | None = None

    def __enter__(self):
        if self._fd is not None:
            raise RuntimeLockError()
        fd = None
        try:
            import fcntl

            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise RuntimeLockError()
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeAlreadyActiveError() from None
            self._fd, self._pid = fd, os.getpid()
            return self
        except BaseException as error:
            if fd is not None:
                os.close(fd)
            if isinstance(error, (OSError, ImportError)):
                raise RuntimeLockError() from None
            raise

    def require_held(self) -> None:
        if self._fd is None or self._pid != os.getpid():
            raise RuntimeLockError()
        try:
            opened, current = os.fstat(self._fd), self.path.stat(follow_symlinks=False)
            if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                raise RuntimeLockError()
        except OSError:
            raise RuntimeLockError() from None

    def __exit__(self, *args):
        fd, self._fd = self._fd, None
        self._pid = None
        if fd is not None:
            # close 释放本进程描述符；绝不 unlink，否则另一进程可在新 inode 上另取锁。
            os.close(fd)
