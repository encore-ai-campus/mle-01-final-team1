
from typing import Any, Literal, NotRequired, TypedDict


class MemorySearchRequest(TypedDict):
    """Supervisor가 과거 기억 검색을 요청할 때 사용하는 입력."""

    query: str
    limit: NotRequired[int]
    memory_types: NotRequired[list[str]]


class MemoryItem(TypedDict):
    """저장되거나 검색된 개별 기억."""

    memory_id: str
    memory_type: str
    content: str
    source_ref: str
    target_ref: NotRequired[str | None]
    metadata: NotRequired[dict[str, Any]]


class MemorySearchResult(TypedDict):
    """Memory Retrieval이 Supervisor에 반환하는 결과."""

    retrieval_status: Literal[
        "found", "no_match", "failed", "access_denied"
    ]
    retrieved: list[MemoryItem]


class MemoryWriteRequest(TypedDict):
    """상담 내용을 장기기억으로 저장하기 위한 입력."""

    memory_type: Literal[
        "consultation_summary",
        "recommendation",
        "user_decision",
        "user_preference",
    ]
    content: str
    source_ref: str
    target_ref: NotRequired[str | None]
    metadata: NotRequired[dict[str, Any]]


class MemoryWriteResult(TypedDict):
    """Memory Writer가 반환하는 저장 결과."""

    status: Literal[
        "saved", "duplicate", "failed", "access_denied"
    ]
    memory_id: NotRequired[str]
