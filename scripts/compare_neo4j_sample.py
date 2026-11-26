"""제공된 Neo4j 조회 결과와 현재 chunk/evidence 샘플을 대조한다."""

from __future__ import annotations

import argparse
import json
import unicodedata
from pathlib import Path
from typing import Any


def _norm(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    return value


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _find_chunk(chunks: list[dict[str, Any]], expected: dict[str, Any]) -> dict[str, Any] | None:
    if expected["kind"] == "rate_option":
        return next(
            (
                row
                for row in chunks
                if row["metadata"].get("rate_option_id") == expected.get("rate_option_id")
            ),
            None,
        )
    chunk_type = "policy_benefit" if expected["kind"] == "benefit" else "policy_application"
    return next(
        (
            row
            for row in chunks
            if row["chunk_type"] == chunk_type
            and row["metadata"].get("policy_id") == expected.get("policy_id")
        ),
        None,
    )


def _graph_path_verified(expected: dict[str, Any]) -> bool:
    relationships = expected.get("relationships") or []
    if not relationships or any(
        not all(
            relation.get(key)
            for key in ("from_label", "from_id", "type", "to_label", "to_id")
        )
        for relation in relationships
    ):
        return False
    kind = expected.get("kind")
    if kind == "rate_option":
        expected_path = [
            ("FinancialProduct", expected.get("product_id"), "HAS_DISCLOSURE", "ProductDisclosure", expected.get("disclosure_id")),
            ("ProductDisclosure", expected.get("disclosure_id"), "HAS_RATE_OPTION", "RateOption", expected.get("rate_option_id")),
            ("RateOption", expected.get("rate_option_id"), "SUPPORTED_BY", "Evidence", expected.get("evidence_id")),
        ]
    elif kind == "benefit":
        expected_path = [
            ("YouthPolicy", expected.get("policy_id"), "HAS_BENEFIT", "Benefit", expected.get("benefit_id")),
            ("Benefit", expected.get("benefit_id"), "SUPPORTED_BY", "Evidence", expected.get("evidence_id")),
        ]
    else:
        expected_path = [
            ("YouthPolicy", expected.get("policy_id"), "SUPPORTED_BY", "Evidence", expected.get("evidence_id")),
        ]
    actual_path = [
        (
            relation.get("from_label"),
            relation.get("from_id"),
            relation.get("type"),
            relation.get("to_label"),
            relation.get("to_id"),
        )
        for relation in relationships
    ]
    return actual_path == expected_path


def _chunk_value_match(chunk: dict[str, Any] | None, expected: dict[str, Any]) -> bool:
    if chunk is None:
        return False
    metadata = chunk.get("metadata") or {}
    evidence = expected.get("evidence") or {}
    if expected.get("kind") == "rate_option":
        return metadata.get("base_rate") == expected.get("base_rate")
    if expected.get("kind") == "benefit":
        return metadata.get("support_content") == evidence.get("source_value")
    return metadata.get("application_period") == evidence.get("source_value")


def compare(neo4j_results: Path, chunks_path: Path) -> dict[str, Any]:
    neo4j = json.loads(neo4j_results.read_text(encoding="utf-8"))
    chunks = _load_jsonl(chunks_path)
    rows = []
    for expected in neo4j.get("results", []):
        chunk = _find_chunk(chunks, expected)
        expected_evidence = expected.get("evidence") or {}
        source_link = (chunk or {}).get("source_link") or {}
        original = source_link.get("original") or {}
        original_path = original.get("path") or ""
        expected_path = expected_evidence.get("source_locator") or ""
        source_match = bool(
            chunk
            and _norm(original.get("file")) == _norm(expected_evidence.get("source_file"))
            and (
                original_path == expected_path
                or expected_path.startswith(original_path.rstrip("/") + "/")
            )
        )
        value_match = _chunk_value_match(chunk, expected)
        graph_path_verified = _graph_path_verified(expected)
        rows.append(
            {
                "kind": expected.get("kind"),
                "neo4j_product_id": expected.get("product_id"),
                "neo4j_disclosure_id": expected.get("disclosure_id"),
                "neo4j_rate_option_id": expected.get("rate_option_id"),
                "neo4j_policy_id": expected.get("policy_id"),
                "neo4j_benefit_id": expected.get("benefit_id"),
                "neo4j_evidence_id": expected.get("evidence_id"),
                "chunk_id": (chunk or {}).get("chunk_id"),
                "chunk_evidence_id": (chunk or {}).get("evidence_id"),
                "semantic_entity_match": chunk is not None,
                "source_match": source_match,
                "source_value_match": value_match,
                "graph_path_verified": graph_path_verified,
                "graph_relationship_count": len(expected.get("relationships") or []),
                "internal_evidence_id_equal": bool(
                    chunk and chunk.get("evidence_id") == expected.get("evidence_id")
                ),
                "verified_relation_ids": [
                    relation.get("type") for relation in expected.get("relationships", [])
                ],
                "graph_path": expected.get("relationships", []),
                "status": (
                    "semantic_and_source_match"
                    if chunk is not None and source_match
                    else "semantic_match_source_unverified"
                    if chunk is not None
                    else "not_in_current_sample"
                ),
            }
        )
    return {
        "neo4j_validation": neo4j.get("validation"),
        "chunk_file": str(chunks_path),
        "neo4j_result_count": len(rows),
        "semantic_match_count": sum(row["semantic_entity_match"] for row in rows),
        "source_match_count": sum(row["source_match"] for row in rows),
        "source_value_match_count": sum(row["source_value_match"] for row in rows),
        "graph_path_verified_count": sum(row["graph_path_verified"] for row in rows),
        "internal_evidence_id_equal_count": sum(
            row["internal_evidence_id_equal"] for row in rows
        ),
        "rows": rows,
        "interpretation": (
            "Neo4j Evidence ID와 Work의 EVD_* ID는 체계가 다르므로 동일 문자열 매칭이 아니라 "
            "rate_option_id/policy_id와 원본 source_locator를 통한 매핑으로 관리한다."
        ),
        "relation_identity_basis": "Neo4j 결과에 관계 고유 ID가 없으므로 from_id + relation_type + to_id로 경로를 검증한다.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--neo4j-results", type=Path, required=True)
    parser.add_argument("--chunks", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = compare(args.neo4j_results, args.chunks)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
