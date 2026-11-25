from datetime import date, datetime
from uuid import UUID

from sqlalchemy import Date, DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from services.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )

    email: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    nickname: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    password_hash: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    birth_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
    )

    gender: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )