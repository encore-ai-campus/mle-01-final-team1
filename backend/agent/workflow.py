"""LangGraph 실행 골격. 실제 금융 상담 로직은 아직 구현하지 않았다."""

from langgraph.graph import END, START, StateGraph

from agent.state import SupervisorState


def _bootstrap(_state: SupervisorState) -> dict[str, str]:
    # 기본 연결 확인용 노드이며 Supervisor와 Dispatcher, 전문 Agent는 아직 연결하지 않았다.
    return {"phase": "planning"}


def build_workflow():
    graph = StateGraph(SupervisorState)
    graph.add_node("bootstrap", _bootstrap)
    graph.add_edge(START, "bootstrap")
    graph.add_edge("bootstrap", END)
    return graph.compile()
