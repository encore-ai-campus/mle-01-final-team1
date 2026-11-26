"""전체 금융 chunk/evidence 매니페스트의 재현성과 출처를 검증한다."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from services.finance_chunk_builder import _rate_option_id, build_finance_chunks  # noqa: E402


INPUT_FILES = {
    "deposit_products": "deposit_products.json",
    "deposit_rates": "deposit_rates.json",
    "savings_products": "savings_products.json",
    "savings_rates": "savings_rates.json",
    "loan_products": "loan_products.json",
    "loan_rates": "loan_rates.json",
    "youth_policy": "youth_policy.json",
}
ID_FIELDS = {
    "deposit_products": "product_id",
    "deposit_rates": "product_id",
    "savings_products": "product_id",
    "savings_rates": "product_id",
    "loan_products": "product_id",
    "loan_rates": "product_id",
    "youth_policy": "policy_id",
}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _input_report(processed_dir: Path) -> dict[str, Any]:
    counts: dict[str, int] = {}
    duplicate_ids: dict[str, int] = {}
    missing_files = []
    for name, filename in INPUT_FILES.items():
        path = processed_dir / filename
        if not path.exists():
            missing_files.append(filename)
            continue
        records = json.loads(path.read_text(encoding="utf-8")).get("records", [])
        counts[name] = len(records)
        field = ID_FIELDS[name]
        if name.endswith("_rates"):
            dataset = name.removesuffix("_rates")
            ids = [
                option_id
                for row in records
                if (option_id := _rate_option_id(dataset, row)) is not None
            ]
        else:
            ids = [str(row.get(field)) for row in records if row.get(field) not in (None, "")]
        duplicate_ids[name] = len(ids) - len(set(ids))
    return {
        "record_counts": counts,
        "duplicate_id_counts": duplicate_ids,
        "missing_files": missing_files,
    }


def _source_traceability(chunks: list[dict[str, Any]], processed_dir: Path) -> dict[str, Any]:
    record_counts = {
        filename: len(json.loads((processed_dir / filename).read_text(encoding="utf-8")).get("records", []))
        for filename in INPUT_FILES.values()
        if (processed_dir / filename).exists()
    }
    invalid_rows = []
    for row in chunks:
        source_file = row.get("source_file")
        source_path = row.get("source_path") or ""
        match = re.search(r"records\[(\d+)\]", source_path)
        index = int(match.group(1)) if match else None
        valid = (
            source_file in record_counts
            and index is not None
            and index < record_counts[source_file]
        )
        if not valid:
            invalid_rows.append(
                {
                    "chunk_id": row.get("chunk_id"),
                    "source_file": source_file,
                    "source_path": source_path,
                }
            )
    return {
        "traceable_count": len(chunks) - len(invalid_rows),
        "untraceable_count": len(invalid_rows),
        "traceability_rate": (len(chunks) - len(invalid_rows)) / len(chunks) if chunks else 0,
        "examples": invalid_rows[:20],
    }


def validate(
    processed_dir: Path,
    manifest_dir: Path,
    raw_dir: Path | None,
    version: str,
) -> dict[str, Any]:
    chunks = _read_jsonl(manifest_dir / "chunks.jsonl")
    evidence = _read_jsonl(manifest_dir / "evidence.jsonl")
    input_report = _input_report(processed_dir)
    source_report = _source_traceability(chunks, processed_dir)
    build_report = json.loads((manifest_dir / "chunk_build_report.json").read_text(encoding="utf-8"))

    rate_option_ids = [
        row["metadata"].get("rate_option_id")
        for row in chunks
        if row.get("chunk_type") == "rate_option"
    ]
    policy_eligibility = [
        row for row in chunks if row.get("chunk_type") == "policy_eligibility"
    ]
    content_keys = [(row.get("dataset"), row.get("chunk_type"), row.get("text", "").strip()) for row in chunks]
    content_groups = {}
    for row, key in zip(chunks, content_keys):
        content_groups.setdefault(key, []).append(row)
    duplicate_content_groups = [
        {
            "dataset": key[0],
            "chunk_type": key[1],
            "count": len(rows),
            "entity_ids": sorted({entity_id for row in rows for entity_id in row.get("entity_ids", [])}),
            "chunk_ids": [row.get("chunk_id") for row in rows[:10]],
            "text": key[2],
        }
        for key, rows in content_groups.items()
        if len(rows) > 1
    ]
    duplicate_content_groups.sort(key=lambda item: item["count"], reverse=True)
    relation_statuses = Counter(
        (row.get("relation_verification") or {}).get("status") for row in chunks
    )

    expected_evidence, expected_chunks = build_finance_chunks(processed_dir, raw_dir=raw_dir)
    stable_ids = {
        "evidence_ids": [row["evidence_id"] for row in expected_evidence]
        == [row["evidence_id"] for row in evidence],
        "chunk_ids": [row["chunk_id"] for row in expected_chunks]
        == [row["chunk_id"] for row in chunks],
    }

    quality_path = processed_dir / "data_quality_report.json"
    quality = json.loads(quality_path.read_text(encoding="utf-8")) if quality_path.exists() else {}
    known_warnings = {
        dataset: {
            key: value
            for key, value in report.items()
            if key.endswith("_count")
            and any(token in key for token in ("duplicate", "conversion", "violation", "missing"))
            and value not in (0, None)
        }
        for dataset, report in quality.get("datasets", {}).items()
    }

    return {
        "manifest_version": version,
        "input": input_report,
        "output": {
            "evidence_count": len(evidence),
            "chunk_count": len(chunks),
            "dataset_counts": dict(Counter(row.get("dataset") for row in chunks)),
            "chunk_type_counts": dict(Counter(row.get("chunk_type") for row in chunks)),
        },
        "duplicates": {
            "evidence_id_count": len(evidence) - len({row["evidence_id"] for row in evidence}),
            "chunk_id_count": len(chunks) - len({row["chunk_id"] for row in chunks}),
            "content_count": len(chunks) - len(set(content_keys)),
            "rate_option_id_count": len(rate_option_ids) - len(set(rate_option_ids)),
            "content_group_count": len(duplicate_content_groups),
            "cross_entity_content_group_count": sum(
                len(item["entity_ids"]) > 1 for item in duplicate_content_groups
            ),
            "content_group_examples": duplicate_content_groups[:20],
        },
        "source": {
            **source_report,
            "source_link_status_counts": dict(
                Counter((row.get("source_link") or {}).get("status") for row in chunks)
            ),
        },
        "missing_value_handling": {
            "policy_income_unresolved_count": sum(
                row.get("metadata", {}).get("income_limit_interpretation") == "unresolved"
                for row in policy_eligibility
            ),
            "relation_verification_status_counts": dict(relation_statuses),
        },
        "id_stability": stable_ids,
        "id_stability_pass": all(stable_ids.values()),
        "known_preprocessing_warnings": known_warnings,
        "status": "pass"
        if not input_report["missing_files"]
        and all(value == 0 for value in input_report["duplicate_id_counts"].values())
        and all(value == 0 for value in (source_report["untraceable_count"], build_report["duplicate_chunk_ids"], build_report["duplicate_evidence_ids"]))
        and len(chunks) - len(set(content_keys)) == 0
        and all(stable_ids.values())
        else "review_required",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--processed-dir", type=Path, default=PROJECT_ROOT / "data" / "processed")
    parser.add_argument("--manifest-dir", type=Path, required=True)
    parser.add_argument("--raw-dir", type=Path, default=None)
    parser.add_argument("--version", default="v1")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = validate(args.processed_dir, args.manifest_dir, args.raw_dir, args.version)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
