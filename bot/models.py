import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class UserSettings(Base):
    __tablename__ = "user_settings"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Moscow")
    quiet_hours_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    quiet_start_hour: Mapped[int] = mapped_column(Integer, default=23)
    quiet_end_hour: Mapped[int] = mapped_column(Integer, default=7)


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    text: Mapped[str] = mapped_column(Text)
    fire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    spam_interval_seconds: Mapped[int] = mapped_column(Integer, default=0)
    spam_until_read: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
