from uuid import UUID

from agent.profile.profile_db_service import ProfileDBService
from agent.supervisor.tools.integration import bind_mvp_ports


def build_profile_ports(authenticated_user_id: UUID):
    """
    Build Supervisor ports for one authenticated user.

    The user ID must come from verified authentication,
    not from an LLM response or user-provided request body.
    """
    profile_service = ProfileDBService(
        authenticated_user_id=authenticated_user_id
    )

    return bind_mvp_ports(
        get_profile_fields=profile_service.get_profile_fields
    )

from fastapi import Depends

from api.auth import get_current_user


def get_authenticated_profile_ports(
    current_user: dict = Depends(get_current_user),
):
    """Create profile ports using a verified FastAPI user."""

    return build_profile_ports(
        authenticated_user_id=current_user["user_id"]
    )