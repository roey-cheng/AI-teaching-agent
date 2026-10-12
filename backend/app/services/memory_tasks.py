"""Durable idle consolidation. Short DB transactions never span model requests."""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select

from app.models import (
    AgentMemory,
    ChatSession,
    GenerationAttempt,
    GenerationControl,
    MemoryJob,
    MemoryState,
    MemoryTopicRevision,
    Message,
)


def utcnow():
    return datetime.now(UTC).replace(tzinfo=None)


def control_lock(session):
    if (
        session.scalar(
            select(GenerationControl).where(GenerationControl.control_id == 1).with_for_update()
        )
        is None
    ):
        raise RuntimeError("Memory control unavailable")


def touch_memory(session, user_id, now, *, dirty=False, message_id=None):
    # Callers serialize through generation_control, after locking their chat/message.
    control_lock(session)
    state = session.get(MemoryState, user_id)
    if state is None:
        state = MemoryState(
            user_id=user_id, cursor=0, pending_ids=[], dirty=False, failures=0, paused=False
        )
        session.add(state)
    state.last_activity_at = now
    state.due_at = now
    state.dirty = state.dirty or dirty
    if message_id is not None:
        state.pending_ids = sorted(set(state.pending_ids) | {message_id})
    state.paused = False
    state.failures = 0


def profile_snapshot(session, user_id):
    facts = {
        row.memory_key: row.summary
        for row in session.scalars(select(AgentMemory).where(AgentMemory.user_id == user_id))
    }
    versions = {
        row.memory_key: (row.revision, row.source_message_id)
        for row in session.scalars(
            select(MemoryTopicRevision).where(MemoryTopicRevision.user_id == user_id)
        )
    }
    return facts, versions


class MemoryConflict(Exception):
    pass


def check_and_bump_revisions(session, user_id, keys, source_ids, expected):
    control_lock(session)
    for key in sorted(keys):
        row = session.get(MemoryTopicRevision, (user_id, key))
        actual = row.revision if row else 0
        source_id = source_ids[key]
        if actual != expected.get(key, (0, 0))[0]:
            raise MemoryConflict("Profile changed; reload before updating")
        if row and source_id < row.source_message_id:
            raise MemoryConflict("Older evidence cannot replace newer memory")
        if row is None:
            row = MemoryTopicRevision(
                user_id=user_id, memory_key=key, revision=0, source_message_id=0
            )
            session.add(row)
        row.revision += 1
        row.source_message_id = source_id


@dataclass(frozen=True)
class ClaimedMemoryJob:
    job_id: str
    user_id: int
    sources: tuple[dict, ...] = field(repr=False)
    profile: dict = field(repr=False)
    versions: dict


def claim_memory_job(factory, settings, global_concurrency, now=None):
    now = now or utcnow()
    with factory.begin() as session:
        control_lock(session)
        # Foreground work has priority; do not dispatch during active chat or another job.
        if session.scalar(
            select(func.count())
            .select_from(GenerationAttempt)
            .where(GenerationAttempt.status == "RUNNING")
        ):
            return None
        if (
            session.scalar(
                select(func.count()).select_from(MemoryJob).where(MemoryJob.status == "RUNNING")
            )
            or global_concurrency < 1
        ):
            return None
        global_used = session.scalar(
            select(func.count()).select_from(MemoryJob).where(MemoryJob.quota_date == now.date())
        )
        if global_used >= settings.global_daily_limit:
            return None
        states = session.scalars(
            select(MemoryState)
            .where(
                MemoryState.dirty.is_(True),
                MemoryState.paused.is_(False),
                MemoryState.due_at <= now,
                MemoryState.last_activity_at <= now - timedelta(seconds=settings.idle_seconds),
            )
            .order_by(MemoryState.due_at)
            .limit(100)
        ).all()
        for state in states:
            latest = session.scalar(
                select(func.max(Message.created_at))
                .join(ChatSession)
                .where(ChatSession.user_id == state.user_id, Message.role == "USER")
            )
            if latest and latest > now - timedelta(seconds=settings.idle_seconds):
                continue
            used = session.scalar(
                select(func.count())
                .select_from(MemoryJob)
                .where(MemoryJob.user_id == state.user_id, MemoryJob.quota_date == now.date())
            )
            if used >= settings.user_daily_limit:
                state.due_at = datetime.combine(now.date() + timedelta(days=1), datetime.min.time())
                continue
            # Also works for terminal callers without a GenerationQuota instance.
            if session.scalar(
                select(Message.message_id)
                .join(ChatSession)
                .where(ChatSession.user_id == state.user_id, Message.generation_status == "RUNNING")
                .limit(1)
            ):
                continue
            rows = session.scalars(
                select(Message)
                .join(ChatSession)
                .where(
                    ChatSession.user_id == state.user_id,
                    Message.role == "USER",
                    Message.message_id.in_(state.pending_ids),
                    Message.generation_status == "SUCCEEDED",
                )
                .order_by(Message.message_id)
                .limit(32)
            ).all()
            if not rows:
                state.dirty = False
                continue
            facts, versions = profile_snapshot(session, state.user_id)
            source_budget = min(
                24_000, max(0, 40_000 - len(json.dumps(facts, ensure_ascii=False).encode()))
            )
            sources, size = [], 0
            for row in rows:
                source = {
                    "message_id": row.message_id,
                    "session_id": row.chat_session_id,
                    "content": row.content,
                }
                cost = len(json.dumps(source, ensure_ascii=False).encode())
                if size + cost > source_budget:
                    break
                sources.append(source)
                size += cost
            if not sources:
                # Never silently truncate a source then advance its cursor.
                state.paused = True
                session.add(
                    MemoryJob(
                        job_id=str(uuid4()),
                        user_id=state.user_id,
                        quota_date=now.date(),
                        status="FAILED",
                        source_ids=[rows[0].message_id],
                        created_at=now,
                        finished_at=now,
                        error_code="MEMORY_SOURCE_TOO_LARGE",
                    )
                )
                global_used += 1
                if global_used >= settings.global_daily_limit:
                    return None
                continue
            job = MemoryJob(
                job_id=str(uuid4()),
                user_id=state.user_id,
                quota_date=now.date(),
                status="RUNNING",
                source_ids=[s["message_id"] for s in sources],
                created_at=now,
            )
            session.add(job)
            return ClaimedMemoryJob(job.job_id, state.user_id, tuple(sources), facts, versions)
    return None


def fail_memory_job(factory, job_id, code="MEMORY_JOB_FAILED", now=None):
    now = now or utcnow()
    with factory.begin() as session:
        control_lock(session)
        job = session.get(MemoryJob, job_id)
        if job is None or job.status != "RUNNING":
            return  # Includes a successful but ambiguously acknowledged commit.
        job.status, job.finished_at, job.error_code = "FAILED", now, code
        state = session.get(MemoryState, job.user_id)
        state.failures += 1
        state.paused = state.failures >= 3
        state.due_at = now + timedelta(seconds=60 if state.failures == 1 else 300)


def recover_memory_jobs(factory):
    # Only run after acquiring the existing database-wide backend lock.
    with factory() as session:
        ids = list(session.scalars(select(MemoryJob.job_id).where(MemoryJob.status == "RUNNING")))
    for job_id in ids:
        fail_memory_job(factory, job_id, "MEMORY_JOB_INTERRUPTED")


def finish_memory_job(factory, claimed, candidates, now=None):
    """Validated candidates + revisions + cursor + job result are a single commit."""
    from app.agent.profile_memory_tool import contains_sensitive_memory
    from app.services.profile_memory import ProfileFact

    now = now or utcnow()
    sources = {s["message_id"]: s["content"] for s in claimed.sources}
    checked, evidence = [], {}
    for item in candidates:
        fact = ProfileFact(memory_key=item.memory_key, summary=item.summary)
        quote = item.source_quote
        if (
            item.source_message_id not in sources
            or not quote.strip()
            or quote not in sources[item.source_message_id]
            or contains_sensitive_memory(sources[item.source_message_id])
            or contains_sensitive_memory(quote + "\n" + fact.summary)
        ):
            raise ValueError("Invalid memory evidence")
        if fact.memory_key in evidence:
            raise ValueError("Duplicate memory topic")
        if item.source_message_id < claimed.versions.get(fact.memory_key, (0, 0))[1]:
            continue  # Explicitly superseded evidence: retain the newer profile, drain old work.
        checked.append(fact)
        evidence[fact.memory_key] = item.source_message_id
    with factory.begin() as session:
        control_lock(session)
        job = session.get(MemoryJob, claimed.job_id)
        if job is None or job.user_id != claimed.user_id or job.source_ids != list(sources):
            raise ValueError("Invalid memory job")
        if job.status == "SUCCEEDED":
            return
        if job.status != "RUNNING":
            raise MemoryConflict("Expired job")
        check_and_bump_revisions(session, claimed.user_id, evidence, evidence, claimed.versions)
        from app.models.agent_memory import MEMORY_TOPIC_TYPES

        for fact in checked:
            row = session.scalar(
                select(AgentMemory).where(
                    AgentMemory.user_id == claimed.user_id,
                    AgentMemory.memory_key == fact.memory_key,
                )
            )
            if row is None:
                row = AgentMemory(
                    user_id=claimed.user_id,
                    memory_key=fact.memory_key,
                    memory_type=MEMORY_TOPIC_TYPES[fact.memory_key],
                    created_at=now,
                )
                session.add(row)
            row.summary, row.updated_at = fact.summary, now
        state = session.get(MemoryState, claimed.user_id)
        state.cursor = max(state.cursor, *job.source_ids)
        state.pending_ids = [mid for mid in state.pending_ids if mid not in job.source_ids]
        state.dirty = bool(state.pending_ids)
        state.failures, state.paused = 0, False
        # Keep dirty: next scan can discover messages arriving during this model call.
        state.due_at = now
        job.status, job.finished_at = "SUCCEEDED", now
