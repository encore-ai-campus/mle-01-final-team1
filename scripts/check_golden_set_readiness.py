"""청크 매니페스트가 골든셋 후보 생성에 필요한 준비를 마쳤는지 점검한다."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


REQUIRED_GOLDEN_FIELDS = [
    "question",
    "reference",
    "expected_facts",
    "reference_context_ids",
    "expected_entity_ids",
    "expected_relation_ids",
    "expected_route",
    "difficulty",
    "hop_count",
    "source_snapshot_version",
]


def _exists_any(root: Path, filename: str) -> list[str]:
    return [str(path) for path in root.rglob(filename) if ".venv" not in path.parts]


def check(project_root: Path, manifest_dir: Path, neo4j_report: Path) -> dict:
    generator_files = _exists_any(project_root, "generate_eval_candidates.py")
    notebook_files = _exists_any(project_root, "rag_eval.ipynb")
    manifest_report = json.loads(
        (manifest_dir / "chunk_validation_report.json").read_text(encoding="utf-8")
    )
    neo4j = json.loads(neo4j_report.read_text(encoding="utf-8"))
    return {
        "manifest_version": manifest_report.get("manifest_version"),
        "data_ready": manifest_report.get("source", {}).get("untraceable_count") == 0
        and manifest_report.get("id_stability_pass") is True,
        "neo4j_aligned_sample_ready": neo4j.get("semantic_match_count") == neo4j.get("neo4j_result_count")
        and neo4j.get("source_match_count") == neo4j.get("neo4j_result_count")
        and neo4j.get("source_value_match_count") == neo4j.get("neo4j_result_count")
        and neo4j.get("graph_path_verified_count") == neo4j.get("neo4j_result_count"),
        "golden_set_frozen": False,
        "candidate_generation_started": False,
        "required_golden_fields": REQUIRED_GOLDEN_FIELDS,
        "existing_generator_files": generator_files,
        "existing_evaluation_notebooks": notebook_files,
        "missing_integration_inputs": [
            "Generator/Critic 호출 계약과 출력 JSON 형식",
            "실제 Retriever 반환 JSON 및 검색 경로 필드",
            "Neo4j 조회 함수 또는 승인된 read-only 조회 결과 형식",
            "사람 검수용 notebook/table 형식",
        ],
        "next_action": "골든셋 후보 자동 생성 전 Generator/Critic·Retriever 인터페이스를 확정한다.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--manifest-dir", type=Path, required=True)
    parser.add_argument("--neo4j-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = check(args.project_root, args.manifest_dir, args.neo4j_report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
