"""최상위 LangGraph를 한 번 컴파일하고 기존 Langfuse 추적 설정을 연결한다."""
from functools import lru_cache

from agent.workflow import build_workflow
from services.observability import get_langfuse_handler


@lru_cache(maxsize=1)
def get_workflow():
    """개발용 연결 그래프. Supervisor는 실행 요청별로 생성된다."""
    graph = build_workflow()
    config = {"run_name": "main_workflow", "recursion_limit": 128}
    handler = get_langfuse_handler()
    if handler is not None:
        config["callbacks"] = [handler]
    return graph.with_config(config)
