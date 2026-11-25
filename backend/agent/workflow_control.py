"""최상위 작업 DAG의 상태 전이와 결과 검사를 담당한다. 금융 판단은 수행하지 않는다."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping


TERMINAL_BLOCKERS = frozenset({"failed", "skipped"})
RESULT_STATUSES = frozenset({"completed", "needs_input", "failed"})
IDENTITY_KEYS = ("task_id", "run_id", "plan_revision", "scenario_id")


def new_report() -> dict[str, Any]:
    return {"mode": "connection_check", "status": "running", "stages": [], "tasks": {}, "waves": []}


def record_stage(state: Mapping[str, Any], name: str, status: str, reason: str | None = None) -> dict[str, Any]:
    report = deepcopy(state.get("workflow_report") or new_report())
    event = {"stage": name, "status": status}
    if reason:
        event["reason"] = reason
    report["stages"].append(event)
    return report


def is_current_result(result: Any, task: Mapping[str, Any], revision: int) -> bool:
    """현재 task/run/revision/scenario에 대응하는 정식 AgentOutput인지 확인한다."""
    return (
        isinstance(result, dict)
        and isinstance(task.get("run_id"), str) and bool(task["run_id"])
        and all(key in result for key in IDENTITY_KEYS)
        and result["task_id"] == task["task_id"]
        and result["run_id"] == task["run_id"]
        and type(result["plan_revision"]) is int and result["plan_revision"] == revision
        and result["scenario_id"] == task.get("scenario_id")
        and isinstance(result.get("status"), str) and result["status"] in RESULT_STATUSES
        and isinstance(result.get("summary"), str)
        and isinstance(result.get("data"), dict)
        and all(isinstance(result.get(key), list) and all(isinstance(x, str) for x in result[key])
                for key in ("evidence_refs", "warnings", "needed_fields"))
    )


def normalize_current(plan: dict, results: dict, reviews: dict) -> tuple[dict, dict, dict]:
    """이전 revision/run 결과를 현재 작업의 성공 근거로 사용하지 않는다."""
    plan = deepcopy(plan)
    current, current_reviews = {}, {}
    # 상위 그래프는 외부 작업이 실행 중인 체크포인트를 복구하지 않는다.
    for task in plan["tasks"]:
        tid = task["task_id"]
        result = results.get(tid)
        if (task["status"] in RESULT_STATUSES and is_current_result(result, task, plan["revision"])
                and result["status"] == task["status"]):
            current[tid] = deepcopy(result)
            review = reviews.get(tid)
            if task["status"] == "completed" and isinstance(review, dict) and review.get("target_run_id") == task["run_id"]:
                current_reviews[tid] = deepcopy(review)
        elif task["status"] in {"running", "completed", "needs_input"}:
            task.update(status="pending", run_id=None)
    # 현재 완료 작업도 선행 결과가 유효하지 않으면 재사용하지 않는다.
    by_id = {task["task_id"]: task for task in plan["tasks"]}
    changed = True
    while changed:
        changed = False
        for task in plan["tasks"]:
            if task["status"] in {"completed", "needs_input"} and any(by_id[dep]["status"] != "completed" for dep in task["depends_on"]):
                task.update(status="pending", run_id=None)
                current.pop(task["task_id"], None)
                current_reviews.pop(task["task_id"], None)
                changed = True
    return plan, current, current_reviews


def select_wave(plan: dict, registered: set[str]) -> list[tuple[str, str]]:
    """한 번에 실행할 작업을 선택한다. 선행 작업 생략/실패는 성공으로 간주하지 않는다."""
    by_id = {task["task_id"]: task for task in plan["tasks"]}
    wave = []
    for task in plan["tasks"]:
        if task["status"] != "pending":
            continue
        dep_status = [by_id[dep]["status"] for dep in task["depends_on"]]
        if any(status in TERMINAL_BLOCKERS for status in dep_status):
            wave.append((task["task_id"], "dependency_unavailable"))
        elif all(status == "completed" for status in dep_status):
            wave.append((task["task_id"], "execute" if task["agent"] in registered else "agent_not_registered"))
    return wave


def failed_output(agent_input: dict, code: str) -> dict[str, Any]:
    """실제 호출 실패를 공통 Envelope로 표현한다. 성공/검색 결과는 생성하지 않는다."""
    return {**{key: agent_input[key] for key in IDENTITY_KEYS}, "status": "failed",
            "summary": "전문 작업 실행 실패", "data": {}, "evidence_refs": [],
            "warnings": [code], "needed_fields": []}


def collect_results(plan: dict, previous: dict, outbox: list, report: dict) -> tuple[dict, dict, dict]:
    """병렬 결과는 여기서만 agent_results에 채택한다. 누락/중복/오래된 결과를 거부한다."""
    plan, results, report = deepcopy(plan), deepcopy(previous), deepcopy(report)
    for task in plan["tasks"]:
        if task["status"] != "running":
            continue
        tid = task["task_id"]
        matches = [r for r in outbox if is_current_result(r, task, plan["revision"])]
        if len(matches) != 1:
            task["status"] = "failed"
            results.pop(tid, None)
            report["tasks"][tid] = {"status": "failed", "reason": "missing_or_duplicate_current_result"}
            continue
        result = deepcopy(matches[0])
        results[tid] = result
        task["status"] = result["status"]
        diagnostic = {"status": result["status"], "run_id": result["run_id"]}
        if result["status"] == "needs_input":
            diagnostic["needed_fields"] = result["needed_fields"]
        if result["status"] == "failed":
            diagnostic["reason"] = "agent_execution_failed"
        report["tasks"][tid] = diagnostic
    return plan, results, report
