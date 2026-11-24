
from typing import Any

from .contracts import MemorySearchRequest
from .memory_service import MemoryService


def retrieve_memory_for_supervisor(
    service: MemoryService,
    request: MemorySearchRequest,
    *,
    authenticated_user_id: str,
) -> dict[str, Any]:
    """
    Supervisor가 사용할 Memory 검색 결과를 반환한다.

    MainState를 직접 수정하지 않고,
    memory 필드에 반영할 데이터만 반환한다.
    """

    result = service.search_memory(
        request,
        authenticated_user_id=authenticated_user_id,
    )

    return {
        "retrieval_status": result["retrieval_status"],
        "retrieved": result["retrieved"],
    }


if __name__ == "__main__":
    # 예시 실행에서는 동일한 서비스 인스턴스를 사용한다.
    service = MemoryService()

    service.save_memory(
        {
            "memory_type": "user_decision",
            "content": "적금 B를 중도해지 조건 때문에 제외함",
            "source_ref": "advice_001",
        },
        authenticated_user_id="demo_user_001",
    )

    result = retrieve_memory_for_supervisor(
        service,
        {"query": "적금 B"},
        authenticated_user_id="demo_user_001",
    )

    print("Supervisor 전달 결과:", result)
