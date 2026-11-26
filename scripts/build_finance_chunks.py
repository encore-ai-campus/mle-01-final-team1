"""금융 정제 데이터를 evidence/chunk JSONL로 변환하는 실행 스크립트."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from services.finance_chunk_builder import build_finance_chunks  # noqa: E402


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _build_report(
    evidence: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
    limit: int | None,
    product_ids: list[str] | None,
    policy_ids: list[str] | None,
    version: str,
) -> dict[str, Any]:
    return {
        "manifest_version": version,
        "limit_per_dataset": limit,
        "selected_product_ids": product_ids,
        "selected_policy_ids": policy_ids,
        "evidence_count": len(evidence),
        "chunk_count": len(chunks),
        "dataset_counts": dict(Counter(row["dataset"] for row in chunks)),
        "chunk_type_counts": dict(Counter(row["chunk_type"] for row in chunks)),
        "duplicate_evidence_ids": len(evidence)
        - len({row["evidence_id"] for row in evidence}),
        "duplicate_chunk_ids": len(chunks) - len({row["chunk_id"] for row in chunks}),
        "duplicate_content_count": len(chunks)
        - len({(row["dataset"], row["chunk_type"], row["text"].strip()) for row in chunks}),
        "source_metadata_missing": sum(
            not row.get("source_file") or not row.get("source_path") for row in chunks
        ),
        "source_link_status_counts": dict(
            Counter((row.get("source_link") or {}).get("status") for row in chunks)
        ),
        "missing_graph_identity_count": sum(
            row["chunk_type"] == "rate_option"
            and (
                not row["metadata"].get("disclosure_id")
                or not row["metadata"].get("rate_option_id")
            )
            for row in chunks
        ),
        "relation_verification_status_counts": dict(
            Counter(row.get("relation_verification", {}).get("status") for row in chunks)
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--processed-dir",
        type=Path,
        default=PROJECT_ROOT / "data" / "processed",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "data" / "processed",
    )
    parser.add_argument(
        "--raw-dir",
        type=Path,
        default=None,
        help="원본 JSON 폴더. 지정하면 원본 JSON 경로도 함께 기록합니다.",
    )
    parser.add_argument(
        "--limit-per-dataset",
        type=int,
        default=None,
        help="각 데이터셋에서 앞부분의 레코드만 사용합니다. 샘플 확인용입니다.",
    )
    parser.add_argument(
        "--product-id",
        action="append",
        dest="product_ids",
        help="지정한 상품 ID만 추출합니다. 여러 번 지정할 수 있습니다.",
    )
    parser.add_argument(
        "--policy-id",
        action="append",
        dest="policy_ids",
        help="지정한 정책 ID만 추출합니다. 여러 번 지정할 수 있습니다.",
    )
    parser.add_argument(
        "--version",
        default="v1",
        help="생성 매니페스트 버전입니다.",
    )
    parser.add_argument(
        "--id-version",
        choices=("v1", "stable-v2"),
        default="v1",
        help="Chunk/Evidence ID 생성 방식입니다. v1은 기존 ID를 보존하고 stable-v2는 원천 레코드 키를 사용합니다.",
    )
    args = parser.parse_args()
    if args.limit_per_dataset is not None and args.limit_per_dataset < 1:
        parser.error("--limit-per-dataset은 1 이상이어야 합니다.")

    evidence, chunks = build_finance_chunks(
        args.processed_dir,
        limit_per_dataset=args.limit_per_dataset,
        raw_dir=args.raw_dir,
        product_ids=set(args.product_ids) if args.product_ids else None,
        policy_ids=set(args.policy_ids) if args.policy_ids else None,
        id_version=args.id_version,
    )
    _write_jsonl(args.output_dir / "evidence.jsonl", evidence)
    _write_jsonl(args.output_dir / "chunks.jsonl", chunks)
    report = _build_report(
        evidence,
        chunks,
        args.limit_per_dataset,
        args.product_ids,
        args.policy_ids,
        args.version,
    )
    report["chunk_id_version"] = args.id_version
    report_path = args.output_dir / "chunk_build_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
