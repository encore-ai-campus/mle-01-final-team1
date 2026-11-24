# Supervisor 최상위 본체 및 직접 실행 진입점. 실제 노드는 nodes/에서 담당한다.
# 상위 State 계약과 실행 책임의 경계를 유지한다.
from __future__ import annotations

# VS Code의 "Python 파일 실행"도 지원한다. 모듈(-m) 실행에는 영향을 주지 않는다.
if __name__ == "__main__" and not __package__:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # backend/
    __package__ = "agent.supervisor"
from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any
from .contracts import Decision, SupervisorOutcome, SupervisorContractError, execution_waves, scenario_plan_view
from .state import SupervisorWorkingState, copy_main_source, create_working_state
from .tools.ports import SupervisorPorts
from .tools.profile_fields import validate_profile, FIELDS
from .tools.memory import initial_memory
from .nodes import reason as reason_node, tools as tools_node, plan as plan_node, finalize as finalize_node

DecisionEngine = Callable[[dict[str, Any]], Decision | dict[str, Any]]

class Supervisor:
    """Profile/Memory Port는 바깥에서 주입하고, 4개 노드를 실행한다."""

    def __init__(
        self,
        decide: DecisionEngine | None = None,
        ports: SupervisorPorts | None = None,
        *,
        max_tool_calls: int = 6,
        max_reason_calls: int = 12,
        max_repairs: int = 1,
        trace_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if min(max_tool_calls, max_reason_calls, max_repairs) < 0 or max_reason_calls < 1:
            raise ValueError("Supervisor 반복 호출 상한 설정이 올바르지 않습니다.")
        if decide is None:
            from .llm.openai_api import OpenAIDecider
            decide = OpenAIDecider.from_env()
        self.decider = decide
        self.ports = ports or SupervisorPorts()
        self.max_tool_calls = max_tool_calls
        self.max_reason_calls = max_reason_calls
        self.max_repairs = max_repairs
        # 진단용 콜백은 요청별 인스턴스에만 연결한다. 운영에서는 기본 비활성화.
        self.trace_sink = trace_sink
        self._compiled_graph: Any = None

    def _emit(self, node: str, output: dict[str, Any], *, route: str | None = None) -> None:
        """공개된 구조화 출력만 추적한다. 내부 추론/인증정보는 기록하지 않는다."""
        if self.trace_sink is not None:
            event = {"node": node, "output": deepcopy(output)}
            if route is not None:
                event["route"] = route
            self.trace_sink(event)

    def initial(self, main_state: Mapping[str, Any]) -> SupervisorWorkingState:
        for domain in FIELDS:
            if domain in main_state:
                validate_profile(domain, main_state[domain])
        source = copy_main_source(main_state)
        previous = source.get("memory") or {}
        memory = initial_memory(source, self.ports)
        if memory is not None:
            source["memory"] = memory
        reused = (memory is not None and previous.get("retrieval_turn_id") == source.get("turn_id")
                  and previous.get("retrieval_consultation_id") == source.get("consultation_id")
                  and previous.get("prefetch_status") in {"found", "no_match", "failed", "access_denied"})
        self._emit("initial_memory", {"reused": reused, "memory": memory}, route="reason")
        return create_working_state(source)

    def reason(self, state: SupervisorWorkingState) -> dict[str, Any]:
        result = reason_node.run(self, state)
        self._emit("reason", result, route=reason_node.route({**state, **result}))
        return result

    @staticmethod
    def reason_route(state: SupervisorWorkingState) -> str:
        return reason_node.route(state)

    def tools(self, state: SupervisorWorkingState) -> dict[str, Any]:
        try:
            result = tools_node.run(self, state)
        except SupervisorContractError as exc:
            if state.get("tool_calls", 0) >= self.max_tool_calls:
                raise
            decision = state["decision"]
            result = {"observations": [*state.get("observations", []), {
                "name": decision["tool_name"], "request": deepcopy(decision["tool_input"]),
                "result": {"status": "failed", "error_code": "invalid_tool_request", "message": str(exc)},
            }], "tool_calls": state.get("tool_calls", 0) + 1}
        # 누적 관찰 전체를 복제하지 않고 방금 실행된 도구의 요청/반환만 표시한다.
        self._emit("tools", {"observation": result["observations"][-1],
                             "tool_calls": result["tool_calls"]}, route="reason")
        return result

    def plan(self, state: SupervisorWorkingState) -> dict[str, Any]:
        result = plan_node.run(self, state)
        trace_output = dict(result)
        if self.trace_sink is not None and not result.get("candidate_error"):
            patch = result["candidate_patch"]
            scenario = patch.get("scenario_state", state["source"].get("scenario_state"))
            trace_output["execution_waves"] = execution_waves(patch["execution_plan"], scenario)
            if scenario:
                trace_output["scenario_execution"] = scenario_plan_view(patch["execution_plan"], scenario)
        self._emit("plan", trace_output, route="finalize")
        return result

    def finalize(self, state: SupervisorWorkingState) -> dict[str, Any]:
        result = finalize_node.run(state, max_repairs=self.max_repairs)
        self._emit("finalize", result, route=finalize_node.route({**state, **result}))
        return result

    @staticmethod
    def finalize_route(state: SupervisorWorkingState) -> str:
        return finalize_node.route(state)

    def _run_without_langgraph(self, initial: SupervisorWorkingState) -> SupervisorWorkingState:
        """LangGraph가 없는 환경에서 node 함수 순서만 점검하는 테스트용."""
        state = dict(initial)
        while True:
            state.update(self.reason(state))
            route = self.reason_route(state)
            if route == "tools":
                state.update(self.tools(state))
                continue
            if route == "plan":
                state.update(self.plan(state))
            state.update(self.finalize(state))
            if self.finalize_route(state) == "end":
                return state

    def run(self, main_state: Mapping[str, Any], *, use_graph: bool = True) -> SupervisorOutcome:
        """입력 MainState -> 검증된 MainState 부분 패치 + MainGraph 다음 노드."""
        try:
            initial = self.initial(main_state)
            if use_graph:
                from .graph import compile_graph
                if self._compiled_graph is None:
                    self._compiled_graph = compile_graph(self)
                result = self._compiled_graph.invoke(initial, config={"recursion_limit": 64})
            else:
                result = self._run_without_langgraph(initial)
            outcome = SupervisorOutcome(
                update=result["main_patch"],
                next_node=result["next_node"],  # type: ignore[arg-type]
                tool_calls=result.get("tool_calls", 0),
            )
        except Exception as exc:
            # 외부 SDK 오류 문자열에 인증정보가 있을 수 있어 예외 타입만 추적한다.
            self._emit("error", {"error_type": type(exc).__name__})
            raise
        self._emit("return", {"next_node": outcome.next_node, "state_patch": outcome.update,
                              "tool_calls": outcome.tool_calls}, route=outcome.next_node)
        return outcome


def main(argv: list[str] | None = None) -> int:
    """실제 LLM 실행 및 MainState 반환을 확인하는 로컬 진입점."""
    from .runtime import main as run
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
