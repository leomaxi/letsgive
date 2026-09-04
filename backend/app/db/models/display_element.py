from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.models.display_template import ElementType

if TYPE_CHECKING:
    from app.db.models.display_template import DisplayTemplate


class DisplayElement(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One positioned object on a DisplayTemplate's canvas (spec 6.1).

    `binding` carries per-type configuration a frontend renderer needs:
    static elements (heading/body_text/sponsor_message) put their text
    there; data-bound elements (contribution_count/amount/goal/countdown)
    are populated at render time from live session state, never from this
    row, so the binding just says how to format it (e.g. locale/decimals).
    """

    __tablename__ = "display_elements"

    template_id: Mapped[str] = mapped_column(
        ForeignKey("display_templates.id"), nullable=False, index=True
    )
    type: Mapped[ElementType] = mapped_column(
        SAEnum(
            ElementType,
            name="display_element_type",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )

    x: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    y: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    width: Mapped[float] = mapped_column(Float, nullable=False, default=200)
    height: Mapped[float] = mapped_column(Float, nullable=False, default=100)
    z_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    style: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    binding: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    is_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    template: Mapped["DisplayTemplate"] = relationship(back_populates="elements")
