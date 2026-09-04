from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.api.v1.schemas import SessionReportOut


def _table(rows: list[list[str]], header: bool = False) -> Table:
    table = Table(rows, hAlign="LEFT")
    style = [
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    if header:
        style += [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0b1220")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ]
    table.setStyle(TableStyle(style))
    return table


def render_session_report_pdf(report: SessionReportOut, organization_name: str) -> bytes:
    """Renders the same data as GET .../reports/sessions/{id} (and the CSV
    export) as a PDF -- spec 7 calls for both; the CSV covers the raw
    per-event ledger for accounting/integration, this covers the
    human-readable summary a Finance officer might print or attach to board
    minutes. Built with reportlab's platypus layer (flowables + tables)
    rather than hand-placed canvas coordinates, so it stays readable and
    paginates itself if a session ever has enough corrections/approvals to
    need it.
    """
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        title=f"Session report {report.session_id}",
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
    )
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph("Let's Give - Session Report", styles["Title"]))
    story.append(Paragraph(organization_name, styles["Heading2"]))
    story.append(Spacer(1, 0.15 * inch))

    story.append(
        _table(
            [
                ["Session ID", report.session_id],
                ["Status", report.status.value.replace("_", " ").title()],
            ]
        )
    )
    story.append(Spacer(1, 0.25 * inch))

    story.append(Paragraph("Validated contributions", styles["Heading2"]))
    amount = str(report.validated_amount) if report.validated_amount is not None else "-"
    story.append(_table([["Count", str(report.validated_count)], ["Amount", amount]]))
    story.append(Spacer(1, 0.25 * inch))

    story.append(Paragraph("Excluded messages", styles["Heading2"]))
    if report.excluded_counts:
        rows = [["Reason", "Count"]] + [
            [reason.replace("_", " ").title(), str(count)]
            for reason, count in report.excluded_counts.items()
        ]
        story.append(_table(rows, header=True))
    else:
        story.append(Paragraph("None.", styles["BodyText"]))
    story.append(Spacer(1, 0.25 * inch))

    story.append(Paragraph("Corrections", styles["Heading2"]))
    if report.corrections:
        rows = [["Reason", "Resolution", "Corrected amount", "Note"]] + [
            [
                c.reason,
                c.resolution.value.replace("_", " ").title() if c.resolution else "-",
                str(c.corrected_amount) if c.corrected_amount is not None else "-",
                c.resolution_note or "-",
            ]
            for c in report.corrections
        ]
        story.append(_table(rows, header=True))
    else:
        story.append(Paragraph("None.", styles["BodyText"]))
    story.append(Spacer(1, 0.25 * inch))

    story.append(Paragraph("Approval history", styles["Heading2"]))
    if report.approval_history:
        rows = [["Requested", "Verified", "Locked", "Attempts"]] + [
            [
                str(a["created_at"]),
                str(a["verified_at"]) if a["verified_at"] else "Not verified",
                "Yes" if a["locked"] else "No",
                str(a["attempts"]),
            ]
            for a in report.approval_history
        ]
        story.append(_table(rows, header=True))
    else:
        story.append(Paragraph("None.", styles["BodyText"]))
    story.append(Spacer(1, 0.25 * inch))

    story.append(Paragraph("Mailbox connection", styles["Heading2"]))
    if report.connection_health:
        ch = report.connection_health
        story.append(
            _table(
                [
                    ["Status", str(ch["status"])],
                    ["Webhook health", str(ch.get("webhook_health") or "-")],
                    ["Last synced", str(ch.get("last_sync_at") or "-")],
                ]
            )
        )
    else:
        story.append(Paragraph("No mailbox connection was bound to this session.", styles["BodyText"]))

    doc.build(story)
    return buffer.getvalue()
