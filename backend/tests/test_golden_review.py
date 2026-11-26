import json

import pytest

from services.golden_candidate_pipeline import generate_candidates, write_candidate_outputs
from services.golden_review import apply_review_action, append_review_history


def make_candidate():
    chunks = [
        {
            "chunk_id": "CHK_1",
            "evidence_id": "EVD_1",
            "chunk_type": "rate_option",
            "text": "테스트예금 12개월 기본금리 3.6%, 최고금리 3.6%",
            "metadata": {"product_name": "테스트예금", "base_rate": 3.6, "max_rate": 3.6},
            "source_link": {"status": "matched", "original": {"file": "정기예금.json"}},
            "entity_ids": ["p-1"],
        }
    ]
    candidates, _ = generate_candidates(
        chunks,
        [],
        counts={"VECTOR": 1, "GRAPH": 0, "HYBRID": 0},
        snapshot_version="v1",
        run_id="review-test",
    )
    return candidates[0], {"CHK_1": chunks[0]}, {}


def test_revision_is_revalidated_and_history_is_append_only(tmp_path):
    candidate, contexts, graph_rows = make_candidate()
    revised, history = apply_review_action(
        candidate,
        action="revise",
        reviewer="tester",
        contexts=contexts,
        graph_rows=graph_rows,
        revised_fields={"question": "테스트예금의 12개월 기본금리는 얼마인가요?"},
    )

    assert revised["review_status"] == "revised"
    assert revised["critic"]["verdict"] == "pass"
    assert history["before"]["question"] != history["after"]["question"]

    append_review_history(tmp_path, history)
    stored = json.loads((tmp_path / "review_history.jsonl").read_text(encoding="utf-8"))
    assert stored["action"] == "revise"


def test_invalid_approval_is_rejected(tmp_path):
    candidate, contexts, graph_rows = make_candidate()
    revised, _ = apply_review_action(
        candidate,
        action="revise",
        reviewer="tester",
        contexts=contexts,
        graph_rows=graph_rows,
        revised_fields={"reference": "근거에 없는 금리는 9.9%입니다."},
    )
    with pytest.raises(ValueError, match="자동 근거 검증"):
        apply_review_action(
            revised,
            action="approve",
            reviewer="tester",
            contexts=contexts,
            graph_rows=graph_rows,
        )


def test_candidate_batch_is_not_overwritten(tmp_path):
    candidate, _, _ = make_candidate()
    write_candidate_outputs(tmp_path, [candidate], {"candidate_count": 1})
    with pytest.raises(FileExistsError):
        write_candidate_outputs(tmp_path, [candidate], {"candidate_count": 1})
