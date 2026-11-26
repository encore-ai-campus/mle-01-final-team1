"""사람 검수 결과와 수정 이력을 안전하게 저장하는 기능."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .golden_candidate_pipeline import RuleBasedCritic


REVIEW_ACTIONS = {"approve", "revise", "reject", "hold"}
REVIEW_STATUSES = {"pending", "approved", "revised", "rejected", "on_hold"}
EDITABLE_FIELDS = {"question", "reference", "expected_facts"}


def apply_review_action(
    candidate: dict[str, Any],
    *,
    action: str,
    reviewer: str,
    contexts: dict[str, dict[str, Any]],
    graph_rows: dict[str, dict[str, Any]],
    revised_fields: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if action not in REVIEW_ACTIONS:
        raise ValueError(f"지원하지 않는 검수 작업입니다: {action}")
    if not reviewer.strip():
        raise ValueError("reviewer는 비어 있을 수 없습니다.")

    before = copy.deepcopy(candidate)
    updated = copy.deepcopy(candidate)
    if action == "revise":
        for field, value in (revised_fields or {}).items():
            if field not in EDITABLE_FIELDS:
                raise ValueError(f"수정할 수 없는 필드입니다: {field}")
            updated[field] = value
        updated["review_status"] = "revised"
    elif action == "approve":
        updated["review_status"] = "approved"
    elif action == "reject":
        updated["review_status"] = "rejected"
    else:
        updated["review_status"] = "on_hold"

    updated["critic"] = RuleBasedCritic().critique(updated, contexts, graph_rows)
    validation = updated["critic"]
    if action == "approve" and validation["verdict"] != "pass":
        raise ValueError(
            "자동 근거 검증을 통과하지 못한 후보는 승인할 수 없습니다: "
            + ", ".join(validation["reasons"])
        )
    history = {
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "reviewer": reviewer,
        "candidate_id": candidate["candidate_id"],
        "action": action,
        "before": before,
        "after": updated,
        "automatic_validation": validation,
    }
    return updated, history


def append_review_history(output_dir: Path, history: dict[str, Any]) -> None:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "review_history.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(history, ensure_ascii=False) + "\n")


def write_reviewed_candidates(output_dir: Path, candidates: list[dict[str, Any]]) -> None:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "reviewed_candidates.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in candidates) + "\n",
        encoding="utf-8",
    )
