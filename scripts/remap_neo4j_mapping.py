"""기존 Neo4j 경로 보고서를 다른 청크 매니페스트에 재매핑한다.

Neo4j 관계·Evidence ID는 변경하지 않고, 원천 ID로 대상 청크의 Chunk/EVD ID만 다시 찾는다.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _chunk_key(row: dict[str, Any]) -> tuple[str, str, str | None]:
    metadata = row.get("metadata") or {}
    if row.get("chunk_type") == "rate_option":
        return row.get("chunk_type", ""), "rate_option", metadata.get("rate_option_id")
    return row.get("chunk_type", ""), "policy", metadata.get("policy_id")


def remap(report: dict[str, Any], chunks: list[dict[str, Any]], target_chunk_file: str) -> dict[str, Any]:
    index = {_chunk_key(row): row for row in chunks}
    remapped_rows = []
    changed = 0
    matched = 0
    for row in report.get("rows", []):
        kind = row.get("kind")
        chunk_type = "rate_option" if kind == "rate_option" else (
            "policy_benefit" if kind == "benefit" else "policy_application"
        )
        identity = row.get("neo4j_rate_option_id") if kind == "rate_option" else row.get("neo4j_policy_id")
        target = index.get((chunk_type, "rate_option" if kind == "rate_option" else "policy", identity))
        updated = dict(row)
        old_chunk_id = updated.get("chunk_id")
        old_evidence_id = updated.get("chunk_evidence_id")
        if target:
            updated["chunk_id"] = target["chunk_id"]
            updated["chunk_evidence_id"] = target["evidence_id"]
            updated["semantic_entity_match"] = True
            updated["source_match"] = (
                (target.get("source_link") or {}).get("status") == "matched"
            )
            updated["source_value_match"] = updated["source_match"]
            updated["status"] = "semantic_and_source_match"
            matched += 1
            if old_chunk_id != updated["chunk_id"] or old_evidence_id != updated["chunk_evidence_id"]:
                changed += 1
        else:
            updated["chunk_id"] = None
            updated["chunk_evidence_id"] = None
            updated["semantic_entity_match"] = False
            updated["source_match"] = False
            updated["source_value_match"] = False
            updated["status"] = "not_in_target_manifest"
        remapped_rows.append(updated)
    result = dict(report)
    result["chunk_file"] = target_chunk_file
    result["rows"] = remapped_rows
    result["semantic_match_count"] = sum(row["semantic_entity_match"] for row in remapped_rows)
    result["source_match_count"] = sum(row["source_match"] for row in remapped_rows)
    result["source_value_match_count"] = sum(row["source_value_match"] for row in remapped_rows)
    result["internal_evidence_id_equal_count"] = sum(
        row["internal_evidence_id_equal"] for row in remapped_rows
    )
    result["remap"] = {
        "basis": "rate_option_id or policy_id + chunk_type",
        "target_chunk_file": target_chunk_file,
        "matched_rows": matched,
        "changed_chunk_id_rows": changed,
        "neo4j_ids_unchanged": True,
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mapping-report", type=Path, required=True)
    parser.add_argument("--chunks", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = json.loads(args.mapping_report.read_text(encoding="utf-8"))
    result = remap(report, _read_jsonl(args.chunks), str(args.chunks))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
