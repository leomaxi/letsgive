import csv
import io
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, get_membership, get_session_membership
from app.api.v1.schemas import (
    ContributionExportFieldOut,
    ContributionExportTemplateCreateRequest,
    ContributionExportTemplateOut,
    SessionReportOut,
)
from app.db.models.approval import Approval
from app.db.models.contribution_event import ContributionDecision, ContributionEvent
from app.db.models.contribution_export_template import ContributionExportTemplate
from app.db.models.mailbox_connection import MailboxConnection, MailboxProviderName
from app.db.models.membership import Membership, Role
from app.db.models.organization import Organization
from app.db.models.parser_profile import ParserProfile
from app.db.models.reconciliation_item import ReconciliationItem, ReconciliationStatus
from app.db.models.session import Session
from app.db.session import get_db
from app.domain.billing import assert_reports_readable
from app.domain.audit import record_audit_event
from app.domain.imap_polling import (
    fetch_imap_message_by_message_id,
    fetch_imap_messages_between,
    get_recent_messages,
)
from app.domain.ingestion import build_export_details
from app.domain.ledger import compute_ledger_totals
from app.domain.mailbox_providers import get_provider
from app.domain.parsing import parse_message
from app.domain.pdf_report import render_session_report_pdf
from app.domain.rbac import require_roles

router = APIRouter(prefix="/v1/reports/sessions", tags=["reports"])
org_reports_router = APIRouter(prefix="/v1/organizations/{organization_id}/contribution-export", tags=["reports"])

DEFAULT_EXPORT_FIELDS = ["sender_name", "received_at", "amount", "message", "reference_number"]
FIELD_LABELS = {
    "sender_name": "Sender's name - Sent from",
    "sent_from": "Sent from",
    "received_at": "Date time",
    "amount": "Amount",
    "currency": "Currency",
    "message": "Message",
    "reference_number": "Reference Number",
    "provider_message_id": "Provider Message ID",
}


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _event_export_value(event: ContributionEvent, key: str) -> str:
    details: dict[str, Any] = event.export_details or {}
    if key == "received_at":
        return event.received_at.isoformat()
    if key == "amount":
        return str(event.amount) if event.amount is not None else ""
    if key == "currency":
        return event.currency or ""
    if key == "provider_message_id":
        return event.provider_message_id
    value = details.get(key)
    return "" if value is None else str(value)


def _row_export_value(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    if isinstance(value, datetime):
        return value.isoformat()
    return "" if value is None else str(value)


def _keyword_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip().lower() for part in raw.split(",") if part.strip()]


def _matches_keywords(event: ContributionEvent, keywords: list[str]) -> bool:
    if not keywords:
        return True
    details: dict[str, Any] = event.export_details or {}
    haystack = " ".join(str(details.get(key) or "") for key in ("message", "sent_from", "sender_name"))
    haystack = haystack.lower()
    return any(keyword in haystack for keyword in keywords)


def _row_matches_keywords(row: dict[str, Any], keywords: list[str]) -> bool:
    if not keywords:
        return True
    haystack = " ".join(str(row.get(key) or "") for key in ("message", "sent_from", "sender_name"))
    haystack = haystack.lower()
    return any(keyword in haystack for keyword in keywords)


def _parsed_export_row(message, profiles: list[ParserProfile]) -> dict[str, Any] | None:
    parsed_by_profile = [(profile, parse_message(message, profile)) for profile in profiles]
    candidates = [(profile, parsed) for profile, parsed in parsed_by_profile if parsed.matched_source]
    best_match = max(candidates, key=lambda c: c[1].match_specificity) if candidates else None
    if best_match is None:
        return None
    _profile, parsed = best_match
    if not parsed.has_credit_intent or parsed.amount is None:
        return None
    details = build_export_details(message)
    return {
        **details,
        "received_at": message.received_at,
        "amount": parsed.amount,
        "currency": parsed.currency,
        "provider_message_id": message.provider_message_id,
    }


async def _email_export_rows(
    db: AsyncSession,
    connection: MailboxConnection,
    from_datetime: datetime,
    to_datetime: datetime,
) -> list[dict[str, Any]]:
    result = await db.execute(
        select(ParserProfile)
        .where(
            ParserProfile.organization_id == connection.organization_id,
            ParserProfile.is_active.is_(True),
        )
        .order_by(ParserProfile.created_at)
    )
    profiles = list(result.scalars().all())
    if not profiles:
        return []

    messages = await fetch_imap_messages_between(connection, from_datetime, to_datetime)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for message in messages:
        if message.provider_message_id in seen:
            continue
        seen.add(message.provider_message_id)
        row = _parsed_export_row(message, profiles)
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda row: row["received_at"])
    return rows


async def _hydrate_missing_export_details(
    db: AsyncSession, connection: MailboxConnection, events: list[ContributionEvent]
) -> None:
    missing = [event for event in events if not event.export_details]
    if not missing:
        return

    hydrated = False
    for event in missing:
        message = None
        if connection.provider == MailboxProviderName.IMAP:
            message = await fetch_imap_message_by_message_id(connection, event.provider_message_id)
        else:
            try:
                message = await get_provider(connection.provider).fetch_message(
                    connection=connection,
                    provider_message_id=event.provider_message_id,
                )
            except NotImplementedError:
                message = None
        if message is None:
            continue
        event.export_details = build_export_details(message)
        hydrated = True

    if hydrated:
        await db.commit()


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


@org_reports_router.get("/templates", response_model=list[ContributionExportTemplateOut])
async def list_export_templates(
    organization_id: str,
    membership: Membership = Depends(get_membership),
    db: AsyncSession = Depends(get_db),
) -> list[ContributionExportTemplate]:
    require_roles(membership, Role.OWNER, Role.FINANCE)
    result = await db.execute(
        select(ContributionExportTemplate)
        .where(ContributionExportTemplate.organization_id == organization_id)
        .order_by(ContributionExportTemplate.created_at)
    )
    return list(result.scalars().all())


@org_reports_router.post(
    "/templates",
    response_model=ContributionExportTemplateOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_export_template(
    organization_id: str,
    payload: ContributionExportTemplateCreateRequest,
    request: Request,
    membership: Membership = Depends(get_membership),
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ContributionExportTemplate:
    require_roles(membership, Role.OWNER, Role.FINANCE)

    unknown = [key for key in payload.field_keys if key not in FIELD_LABELS]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown export field(s): {', '.join(unknown)}.",
        )

    labels = {key: FIELD_LABELS[key] for key in payload.field_keys}
    labels.update(payload.field_labels or {})
    template = ContributionExportTemplate(
        organization_id=organization_id,
        name=payload.name,
        field_keys=payload.field_keys,
        field_labels=labels,
        sample=payload.sample,
    )
    db.add(template)
    await db.flush()

    await record_audit_event(
        db,
        action="contribution_export_template.created",
        target_type="contribution_export_template",
        target_id=template.id,
        organization_id=organization_id,
        actor_user_id=current_user.id,
        after={"name": template.name, "field_keys": template.field_keys},
        ip_address=_client_ip(request),
    )
    await db.commit()
    await db.refresh(template)
    return template


@org_reports_router.delete("/templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_export_template(
    organization_id: str,
    template_id: str,
    request: Request,
    membership: Membership = Depends(get_membership),
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    require_roles(membership, Role.OWNER, Role.FINANCE)
    template = await db.get(ContributionExportTemplate, template_id)
    if template is None or template.organization_id != organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Export template not found.")
    await db.delete(template)
    await record_audit_event(
        db,
        action="contribution_export_template.deleted",
        target_type="contribution_export_template",
        target_id=template_id,
        organization_id=organization_id,
        actor_user_id=current_user.id,
        before={"name": template.name, "field_keys": template.field_keys},
        ip_address=_client_ip(request),
    )
    await db.commit()


@org_reports_router.get("/fields/sample", response_model=list[ContributionExportFieldOut])
async def sample_export_fields(
    organization_id: str,
    connection_id: str | None = None,
    membership: Membership = Depends(get_membership),
    db: AsyncSession = Depends(get_db),
) -> list[ContributionExportFieldOut]:
    require_roles(membership, Role.OWNER, Role.FINANCE)

    samples: dict[str, list[str]] = {key: [] for key in FIELD_LABELS}
    connection = None

    if connection_id:
        connection = await db.get(MailboxConnection, connection_id)
        if connection is None or connection.organization_id != organization_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found.")
        for entry in get_recent_messages(connection_id)[:10]:
            values = {
                "sender_name": entry.sender,
                "sent_from": entry.sender,
                "received_at": entry.received_at.isoformat(),
                "message": entry.body_snippet or entry.subject,
            }
            for key, value in values.items():
                if value and value not in samples[key]:
                    samples[key].append(value)

    conditions = [
        ContributionEvent.organization_id == organization_id,
        ContributionEvent.decision == ContributionDecision.ACCEPTED,
    ]
    if connection_id:
        conditions.append(ContributionEvent.mailbox_connection_id == connection_id)
    result = await db.execute(
        select(ContributionEvent)
        .where(*conditions)
        .order_by(ContributionEvent.received_at.desc())
        .limit(10)
    )
    events = list(result.scalars().all())
    if connection is not None:
        await _hydrate_missing_export_details(db, connection, events)
    for event in events:
        for key in FIELD_LABELS:
            value = _event_export_value(event, key)
            if value and value not in samples[key]:
                samples[key].append(value)

    return [
        ContributionExportFieldOut(key=key, label=label, sample_values=samples[key][:3])
        for key, label in FIELD_LABELS.items()
    ]


@org_reports_router.get(".csv")
async def export_contributions_csv(
    organization_id: str,
    connection_id: str = Query(...),
    from_datetime: datetime = Query(...),
    to_datetime: datetime = Query(...),
    keywords: str | None = None,
    template_id: str | None = None,
    membership: Membership = Depends(get_membership),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    require_roles(membership, Role.OWNER, Role.FINANCE)
    if from_datetime > to_datetime:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="From date must be before to date.")
    connection = await db.get(MailboxConnection, connection_id)
    if connection is None or connection.organization_id != organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found.")
    if connection.status.value != "connected":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Select a connected mailbox.")

    field_keys = DEFAULT_EXPORT_FIELDS
    field_labels = {key: FIELD_LABELS[key] for key in field_keys}
    if template_id:
        template = await db.get(ContributionExportTemplate, template_id)
        if template is None or template.organization_id != organization_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Export template not found.")
        field_keys = template.field_keys
        field_labels = template.field_labels

    keyword_terms = _keyword_list(keywords)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([field_labels.get(key, FIELD_LABELS.get(key, key)) for key in field_keys])
    if connection.provider == MailboxProviderName.IMAP:
        rows = [
            row
            for row in await _email_export_rows(db, connection, from_datetime, to_datetime)
            if _row_matches_keywords(row, keyword_terms)
        ]
        for row in rows:
            writer.writerow([_row_export_value(row, key) for key in field_keys])
    else:
        result = await db.execute(
            select(ContributionEvent)
            .where(
                ContributionEvent.organization_id == organization_id,
                ContributionEvent.mailbox_connection_id == connection_id,
                ContributionEvent.decision == ContributionDecision.ACCEPTED,
                ContributionEvent.test_mode.is_(False),
                ContributionEvent.received_at >= from_datetime,
                ContributionEvent.received_at <= to_datetime,
            )
            .order_by(ContributionEvent.received_at)
        )
        all_events = list(result.scalars().all())
        await _hydrate_missing_export_details(db, connection, all_events)
        events = [event for event in all_events if _matches_keywords(event, keyword_terms)]
        for event in events:
            writer.writerow([_event_export_value(event, key) for key in field_keys])
    buffer.seek(0)

    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="contribution-export.csv"'},
    )
