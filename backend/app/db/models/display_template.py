import enum
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.db.models.display_element import DisplayElement

DEFAULT_CANVAS = {"width": 1920, "height": 1080, "background_color": "#0B1220"}


class DisplayTemplate(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A reusable 16:9 projection layout an org's media team builds in the
    Display Studio (spec 6.1). Elements are stored as a separate table
    (DisplayElement) so the canvas can be replaced wholesale on autosave
    without losing per-element identity that a real drag/drop frontend
    would want (undo/redo, selection, etc).
    """

    __tablename__ = "display_templates"

    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id"), nullable=False, index=True
    )
    created_by_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )

    name: Mapped[str] = mapped_column(String(150), nullable=False)
    canvas: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=lambda: dict(DEFAULT_CANVAS)
    )
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    elements: Mapped[list["DisplayElement"]] = relationship(
        back_populates="template",
        cascade="all, delete-orphan",
        order_by="DisplayElement.z_index",
    )


class ElementType(str, enum.Enum):
    LOGO = "logo"
    HEADING = "heading"
    BODY_TEXT = "body_text"
    CONTRIBUTION_COUNT = "contribution_count"
    AMOUNT = "amount"
    GOAL = "goal"
    PROGRESS_BAR = "progress_bar"
    COUNTDOWN = "countdown"
    PAYMENT_INSTRUCTIONS = "payment_instructions"
    QR_CODE = "qr_code"
    BACKGROUND = "background"
    SPONSOR_MESSAGE = "sponsor_message"
