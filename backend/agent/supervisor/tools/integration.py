# Profile/Memory 구현을 수정하지 않고 공개 서비스 계약에 연결한다.
# 인증된 사용자 범위와 검색 요청을 바인딩하며 DB 원본을 임의로 만들지 않는다.
from __future__ import annotations

import re
from collections.abc import Callable
from copy import deepcopy
from typing import Any
from uuid import UUID

from .ports import (SupervisorPorts, MEMORY_TYPES, validate_memory_request,
                    normalize_memory_result, select_memory_items, memory_request_key)


# 현재 Memory API는 한 번에 문자열 하나를 검색한다. DB 연결 비용을 고려해
# 초기 자동 조회만 제한한다. 상세 조회는 기존 Supervisor 호출 상한을 따른다.
MAX_PREFETCH_SEARCHES = 5
MAX_TOPIC_SEARCHES = 3
PREFERENCE_TERMS = ("선호", "제외")
TOPIC_TERMS = ("적금", "예금", "대출", "정책", "저축", "전세", "월세", "주택",
               "결혼", "목표", "자산", "소득", "금리", "부채", "비상금")


def _prefetch_requests(request: dict[str, Any]) -> list[dict[str, Any]]:
    """부분문자열 검색을 위한 제한적 탐색. 검색어·유형·한도를 중복하지 않는다."""
    validate_memory_request(request)
    query = request["query"].strip()
    types = list(request.get("memory_types") or MEMORY_TYPES)
    keywords = sorted((term for term in TOPIC_TERMS if term in query), key=query.index)
    words = sorted(set(re.findall(r"[\w]+", query)), key=lambda word: (-len(word), word))
    terms = list(dict.fromkeys([query, *keywords, *[word for word in words if len(word) >= 2]]))
    searches = [(term, types) for term in terms[:MAX_TOPIC_SEARCHES]]
    if "user_preference" in types:
        searches.extend((term, ["user_preference"]) for term in PREFERENCE_TERMS)
    unique = []
    seen = set()
    for term, search_types in searches:
        candidate = {"query": term, "limit": 20, "memory_types": search_types}
        key = memory_request_key(candidate)
        if key not in seen:
            unique.append(candidate)
            seen.add(key)
    return unique[:MAX_PREFETCH_SEARCHES]


def memory_ports(service: Any, *, authenticated_user_id: str):
    """동일 인증 사용자에게만 바인딩된 읽기 포트. 실제 저장은 하지 않는다."""
    def retrieve(request: dict[str, Any]) -> dict[str, Any]:
        validate_memory_request(request)
        if not authenticated_user_id:
            return {"retrieval_status": "access_denied", "retrieved": []}
        # 두 Memory 구현 모두 공개 search_memory 계약을 제공한다.
        # supervisor_adapter의 단순 전달과 동등하며 로컬 Memory 사용 시에도
        # PostgreSQL 드라이버를 불필요하게 import하지 않는다.
        clean = deepcopy(request)
        clean["query"] = clean["query"].strip()
        clean["limit"] = clean.get("limit", 5)
        clean["memory_types"] = list(dict.fromkeys(clean.get("memory_types") or MEMORY_TYPES))
        try:
            result = normalize_memory_result(service.search_memory(
                clean, authenticated_user_id=authenticated_user_id))
            if any(item["memory_type"] not in clean["memory_types"] for item in result["retrieved"]):
                return {"retrieval_status": "failed", "retrieved": []}
            result["retrieved"] = select_memory_items(result["retrieved"], clean["limit"])
            return result
        except Exception:
            # 외부 DB 예외 본문과 연결 정보는 노출하지 않는다.
            return {"retrieval_status": "failed", "retrieved": []}

    def prefetch(request: dict[str, Any]) -> dict[str, Any]:
        validate_memory_request(request)
        if not authenticated_user_id:
            return {"retrieval_status": "access_denied", "retrieved": []}
        hits = []
        failed = False
        for search in _prefetch_requests(request):
            result = retrieve(search)
            status = result["retrieval_status"]
            if status == "access_denied":
                return {"retrieval_status": "access_denied", "retrieved": []}
            if status == "failed":
                # 같은 DB 장애에 대해 자동 검색을 연달아 시도하지 않는다.
                failed = True
                break
            hits.extend(result["retrieved"])
        items = select_memory_items(hits, limit=request.get("limit", 10))
        return {"retrieval_status": "found" if items else "failed" if failed else "no_match",
                "retrieved": items}

    return prefetch, retrieve


def bind_mvp_ports(*, memory_service: Any = None, authenticated_user_id: str = "",
                   get_profile_fields: Callable | None = None,
                   load_memory_context: Callable | None = None,
                   load_advice_snapshot: Callable | None = None) -> SupervisorPorts:
    """운영에서는 서비스 인스턴스/권한을 서버에서 주입. 로컬에서도 동일 공개 API 사용."""
    prefetch = retrieve = None
    if memory_service is not None:
        prefetch, retrieve = memory_ports(memory_service, authenticated_user_id=authenticated_user_id)
    if load_memory_context is not None:
        # 이후 Memory 서비스에 인증된 활성 선호 조회가 추가되면 이 포트만 교체한다.
        prefetch = load_memory_context
    try:
        return SupervisorPorts.with_profile_mvp(retrieve_memory=retrieve,
            load_memory_context=prefetch, get_profile_fields=get_profile_fields,
            load_advice_snapshot=load_advice_snapshot)
    except ModuleNotFoundError as exc:
        if exc.name not in {"agent.profile", "agent.profile.profile_agent"}:
            raise
        return SupervisorPorts(retrieve_memory=retrieve, load_memory_context=prefetch,
                               get_profile_fields=get_profile_fields, load_advice_snapshot=load_advice_snapshot)


def bind_authenticated_ports(*, authenticated_user_id: UUID,
                             load_advice_snapshot: Callable | None = None) -> SupervisorPorts:
    """서버가 검증한 UUID로 Profile DB와 Memory DB를 함께 연결한다.

    인증은 호출자(FastAPI dependency 등)의 책임이다. 요청별로 생성하고
    사용자 ID를 LLM, 사용자 입력, MainState에서 받지 않는다. 스냅샷 조회는
    동일 사용자에게 별도로 바인딩된 읽기 함수만 주입한다.
    """
    if not isinstance(authenticated_user_id, UUID):
        raise TypeError("authenticated_user_id must be a verified UUID")
    # DB 관련 모듈은 인증된 실제 연동 경로에서만 import한다.
    from agent.profile.profile_db_service import ProfileDBService
    from agent.memory.postgres_memory_service import PostgresMemoryService

    profile_service = ProfileDBService(authenticated_user_id=authenticated_user_id)
    memory_service = PostgresMemoryService()
    return bind_mvp_ports(
        memory_service=memory_service,
        authenticated_user_id=str(authenticated_user_id),
        get_profile_fields=profile_service.get_profile_fields,
        load_advice_snapshot=load_advice_snapshot,
    )
