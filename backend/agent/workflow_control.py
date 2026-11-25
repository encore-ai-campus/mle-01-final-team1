"""최상위 작업 DAG의 상태 전이와 결과 검사를 담당한다. 금융 판단은 수행하지 않는다."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from agent.supervisor.candidate_budget import SEARCH_AGENTS, validate_candidate_output
from agent.supervisor.contracts import SupervisorContractError


TERMINAL_BLOCKERS = frozenset({"failed", "skipped"})
RESULT_STATUSES = frozenset({"completed", "needs_input", "failed"})
IDENTITY_KEYS = ("task_id", "run_id", "plan_revision", "scenario_id")


def new_report() -> dict[str, Any]:
    return {"mode": "connection_check", "status": "running", "stages": [], "tasks": {}, "waves": [], "replans": []}


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


def candidate_issue(task: Mapping[str, Any], result: dict) -> str | None:
    """Reuse the Supervisor-owned search-result contract; no candidate truncation."""
    if task.get("agent") not in SEARCH_AGENTS:
        return None
    try:
        validate_candidate_output(dict(task), result)
    except SupervisorContractError as exc:
        return str(exc)
    return None


def required_upstream(plan: dict, results: dict, task: Mapping[str, Any]) -> dict[str, Any]:
    """Only accepted, current, completed direct dependencies may cross the boundary."""
    by_id = {t["task_id"]: t for t in plan["tasks"]}
    accepted = {}
    for dep_id in task["depends_on"]:
        dep = by_id[dep_id]
        result = results.get(dep_id)
        if (dep.get("scenario_id") is not None and dep.get("scenario_id") != task.get("scenario_id")
                or dep["status"] != "completed"
                or not is_current_result(result, dep, plan["revision"])
                or result["status"] != "completed"
                or candidate_issue(dep, result) is not None):
            raise SupervisorContractError("Required upstream result is missing, stale, or invalid: " + dep_id)
        accepted[dep_id] = deepcopy(result)
    return accepted


def resolved_task_answers(state: Mapping[str, Any], task: Mapping[str, Any]) -> dict[str, Any]:
    """The server already validates answers; select this task's resolved questions only."""
    return {q["id"]: deepcopy(q["answers"])
            for q in state.get("pending_input") or []
            if isinstance(q, dict) and q.get("status") == "resolved"
            and q.get("target") == task["task_id"] and q.get("scenario_id") == task.get("scenario_id")
            and isinstance(q.get("id"), str) and isinstance(q.get("answers"), dict)
            and q["answers"]}


def review_issues(review: Any, run_id: Any) -> list[str]:
    if not isinstance(run_id, str) or not run_id or not isinstance(review, dict) or review.get("target_run_id") != run_id:
        return []
    issues = []
    for name in ("evidence", "reflection"):
        check = review.get(name)
        if not isinstance(check, dict) or check.get("status") != "revise":
            continue
        details = check.get("issues")
        strings = [v for v in details if isinstance(v, str) and v.strip()] if isinstance(details, list) else []
        issues.extend(strings or [name + ": revise"])
    return issues


def archive_review_feedback(old_results: dict, old_reviews: dict, current: dict,
                            previous: dict) -> dict:
    """Keep only metadata of invalidated reviewed runs, never copy their data."""
    archived = deepcopy(previous)
    for tid, output in old_results.items():
        if not isinstance(output, dict) or output.get("task_id") != tid:
            continue
        run_id = output.get("run_id")
        if tid in current and current[tid].get("run_id") == run_id:
            continue
        issues = review_issues(old_reviews.get(tid), run_id)
        if issues and type(output.get("plan_revision")) is int:
            archived[run_id] = {"task_id": tid, "scenario_id": output.get("scenario_id"),
                                "plan_revision": output["plan_revision"], "issues": issues}
    return archived


def revision_feedback(state: Mapping[str, Any], task: Mapping[str, Any]) -> dict | None:
    revision = state["execution_plan"]["revision"]
    records = [(run_id, record) for run_id, record in (state.get("superseded_results") or {}).items()
               if isinstance(record, dict) and record.get("task_id") == task["task_id"]
               and record.get("scenario_id") == task.get("scenario_id")
               and type(record.get("plan_revision")) is int and record["plan_revision"] in {revision, revision - 1}
               and isinstance(record.get("issues"), list) and record["issues"]]
    if not records:
        return None
    run_id, record = max(records, key=lambda pair: pair[1]["plan_revision"])
    return {"previous_run_id": run_id, "issues": deepcopy(record["issues"])}


def work_replan_tasks(state: Mapping[str, Any]) -> list[str]:
    """Skipped, unregistered tasks must not cause additional LLM calls."""
    return [t["task_id"] for t in (state.get("execution_plan") or {}).get("tasks", [])
            if t.get("status") in {"failed", "needs_input"} and t.get("run_id")]


def review_replan_tasks(state: Mapping[str, Any]) -> list[str]:
    plan = state.get("execution_plan") or {}
    results, reviews = state.get("agent_results") or {}, state.get("review_results") or {}
    return [t["task_id"] for t in plan.get("tasks", [])
            if t.get("status") == "completed"
            and is_current_result(results.get(t["task_id"]), t, plan["revision"])
            and results[t["task_id"]]["status"] == "completed"
            and review_issues(reviews.get(t["task_id"]), t.get("run_id"))]


def normalize_current(plan: dict, results: dict, reviews: dict) -> tuple[dict, dict, dict]:
    """이전 revision/run 결과를 현재 작업의 성공 근거로 사용하지 않는다."""
    plan = deepcopy(plan)
    current, current_reviews = {}, {}
    # 상위 그래프는 외부 작업이 실행 중인 체크포인트를 복구하지 않는다.
    for task in plan["tasks"]:
        tid = task["task_id"]
        result = results.get(tid)
        if (task["status"] in RESULT_STATUSES and is_current_result(result, task, plan["revision"])
                and result["status"] == task["status"]
                and candidate_issue(task, result) is None):
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
        issue = candidate_issue(task, result)
        if issue is not None:
            # A failed envelope communicates the real contract rejection, not a search result.
            result = failed_output(result, "CANDIDATE_CONTRACT_VIOLATION")
            result["warnings"].append(issue)
            results[tid] = result
            task["status"] = "failed"
            report["tasks"][tid] = {"status": "failed", "run_id": task["run_id"],
                                    "reason": "candidate_contract_violation", "issue": issue}
            continue
        results[tid] = result
        task["status"] = result["status"]
        diagnostic = {"status": result["status"], "run_id": result["run_id"]}
        if result["status"] == "needs_input":
            diagnostic["needed_fields"] = result["needed_fields"]
        if result["status"] == "failed":
            diagnostic["reason"] = ("adapter_prepare_failed" if any(
                w.startswith("ADAPTER_PREPARE_ERROR:") for w in result["warnings"])
                else "agent_execution_failed")
        report["tasks"][tid] = diagnostic
    return plan, results, report
