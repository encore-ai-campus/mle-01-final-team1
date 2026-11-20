"""LangGraph 최소 실행 골격. v3.0 공용 MainState를 사용한다.

Supervisor와 전문 Agent를 연결하는 실제 금융 상담 흐름은 아직 구현하지 않았다.
"""

from langgraph.graph import END, START, StateGraph

from agent.state import MainState


def _bootstrap(_state: MainState) -> dict[str, str]:
    # 기본 그래프 연결 확인용 노드이며 실제 Supervisor 처리는 아직 없다.
    return {"phase": "planning"}


def build_workflow():
    """최소 실행 가능한 LangGraph를 컴파일한다."""
    graph = StateGraph(MainState)
    graph.add_node("bootstrap", _bootstrap)
    graph.add_edge(START, "bootstrap")
    graph.add_edge("bootstrap", END)
    return graph.compile()
