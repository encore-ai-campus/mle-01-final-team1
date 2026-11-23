"""최상위 LangGraph를 한 번 컴파일하고, 실행 전 Langfuse 추적 설정을 연결한다."""

from functools import lru_cache

from agent.workflow import build_workflow
from services.observability import get_langfuse_handler


@lru_cache(maxsize=1)
def get_workflow():
    """Return a compiled workflow with optional Langfuse tracing."""
    graph = build_workflow()  # build_workflow() already compiles the graph.
    handler = get_langfuse_handler()
    if handler is None:
        return graph

    return graph.with_config(
        {
            "callbacks": [handler],
            "run_name": "main_supervisor",
        }
    )
