#!/usr/bin/env python3
"""Normalize the project's raw JSON files using schema/field_mapping.csv."""

import argparse
import csv
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


DATASETS = {
    "youth_policy": "data/청년정책.json",
    "loan": "data/전체자금 대출.json",
    "savings": "data/적금.json",
    "deposit": "data/정기예금.json",
}


def product_type_for_dataset(dataset: str) -> str:
    """Return the current service-level product category for one source dataset."""
    if dataset not in {"savings", "deposit", "loan"}:
        raise ValueError(f"Unsupported financial dataset: {dataset}")
    return dataset


def _normalize_path(path: str) -> str:
    return path[:-2] if path.endswith("[]") else path


def load_mapping(path: str | Path) -> dict[tuple[str, str, str], tuple[str, str]]:
    mapping = {}
    with Path(path).open(encoding="utf-8", newline="") as file:
        for row in csv.DictReader(file):
            key = (
                row["dataset"],
                _normalize_path(row["source_path"]),
                row["source_field"],
            )
            mapping[key] = (row["standard_field"], row["data_type"])
    return mapping


def load_missing_policy(path: str | Path) -> dict[tuple[str, str], tuple[str, Any]]:
    policy = {}
    with Path(path).open(encoding="utf-8", newline="") as file:
        for row in csv.DictReader(file):
            default_value = None if row["default_value"] == "null" else row["default_value"]
            policy[(row["dataset"], row["standard_field"])] = (
                row["missing_status"],
                default_value,
            )
    return policy


def _clean_string(value: str) -> str | None:
    value = value.replace("\u3000", " ").strip()
    return value or None


def _split_values(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, list):
        return value
    parts = re.split(r"[,\n·]+", str(value))
    cleaned = [_clean_string(part) for part in parts]
    return [part for part in cleaned if part is not None] or None


def convert_value(value: Any, data_type: str) -> Any:
    """Clean and convert one value according to the mapping data type."""
    if value is None:
        return None
    if data_type == "string_or_array":
        return _split_values(value)
    if data_type in {"string", "category", "boolean_like_string"}:
        return _clean_string(str(value)) if isinstance(value, str) else value
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if not value:
            return None
    if data_type in {"integer"}:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None
    if data_type in {"numeric", "numeric_or_null"}:
        try:
            number = float(value)
            return int(number) if number.is_integer() else number
        except (TypeError, ValueError):
            return None
    if data_type in {"date", "date_or_null"}:
        text = str(value)
        for fmt in ("%Y%m%d", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt).date().isoformat()
            except ValueError:
                continue
        return None
    if data_type == "datetime":
        text = str(value)
        for fmt in (
            "%Y%m%d%H%M",
            "%Y%m%d%H%M%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
        ):
            try:
                return datetime.strptime(text, fmt).isoformat()
            except ValueError:
                continue
        return None
    return value


def _iso_date_token(token: str) -> str | None:
    digits = re.sub(r"[^0-9]", "", token)
    if len(digits) != 8:
        return None
    try:
        return datetime.strptime(digits, "%Y%m%d").date().isoformat()
    except ValueError:
        return None


def parse_application_period(value: Any) -> dict[str, str | None]:
    """Parse a policy application period without guessing an end date."""
    result = {"application_start_date": None, "application_end_date": None}
    if value is None or not str(value).strip():
        return result
    tokens = re.findall(r"\d{4}[./-]?\d{2}[./-]?\d{2}", str(value))
    dates = [_iso_date_token(token) for token in tokens]
    dates = [date for date in dates if date is not None]
    if dates:
        result["application_start_date"] = dates[0]
    if len(dates) > 1:
        result["application_end_date"] = dates[1]
    return result


def normalize_income_condition(
    code: Any,
    note: Any,
    min_income: Any = None,
    max_income: Any = None,
) -> dict[str, Any]:
    """Keep income code/text and numeric bounds without guessing their semantics."""
    return {
        "code": _clean_string(str(code)) if code is not None else None,
        "note": _clean_string(str(note)) if note is not None else None,
        "min_income_krw": min_income,
        "max_income_krw": max_income,
        "unit": None,
        "basis": None,
    }


def build_rate_basis(dataset: str) -> dict[str, str]:
    if dataset == "loan":
        return {
            "min_rate": "advertised_range",
            "average_rate": "average_new_loans",
            "max_rate": "advertised_range",
        }
    return {
        "base_rate": "advertised_base_rate",
        "max_rate": "advertised_max_rate",
    }


def parse_min_subscription_amount(note: Any) -> int | None:
    """Extract an explicitly stated minimum subscription amount from a note."""
    if note is None:
        return None
    match = re.search(
        r"최소\s*가입\s*금액\s*[:：]?\s*([0-9][0-9,]*)\s*(만원|만|원)",
        str(note),
    )
    if not match:
        return None
    amount = int(match.group(1).replace(",", ""))
    if match.group(2) in {"만원", "만"}:
        amount *= 10000
    return amount


def normalize_record(
    record: dict[str, Any],
    mapping: dict[tuple[str, str, str], tuple[str, str]],
    dataset: str,
    source_path: str,
    missing_policy: dict[tuple[str, str], tuple[str, Any]] | dict[tuple[str, str, str], tuple[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Rename and convert one record, returning conversion issues separately."""
    normalized = {}
    issues = []
    path = _normalize_path(source_path)
    for source_field, value in record.items():
        map_key = (dataset, path, source_field)
        if map_key not in mapping:
            continue
        standard_field, data_type = mapping[map_key]
        converted = convert_value(value, data_type)
        missing_value = value is None or (
            isinstance(value, str) and not value.replace("\u3000", " ").strip()
        )
        if not missing_value and converted is None:
            issues.append(
                {
                    "dataset": dataset,
                    "path": source_path,
                    "field": source_field,
                    "value": value,
                    "issue": "conversion_failed",
                }
            )
            normalized[f"{standard_field}_raw"] = value
        normalized[standard_field] = converted

    if missing_policy:
        for policy_key, (missing_status, default_value) in missing_policy.items():
            if len(policy_key) == 2:
                policy_dataset, standard_field = policy_key
                if policy_dataset != dataset:
                    continue
            else:
                policy_dataset, policy_path, standard_field = policy_key
                if policy_dataset != dataset or policy_path != path:
                    continue
            if standard_field not in normalized:
                normalized[standard_field] = default_value
            normalized[f"{standard_field}_status"] = (
                "present" if normalized.get(standard_field) is not None else missing_status
            )
    return normalized, issues


def _duplicate_count(records: list[dict[str, Any]], id_field: str) -> int:
    values = [record.get(id_field) for record in records]
    counts = Counter(value for value in values if value not in (None, ""))
    return sum(count - 1 for count in counts.values() if count > 1)


def validate_rates(
    rates: list[dict[str, Any]], products_by_id: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Validate loan rate ranges and return summary plus product-level details."""
    violations = []
    missing_average_count = 0
    for rate in rates:
        minimum = rate.get("min_rate")
        maximum = rate.get("max_rate")
        average = rate.get("average_rate")
        if average is None:
            missing_average_count += 1

        violation_types = []
        if isinstance(minimum, (int, float)) and isinstance(maximum, (int, float)):
            if minimum > maximum:
                violation_types.append("min_above_max")
            if isinstance(average, (int, float)):
                if average < minimum:
                    violation_types.append("average_below_min")
                elif average > maximum:
                    violation_types.append("average_above_max")

        if violation_types:
            product = products_by_id.get(rate.get("product_id"), {})
            violations.append(
                {
                    "product_id": rate.get("product_id"),
                    "institution_name": product.get("institution_name"),
                    "product_name": product.get("product_name"),
                    "rate_type": rate.get("rate_type"),
                    "repayment_type": rate.get("repayment_type"),
                    "min_rate": minimum,
                    "average_rate": average,
                    "max_rate": maximum,
                    "violation_types": violation_types,
                }
            )

    return {
        "violation_count": len(violations),
        "missing_average_count": missing_average_count,
        "violations": violations,
    }


def process_policy(
    payload: dict[str, Any],
    mapping: dict[tuple[str, str, str], tuple[str, str]],
    missing_policy: dict[tuple[str, str, str], tuple[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records = []
    issues = []
    for source_record in payload.get("policies", []):
        record, record_issues = normalize_record(
            source_record, mapping, "youth_policy", "policies", missing_policy
        )
        if record.get("max_age") == 999:
            record["max_age"] = None
            record["age_unlimited"] = True
            record["max_age_status"] = "unknown"
        record.update(parse_application_period(record.get("application_period")))
        record["income_condition_normalized"] = normalize_income_condition(
            record.get("income_condition_code"),
            record.get("income_condition_note"),
            record.get("min_income"),
            record.get("max_income"),
        )
        records.append(record)
        issues.extend(record_issues)
    return records, {
        "record_count": len(records),
        "duplicate_id_count": _duplicate_count(records, "policy_id"),
        "conversion_issue_count": len(issues),
        "conversion_issues": issues,
    }


def process_financial(
    payload: dict[str, Any],
    dataset: str,
    missing_policy: dict[tuple[str, str, str], tuple[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    products = []
    rates = []
    issues = []
    mapping = process_financial.mapping
    for source_product in payload.get("products", []):
        wrapper, wrapper_issues = normalize_record(
            source_product, mapping, dataset, "products", missing_policy
        )
        base, base_issues = normalize_record(
            source_product.get("base", {}), mapping, dataset, "products[].base", missing_policy
        )
        product = {**wrapper, **base}
        product["product_type"] = product_type_for_dataset(dataset)
        if dataset in {"savings", "deposit"}:
            product["min_subscription_amount_krw"] = parse_min_subscription_amount(
                product.get("additional_note")
            )
        products.append(product)
        issues.extend(wrapper_issues + base_issues)

        for source_option in source_product.get("rate_options", []):
            rate, rate_issues = normalize_record(
                source_option.get("data", {}),
                mapping,
                dataset,
                "products[].rate_options[].data",
                missing_policy,
            )
            if product.get("product_id") is not None:
                rate["product_id"] = product["product_id"]
            rate["rate_basis"] = build_rate_basis(dataset)
            rates.append(rate)
            issues.extend(rate_issues)

    rate_validation = validate_rates(
        rates, {product.get("product_id"): product for product in products}
    )

    quality = {
        "product_count": len(products),
        "rate_option_count": len(rates),
        "duplicate_product_id_count": _duplicate_count(products, "product_id"),
        "conversion_issue_count": len(issues),
        "rate_range_violation_count": rate_validation["violation_count"],
        "average_rate_missing_count": rate_validation["missing_average_count"],
        "rate_range_violations": rate_validation["violations"],
        "conversion_issues": issues,
    }
    return products, rates, quality


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def run(project_root: str | Path, output_dir: str | Path) -> dict[str, Any]:
    root = Path(project_root)
    output = root / output_dir if not Path(output_dir).is_absolute() else Path(output_dir)
    mapping = load_mapping(root / "schema/field_mapping.csv")
    missing_policy = load_missing_policy(root / "schema/missing_value_policy.csv")
    quality_report = {"datasets": {}}

    for dataset, relative_path in DATASETS.items():
        payload = json.loads((root / relative_path).read_text(encoding="utf-8"))
        if dataset == "youth_policy":
            records, quality = process_policy(payload, mapping, missing_policy)
            _write_json(output / "youth_policy.json", {"records": records})
        else:
            process_financial.mapping = mapping
            products, rates, quality = process_financial(payload, dataset, missing_policy)
            _write_json(output / f"{dataset}_products.json", {"records": products})
            _write_json(output / f"{dataset}_rates.json", {"records": rates})
        quality_report["datasets"][dataset] = quality

    _write_json(output / "data_quality_report.json", quality_report)
    return quality_report


def main() -> None:
    parser = argparse.ArgumentParser(description="원천 JSON 전처리기")
    parser.add_argument(
        "--project-root",
        default=Path(__file__).resolve().parents[1],
        type=Path,
    )
    parser.add_argument("--output-dir", default="data/processed", type=Path)
    args = parser.parse_args()
    report = run(args.project_root, args.output_dir)
    for dataset, quality in report["datasets"].items():
        print(
            f"{dataset}: products/policies={quality.get('product_count', quality.get('record_count'))}, "
            f"rates={quality.get('rate_option_count', '-')}, "
            f"issues={quality['conversion_issue_count']}"
        )
    print(f"저장 위치: {(args.project_root / args.output_dir).resolve()}")


if __name__ == "__main__":
    main()
