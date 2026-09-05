import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


def new_uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime) -> datetime:
    """SQLite (used for local dev/tests) drops tzinfo on round-trip through
    DateTime(timezone=True), unlike Postgres. Normalize before comparing
    against an aware 'now' so behavior doesn't depend on which DB is active.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class UTCDateTime(TypeDecorator):
    """DateTime(timezone=True) that's actually reliably timezone-aware on
    every dialect, not just Postgres.

    Without this, SQLite silently hands back naive datetimes, which then
    serialize to JSON with no UTC offset (e.g. "2026-09-02T21:11:29" instead
    of "...+00:00"). A browser's `new Date(...)` interprets an offset-less
    string as *local* time, not UTC -- so every timestamp in every API
    response would be wrong by the server's local UTC offset, silently and
    only in dev. Normalizing here, once, at the type level fixes it for every
    column that uses this type instead of needing `ensure_utc()` sprinkled
    into every endpoint that happens to serialize a datetime.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        return ensure_utc(value)


class Base(DeclarativeBase):
    pass


class UUIDPrimaryKeyMixin:
    # Explicit length (matches every Alembic migration's sa.String(36)):
    # MySQL rejects an unlengthed VARCHAR outright at CREATE TABLE time
    # (Postgres and SQLite both tolerate it), so this also has to be a real
    # column type here, not just in the migration files, for
    # Base.metadata.create_all() (used by the test suite) to work on MySQL.
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )
