
from uuid import UUID

from fastapi import Depends

from api.auth import get_current_user
from agent.memory.postgres_memory_service import PostgresMemoryService
from agent.supervisor.tools.integration import bind_mvp_ports


def build_memory_ports(authenticated_user_id: UUID):
    """
    Build PostgreSQL Memory ports for one authenticated user.

    The user ID must come from verified authentication,
    not from an LLM response or user-provided request body.
    """
    if not isinstance(authenticated_user_id, UUID):
        raise TypeError("authenticated_user_id must be a UUID")

    memory_service = PostgresMemoryService()

    return bind_mvp_ports(
        memory_service=memory_service,
        authenticated_user_id=str(authenticated_user_id),
    )


def get_authenticated_memory_ports(
    current_user: dict = Depends(get_current_user),
):
    """Create Memory ports using a verified FastAPI user."""
    return build_memory_ports(
        authenticated_user_id=current_user["user_id"]
    )
