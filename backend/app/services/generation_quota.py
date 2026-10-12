"""使用 MySQL 事务持久化每日用量与全站并发准入。"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import GenerationQuotaSettings
from app.models import GenerationAttempt, GenerationControl, MemoryJob
from app.services.errors import (
    GlobalConcurrencyLimitError,
    GlobalDailyQuotaExceededError,
    MessageSendUnavailableError,
    UserDailyQuotaExceededError,
)


def _seconds_until_next_utc_day(now: datetime) -> int:
    tomorrow = (now + timedelta(days=1)).date()
    boundary = datetime.combine(tomorrow, datetime.min.time(), tzinfo=UTC)
    return max(1, int((boundary - now).total_seconds()))


class GenerationQuota:
    def __init__(self, settings: GenerationQuotaSettings):
        self.settings = settings

    def admit(
        self,
        session: Session,
        *,
        user_id: int,
        message_id: int,
        attempt_id: str,
        now: datetime,
    ) -> None:
        """调用方已锁会话行；控制行把不同会话的并发准入串行化。"""
        control = session.scalar(
            select(GenerationControl).where(GenerationControl.control_id == 1).with_for_update()
        )
        if control is None:
            raise MessageSendUnavailableError()
        quota_date = now.date()
        global_used = session.scalar(
            select(func.count())
            .select_from(GenerationAttempt)
            .where(GenerationAttempt.quota_date == quota_date)
        )
        user_used = session.scalar(
            select(func.count())
            .select_from(GenerationAttempt)
            .where(
                GenerationAttempt.quota_date == quota_date,
                GenerationAttempt.user_id == user_id,
            )
        )
        active = session.scalar(
            select(func.count())
            .select_from(GenerationAttempt)
            .where(GenerationAttempt.status == "RUNNING")
        )
        background_active = session.scalar(
            select(func.count()).select_from(MemoryJob).where(MemoryJob.status == "RUNNING")
        )
        retry_after = _seconds_until_next_utc_day(now.replace(tzinfo=UTC))
        if int(global_used or 0) >= self.settings.global_daily_limit:
            raise GlobalDailyQuotaExceededError(retry_after)
        if int(user_used or 0) >= self.settings.user_daily_limit:
            raise UserDailyQuotaExceededError(retry_after)
        if int(active or 0) + int(background_active or 0) >= self.settings.global_concurrency_limit:
            raise GlobalConcurrencyLimitError()
        session.add(
            GenerationAttempt(
                attempt_id=attempt_id,
                user_id=user_id,
                message_id=message_id,
                quota_date=quota_date,
                status="RUNNING",
                created_at=now,
                finished_at=None,
            )
        )
        session.flush()

    def finish(self, session: Session, attempt_id: str, status: str, now: datetime) -> None:
        if status not in {"SUCCEEDED", "FAILED"}:
            raise ValueError("Generation quota status must be terminal")
        attempt = session.scalar(
            select(GenerationAttempt)
            .where(GenerationAttempt.attempt_id == attempt_id)
            .with_for_update()
        )
        if attempt is None:
            raise MessageSendUnavailableError()
        if attempt.status == status:
            return
        if attempt.status != "RUNNING":
            raise MessageSendUnavailableError()
        attempt.status = status
        attempt.finished_at = now
        session.flush()
