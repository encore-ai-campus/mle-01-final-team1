"""후보 상한을 Python에서 배정하고 Dispatcher/Collector에 전달하는 계약.

후보는 실행 분기가 아니다. 검색 에이전트 종류별, 현재 계획 단위로 제한한다.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from .contracts import SupervisorContractError, validate_plan, scenario_plan_view

SEARCH_AGENTS = frozenset({"product", "policy"})
DEFAULT_CANDIDATE_LIMIT = 3
MAX_CANDIDATES_PER_AGENT = 6


def candidate_limits(plan: dict[str, Any], scenario: dict[str, Any] | None) -> dict[str, int]:
    """검증된 전체 DAG에 후보 슬롯을 배정한다(LLM 호출 없음).

    분기 내 여러 검색 작업은 슬롯을 나눠 쓴다. 공통 검색은 사용하는 모든
    분기의 슬롯에서 차감한다. 각 검색에 최소 1개가 불가능하면 계획을 반려한다.
    """
    validate_plan(plan, scenario)
    branches = (scenario or {}).get("scenarios", [])
    active = [b["id"] for b in branches] or [None]
    per_branch = min(DEFAULT_CANDIDATE_LIMIT, MAX_CANDIDATES_PER_AGENT // len(active))
    scopes: dict[str, set[str | None]] = {}
    if branches:
        view = scenario_plan_view(plan, scenario)
        for branch in view["branches"]:
            for tid in branch["task_ids"] + branch["common_dependency_ids"]:
                scopes.setdefault(tid, set()).add(branch["scenario_id"])
    else:
        scopes = {t["task_id"]: {None} for t in plan["tasks"]}
    limits: dict[str, int] = {}
    for agent in sorted(SEARCH_AGENTS):
        remaining = {sid: per_branch for sid in active}
        tasks = sorted(t["task_id"] for t in plan["tasks"] if t["agent"] == agent)
        # 먼저 모든 검색 작업에 1개씩 보장한다. 초과 시 다른 필수 작업을
        # 조용히 삭제하지 않고 LLM에 작업 통합/간소화를 요청한다.
        for tid in tasks:
            for sid in scopes[tid]:
                remaining[sid] -= 1
            limits[tid] = 1
        if any(v < 0 for v in remaining.values()):
            raise SupervisorContractError(f"{agent} 검색 작업 수가 분기별 후보 슬롯({per_branch})을 초과했습니다. 중복 조회를 통합하세요.")
        # 결정론적 라운드로빈. task_id 순서로 남은 슬롯을 분배한다.
        while True:
            changed = False
            for tid in tasks:
                if all(remaining[sid] > 0 for sid in scopes[tid]):
                    limits[tid] += 1
                    for sid in scopes[tid]:
                        remaining[sid] -= 1
                    changed = True
            if not changed:
                break
    return limits


def apply_candidate_limits(plan: dict[str, Any], scenario: dict[str, Any] | None,
                           previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """서버 소유의 task.candidate_limit을 추가한다. 이전 상한이 다르면 재실행한다."""
    plan = deepcopy(plan)
    limits = candidate_limits(plan, scenario)
    for task in plan["tasks"]:
        task.pop("candidate_limit", None)
        if task["task_id"] in limits:
            task["candidate_limit"] = limits[task["task_id"]]
    if previous and previous.get("revision") == plan["revision"]:
        old = {t["task_id"]: t.get("candidate_limit") for t in previous.get("tasks", [])}
        if any(old.get(t["task_id"]) != t.get("candidate_limit") for t in plan["tasks"]):
            # v15 결과 또는 상한 변경 이전 결과를 새로운 계약의 결과로 재사용하지 않는다.
            plan["revision"] += 1
            for task in plan["tasks"]:
                task.update(status="pending", run_id=None)
    return plan


def validate_candidate_limits(plan: dict[str, Any], scenario: dict[str, Any] | None) -> None:
    expected = candidate_limits(plan, scenario)
    for task in plan["tasks"]:
        tid = task["task_id"]
        if tid in expected:
            if type(task.get("candidate_limit")) is not int or task["candidate_limit"] != expected[tid]:
                raise SupervisorContractError("서버 후보 상한이 누락되었거나 변조되었습니다.")
        elif "candidate_limit" in task:
            raise SupervisorContractError("Fund에는 후보 상한을 배정하지 않습니다.")


def _limit(task: dict[str, Any]) -> int:
    limit = task.get("candidate_limit")
    if task.get("agent") not in SEARCH_AGENTS or type(limit) is not int or not 1 <= limit <= DEFAULT_CANDIDATE_LIMIT:
        raise SupervisorContractError("유효한 검색 작업과 서버 candidate_limit이 필요합니다.")
    return limit


def search_agent_input(task: dict[str, Any], agent_input: dict[str, Any]) -> dict[str, Any]:
    """Dispatcher용 어댑터. 공용 입력을 복사하여 숫자 상한만 추가한다.

    전문 검색 구현은 이 숫자를 검색/순위 결정/반환 단계에서 사용한다.
    작업 목적에 같은 숫자를 자연어로 중복 작성하지 않는다.
    """
    if (agent_input.get("task_id") != task.get("task_id")
            or agent_input.get("scenario_id") != task.get("scenario_id")):
        raise SupervisorContractError("검색 작업과 AgentInput 참조가 일치하지 않습니다.")
    if task.get("run_id") is not None and agent_input.get("run_id") != task["run_id"]:
        raise SupervisorContractError("검색 작업과 AgentInput run_id가 일치하지 않습니다.")
    return {**deepcopy(agent_input), "candidate_limit": _limit(task)}


def validate_candidate_output(task: dict[str, Any], result: dict[str, Any]) -> None:
    """Collector용 검증. 초과 후보를 조용히 자르거나 Fund에 전달하지 않는다.

    검색 Agent 어댑터는 완료 결과의 data.candidates를 정규화한다(사실 조회는 []).
    근거/evidence_refs는 후보 상한 대상이 아니다. 기존 실행 ID 검증도 별도로 유지한다.
    """
    limit = _limit(task)
    if result.get("task_id") != task.get("task_id") or result.get("scenario_id") != task.get("scenario_id"):
        raise SupervisorContractError("검색 결과의 작업/분기 참조가 일치하지 않습니다.")
    if task.get("run_id") is not None and result.get("run_id") != task["run_id"]:
        raise SupervisorContractError("검색 결과의 run_id가 일치하지 않습니다.")
    data = result.get("data")
    if result.get("status") != "completed" and (not isinstance(data, dict) or "candidates" not in data):
        return
    candidates = data.get("candidates") if isinstance(data, dict) else None
    if not isinstance(candidates, list) or any(not isinstance(c, dict) for c in candidates):
        raise SupervisorContractError("검색 완료 결과에는 data.candidates[]가 필요합니다.")
    if len(candidates) > limit:
        raise SupervisorContractError(f"후보 상한 초과: {len(candidates)} > {limit}. 해당 검색 작업을 수정/재실행하세요.")
