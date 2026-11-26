import json
from pathlib import Path

from services.golden_candidate_pipeline import (
    TemplateGenerator,
    generate_candidates,
    write_candidate_outputs,
)


def chunk(chunk_id: str, chunk_type: str = "rate_option") -> dict:
    return {
        "chunk_id": chunk_id,
        "evidence_id": f"EVD_{chunk_id}",
        "dataset": "deposit",
        "chunk_type": chunk_type,
        "text": "테스트은행 테스트예금 12개월 기본금리 3.6%, 최고금리 3.6%",
        "source_file": "deposit_rates.json",
        "source_path": "records[0]",
        "source_record_id": "p-1",
        "entity_ids": ["p-1"],
        "relation_ids": ["HAS_DISCLOSURE", "HAS_RATE_OPTION"],
        "relation_verification": {"status": "not_verified"},
        "metadata": {
            "product_id": "p-1",
            "product_name": "테스트예금",
            "institution_name": "테스트은행",
            "disclosure_id": "p-1:202610",
            "rate_option_id": "p-1:202610:S:12",
            "term_months": 12,
            "base_rate": 3.6,
            "max_rate": 3.6,
        },
        "source_link": {
            "status": "matched",
            "original": {"file": "정기예금.json", "path": "/products/0/rate_options/0/data"},
        },
    }


def graph_row() -> dict:
    return {
        "kind": "rate_option",
        "neo4j_product_id": "p-1",
        "neo4j_disclosure_id": "p-1:202610",
        "neo4j_rate_option_id": "p-1:202610:S:12",
        "neo4j_evidence_id": "deposit|p-1:202610:S:12|base_rate|001",
        "chunk_id": "CHK_1",
        "chunk_evidence_id": "EVD_CHK_1",
        "graph_path_verified": True,
        "verified_relation_ids": ["HAS_DISCLOSURE", "HAS_RATE_OPTION", "SUPPORTED_BY"],
        "graph_path": [
            {
                "from_label": "FinancialProduct",
                "from_id": "p-1",
                "type": "HAS_DISCLOSURE",
                "to_label": "ProductDisclosure",
                "to_id": "p-1:202610",
            },
            {
                "from_label": "ProductDisclosure",
                "from_id": "p-1:202610",
                "type": "HAS_RATE_OPTION",
                "to_label": "RateOption",
                "to_id": "p-1:202610:S:12",
            },
            {
                "from_label": "RateOption",
                "from_id": "p-1:202610:S:12",
                "type": "SUPPORTED_BY",
                "to_label": "Evidence",
                "to_id": "deposit|p-1:202610:S:12|base_rate|001",
            },
        ],
    }


def policy_chunk(chunk_id: str = "CHK_POLICY") -> dict:
    return {
        "chunk_id": chunk_id,
        "evidence_id": "EVD_POLICY",
        "dataset": "youth_policy",
        "chunk_type": "policy_benefit",
        "text": "영암군 청년 자격증 응시료 지원사업의 지원내용은 자격증 응시료를 지원하는 것입니다.",
        "source_file": "youth_policy.json",
        "source_path": "records[0].benefits[0]",
        "source_record_id": "policy-1",
        "entity_ids": ["policy-1"],
        "relation_ids": ["HAS_BENEFIT", "SUPPORTED_BY"],
        "relation_verification": {"status": "not_verified"},
        "metadata": {
            "policy_id": "policy-1",
            "policy_name": "영암군 청년 자격증 응시료 지원사업",
            "benefit_id": "policy-1:001",
            "support_content": "자격증 응시료를 지원",
        },
        "source_link": {
            "status": "matched",
            "original": {"file": "청년정책.json", "path": "/policies/0/benefits/0"},
        },
    }


def policy_graph_row() -> dict:
    return {
        "kind": "benefit",
        "neo4j_policy_id": "policy-1",
        "neo4j_benefit_id": "policy-1:001",
        "neo4j_evidence_id": "youth_policy|policy-1|description|001",
        "chunk_id": "CHK_POLICY",
        "chunk_evidence_id": "EVD_POLICY",
        "graph_path_verified": True,
        "graph_relationship_count": 2,
        "verified_relation_ids": ["HAS_BENEFIT", "SUPPORTED_BY"],
        "graph_path": [
            {
                "from_label": "YouthPolicy",
                "from_id": "policy-1",
                "type": "HAS_BENEFIT",
                "to_label": "Benefit",
                "to_id": "policy-1:001",
            },
            {
                "from_label": "Benefit",
                "from_id": "policy-1:001",
                "type": "SUPPORTED_BY",
                "to_label": "Evidence",
                "to_id": "youth_policy|policy-1|description|001",
            },
        ],
    }


def test_generates_grounded_candidates_and_review_outputs(tmp_path):
    candidates, report = generate_candidates(
        [chunk("CHK_1")],
        [graph_row()],
        counts={"VECTOR": 1, "GRAPH": 1, "HYBRID": 1},
        snapshot_version="v1",
        run_id="sample-01",
    )

    assert len(candidates) == 3
    assert report["accepted_by_critic"] == 3
    assert {row["review_status"] for row in candidates} == {"pending"}
    assert all(row["reference_context_ids"] for row in candidates)
    assert all(row["source_snapshot_version"] == "v1" for row in candidates)
    assert all(row["critic"]["verdict"] == "pass" for row in candidates)
    assert all(
        all(set(relation) >= {"from_id", "relation_type", "to_id"} for relation in row["expected_relation_ids"])
        for row in candidates
        if row["expected_route"] in {"GRAPH", "HYBRID"}
    )

    output = tmp_path / "review"
    write_candidate_outputs(output, candidates, report)
    assert (output / "candidates.jsonl").exists()
    assert (output / "review.csv").exists()
    saved = [json.loads(line) for line in (output / "candidates.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(saved) == 3
    review_header = (output / "review.csv").read_text(encoding="utf-8-sig").splitlines()[0]
    assert "route_rationale" in review_header
    assert "source_evidence_ids" in review_header


def test_unverified_graph_path_is_not_accepted(tmp_path):
    row = graph_row()
    row["graph_path_verified"] = False
    candidates, report = generate_candidates(
        [chunk("CHK_1")],
        [row],
        counts={"VECTOR": 0, "GRAPH": 1, "HYBRID": 1},
        snapshot_version="v1",
        run_id="sample-02",
    )

    assert candidates == []
    assert report["skipped_unverified_graph"] == 2


def test_policy_benefit_graph_path_is_grounded_for_graph_and_hybrid():
    candidates, report = generate_candidates(
        [policy_chunk()],
        [policy_graph_row()],
        counts={"VECTOR": 0, "GRAPH": 1, "HYBRID": 1},
        snapshot_version="v1",
        run_id="policy-test",
    )

    assert len(candidates) == 2
    assert report["accepted_by_critic"] == 2
    assert {row["expected_route"] for row in candidates} == {"GRAPH", "HYBRID"}
    assert all(row["hop_count"] == 2 for row in candidates)
    assert all(row["critic"]["verdict"] == "pass" for row in candidates)
    assert all(
        all(set(relation) >= {"from_id", "relation_type", "to_id"} for relation in row["expected_relation_ids"])
        for row in candidates
    )


def test_graph_and_hybrid_questions_are_user_facing_not_id_traversal_prompts():
    graph_candidate = TemplateGenerator().generate(
        "GRAPH", chunk("CHK_1"), graph_row(), "natural-graph", "v1"
    )
    hybrid_candidate = TemplateGenerator().generate(
        "HYBRID", chunk("CHK_1"), graph_row(), "natural-hybrid", "v1"
    )

    assert "어떤 공시 기준" in graph_candidate["question"]
    assert "어떤 공시·금리옵션 경로로 연결되어 있나요" not in graph_candidate["question"]
    assert "얼마인지" in hybrid_candidate["question"]
    assert "원본 근거" in hybrid_candidate["question"]
