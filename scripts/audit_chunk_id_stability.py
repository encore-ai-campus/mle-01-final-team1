"""전체 매니페스트와 부분 샘플의 Chunk ID 안정성을 점검한다."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from services.finance_chunk_builder import build_finance_chunks


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _identity(row: dict[str, Any]) -> tuple[str, str, str | None]:
    metadata = row.get("metadata") or {}
    if row.get("chunk_type") == "rate_option":
        return row.get("chunk_type", ""), "rate_option", metadata.get("rate_option_id")
    if row.get("dataset") == "youth_policy":
        return row.get("chunk_type", ""), "policy", metadata.get("policy_id")
    return row.get("chunk_type", ""), "product", metadata.get("product_id")


def audit(full_path: Path, sample_path: Path) -> dict[str, Any]:
    full = {_identity(row): row for row in _read_jsonl(full_path)}
    sample = _read_jsonl(sample_path)
    rows = []
    for sample_row in sample:
        full_row = full.get(_identity(sample_row))
        rows.append(
            {
                "identity": _identity(sample_row),
                "sample_chunk_id": sample_row.get("chunk_id"),
                "full_chunk_id": (full_row or {}).get("chunk_id"),
                "chunk_id_equal": bool(full_row and sample_row.get("chunk_id") == full_row.get("chunk_id")),
                "sample_source_path": sample_row.get("source_path"),
                "full_source_path": (full_row or {}).get("source_path"),
                "record_index_changed": bool(
                    full_row and sample_row.get("source_path") != full_row.get("source_path")
                ),
                "text_equal": bool(full_row and sample_row.get("text") == full_row.get("text")),
            }
        )
    return {
        "full_manifest": str(full_path),
        "sample_manifest": str(sample_path),
        "sample_count": len(rows),
        "matched_identity_count": sum(bool(row["full_chunk_id"]) for row in rows),
        "chunk_id_equal_count": sum(row["chunk_id_equal"] for row in rows),
        "record_index_changed_count": sum(row["record_index_changed"] for row in rows),
        "text_equal_count": sum(row["text_equal"] for row in rows),
        "rows": rows,
    }


def stable_v2_check(processed_dir: Path, sample_path: Path) -> dict[str, Any]:
    sample = _read_jsonl(sample_path)
    product_ids = {
        str(row["metadata"]["product_id"])
        for row in sample
        if row.get("metadata", {}).get("product_id")
    }
    policy_ids = {
        str(row["metadata"]["policy_id"])
        for row in sample
        if row.get("metadata", {}).get("policy_id")
    }
    _, full_v2 = build_finance_chunks(processed_dir, id_version="stable-v2")
    _, sample_v2 = build_finance_chunks(
        processed_dir,
        product_ids=product_ids or None,
        policy_ids=policy_ids or None,
        id_version="stable-v2",
    )
    full_index = {_identity(row): row["chunk_id"] for row in full_v2}
    sample_index = {_identity(row): row["chunk_id"] for row in sample_v2}
    common = set(full_index) & set(sample_index)
    return {
        "stable_v2_common_identity_count": len(common),
        "stable_v2_equal_id_count": sum(full_index[key] == sample_index[key] for key in common),
        "stable_v2_all_equal": bool(common) and all(full_index[key] == sample_index[key] for key in common),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full-chunks", type=Path, required=True)
    parser.add_argument("--sample-chunks", type=Path, required=True)
    parser.add_argument("--processed-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit(args.full_chunks, args.sample_chunks)
    report.update(stable_v2_check(args.processed_dir, args.sample_chunks))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
