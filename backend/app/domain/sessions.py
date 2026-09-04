from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ensure_utc
from app.db.models.mailbox_connection import ConnectionStatus, MailboxConnection
from app.db.models.reconciliation_item import ReconciliationItem, ReconciliationStatus
from app.db.models.session import ALLOWED_TRANSITIONS, Session, SessionStatus

# How long a live session can go without a mailbox sync before the operator
# is warned that the provider may have disconnected (spec 4's fail-safe:
# "never guess or increment optimistically... show a private operator
# warning"). Deliberately generous to avoid false alarms right after
# authorization, before any webhook could plausibly have arrived yet.
STALE_SYNC_THRESHOLD = timedelta(minutes=10)


def check_version(session: Session, expected_version: int) -> None:
    if session.version != expected_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Session was modified by someone else (expected version {expected_version}, "
                f"current version {session.version}). Reload and retry."
            ),
        )


def transition(session: Session, to_status: SessionStatus) -> None:
    allowed = ALLOWED_TRANSITIONS.get(session.status, set())
    if to_status not in allowed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot move session from '{session.status.value}' to '{to_status.value}'.",
        )
    session.status = to_status
    session.version += 1


async def compute_operator_warning(db: AsyncSession, session: Session) -> str | None:
    """Private, operator-only fail-safe signal (spec 4): the public count
    never guesses or degrades, but a live session's operator should know
    when something's off -- the connection's gone quiet, or recent
    messages are piling up for review. Computed fresh on every read rather
    than persisted, so it can never go stale.
    """
    warnings: list[str] = []

    if session.status == SessionStatus.LIVE and session.mailbox_connection_id:
        connection = await db.get(MailboxConnection, session.mailbox_connection_id)
        if connection is not None:
            if connection.status != ConnectionStatus.CONNECTED:
                warnings.append(
                    f"Mailbox connection is '{connection.status.value}' -- "
                    "new deposit notifications will not be received."
                )
            else:
                reference = connection.last_sync_at or session.watermark
                if reference is not None:
                    age = datetime.now(timezone.utc) - ensure_utc(reference)
                    if age > STALE_SYNC_THRESHOLD:
                        minutes = int(age.total_seconds() // 60)
                        warnings.append(
                            f"No mailbox activity received in {minutes} minutes -- "
                            "verify the connection is still working."
                        )

    result = await db.execute(
        select(func.count(ReconciliationItem.id)).where(
            ReconciliationItem.session_id == session.id,
            ReconciliationItem.status == ReconciliationStatus.PENDING,
        )
    )
    pending = result.scalar_one()
    if pending:
        warnings.append(f"{pending} message(s) awaiting reconciliation review.")

    return " ".join(warnings) if warnings else None
