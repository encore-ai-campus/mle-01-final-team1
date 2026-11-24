
from copy import deepcopy
from uuid import uuid4

from .contracts import (
    MemoryItem,
    MemorySearchRequest,
    MemorySearchResult,
    MemoryWriteRequest,
    MemoryWriteResult,
)


class MemoryService:
    """PostgreSQL 연결 전 사용하는 인메모리 MVP."""

    def __init__(self):
        # user_id별 기억을 분리해서 관리한다.
        self._memories: dict[str, list[MemoryItem]] = {}

    def save_memory(
        self,
        request: MemoryWriteRequest,
        *,
        authenticated_user_id: str,
    ) -> MemoryWriteResult:
        """인증된 사용자에게 기억을 저장한다."""

        if not authenticated_user_id:
            return {"status": "access_denied"}

        if not request["content"].strip() or not request["source_ref"].strip():
            return {"status": "failed"}

        user_memories = self._memories.setdefault(
            authenticated_user_id, []
        )

        # 같은 출처·유형·대상·내용의 중복 저장 방지
        for memory in user_memories:
            if (
                memory["source_ref"] == request["source_ref"]
                and memory["memory_type"] == request["memory_type"]
                and memory.get("target_ref") == request.get("target_ref")
                and memory["content"] == request["content"]
            ):
                return {
                    "status": "duplicate",
                    "memory_id": memory["memory_id"],
                }

        memory: MemoryItem = {
            "memory_id": str(uuid4()),
            "memory_type": request["memory_type"],
            "content": request["content"],
            "source_ref": request["source_ref"],
            "target_ref": request.get("target_ref"),
            "metadata": deepcopy(request.get("metadata", {})),
        }

        user_memories.append(memory)

        return {
            "status": "saved",
            "memory_id": memory["memory_id"],
        }

    def search_memory(
        self,
        request: MemorySearchRequest,
        *,
        authenticated_user_id: str,
    ) -> MemorySearchResult:
        """인증된 사용자 기억에서 키워드 일치 검색을 수행한다."""

        if not authenticated_user_id:
            return {
                "retrieval_status": "access_denied",
                "retrieved": [],
            }

        query = request["query"].strip().lower()

        if not query:
            return {
                "retrieval_status": "no_match",
                "retrieved": [],
            }

        limit = max(1, min(request.get("limit", 5), 20))
        allowed_types = request.get("memory_types")

        results = []

        for memory in self._memories.get(authenticated_user_id, []):
            if allowed_types and memory["memory_type"] not in allowed_types:
                continue

            if query in memory["content"].lower():
                results.append(deepcopy(memory))

        results = results[:limit]

        return {
            "retrieval_status": "found" if results else "no_match",
            "retrieved": results,
        }


if __name__ == "__main__":
    service = MemoryService()

    user_context = "demo_user_001"

    save_result = service.save_memory(
        {
            "memory_type": "user_decision",
            "content": "적금 B를 중도해지 조건 때문에 제외함",
            "source_ref": "advice_001",
            "target_ref": "product_B",
            "metadata": {
                "decision": "rejected",
                "reason": "중도해지 조건이 불리함",
            },
        },
        authenticated_user_id=user_context,
    )

    print("저장 결과:", save_result)

    search_result = service.search_memory(
        {
            "query": "적금 B",
            "limit": 5,
        },
        authenticated_user_id=user_context,
    )

    print("검색 결과:", search_result)
