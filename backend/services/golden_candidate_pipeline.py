"""금융 GraphRAG 골든셋 후보 생성·검수·사람 검토용 출력 파이프라인."""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any


ROUTES = ("VECTOR", "GRAPH", "HYBRID")


def _normalise_question(value: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]+", "", value.lower())


def _value_in_context(value: Any, context: dict[str, Any]) -> bool:
    if value is None:
        return False
    metadata = context.get("metadata") or {}
    if value in metadata.values():
        return True
    original = (context.get("source_link") or {}).get("original") or {}
    if value in original.values():
        return True
    text = context.get("text", "")
    return str(value) in text


def _relation_objects(graph_row: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "from_label": relation.get("from_label"),
            "from_id": relation.get("from_id"),
            "relation_type": relation.get("type"),
            "to_label": relation.get("to_label"),
            "to_id": relation.get("to_id"),
        }
        for relation in graph_row.get("graph_path", [])
    ]


def _entity_ids(context: dict[str, Any], graph_row: dict[str, Any] | None) -> list[str]:
    values = list(context.get("entity_ids") or [])
    if graph_row:
        for relation in _relation_objects(graph_row):
            for key in ("from_id", "to_id"):
                value = relation.get(key)
                if value and value not in values and not str(value).startswith("deposit|") and not str(value).startswith("youth_policy|"):
                    values.append(value)
    return values


class TemplateGenerator:
    """실제 원천값을 그대로 사용해 후보 형식을 만드는 로컬 Generator."""

    provider = "template"
    prompt_version = "finance-candidate-template-v1"

    def generate(
        self,
        route: str,
        context: dict[str, Any],
        graph_row: dict[str, Any] | None,
        candidate_id: str,
        snapshot_version: str,
    ) -> dict[str, Any]:
        metadata = context.get("metadata") or {}
        product_name = metadata.get("product_name") or "상품"
        term = metadata.get("term_months")
        base_rate = metadata.get("base_rate")
        max_rate = metadata.get("max_rate")
        is_policy = context.get("dataset") == "youth_policy"

        if route == "VECTOR":
            if context.get("chunk_type") == "rate_option":
                question = f"{product_name}의 {term}개월 금리옵션에서 기본금리와 최고금리는 각각 얼마인가요?"
                reference = f"기본금리는 {base_rate}%이고, 최고금리는 {max_rate}%입니다."
                facts = [
                    {"fact": "base_rate", "value": base_rate, "unit": "%"},
                    {"fact": "max_rate", "value": max_rate, "unit": "%"},
                ]
            else:
                question = f"{metadata.get('policy_name', '이 정책')}의 지원내용은 무엇인가요?"
                reference = context.get("text", "")
                facts = [{"fact": "support_content", "value": metadata.get("support_content")}]
            relations: list[dict[str, Any]] = []
            hop_count = 0
            difficulty = "medium"
        elif route == "GRAPH":
            path = _relation_objects(graph_row or {})
            if is_policy:
                policy_name = metadata.get("policy_name") or "청년정책"
                support_content = metadata.get("support_content")
                question = f"{policy_name}의 지원내용이 정책의 혜택 항목과 같은 내용인지 확인해줘."
                reference = (
                    f"정책 {policy_name}의 혜택은 '{support_content}'이며, "
                    "정책에서 혜택을 거쳐 Evidence 근거로 연결됩니다."
                )
                facts = [
                    {"fact": "policy_name", "value": policy_name},
                    {"fact": "support_content", "value": support_content},
                ]
            else:
                rate_id = metadata.get("rate_option_id")
                question = f"{product_name}의 {term}개월 금리 정보가 어떤 공시 기준으로 등록되어 있는지 확인해줘."
                reference = (
                    f"상품은 공시 {metadata.get('disclosure_id')}를 거쳐 금리옵션 "
                    f"{rate_id}로 연결됩니다."
                )
                facts = [
                    {"fact": "disclosure_id", "value": metadata.get("disclosure_id")},
                    {"fact": "rate_option_id", "value": rate_id},
                ]
            relations = path
            hop_count = len(path)
            difficulty = "hard"
        else:
            path = _relation_objects(graph_row or {})
            source_file = (context.get("source_link") or {}).get("original", {}).get("file")
            if is_policy:
                policy_name = metadata.get("policy_name") or "청년정책"
                support_content = metadata.get("support_content")
                question = f"{policy_name}에서 실제로 어떤 지원을 받을 수 있는지, 원본 근거와 함께 알려줘."
                reference = (
                    f"정책 {policy_name}의 혜택은 '{support_content}'이며, "
                    f"원본 파일 {source_file}과 그래프 경로로 확인됩니다."
                )
                facts = [
                    {"fact": "policy_name", "value": policy_name},
                    {"fact": "support_content", "value": support_content},
                    {"fact": "source_file", "value": source_file},
                ]
            else:
                question = f"{product_name} {term}개월 금리가 얼마인지, 원본 근거가 어느 공시에서 나온 것인지 함께 알려줘."
                reference = (
                    f"기본금리는 {base_rate}%이며, {metadata.get('disclosure_id')} 공시와 "
                    f"{metadata.get('rate_option_id')} 금리옵션을 거쳐 "
                    f"원본 파일 {source_file} 근거에 연결됩니다."
                )
                facts = [
                    {"fact": "base_rate", "value": base_rate, "unit": "%"},
                    {"fact": "source_file", "value": source_file},
                ]
            relations = path
            hop_count = len(path)
            difficulty = "hard"

        return {
            "candidate_id": candidate_id,
            "question": question,
            "reference": reference,
            "expected_facts": facts,
            "reference_context_ids": [context["chunk_id"]],
            "expected_entity_ids": _entity_ids(context, graph_row),
            "expected_relation_ids": relations,
            "expected_route": route,
            "route_rationale": (
                "single_context_fact"
                if route == "VECTOR"
                else "verified_relationship_required"
                if route == "GRAPH"
                else "verified_relationship_and_context_value_required"
            ),
            "difficulty": difficulty,
            "hop_count": hop_count,
            "source_snapshot_version": snapshot_version,
            "source_evidence_ids": [context["evidence_id"]],
            "neo4j_evidence_ids": [graph_row["neo4j_evidence_id"]]
            if graph_row and graph_row.get("neo4j_evidence_id")
            else [],
            "relation_identity_basis": "from_id+relation_type+to_id",
            "generator": {
                "provider": self.provider,
                "prompt_version": self.prompt_version,
                "status": "generated",
            },
            "review_status": "pending",
        }


class RuleBasedCritic:
    """LLM 호출 전 단계의 재현 가능한 근거·구조 검수기."""

    provider = "rules"

    def critique(
        self,
        candidate: dict[str, Any],
        contexts: dict[str, dict[str, Any]],
        graph_rows: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        checks: dict[str, bool] = {}
        context_rows = [contexts.get(context_id) for context_id in candidate["reference_context_ids"]]
        checks["context_exists"] = all(row is not None for row in context_rows)
        checks["source_is_traceable"] = all(
            (row or {}).get("source_link", {}).get("status") == "matched" for row in context_rows
        )
        checks["facts_in_context"] = all(
            any(_value_in_context(fact.get("value"), row or {}) for row in context_rows)
            for fact in candidate["expected_facts"]
        )
        checks["reference_contains_expected_facts"] = all(
            fact.get("value") is None
            or str(fact.get("value")) in candidate.get("reference", "")
            for fact in candidate["expected_facts"]
        )
        checks["question_and_reference_present"] = bool(
            candidate.get("question", "").strip() and candidate.get("reference", "").strip()
        )
        if candidate["expected_route"] == "VECTOR":
            checks["route_shape"] = candidate["hop_count"] == 0 and not candidate["expected_relation_ids"]
            checks["graph_path_verified"] = True
        else:
            graph_row = next(
                (
                    row
                    for row in graph_rows.values()
                    if row.get("chunk_id") in candidate["reference_context_ids"]
                ),
                None,
            )
            checks["route_shape"] = candidate["hop_count"] >= 2 and bool(candidate["expected_relation_ids"])
            checks["graph_path_verified"] = bool(graph_row and graph_row.get("graph_path_verified"))
            checks["relation_identity_is_explicit"] = all(
                set(relation) >= {"from_id", "relation_type", "to_id"}
                for relation in candidate["expected_relation_ids"]
            )
            relation_signals = ("연결", "경로", "공시 기준", "근거", "등록되어", "같은 내용", "항목")
            checks["question_requires_relationship"] = any(
                signal in candidate.get("question", "") for signal in relation_signals
            )
            if candidate["expected_route"] == "HYBRID":
                value_signals = ("얼마", "금리", "지원", "혜택", "조건", "원본")
                checks["question_requires_context_value"] = any(
                    signal in candidate.get("question", "") for signal in value_signals
                )
        passed = all(checks.values())
        return {
            "provider": self.provider,
            "verdict": "pass" if passed else "fail",
            "checks": checks,
            "reasons": [] if passed else [name for name, ok in checks.items() if not ok],
        }


def _candidate_source_rows(
    route: str,
    chunks: list[dict[str, Any]],
    graph_rows: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
    if route == "VECTOR":
        return [
            (row, None)
            for row in chunks
            if row.get("chunk_type") in {"rate_option", "policy_benefit", "policy_application"}
        ]
    graph_sources = [
        row
        for row in graph_rows
        if row.get("graph_path_verified") and len(row.get("graph_path", [])) >= 2
    ]
    chunks_by_id = {row["chunk_id"]: row for row in chunks}
    return [
        (chunks_by_id[row["chunk_id"]], row)
        for row in graph_sources
        if row.get("chunk_id") in chunks_by_id
    ]


def generate_candidates(
    chunks: list[dict[str, Any]],
    graph_rows: list[dict[str, Any]],
    *,
    counts: dict[str, int],
    snapshot_version: str,
    run_id: str,
    generator: TemplateGenerator | None = None,
    critic: RuleBasedCritic | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    generator = generator or TemplateGenerator()
    critic = critic or RuleBasedCritic()
    contexts = {row["chunk_id"]: row for row in chunks}
    graph_by_chunk = {row["chunk_id"]: row for row in graph_rows if row.get("chunk_id")}
    candidates: list[dict[str, Any]] = []
    used_questions: set[str] = set()
    report: dict[str, Any] = {
        "run_id": run_id,
        "source_snapshot_version": snapshot_version,
        "requested": {route: counts.get(route, 0) for route in ROUTES},
        "generated": dict.fromkeys(ROUTES, 0),
        "accepted_by_critic": 0,
        "skipped_unverified_graph": 0,
        "skipped_duplicate": 0,
        "shortage": {},
    }

    sequence = 0
    for route in ROUTES:
        requested = counts.get(route, 0)
        sources = _candidate_source_rows(route, chunks, graph_rows)
        if route != "VECTOR":
            all_graph_sources = [
                row for row in graph_rows if row.get("chunk_id") in contexts
            ]
            unverified_sources = [
                row for row in all_graph_sources if not row.get("graph_path_verified")
            ]
            report["skipped_unverified_graph"] += min(requested, len(unverified_sources))
            verified_sources = len(sources)
            if verified_sources < requested:
                report["shortage"][route] = {
                    "requested": requested,
                    "available_verified_paths": verified_sources,
                }
        source_index = 0
        while report["generated"][route] < requested and source_index < len(sources):
            context, graph_row = sources[source_index]
            source_index += 1
            sequence += 1
            candidate_id = f"{run_id}_{route}_{sequence:04d}"
            candidate = generator.generate(
                route, context, graph_row, candidate_id, snapshot_version
            )
            normalized = _normalise_question(candidate["question"])
            if normalized in used_questions:
                report["skipped_duplicate"] += 1
                continue
            candidate["critic"] = critic.critique(candidate, contexts, graph_by_chunk)
            if route != "VECTOR" and candidate["critic"]["verdict"] != "pass":
                if not candidate["critic"]["checks"].get("graph_path_verified", False):
                    report["skipped_unverified_graph"] += 1
                continue
            if candidate["critic"]["verdict"] != "pass":
                continue
            used_questions.add(normalized)
            candidates.append(candidate)
            report["generated"][route] += 1
            report["accepted_by_critic"] += 1
        if report["generated"][route] < requested and route not in report["shortage"]:
            report["shortage"][route] = {
                "requested": requested,
                "available_sources": len(sources),
            }
    report["candidate_count"] = len(candidates)
    return candidates, report


def write_candidate_outputs(
    output_dir: Path,
    candidates: list[dict[str, Any]],
    report: dict[str, Any],
    *,
    overwrite: bool = False,
) -> None:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if (output_dir / "candidates.jsonl").exists() and not overwrite:
        raise FileExistsError(
            f"기존 후보 배치를 보존하기 위해 덮어쓰기를 중단했습니다: {output_dir}"
        )
    (output_dir / "candidates.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in candidates) + "\n",
        encoding="utf-8",
    )
    fields = [
        "candidate_id",
        "question",
        "reference",
        "expected_facts",
        "reference_context_ids",
        "expected_entity_ids",
        "expected_relation_ids",
        "expected_route",
        "route_rationale",
        "difficulty",
        "hop_count",
        "source_snapshot_version",
        "source_evidence_ids",
        "neo4j_evidence_ids",
        "relation_identity_basis",
        "generator",
        "critic",
        "review_status",
    ]
    with (output_dir / "review.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in candidates:
            writer.writerow(
                {
                    field: json.dumps(row[field], ensure_ascii=False)
                    if isinstance(row.get(field), (dict, list))
                    else row.get(field)
                    for field in fields
                }
            )
    (output_dir / "candidate_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
