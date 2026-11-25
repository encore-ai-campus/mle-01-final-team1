"""최상위 LangGraph를 조립한다. 기존 Supervisor를 사용하고 미등록 전문/검수 노드는 생략한다.

금융상담의 개별 기능은 구현하지 않는다. 개발용 연결 그래프이며 최종 금융 답변을 생성하지 않는다.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal, TypedDict
from uuid import uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send

from agent.contracts import AgentInput, AgentOutput
from agent.state import MainState, RESET_OUTBOX
from agent.supervisor.contracts import SupervisorContractError, execution_waves, validate_intent_scenarios
from agent.supervisor.candidate_budget import SEARCH_AGENTS, validate_candidate_limits, search_agent_input
from agent.supervisor.tools.profile_fields import build_profile_view, merge_goals, PROFILE_FIELD_SETS
from agent.supervisor.graph import build_main_supervisor_node
from agent.supervisor.tools.memory import latest_user_text
from agent.workflow_control import (
    collect_results, failed_output, is_current_result,
    normalize_current, record_stage, select_wave, required_upstream, resolved_task_answers,
    archive_review_feedback, revision_feedback, work_replan_tasks, review_replan_tasks,
)


class WorkflowState(MainState, total=False):
    """그래프 내부/개발 출력용 채널. 공용 MainState 계약에는 추가하지 않는다."""
    workflow_report: dict[str, Any]


@dataclass(frozen=True)
class AgentBinding:
    """향후 실제 Agent를 연결하는 포트. prepare는 코드 기반 필드 선택/변환만 수행한다."""
    prepare: Callable[[MainState, dict[str, Any]], dict[str, Any]]
    invoke: Callable[[AgentInput, RunnableConfig], AgentOutput]


class TaskInvocation(TypedDict):
    agent: str
    agent_input: AgentInput


def _default_supervisor():
    """요청마다 기존 Supervisor를 생성한다. 인증/DB는 별도 포트 주입 시에만 연결한다."""
    from agent.supervisor import Supervisor
    from agent.supervisor.tools.integration import bind_mvp_ports
    return Supervisor(ports=bind_mvp_ports())


def _task_scenario(state: Mapping[str, Any], task: Mapping[str, Any]) -> dict | None:
    sid = task.get("scenario_id")
    if sid is None:
        return None
    matches = [b for b in (state.get("scenario_state") or {}).get("scenarios", []) if b["id"] == sid]
    if len(matches) != 1:
        raise SupervisorContractError("Task scenario is not uniquely defined")
    return deepcopy(matches[0])


def profile_view_for_task(state: MainState, task: Mapping[str, Any], task_type: str,
                          **options: Any) -> dict[str, Any]:
    """Adapter helper: select an existing Supervisor Field Set, never infer it from text."""
    branch = _task_scenario(state, task)
    supplied = (state.get("intent") or {}).get("supplied_values") or {}
    overrides = (branch or {}).get("overrides") or {}
    assumptions = (branch or {}).get("assumptions") or {}
    if "goals" in PROFILE_FIELD_SETS[task_type][0] and ("goals" in supplied or "goals" in overrides):
        # Supervisor has already merged supplied goals; only branch overrides need merging.
        goals = supplied.get("goals", (state.get("basic_profile") or {}).get("goals"))
        if "goals" in overrides:
            goals = merge_goals(goals, overrides["goals"], replace=assumptions.get("goal_mode") == "replace")
        options["goal_view"] = {"goals": goals}
    view = build_profile_view(state, task_type, scenario=branch, **options)
    for key in ("holding_actions", "horizon_months", "allocation_pct", "notes"):
        if key in assumptions:
            view[key] = deepcopy(assumptions[key])
    return view


def _build_input(binding: AgentBinding, state: WorkflowState, task: dict) -> AgentInput:
    # Authoritative dependencies are not delegated to individual adapters.
    plan = state["execution_plan"]
    upstream = required_upstream(plan, state.get("agent_results") or {}, task)
    answers = resolved_task_answers(state, task)
    feedback = revision_feedback(state, task)
    branch = _task_scenario(state, task)
    snapshot = {k: deepcopy(v) for k, v in state.items() if k in MainState.__annotations__}
    snapshot["agent_results"] = deepcopy(upstream)
    # No other branch's assumptions, results, task instructions, or questions in the adapter snapshot.
    snapshot["scenario_state"] = ({**{k: deepcopy(v) for k, v in state["scenario_state"].items()
                                      if k not in {"scenarios", "preparation"}}, "scenarios": [branch]}
                                  if branch is not None else None)
    visible = set(task["depends_on"]) | {task["task_id"]}
    snapshot["execution_plan"] = {**deepcopy(plan), "tasks": [deepcopy(t) for t in plan["tasks"]
                                                              if t["task_id"] in visible]}
    snapshot["pending_input"] = [deepcopy(q) for q in state.get("pending_input") or []
                                 if q.get("scenario_id") in {None, task.get("scenario_id")}
                                 and q.get("target") in {"profile", "supervisor", task["task_id"]}]
    for key in ("agent_outbox", "review_results", "superseded_results", "final_advice", "post_response",
                "memory_candidates", "basic_profile_changes", "financial_profile_changes"):
        snapshot.pop(key, None)
    selected = binding.prepare(snapshot, deepcopy(task))
    optional = {"profile_view", "upstream_results", "extra_answers", "prior_advice", "revision_feedback"}
    if not isinstance(selected, dict) or set(selected) - optional:
        raise SupervisorContractError("AgentBinding.prepare returned unsupported fields")
    if not isinstance(selected.get("profile_view"), dict):
        raise SupervisorContractError("profile_view must be selected by the adapter")
    # Accept legacy adapters returning the authoritative values, but not omissions/substitutions.
    for name, value in (("upstream_results", upstream), ("extra_answers", answers), ("revision_feedback", feedback)):
        if name in selected and selected[name] != value:
            raise SupervisorContractError("Adapter cannot replace authoritative " + name)
    payload = {k: deepcopy(v) for k, v in selected.items() if k in {"profile_view", "prior_advice"}}
    payload.update(task_id=task["task_id"], run_id=task["run_id"], plan_revision=plan["revision"],
                   scenario_id=task.get("scenario_id"), objective=task["objective"],
                   user_query=latest_user_text(state) or "")
    if upstream:
        payload["upstream_results"] = upstream
    if answers:
        payload["extra_answers"] = answers
    if feedback:
        payload["revision_feedback"] = feedback
    if task["agent"] in SEARCH_AGENTS:
        payload = search_agent_input(task, payload)
    return payload


def build_workflow(*, supervisor_factory: Callable[[], Any] | None = None,
                   agents: Mapping[str, AgentBinding] | None = None,
                   max_execution_replans: int = 1):
    """기존 MainState를 입력으로 받아 개발용 실행 보고서가 포함된 그래프를 컴파일한다.

    기본 agents={}이며 미등록 작업은 skipped, 필수 선행 작업 생략도 skipped 처리한다.
    등록된 작업만 Send로 병렬 배정한다. 그래프가 금융 검수/답변을 완료했다고 표시하지 않는다.
    """
    if type(max_execution_replans) is not int or not 0 <= max_execution_replans <= 1:
        raise ValueError("max_execution_replans must be 0 or 1")
    factory = supervisor_factory or _default_supervisor
    registry = dict(agents or {})
    graph = StateGraph(WorkflowState, input_schema=MainState)

    def entry(state: WorkflowState):
        if not latest_user_text(state):
            raise ValueError("MainState.messages에 사용자 질문이 필요합니다.")
        return {"workflow_report": record_stage({}, "entry", "completed"),
                "agent_outbox": [RESET_OUTBOX], "final_advice": None}

    def supervisor(state: WorkflowState) -> Command[Literal["plan_validator", "ask_user", "final_advice_stage"]]:
        # 기존 Supervisor의 공개 부모 노드 연결 계약(Command)을 그대로 사용한다.
        command = build_main_supervisor_node(factory())(state)
        # LangGraph에서는 State Key와 동일한 이름의 노드를 등록할 수 없다.
        # 기존 Supervisor의 반환 계약은 유지하고 부모 그래프에서만 별칭으로 변환한다.
        report = record_stage(state, "supervisor", "completed")
        report["supervisor_route"] = command.goto
        target = "final_advice_stage" if command.goto == "final_advice" else command.goto
        return Command(update={**command.update, "workflow_report": report}, goto=target)

    def plan_validator(state: WorkflowState):
        plan, scenario = state.get("execution_plan"), state.get("scenario_state")
        validate_intent_scenarios(state.get("intent"), scenario)
        validate_candidate_limits(plan, scenario)
        plan, results, reviews = normalize_current(plan, state.get("agent_results") or {}, state.get("review_results") or {})
        report = record_stage(state, "plan_validator", "completed")
        report["planned_waves"] = execution_waves(plan, scenario)
        prior_tasks = report.get("tasks", {}) if report.get("plan_revision") == plan["revision"] else {}
        report["tasks"] = {}
        report["plan_revision"] = plan["revision"]
        archived = archive_review_feedback(state.get("agent_results") or {}, state.get("review_results") or {},
                                           results, state.get("superseded_results") or {})
        for task in plan["tasks"]:
            old = prior_tasks.get(task["task_id"], {})
            kept = old if old.get("status") == task["status"] and old.get("run_id") == task.get("run_id") else {}
            report["tasks"][task["task_id"]] = {**kept, "status": task["status"], "agent": task["agent"],
                                                  "run_id": task.get("run_id")}
        return {"execution_plan": plan, "agent_results": results, "review_results": reviews,
                "superseded_results": archived, "phase": "executing", "workflow_report": report}

    def dispatcher(state: WorkflowState) -> Command[Literal["task_runner", "skip_unavailable", "evidence", "replan_execution"]]:
        plan = deepcopy(state["execution_plan"])
        report = record_stage(state, "dispatcher", "completed")
        wave = select_wave(plan, set(registry))
        by_id = {task["task_id"]: task for task in plan["tasks"]}
        if not wave:
            # needs_input에 걸린 후속 작업은 미실행 상태로 보존한다.
            for task in plan["tasks"]:
                if task["status"] == "pending":
                    report["tasks"][task["task_id"]] = {"status": "pending", "reason": "dependency_not_ready"}
            return Command(update={"workflow_report": report}, goto=after_work(state))
        report["waves"].append([{"task_id": tid, "action": action} for tid, action in wave])
        sends = []
        preparation_failures = []
        for tid, action in wave:
            task = by_id[tid]
            if action != "execute":
                task.update(status="skipped", run_id=None)
                report["tasks"][tid] = {"status": "skipped", "reason": action}
                continue
            task.update(status="running", run_id=f"run_{uuid4().hex}")
            try:
                payload = _build_input(registry[task["agent"]], state, task)
            except Exception as exc:
                # 예외 본문에 금융정보가 포함될 수 있으므로 오류 유형만 출력한다.
                # Keep the issued identity so Collector and Supervisor see the actual failure.
                identity = {"task_id": tid, "run_id": task["run_id"], "plan_revision": plan["revision"],
                            "scenario_id": task.get("scenario_id")}
                preparation_failures.append(failed_output(identity, "ADAPTER_PREPARE_ERROR:" + type(exc).__name__))
                report["tasks"][tid] = {"status": "running", "run_id": task["run_id"],
                                         "reason": "adapter_prepare_failed", "error_type": type(exc).__name__}
                continue
            report["tasks"][tid] = {"status": "running", "run_id": task["run_id"]}
            sends.append(Send("task_runner", {"agent": task["agent"], "agent_input": payload}))
        update = {"execution_plan": plan, "workflow_report": report}
        if preparation_failures:
            update["agent_outbox"] = preparation_failures
        return Command(update=update, goto=sends if sends else "skip_unavailable")

    def task_runner(state: TaskInvocation, config: RunnableConfig):
        payload = deepcopy(state["agent_input"])
        try:
            output = registry[state["agent"]].invoke(deepcopy(payload), config)
            # 반환의 ID를 덮어써서 잘못된 결과를 수락하지 않는다.
            if not is_current_result(output, payload, payload["plan_revision"]):
                output = failed_output(payload, "INVALID_AGENT_OUTPUT")
        except Exception as exc:
            output = failed_output(payload, f"AGENT_ERROR:{type(exc).__name__}")
        return {"agent_outbox": [output]}

    def skip_unavailable(state: WorkflowState):
        return {"workflow_report": record_stage(state, "skip_unavailable", "skipped", "no_callable_task_in_wave")}

    def collector(state: WorkflowState):
        plan, results, report = collect_results(state["execution_plan"], state.get("agent_results") or {},
                                               state.get("agent_outbox") or [], state["workflow_report"])
        report["stages"].append({"stage": "collector", "status": "completed"})
        return {"execution_plan": plan, "agent_results": results, "agent_outbox": [RESET_OUTBOX], "workflow_report": report}

    def after_work(state: WorkflowState):
        attempts = sum(r["reason"] == "execution_blocked" for r in state["workflow_report"]["replans"])
        if work_replan_tasks(state) and attempts < max_execution_replans:
            return "replan_execution"
        return "evidence"

    def after_collect(state: WorkflowState):
        # Drain independent work first; only actual failures or needs_input request a replan.
        return "dispatcher" if select_wave(state["execution_plan"], set(registry)) else after_work(state)

    def replan_node(reason: str):
        def node(state: WorkflowState):
            report = record_stage(state, "replan", "completed", reason)
            tasks = review_replan_tasks(state) if reason == "review_rejected" else work_replan_tasks(state)
            report["replans"].append({"reason": reason, "task_ids": tasks,
                                      "plan_revision": state["execution_plan"]["revision"],
                                      "diagnostics": {tid: deepcopy(report["tasks"].get(tid, {})) for tid in tasks}})
            return {"workflow_report": report}
        return node

    def skipped_stage(name: str):
        def node(state: WorkflowState):
            return {"workflow_report": record_stage(state, name, "skipped", "stage_not_implemented")}
        return node

    def after_reflection(state: WorkflowState):
        # The review stages remain unimplemented. This route is activated only by a real current-run revise.
        plan = state.get("execution_plan") or {}
        if (review_replan_tasks(state) and plan.get("review_retry_count", 0) < 1
                and not any(r["reason"] == "review_rejected" for r in state["workflow_report"]["replans"])):
            return "replan_review"
        branches = (state.get("scenario_state") or {}).get("scenarios", [])
        return "comparison" if len(branches) > 1 else "final_advice_stage"

    def final_advice(state: WorkflowState):
        # 검수/답변 미구현 상태에서 가짜 final_advice를 만들지 않는다.
        return {"phase": "composing", "final_advice": None,
                "workflow_report": record_stage(state, "final_advice", "skipped", "stage_not_implemented")}

    def renderer(state: WorkflowState):
        report = record_stage(state, "renderer", "skipped", "stage_not_implemented")
        report["status"] = "connection_check_completed"
        report["financial_advice_available"] = False
        report["message"] = "그래프 연결 확인을 종료했습니다. 금융 상담 답변 및 검수 기능은 아직 연결되지 않았습니다."
        for task in (state.get("execution_plan") or {}).get("tasks", []):
            diagnostic = report["tasks"].setdefault(task["task_id"], {"status": task["status"]})
            diagnostic.update(agent=task["agent"], scenario_id=task.get("scenario_id"), depends_on=task["depends_on"])
            if task["status"] == "pending":
                diagnostic.setdefault("reason", "dependency_not_ready")
        # 연결 테스트는 정상 종료했어도 금융 상담은 완료되지 않았다.
        return {"phase": "failed", "final_advice": None, "workflow_report": report}

    def ask_user(state: WorkflowState):
        report = record_stage(state, "ask_user", "completed")
        report.update(status="waiting_for_user", financial_advice_available=False,
                      message="Supervisor가 추가 정보를 요청했습니다.")
        return {"phase": "waiting_for_user", "final_advice": None, "workflow_report": report}

    graph.add_node("entry", entry)
    graph.add_node("supervisor", supervisor)
    graph.add_node("plan_validator", plan_validator)
    graph.add_node("dispatcher", dispatcher)
    graph.add_node("task_runner", task_runner)
    graph.add_node("skip_unavailable", skip_unavailable)
    graph.add_node("collector", collector)
    graph.add_node("replan_execution", replan_node("execution_blocked"))
    graph.add_node("replan_review", replan_node("review_rejected"))
    graph.add_node("evidence", skipped_stage("evidence"))
    graph.add_node("reflection", skipped_stage("reflection"))
    graph.add_node("comparison", skipped_stage("comparison"))
    graph.add_node("final_advice_stage", final_advice)
    graph.add_node("output_guard", skipped_stage("output_guard"))
    graph.add_node("renderer", renderer)
    graph.add_node("ask_user", ask_user)
    graph.add_edge(START, "entry")
    graph.add_edge("entry", "supervisor")
    # supervisor/dispatcher는 Command로 다음 노드를 정한다. 정적 edge를 추가하지 않는다.
    graph.add_edge("plan_validator", "dispatcher")
    graph.add_edge("task_runner", "collector")
    graph.add_edge("skip_unavailable", "collector")
    graph.add_conditional_edges("collector", after_collect, {"dispatcher": "dispatcher", "evidence": "evidence",
                                                           "replan_execution": "replan_execution"})
    graph.add_edge("replan_execution", "supervisor")
    graph.add_edge("replan_review", "supervisor")
    graph.add_edge("evidence", "reflection")
    graph.add_conditional_edges("reflection", after_reflection, {"comparison": "comparison", "final_advice_stage": "final_advice_stage",
                                                                "replan_review": "replan_review"})
    graph.add_edge("comparison", "final_advice_stage")
    graph.add_edge("final_advice_stage", "output_guard")
    graph.add_edge("output_guard", "renderer")
    graph.add_edge("renderer", END)
    graph.add_edge("ask_user", END)
    return graph.compile()
