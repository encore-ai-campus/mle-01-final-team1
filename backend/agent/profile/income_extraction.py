
import re
from typing import Any


INCOME_PATTERN = re.compile(
    r"(?P<basis>세전|세후)?\s*"
    r"(?P<label>월\s*실수령액|월급|월\s*소득|연봉)"
    r"\s*(?:은|는|이|을|도|:)?\s*"
    r"(?P<amount>\d[\d,]*)\s*"
    r"(?P<unit>만\s*원|원)"
)


def extract_income_info(text: str) -> list[dict[str, Any]]:
    """Extract income facts without creating profile changes."""

    extracted = []

    for match in INCOME_PATTERN.finditer(text):
        label = re.sub(r"\s+", "", match.group("label"))
        basis = match.group("basis") or "unknown"

        amount = int(match.group("amount").replace(",", ""))

        if "만" in match.group("unit"):
            amount *= 10000

        period = "annual" if label == "연봉" else "monthly"

        if label == "월실수령액":
            basis = "net"

        elif basis == "세후":
            basis = "net"

        elif basis == "세전":
            basis = "gross"

        target_field = None

        if period == "monthly" and basis == "net":
            target_field = "financial_profile.monthly_net_income_krw"

        elif period == "annual" and basis == "gross":
            target_field = "financial_profile.annual_gross_income_krw"

        extracted.append({
            "type": "income",
            "period": period,
            "amount_krw": amount,
            "tax_basis": basis,
            "target_field": target_field,
            "raw_text": match.group(0),
        })

    return extracted
