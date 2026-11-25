# 기존 Profile/Memory MVP를 수정하지 않고 실제 공개 함수에 연결한다.
# 인증된 사용자 범위와 검색 요청을 바인딩하며 DB 원본을 임의로 만들지 않는다.
from __future__ import annotations

import re
from collections.abc import Callable
from copy import deepcopy
from typing import Any

from .ports import (SupervisorPorts, MEMORY_TYPES, validate_memory_request,
                    normalize_memory_result, select_memory_items)


def memory_ports(service: Any, *, authenticated_user_id: str):
    """서버가 보유한 동일 MemoryService 인스턴스와 인증 ID를 넘긴다."""
    from agent.memory.supervisor_adapter import retrieve_memory_for_supervisor

    def retrieve(request: dict[str, Any]) -> dict[str, Any]:
        validate_memory_request(request)
        if not authenticated_user_id:
            return {"retrieval_status": "access_denied", "retrieved": []}
        return normalize_memory_result(retrieve_memory_for_supervisor(
            service, deepcopy(request), authenticated_user_id=authenticated_user_id))

    def prefetch(request: dict[str, Any]) -> dict[str, Any]:
        # MVP는 문장 전체의 부분문자열 검색만 지원한다. 전체 질의 + 관련 키워드로
        # 제한된 횟수만 보완한다. 전체 선호 목록 조회/의미 검색은 지원하지 않는다.
        validate_memory_request(request)
        query = request["query"].strip()
        keywords = [term for term in ("적금", "예금", "대출", "정책", "저축", "전세", "주택",
                                      "결혼", "목표", "자산", "소득", "금리", "부채", "비상금") if term in query]
        words = sorted(set(re.findall(r"[\w]+", query)), key=lambda x: (-len(x), x))
        queries = list(dict.fromkeys([query, *keywords, *[w for w in words if len(w) >= 2]]))[:5]
        types = request.get("memory_types", list(MEMORY_TYPES))
        searches = [(text, types) for text in queries]
        # 사용자 선호는 현재 질문에 등장하지 않을 수도 있다. MVP의 공개 API로
        # 대표 표현도 제한적으로 검색한다. 전체 활성 선호 조회를 보장하지 않는다.
        if "user_preference" in types:
            searches += [(text, ["user_preference"]) for text in ("선호", "제외", "원하지", "피하", "싫")]
        hits = []
        failed = False
        for text, search_types in searches:
            try:
                result = retrieve({"query": text, "limit": 20,
                                   "memory_types": search_types})
            except Exception:
                failed = True
                continue
            if result["retrieval_status"] == "access_denied":
                return {"retrieval_status": "access_denied", "retrieved": []}
            failed |= result["retrieval_status"] == "failed"
            hits.extend(result["retrieved"])
        # 부분 실패로 발견한 기억을 잃지 않는다. 아무 결과도 없으면 실패를 구분한다.
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
