import hashlib
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ensure_utc
from app.db.models.contribution_event import ContributionDecision, ContributionEvent
from app.db.models.mailbox_connection import MailboxConnection
from app.db.models.parser_profile import ParserProfile
from app.db.models.reconciliation_item import ReconciliationItem, ReconciliationStatus
from app.db.models.session import Session, SessionStatus
from app.domain.mailbox_providers import RawMessage
from app.domain.parsing import parse_message

ELIGIBLE_SESSION_STATUSES = (SessionStatus.AUTHORIZED, SessionStatus.LIVE, SessionStatus.PAUSED)

# Decisions genuinely worth a human look (spec 7): matched-but-uncertain, or
# arrived just outside the window and might belong to that session anyway.
# Confidently-rejected decisions (wrong sender, no credit intent, no active
# session at all) aren't queued -- they were never ambiguous.
RECONCILIABLE_DECISIONS = (ContributionDecision.AMBIGUOUS, ContributionDecision.EXCLUDED_TIME_WINDOW)


def _fingerprint(message: RawMessage) -> str:
    raw = f"{message.provider_message_id}:{message.received_at.isoformat()}:{message.sender}"
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass
class IngestResult:
    event: ContributionEvent
    already_processed: bool


async def _find_target_session(
    db: AsyncSession, connection: MailboxConnection, message: RawMessage
) -> tuple[Session | None, bool]:
    """Returns (best_candidate_session, out_of_window).

    When a message arrives before any matching session's watermark, it's
    still attributed to the soonest such session (out_of_window=True) so a
    reconciliation reviewer can see what it probably belonged to, even
    though it doesn't count toward that session's total.
    """
    result = await db.execute(
        select(Session).where(
            Session.organization_id == connection.organization_id,
            Session.status.in_(ELIGIBLE_SESSION_STATUSES),
            Session.watermark.is_not(None),
            (Session.mailbox_connection_id.is_(None))
            | (Session.mailbox_connection_id == connection.id),
        )
    )
    candidates = list(result.scalars().all())
    if not candidates:
        return None, False

    in_window = [c for c in candidates if ensure_utc(c.watermark) <= message.received_at]
    if in_window:
        in_window.sort(key=lambda s: ensure_utc(s.watermark), reverse=True)
        return in_window[0], False

    candidates.sort(key=lambda s: ensure_utc(s.watermark))
    return candidates[0], True


async def _maybe_queue_for_reconciliation(db: AsyncSession, event: ContributionEvent) -> None:
    if event.decision not in RECONCILIABLE_DECISIONS or event.session_id is None:
        return
    item = ReconciliationItem(
        organization_id=event.organization_id,
        session_id=event.session_id,
        contribution_event_id=event.id,
        reason=event.decision_reason or event.decision.value,
        status=ReconciliationStatus.PENDING,
        evidence={
            "received_at": event.received_at.isoformat(),
            "decision": event.decision.value,
            "amount": str(event.amount) if event.amount is not None else None,
            "currency": event.currency,
            "parser_confidence": event.parser_confidence,
        },
    )
    db.add(item)


async def ingest_message(
    db: AsyncSession, *, connection: MailboxConnection, message: RawMessage
) -> IngestResult:
    """Runs one fetched message through validation, session-matching and
    parsing, and persists the resulting immutable ContributionEvent (spec
    9.1 logical event flow). Idempotent: redelivering the same
    provider_message_id returns the original decision instead of
    reprocessing. Ambiguous/borderline outcomes also get queued for finance
    review (spec 7).
    """
    existing = await db.execute(
        select(ContributionEvent).where(
            ContributionEvent.organization_id == connection.organization_id,
            ContributionEvent.mailbox_connection_id == connection.id,
            ContributionEvent.provider_message_id == message.provider_message_id,
        )
    )
    found = existing.scalar_one_or_none()
    if found is not None:
        return IngestResult(event=found, already_processed=True)

    session, out_of_window = await _find_target_session(db, connection, message)

    event = ContributionEvent(
        organization_id=connection.organization_id,
        mailbox_connection_id=connection.id,
        session_id=session.id if session else None,
        provider_message_id=message.provider_message_id,
        fingerprint=_fingerprint(message),
        received_at=message.received_at,
    )

    if session is None:
        event.decision = ContributionDecision.EXCLUDED_NO_ACTIVE_SESSION
        event.decision_reason = "No session in this organization is currently authorized to monitor."
    elif out_of_window:
        event.decision = ContributionDecision.EXCLUDED_TIME_WINDOW
        event.decision_reason = "Message arrived before the session's approved watermark."
    else:
        result = await db.execute(
            select(ParserProfile)
            .where(
                ParserProfile.organization_id == connection.organization_id,
                ParserProfile.is_active.is_(True),
            )
            .order_by(ParserProfile.created_at)
        )
        profiles = list(result.scalars().all())

        # An org can have genuinely overlapping profiles (one for
        # "@bank.com" broadly, another for the specific
        # "deposits@bank.com"). Pick the most specific match rather than
        # whichever profile happens to come first; ties (e.g. two profiles
        # with the same exact-address pattern) fall back to the query's
        # stable oldest-first order, since max() over a stable sequence
        # keeps the first-encountered maximum.
        parsed_by_profile = [(profile, parse_message(message, profile)) for profile in profiles]
        candidates = [
            (profile, parsed) for profile, parsed in parsed_by_profile if parsed.matched_source
        ]
        best_match = max(candidates, key=lambda c: c[1].match_specificity) if candidates else None

        if best_match is None:
            event.decision = ContributionDecision.EXCLUDED_SOURCE_MISMATCH
            event.decision_reason = "No active parser profile's sender pattern matched."
        else:
            profile, parsed = best_match
            event.template_version = profile.template_version
            event.parser_confidence = parsed.confidence
            event.amount = parsed.amount
            event.currency = parsed.currency
            if not parsed.has_credit_intent:
                event.decision = ContributionDecision.EXCLUDED_NO_CREDIT_INTENT
                event.decision_reason = parsed.reason
            elif parsed.confidence < profile.confidence_threshold:
                event.decision = ContributionDecision.AMBIGUOUS
                event.decision_reason = parsed.reason or "Confidence below the profile's threshold."
            else:
                event.decision = ContributionDecision.ACCEPTED

    db.add(event)
    try:
        await db.flush()
    except IntegrityError:
        # Lost a race against a concurrent delivery of the same message.
        await db.rollback()
        existing = await db.execute(
            select(ContributionEvent).where(
                ContributionEvent.organization_id == connection.organization_id,
                ContributionEvent.mailbox_connection_id == connection.id,
                ContributionEvent.provider_message_id == message.provider_message_id,
            )
        )
        return IngestResult(event=existing.scalar_one(), already_processed=True)

    await _maybe_queue_for_reconciliation(db, event)
    await db.flush()

    return IngestResult(event=event, already_processed=False)
