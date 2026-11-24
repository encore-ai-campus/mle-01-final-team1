# LangGraph 제어 흐름: reason -> tools? -> plan? -> finalize -> END/repair.
# 상위 State 계약과 실행 책임의 경계를 유지한다.
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .supervisor_main import Supervisor


def compile_graph(supervisor: "Supervisor") -> Any:
    from langgraph.graph import END, START, StateGraph
    from .state import SupervisorWorkingState

    graph = StateGraph(SupervisorWorkingState)
    graph.add_node("reason", supervisor.reason)
    graph.add_node("tools", supervisor.tools)
    graph.add_node("plan", supervisor.plan)
    graph.add_node("finalize", supervisor.finalize)
    graph.add_edge(START, "reason")
    graph.add_conditional_edges(
        "reason", supervisor.reason_route,
        {"tools": "tools", "plan": "plan", "finalize": "finalize"},
    )
    graph.add_edge("tools", "reason")
    graph.add_edge("plan", "finalize")
    graph.add_conditional_edges(
        "finalize", supervisor.finalize_route,
        {"reason": "reason", "end": END},
    )
    return graph.compile()


def build_main_supervisor_node(supervisor: "Supervisor"):
    """부모 Graph의 supervisor 노드에서 Command(update=..., goto=...) 반환.

    부모 Graph에 plan_validator, ask_user, final_advice 노드가 있어야 한다.
    """
    def supervisor_node(main_state: dict[str, Any]):
        from langgraph.types import Command
        outcome = supervisor.run(main_state, use_graph=True)
        return Command(update=outcome.update, goto=outcome.next_node)

    return supervisor_node
