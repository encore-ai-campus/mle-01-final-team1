# 타 개발자의 Profile/Memory 구현에 대한 유일한 연결 지점.
# 상위 State 계약과 실행 책임의 경계를 유지한다.
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from ..contracts import SupervisorContractError
from .profile_fields import normalize_profile, validate_read_fields

ToolFn = Callable[[dict[str, Any]], dict[str, Any]]


def validate_profile_request(data: dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise SupervisorContractError("Profile 요청은 dict이어야 합니다.")
    if data.get("operation", "prepare") not in ("prepare", "inspect"):
        raise SupervisorContractError("Profile 작업은 prepare 또는 inspect만 허용됩니다.")
    if not isinstance(data.get("instruction"), str) or not data["instruction"].strip():
        raise SupervisorContractError("Profile instruction이 필요합니다.")
    if type(data.get("is_temporary")) is not bool:
        raise SupervisorContractError("Profile is_temporary는 bool이어야 합니다.")
    # is_temporary=False도 변경안 생성으로만 처리하며 직접 DB에 저장하지 않는다.
    if data.get("operation") == "inspect" and data["is_temporary"] is not True:
        raise SupervisorContractError("Inspect must be read-only (is_temporary=true)")
    if "fields" in data:
        if data.get("operation") != "inspect" or data["is_temporary"] is not True:
            raise SupervisorContractError("fields are read-only inspect paths")
        validate_read_fields(data["fields"])
    if set(data) - {"operation", "instruction", "is_temporary", "fields"}:
        raise SupervisorContractError("Profile 입력에 정의되지 않은 필드가 있습니다.")


def normalize_profile_result(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or not isinstance(data.get("profile"), dict):
        raise SupervisorContractError("ProfileResult.profile은 dict이어야 합니다.")
    missing = data.get("missing_info")
    if not isinstance(missing, list) or not all(isinstance(x, str) for x in missing):
        raise SupervisorContractError("ProfileResult.missing_info는 문자열 목록이어야 합니다.")
    result = {"profile": normalize_profile(data["profile"]), "missing_info": list(dict.fromkeys(missing))}
    if "changes" in data:
        if not isinstance(data["changes"], list) or any(not isinstance(c, dict) for c in data["changes"]):
            raise SupervisorContractError("ProfileResult.changes는 dict 목록이어야 합니다.")
        result["changes"] = data["changes"]
    if "baseline_ref" in data:
        if not isinstance(data["baseline_ref"], str) or not data["baseline_ref"]:
            raise SupervisorContractError("ProfileResult.baseline_ref가 올바르지 않습니다.")
        result["baseline_ref"] = data["baseline_ref"]
    if "field_status" in data:
        if not isinstance(data["field_status"], dict):
            raise SupervisorContractError("ProfileResult.field_status는 dict이어야 합니다.")
        result["field_status"] = data["field_status"]
    if "branch_specs" in data:
        specs = data["branch_specs"]
        if not isinstance(specs, list) or any(not isinstance(spec, dict) or not isinstance(spec.get("id"), str) or spec.get("status") not in {"ready", "conditional"} or not isinstance(spec.get("holding_actions", []), list) for spec in specs):
            raise SupervisorContractError("Invalid Profile branch_specs")
        result["branch_specs"] = specs
    if "missing_inputs" in data:
        if not isinstance(data["missing_inputs"], list) or not all(isinstance(item, str) for item in data["missing_inputs"]):
            raise SupervisorContractError("Invalid Profile missing_inputs")
        result["missing_inputs"] = data["missing_inputs"]
    return result


# Memory MVP PR #4의 공개 입출력 계약을 그대로 사용한다.
MEMORY_TYPES = ("user_preference", "user_decision", "consultation_summary", "recommendation")
MEMORY_STATUSES = {"found", "no_match", "access_denied", "failed"}


def validate_memory_request(data: dict[str, Any]) -> None:
    if not isinstance(data, dict) or set(data) - {"query", "limit", "memory_types"}:
        raise SupervisorContractError("MemorySearchRequest: query, limit, memory_types만 허용합니다.")
    if not isinstance(data.get("query"), str) or not data["query"].strip():
        raise SupervisorContractError("Memory query가 필요합니다.")
    if type(data.get("limit", 5)) is not int or not 1 <= data.get("limit", 5) <= 20:
        raise SupervisorContractError("Memory limit은 1~20이어야 합니다.")
    types = data.get("memory_types")
    if types is not None and (not isinstance(types, list) or not types or
                              any(t not in MEMORY_TYPES for t in types)):
        raise SupervisorContractError("Memory memory_types는 등록된 네 유형의 목록이어야 합니다.")


def memory_key(item: dict[str, Any]) -> str:
    return item["memory_id"]


def select_memory_items(items: list[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    unique = {memory_key(i): i for i in reversed(items)}
    ordered = [unique[k] for k in dict.fromkeys(memory_key(i) for i in items)]
    # 발견한 기억의 유형이 많은 경우에도 각 유형의 첫 결과를 보존한다.
    selected = []
    for kind in MEMORY_TYPES:
        hit = next((i for i in ordered if i["memory_type"] == kind), None)
        if hit is not None:
            selected.append(hit)
    used = {memory_key(i) for i in selected}
    return (selected + [i for i in ordered if memory_key(i) not in used])[:limit]


def normalize_memory_result(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or data.get("retrieval_status") not in MEMORY_STATUSES:
        raise SupervisorContractError("MemorySearchResult.retrieval_status가 올바르지 않습니다.")
    items = data.get("retrieved")
    if not isinstance(items, list):
        raise SupervisorContractError("MemorySearchResult.retrieved는 list이어야 합니다.")
    status = data["retrieval_status"]
    if status != "found":
        return {"retrieval_status": status, "retrieved": []}
    clean = []
    for item in items:
        if (not isinstance(item, dict) or item.get("memory_type") not in MEMORY_TYPES or
                any(not isinstance(item.get(k), str) or not item[k].strip()
                    for k in ("memory_id", "content", "source_ref"))):
            raise SupervisorContractError("MemoryItem 필수 필드가 잘못되었습니다.")
        metadata = item.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise SupervisorContractError("MemoryItem.metadata는 dict이어야 합니다.")
        if metadata.get("status") in {"expired", "superseded", "deleted", "inactive", "revoked"}:
            continue
        saved = {k: item[k] for k in ("memory_id", "memory_type", "content", "source_ref")}
        if "target_ref" in item and (item["target_ref"] is None or isinstance(item["target_ref"], str)):
            saved["target_ref"] = item["target_ref"]
        # 제3자 메타데이터 전체 대신 판단에 필요한 공개 속성만 전달한다.
        saved["metadata"] = {k: metadata[k] for k in ("decision", "reason", "status", "as_of", "created_at", "updated_at")
                             if isinstance(metadata.get(k), str)}
        clean.append(saved)
    clean = select_memory_items(clean, limit=20)
    return {"retrieval_status": "found" if clean else "no_match", "retrieved": clean}


@dataclass(frozen=True)
class SupervisorPorts:
    """각 함수는 호출 전 서버에서 인증된 사용자 컨텍스트에 바인딩한다.

    Supervisor가 인증 사용자 ID/토큰을 LLM tool_input으로 받지 않는다.
    """
    profile_agent: ToolFn | None = None
    retrieve_memory: ToolFn | None = None
    get_profile_fields: Callable[[list[str]], dict[str, Any]] | None = None
    load_memory_context: ToolFn | None = None

    @classmethod
    def with_profile_mvp(cls, *, retrieve_memory: ToolFn | None = None,
                         load_memory_context: ToolFn | None = None,
                         get_profile_fields: Callable[[list[str]], dict[str, Any]] | None = None) -> "SupervisorPorts":
        from agent.profile.profile_agent import run_profile_agent

        def profile_adapter(payload: dict[str, Any]) -> dict[str, Any]:
            # Only the actual MVP request fields cross the adapter.
            return run_profile_agent({
                "instruction": payload["instruction"],
                "is_temporary": payload["is_temporary"],
                "current_profile": deepcopy(payload.get("current_profile", {})),
            })

        return cls(profile_agent=profile_adapter, retrieve_memory=retrieve_memory,
                   load_memory_context=load_memory_context, get_profile_fields=get_profile_fields)
