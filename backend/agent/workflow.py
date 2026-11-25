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
from agent.supervisor.contracts import validate_plan, execution_waves
from agent.supervisor.graph import build_main_supervisor_node
from agent.supervisor.tools.memory import latest_user_text
from agent.workflow_control import (
    collect_results, failed_output, is_current_result,
    normalize_current, record_stage, select_wave,
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


def _build_input(binding: AgentBinding, state: WorkflowState, task: dict) -> AgentInput:
    # 이 스냅샷은 내부 Python adapter 전용이다. LLM에는 선별된 AgentInput만 전달한다.
    snapshot = {k: deepcopy(v) for k, v in state.items() if k in MainState.__annotations__}
    dependencies = set(task["depends_on"])
    snapshot["agent_results"] = {k: v for k, v in snapshot.get("agent_results", {}).items() if k in dependencies}
    for key in ("agent_outbox", "review_results", "superseded_results", "final_advice"):
        snapshot.pop(key, None)
    selected = binding.prepare(snapshot, deepcopy(task))
    optional = {"profile_view", "upstream_results", "extra_answers", "prior_advice", "revision_feedback"}
    if not isinstance(selected, dict) or set(selected) - optional:
        raise ValueError("AgentBinding.prepare 반환 계약 위반")
    if not isinstance(selected.get("profile_view"), dict):
        raise ValueError("profile_view를 업무별로 선별해야 합니다.")
    if "upstream_results" in selected and (not isinstance(selected["upstream_results"], dict)
            or set(selected["upstream_results"]) - dependencies):
        raise ValueError("선행 작업 외 결과 참조 금지")
    return {
        **deepcopy(selected),
        "task_id": task["task_id"], "run_id": task["run_id"],
        "plan_revision": state["execution_plan"]["revision"],
        "scenario_id": task.get("scenario_id"), "objective": task["objective"],
        "user_query": latest_user_text(state) or "",
    }


def build_workflow(*, supervisor_factory: Callable[[], Any] | None = None,
                   agents: Mapping[str, AgentBinding] | None = None):
    """기존 MainState를 입력으로 받아 개발용 실행 보고서가 포함된 그래프를 컴파일한다.

    기본 agents={}이며 미등록 작업은 skipped, 필수 선행 작업 생략도 skipped 처리한다.
    등록된 작업만 Send로 병렬 배정한다. 그래프가 금융 검수/답변을 완료했다고 표시하지 않는다.
    """
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
        validate_plan(plan, scenario)
        plan, results, reviews = normalize_current(plan, state.get("agent_results") or {}, state.get("review_results") or {})
        report = record_stage(state, "plan_validator", "completed")
        report["planned_waves"] = execution_waves(plan, scenario)
        for task in plan["tasks"]:
            report["tasks"][task["task_id"]] = {"status": task["status"], "agent": task["agent"]}
        return {"execution_plan": plan, "agent_results": results, "review_results": reviews,
                "phase": "executing", "workflow_report": report}

    def dispatcher(state: WorkflowState) -> Command[Literal["task_runner", "skip_unavailable", "evidence"]]:
        plan = deepcopy(state["execution_plan"])
        report = record_stage(state, "dispatcher", "completed")
        wave = select_wave(plan, set(registry))
        by_id = {task["task_id"]: task for task in plan["tasks"]}
        if not wave:
            # needs_input에 걸린 후속 작업은 미실행 상태로 보존한다.
            for task in plan["tasks"]:
                if task["status"] == "pending":
                    report["tasks"][task["task_id"]] = {"status": "pending", "reason": "dependency_not_ready"}
            return Command(update={"workflow_report": report}, goto="evidence")
        report["waves"].append([{"task_id": tid, "action": action} for tid, action in wave])
        sends = []
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
                task.update(status="failed", run_id=None)
                report["tasks"][tid] = {"status": "failed", "reason": "adapter_prepare_failed", "error_type": type(exc).__name__}
                continue
            report["tasks"][tid] = {"status": "running", "run_id": task["run_id"]}
            sends.append(Send("task_runner", {"agent": task["agent"], "agent_input": payload}))
        return Command(update={"execution_plan": plan, "workflow_report": report},
                       goto=sends if sends else "skip_unavailable")

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

    def after_collect(state: WorkflowState):
        return "dispatcher" if select_wave(state["execution_plan"], set(registry)) else "evidence"

    def skipped_stage(name: str):
        def node(state: WorkflowState):
            return {"workflow_report": record_stage(state, name, "skipped", "stage_not_implemented")}
        return node

    def after_reflection(state: WorkflowState):
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
    graph.add_conditional_edges("collector", after_collect, {"dispatcher": "dispatcher", "evidence": "evidence"})
    graph.add_edge("evidence", "reflection")
    graph.add_conditional_edges("reflection", after_reflection, {"comparison": "comparison", "final_advice_stage": "final_advice_stage"})
    graph.add_edge("comparison", "final_advice_stage")
    graph.add_edge("final_advice_stage", "output_guard")
    graph.add_edge("output_guard", "renderer")
    graph.add_edge("renderer", END)
    graph.add_edge("ask_user", END)
    return graph.compile()
