"""每次图运行单独提供的服务端依赖；不把用户资料固定保存在可复用的图里。"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from threading import Lock
from typing import TYPE_CHECKING

from langgraph.runtime import get_runtime
from langgraph.store.base import BaseStore, Op, Result

from app.agent.profile_store import PROFILE_MEMORY_PATH, ProfileStore
from app.services.agent_input import ChatAdmissionSnapshot

if TYPE_CHECKING:
    from app.agent.orchestrator import ChatAttemptState


@dataclass(frozen=True, repr=False)
class ChatRunDependencies:  # 规定本轮运行资料应该有哪些
    """一次图调用的依赖包，不是模型上下文，也不是 checkpoint 中的会话状态。

    每次调用都新建；同一会话复用图时，不复用这份快照和尝试状态。
    """

    admission_snapshot: ChatAdmissionSnapshot
    profile_snapshot_store: ProfileStore
    attempt_state: ChatAttemptState | None = None
    _started: Lock = field(default_factory=Lock, init=False, repr=False)

    def claim(self):
        if not self._started.acquire(blocking=False):
            raise ValueError("Each graph invocation requires fresh chat run dependencies")


def build_chat_run_dependencies(
    admission_snapshot: ChatAdmissionSnapshot, attempt_state=None
) -> ChatRunDependencies:
    """核对资料归属，并为本次调用创建只读画像 Store 和依赖包。"""
    if admission_snapshot.memory_path != PROFILE_MEMORY_PATH:
        raise ValueError("Unexpected profile memory path")
    if attempt_state is not None and (
        admission_snapshot.user_id != attempt_state.accepted.admission_snapshot.user_id
        or admission_snapshot.session_id != attempt_state.accepted.session_id
    ):
        raise ValueError("Chat attempt and admission snapshot ownership mismatch")
    profile_snapshot_store = ProfileStore.read_only_snapshot(
        int(admission_snapshot.user_id),
        admission_snapshot.profile_memory,
        facts=attempt_state.profile_facts if attempt_state is not None else None,
        strip_memory_mount_prefix=True,
    )
    return ChatRunDependencies(admission_snapshot, profile_snapshot_store, attempt_state)


def require_chat_run_dependencies(runtime=None) -> ChatRunDependencies:
    runtime = runtime if runtime is not None else get_runtime()
    dependencies = runtime.context
    if not isinstance(dependencies, ChatRunDependencies):
        raise TypeError("A server-owned ChatRunDependencies is required")
    return dependencies


class ProfileSnapshotStoreRouter(BaseStore):
    """图复用的无状态转接器：把 StoreBackend 的操作交给本次调用的画像快照。

    它自己不保存画像，也不固定持有某个用户的 Store；实际快照在运行依赖包里。
    LangGraph 会把运行依赖传到异步任务和同步工作线程，包括文件系统钩子。
    batch/abatch 是 BaseStore 规定的接口名，表示批量执行 Store 操作。
    """

    def batch(self, ops: Iterable[Op]) -> list[Result]:
        return require_chat_run_dependencies().profile_snapshot_store.batch(ops)

    async def abatch(self, ops: Iterable[Op]) -> list[Result]:
        return await require_chat_run_dependencies().profile_snapshot_store.abatch(ops)
