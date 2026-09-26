from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ExportUsage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "export_usage"
    __table_args__ = (UniqueConstraint("organization_id", "month", name="uq_export_usage_org_month"),)

    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id"), nullable=False, index=True
    )
    month: Mapped[str] = mapped_column(String(7), nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
