from typing import Any

from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ContributionExportTemplate(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "contribution_export_templates"

    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    field_keys: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    field_labels: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    sample: Mapped[dict[str, Any] | None] = mapped_column(JSON)
