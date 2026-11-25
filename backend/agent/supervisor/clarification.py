# Supervisor의 사용자 확인 질문 발행/재개 계약. 금융 판단이나 자연어 추출은 하지 않는다.
from __future__ import annotations

from copy import deepcopy
from typing import Any

from .contracts import SupervisorContractError, validate_questions
from .tools.profile_fields import canonical_profile_path, validate_profile


def _scope(question: dict[str, Any]) -> tuple[str, str | None, frozenset[str]]:
    fields = []
    for field in question.get("fields", []):
        try:
            fields.append(canonical_profile_path(field))
        except SupervisorContractError:
            fields.append(field)
    return (question.get("target", "supervisor"), question.get("scenario_id"), frozenset(fields))


def merge_pending_questions(items: Any, source: dict[str, Any],
                            scenario: dict[str, Any] | None) -> list[dict[str, Any]]:
    """정확히 동일한 확인 항목은 한 번만 발행하고 기존 질문 ID/답변을 보존한다.

    질문 내용의 필요성은 LLM 판단이다. Python은 대상·분기·ID·필드의 정합성만 확인한다.
    """
    old = deepcopy(source.get("pending_input") or [])
    by_id = {q["id"]: q for q in old if isinstance(q, dict) and isinstance(q.get("id"), str)}
    if len(by_id) != len(old):
        raise SupervisorContractError("Existing pending_input IDs are invalid or duplicated")
    drafts = validate_questions(items, reserved_ids=set(by_id))
    branches = {item["id"] for item in (scenario or {}).get("scenarios", [])}
    tasks = {task["task_id"]: task for task in (source.get("execution_plan") or {}).get("tasks", [])}
    for q in drafts:
        target = q["target"]
        if target not in {"profile", "supervisor"}:
            if target not in tasks:
                raise SupervisorContractError("pending_input target must be profile, supervisor, or an existing task ID")
            task_branch = tasks[target].get("scenario_id")
            if q["scenario_id"] is None:
                q["scenario_id"] = task_branch
            elif q["scenario_id"] != task_branch:
                raise SupervisorContractError("pending_input target and scenario_id do not match")
        if q["scenario_id"] is not None and q["scenario_id"] not in branches:
            raise SupervisorContractError("pending_input references an unknown scenario_id")
        previous = by_id.get(q["id"])
        if previous is not None:
            if previous.get("status") == "resolved":
                raise SupervisorContractError("A resolved question ID cannot be reused")
            if _scope(previous) != _scope(q):
                raise SupervisorContractError("Use a new question ID for different target, fields, or scenario")
        # Keep the first stable question even if the model supplies a new ID or slightly different wording.
        duplicate = next((prior for prior in by_id.values()
                          if prior.get("status") == "pending" and _scope(prior) == _scope(q)), None)
        if duplicate is not None:
            # 같은 ID로 재질문할 때 표현은 다듬을 수 있다(필드와 범위는 유지).
            if previous is duplicate:
                duplicate["question"] = q["question"]
            continue
        # Do not merge questions with partially overlapping natural-language meaning automatically.
        for prior in by_id.values():
            if prior.get("status") != "pending":
                continue
            a, b = _scope(prior), _scope(q)
            if a[:2] == b[:2] and a[2] & b[2]:
                raise SupervisorContractError("Overlapping pending fields in the same scope; ask only the remaining fields")
        by_id[q["id"]] = q
    return list(by_id.values())


def resolve_validated_answers(source: dict[str, Any], answers_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Entry Router 연동용: 이미 해석·검증된 답변만 기존 질문에 연결한다.

    자연어 답변의 의미 판단, 인증, DB 저장, 실행 재개 자체는 외부 책임이다.
    이 함수는 원본을 변경하거나 실제 프로필에 값을 쓰지 않는다.
    """
    pending = deepcopy(source.get("pending_input") or [])
    by_id = {q["id"]: q for q in pending if isinstance(q, dict) and isinstance(q.get("id"), str)}
    if len(by_id) != len(pending) or not isinstance(answers_by_id, dict) or not answers_by_id:
        raise SupervisorContractError("Invalid pending_input or answers_by_id")
    for qid, answers in answers_by_id.items():
        q = by_id.get(qid)
        if q is None or q.get("status") != "pending":
            raise SupervisorContractError("Answers must reference an existing pending question")
        fields = q.get("fields")
        if (not isinstance(fields, list) or not fields or not isinstance(answers, dict)
                or set(answers) != set(fields)):
            raise SupervisorContractError("Answers must cover exactly the question fields")
        for field, value in answers.items():
            if value is None or (isinstance(value, str) and not value.strip()):
                raise SupervisorContractError("A required answer is still unknown")
            try:
                path = canonical_profile_path(field)
            except SupervisorContractError:
                continue  # caller has validated non-profile values
            domain, key = path.split(".")
            validate_profile(domain, {key: value})
        q["answers"] = deepcopy(answers)
        q["status"] = "resolved"
    return {"pending_input": pending}


def has_answered_waiting_turn(source: dict[str, Any]) -> bool:
    """Entry Router는 승인된 답변을 붙인 뒤 기존 waiting_for_user phase를 유지한다."""
    return source.get("phase") == "waiting_for_user" and any(
        isinstance(q, dict) and q.get("status") == "resolved" and bool(q.get("answers"))
        for q in source.get("pending_input") or [])
