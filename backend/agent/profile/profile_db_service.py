from uuid import UUID

from sqlalchemy import MetaData, Table, select

from services.database import SessionLocal, engine
from agent.supervisor.tools.profile_fields import (
    validate_read_fields,
    validate_profile,
)


# Existing PostgreSQL tables
metadata = MetaData()

basic_profiles = Table(
    "basic_profiles",
    metadata,
    autoload_with=engine,
)

financial_profiles = Table(
    "financial_profiles",
    metadata,
    autoload_with=engine,
)

TABLES = {
    "basic_profile": basic_profiles,
    "financial_profile": financial_profiles,
}


class ProfileDBService:
    """Read profile fields for one authenticated user."""

    def __init__(self, authenticated_user_id: UUID):
        if not isinstance(authenticated_user_id, UUID):
            raise ValueError("A verified user UUID is required.")

        # User identity cannot be changed by a tool request.
        self.user_id = authenticated_user_id

    def get_profile_fields(self, fields: list[str]) -> dict:
        """Return only requested fields without modifying the DB."""

        paths = validate_read_fields(fields)

        requested = {
            "basic_profile": [],
            "financial_profile": [],
        }

        for path in paths:
            domain, field = path.split(".", 1)
            requested[domain].append(field)

        result = {}

        with SessionLocal() as db:
            for domain, field_names in requested.items():
                if not field_names:
                    continue

                table = TABLES[domain]

                stmt = (
                    select(*(table.c[name] for name in field_names))
                    .where(table.c.user_id == self.user_id)
                )

                row = db.execute(stmt).mappings().one_or_none()

                if row is None:
                    # Profile row has not been registered.
                    continue

                values = dict(row)

                # Validate DB values against Supervisor's schema.
                validate_profile(domain, values)

                result[domain] = values

        return result