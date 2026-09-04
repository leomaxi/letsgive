import csv
import io

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_session_membership
from app.api.v1.schemas import SessionReportOut
from app.db.models.approval import Approval
from app.db.models.contribution_event import ContributionDecision, ContributionEvent
from app.db.models.mailbox_connection import MailboxConnection
from app.db.models.membership import Membership, Role
from app.db.models.organization import Organization
from app.db.models.reconciliation_item import ReconciliationItem, ReconciliationStatus
from app.db.models.session import Session
from app.db.session import get_db
from app.domain.billing import assert_reports_readable
from app.domain.ledger import compute_ledger_totals
from app.domain.pdf_report import render_session_report_pdf
from app.domain.rbac import require_roles

router = APIRouter(prefix="/v1/reports/sessions", tags=["reports"])


async def _build_report(db: AsyncSession, session: Session) -> SessionReportOut:
    validated_count, validated_amount = await compute_ledger_totals(db, session.id)

    result = await db.execute(
        select(ContributionEvent.decision, func.count(ContributionEvent.id))
        .where(
            ContributionEvent.session_id == session.id,
            ContributionEvent.decision != ContributionDecision.ACCEPTED,
        )
        .group_by(ContributionEvent.decision)
    )
    excluded_counts = {decision.value: count for decision, count in result.all()}

    result = await db.execute(
        select(ReconciliationItem).where(
            ReconciliationItem.session_id == session.id,
            ReconciliationItem.status == ReconciliationStatus.RESOLVED,
        )
    )
    corrections = list(result.scalars().all())

    result = await db.execute(
        select(Approval).where(Approval.session_id == session.id).order_by(Approval.created_at)
    )
    approvals = list(result.scalars().all())
    approval_history = [
        {
            "id": a.id,
            "requested_by_user_id": a.requested_by_user_id,
            "created_at": a.created_at.isoformat(),
            "expires_at": a.expires_at.isoformat(),
            "attempts": a.attempts,
            "locked": a.locked,
            "verified_at": a.verified_at.isoformat() if a.verified_at else None,
        }
        for a in approvals
    ]

    connection_health = None
    if session.mailbox_connection_id:
        connection = await db.get(MailboxConnection, session.mailbox_connection_id)
        if connection is not None:
            connection_health = {
                "connection_id": connection.id,
                "status": connection.status.value,
                "webhook_health": connection.webhook_health,
                "last_sync_at": connection.last_sync_at.isoformat() if connection.last_sync_at else None,
            }

    return SessionReportOut(
        session_id=session.id,
        status=session.status,
        validated_count=validated_count or 0,
        validated_amount=validated_amount,
        excluded_counts=excluded_counts,
        corrections=corrections,
        approval_history=approval_history,
        connection_health=connection_health,
    )


@router.get("/{session_id}", response_model=SessionReportOut)
async def get_session_report(
    session_id: str,
    session_membership: tuple[Session, Membership] = Depends(get_session_membership),
    db: AsyncSession = Depends(get_db),
) -> SessionReportOut:
    session, membership = session_membership
    require_roles(membership, Role.FINANCE, Role.AUDITOR, Role.OWNER)
    org = await db.get(Organization, session.organization_id)
    if org is not None:
        assert_reports_readable(org)
    return await _build_report(db, session)


@router.get("/{session_id}/export.csv")
async def export_session_csv(
    session_id: str,
    session_membership: tuple[Session, Membership] = Depends(get_session_membership),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    session, membership = session_membership
    require_roles(membership, Role.FINANCE, Role.AUDITOR, Role.OWNER)
    org = await db.get(Organization, session.organization_id)
    if org is not None:
        assert_reports_readable(org)

    result = await db.execute(
        select(ContributionEvent)
        .where(ContributionEvent.session_id == session.id)
        .order_by(ContributionEvent.received_at)
    )
    events = list(result.scalars().all())

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "event_id",
            "received_at",
            "decision",
            "amount",
            "currency",
            "parser_confidence",
            "test_mode",
            "corrects_event_id",
        ]
    )
    for event in events:
        writer.writerow(
            [
                event.id,
                event.received_at.isoformat(),
                event.decision.value,
                event.amount if event.amount is not None else "",
                event.currency or "",
                event.parser_confidence if event.parser_confidence is not None else "",
                event.test_mode,
                event.corrects_event_id or "",
            ]
        )
    buffer.seek(0)

    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="session-{session.id}.csv"'},
    )


@router.get("/{session_id}/export.pdf")
async def export_session_pdf(
    session_id: str,
    session_membership: tuple[Session, Membership] = Depends(get_session_membership),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Spec 7 calls for both a PDF and a CSV export; the CSV (above) is the
    raw per-event ledger for accounting/integration, this is the
    human-readable summary -- same underlying data as GET .../{id}, just
    laid out for printing or attaching to board minutes.
    """
    session, membership = session_membership
    require_roles(membership, Role.FINANCE, Role.AUDITOR, Role.OWNER)
    org = await db.get(Organization, session.organization_id)
    if org is not None:
        assert_reports_readable(org)

    report = await _build_report(db, session)
    pdf_bytes = render_session_report_pdf(report, org.name if org else "")

    return StreamingResponse(
        iter([pdf_bytes]),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="session-{session.id}.pdf"'},
    )
