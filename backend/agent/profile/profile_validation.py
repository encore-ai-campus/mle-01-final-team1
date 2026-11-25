from typing import Any

from agent.supervisor.tools.profile_fields import (
    validate_profile,
    validate_read_fields,
)


def inspect_profile_fields(
    required_fields: list[str],
    profile: dict[str, Any],
) -> dict[str, Any]:
    """Check requested profile fields without modifying data."""

    paths = validate_read_fields(required_fields)

    field_status = {}
    missing_info = []

    for path in paths:
        domain, field = path.split(".", 1)
        domain_data = profile.get(domain, {})

        if not isinstance(domain_data, dict):
            field_status[path] = "invalid"
            missing_info.append(path)
            continue

        if field not in domain_data:
            status = "not_loaded"

        elif domain_data[field] is None:
            status = "unknown"

        else:
            try:
                validate_profile(
                    domain,
                    {field: domain_data[field]},
                )
                status = "available"

            except (ValueError, TypeError):
                status = "invalid"

            except Exception as exc:
                from agent.supervisor.contracts import (
                    SupervisorContractError,
                )
                if isinstance(exc, SupervisorContractError):
                    status = "invalid"
                else:
                    raise

        field_status[path] = status

        if status != "available":
            missing_info.append(path)

    return {
        "field_status": field_status,
        "missing_info": missing_info,
    }