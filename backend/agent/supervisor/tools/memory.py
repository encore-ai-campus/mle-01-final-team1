# 새 turn의 기본 기억을 확보하고 정밀 조회 결과를 기존 memory 필드에 병합한다.
# 최신 Memory MVP의 memory_type/content/metadata 계약을 사용하고 권한 거부 시 비운다.
from __future__ import annotations

from copy import deepcopy
from typing import Any
from .ports import SupervisorPorts, MEMORY_TYPES, MEMORY_STATUSES, normalize_memory_result, select_memory_items, memory_key

MAX_MEMORY_ITEMS = 10


def latest_user_text(source: dict[str, Any]) -> str | None:
    for msg in reversed(source.get("messages") or []):
        role = msg.get("type", msg.get("role")) if isinstance(msg, dict) else getattr(msg, "type", None)
        text = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
        if role in {"human", "user"} and isinstance(text, str) and text.strip():
            return text
    return None


def call_memory(port: Any, args: dict[str, Any]) -> dict[str, Any]:
    if port is None:
        return {"retrieval_status": "failed", "retrieved": []}
    try:
        return normalize_memory_result(port(deepcopy(args)))
    except Exception:
        return {"retrieval_status": "failed", "retrieved": []}


def initial_memory(source: dict[str, Any], ports: SupervisorPorts) -> dict[str, Any] | None:
    previous = deepcopy(source.get("memory") or {})
    query = latest_user_text(source)
    turn = source.get("turn_id")
    if not turn or query is None:
        return None
    same = previous.get("retrieval_turn_id") == turn and previous.get("retrieval_consultation_id") == source.get("consultation_id")
    if same and previous.get("prefetch_status") in MEMORY_STATUSES:
        if previous.get("prefetch_status") == "access_denied":
            previous.update(retrieval_status="access_denied", retrieved=[])
        else:
            try:
                previous.update(normalize_memory_result(previous))
            except Exception:
                previous.update(retrieval_status="failed", retrieved=[])
        return previous
    port = ports.load_memory_context or ports.retrieve_memory
    result = call_memory(port, {"query": query, "limit": MAX_MEMORY_ITEMS,
                               "memory_types": list(MEMORY_TYPES)}) if port else {"retrieval_status": "not_requested", "retrieved": []}
    memory = {"retrieval_turn_id": turn, "retrieval_consultation_id": source.get("consultation_id"),
              "prefetch_status": result["retrieval_status"], "retrieval_status": result["retrieval_status"],
              "retrieved": select_memory_items(result["retrieved"], MAX_MEMORY_ITEMS)}
    if "last_advice_ref" in previous:
        memory["last_advice_ref"] = previous["last_advice_ref"]
    return memory


def merge_memory(source: dict[str, Any], observations: list[dict[str, Any]]) -> dict[str, Any]:
    memory = deepcopy(source.get("memory") or {})
    status = memory.get("retrieval_status", "not_requested")
    retrieved = deepcopy(memory.get("retrieved", [])) if status == "found" else []
    for obs in observations:
        if obs.get("name") != "retrieve_memory":
            continue
        result = obs["result"]
        incoming = result.get("retrieval_status", "failed")
        if status == "access_denied" or incoming == "access_denied":
            status, retrieved = "access_denied", []
            continue
        if incoming == "found":
            fresh = deepcopy(result["retrieved"])
            keys = {memory_key(i) for i in fresh}
            retrieved = fresh + [i for i in retrieved if memory_key(i) not in keys]
            status = "found"
        elif status != "found":
            status = incoming
    items = select_memory_items(retrieved, MAX_MEMORY_ITEMS)
    if status == "found" and not items:
        status = "no_match"
    if memory or observations:
        memory.update(retrieval_status=status, retrieved=items)
    return memory
