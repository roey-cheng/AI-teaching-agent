"""Native filesystem helper for read-only graph snapshots and extraction probes.

Durable writes use transaction-bound ProfileStore, never this disposable view.
"""

from deepagents.backends import StoreBackend

from app.agent.profile_store import PROFILE_MEMORY_PATH, ProfileStore

__all__ = ["PROFILE_MEMORY_PATH", "snapshot_backend"]


def snapshot_backend(
    profile_text: str, *, user_id: int = 1, skill_text: str | None = None
) -> StoreBackend:
    return ProfileStore.snapshot(user_id, profile_text, skill_text=skill_text).backend()
