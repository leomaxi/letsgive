from typing import Any

from sqlalchemy import JSON, Boolean, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

DEFAULT_CREDIT_KEYWORDS = ["deposited", "credited", "received", "sent you"]
DEFAULT_REJECT_KEYWORDS = [
    "request",
    "requested",
    "cancel",
    "cancelled",
    "reminder",
    "declined",
    "failed",
]

# Named groups 'amount' and 'currency'; profiles can override per bank/provider template.
DEFAULT_AMOUNT_PATTERN = (
    r"(?P<currency>CAD|USD|EUR|GBP)?\s*\$?(?P<amount>\d{1,3}(?:,\d{3})*(?:\.\d{2})?)"
)


class ParserProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Tenant-configured rules for recognizing a bank/provider's deposit
    notification emails (spec 5.3, 9.2 'ingestion'). Versioned via
    template_version so parser drift is traceable per spec 16 ("Bank email
    formats change").
    """

    __tablename__ = "parser_profiles"

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    template_version: Mapped[str] = mapped_column(String(50), nullable=False, default="v1")

    sender_patterns: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    credit_keywords: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=lambda: list(DEFAULT_CREDIT_KEYWORDS)
    )
    reject_keywords: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=lambda: list(DEFAULT_REJECT_KEYWORDS)
    )
    amount_pattern: Mapped[str] = mapped_column(
        String(500), nullable=False, default=DEFAULT_AMOUNT_PATTERN
    )
    default_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CAD")
    confidence_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.75)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSON)
