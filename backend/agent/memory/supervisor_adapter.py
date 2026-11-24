from typing import Any

from .contracts import MemorySearchRequest
from .memory_service import MemoryService
from .postgres_memory_service import PostgresMemoryService


def retrieve_memory_for_supervisor(
    service: MemoryService | PostgresMemoryService,
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
    # PostgreSQL에 저장된 기억을 검색한다.
    service = PostgresMemoryService()

    result = retrieve_memory_for_supervisor(
        service,
        {"query": "savings product B"},
        authenticated_user_id="test_user_001",
    )

    print("SUPERVISOR MEMORY:", result)