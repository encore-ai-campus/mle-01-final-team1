
from typing import Any
from uuid import uuid4


def build_income_changes(
    extracted_info: list[dict[str, Any]],
    *,
    change_requested: bool,
    is_temporary: bool,
) -> list[dict[str, Any]]:
    """
    Create profile change proposals only when explicitly requested.

    Never write to the database.
    Never convert unsupported income fields.
    Never save hypothetical information.
    """

    if not change_requested or is_temporary:
        return []

    changes = []
    seen = {}

    for fact in extracted_info:
        target_field = fact.get("target_field")

        # Income without a supported DB field cannot be saved.
        if target_field not in {
            "financial_profile.monthly_net_income_krw",
            "financial_profile.annual_gross_income_krw",
        }:
            continue

        amount = fact.get("amount_krw")

        if type(amount) is not int or amount < 0:
            raise ValueError("Invalid income amount")

        # Reject contradictory values for the same field.
        if target_field in seen:
            if seen[target_field] != amount:
                raise ValueError("Conflicting income values")
            continue

        seen[target_field] = amount

        changes.append({
            "change_id": f"change_{uuid4().hex}",
            "domain": "financial",
            "field": target_field.split(".", 1)[1],
            "value": amount,
        })

    return changes
