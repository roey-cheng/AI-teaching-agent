"""Run-local capabilities for a reusable graph; never kept on shared middleware."""

from collections.abc import Iterable
from dataclasses import dataclass, field
from threading import Lock
from typing import TYPE_CHECKING

from langgraph.runtime import get_runtime
from langgraph.store.base import BaseStore, Op, Result

from app.agent.profile_store import PROFILE_MEMORY_PATH, ProfileStore
from app.agent.teaching_skill import load_teaching_skill
from app.services.agent_input import PreparedAgentInput

if TYPE_CHECKING:
    from app.agent.orchestrator import TurnMemory


@dataclass(frozen=True, repr=False)
class ChatRunContext: #规定本轮运行资料应该有哪些
    prepared: PreparedAgentInput
    store: ProfileStore
    turn: TurnMemory | None = None
    _started: Lock = field(default_factory=Lock, init=False, repr=False)

    def claim(self):
        if not self._started.acquire(blocking=False):
            raise ValueError("Each graph invocation requires a fresh chat context")


def prepare_run_context(prepared: PreparedAgentInput, turn=None) -> ChatRunContext:
    if prepared.memory_path != PROFILE_MEMORY_PATH:
        raise ValueError("Unexpected profile memory path")
    if turn is not None and (
        prepared.user_id != turn.accepted.prepared_input.user_id
        or prepared.session_id != turn.accepted.session_id
    ):
        raise ValueError("Turn and prepared input ownership mismatch")
    store = ProfileStore.snapshot(
        int(prepared.user_id),
        prepared.profile_memory,
        facts=turn.facts if turn is not None else None,
        skill_text=load_teaching_skill() if turn is not None else None,
    )
    return ChatRunContext(prepared, store, turn)


def require_run_context(runtime=None) -> ChatRunContext:
    runtime = runtime if runtime is not None else get_runtime()
    context = runtime.context
    if not isinstance(context, ChatRunContext):
        raise TypeError("A server-owned ChatRunContext is required")
    return context


class RunScopedStore(BaseStore):
    """Stateless router: native StoreBackend uses the current invocation's Store.

    Never assign a user/store to this object. LangGraph runtime context propagates
    through its async tasks and sync worker threads, including filesystem hooks.
    """

    def batch(self, ops: Iterable[Op]) -> list[Result]:
        return require_run_context().store.batch(ops)

    async def abatch(self, ops: Iterable[Op]) -> list[Result]:
        return await require_run_context().store.abatch(ops)
