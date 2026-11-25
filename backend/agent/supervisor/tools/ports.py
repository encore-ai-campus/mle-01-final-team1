# 타 개발자의 Profile/Memory 구현에 대한 유일한 연결 지점.
# 상위 State 계약과 실행 책임의 경계를 유지한다.
from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from ..contracts import SupervisorContractError
from .profile_fields import FIELDS, METADATA, normalize_profile, validate_profile, validate_read_fields, canonical_profile_path

ToolFn = Callable[[dict[str, Any]], dict[str, Any]]

# 누락과 모호/충돌/서비스 오류를 구별한다. 레거시 unknown은 값 미확인으로 해석한다.
PROFILE_FIELD_STATUSES = frozenset({"available", "missing", "ambiguous", "conflict", "invalid",
                                    "not_loaded", "unavailable", "failed", "access_denied"})


def normalize_field_status(value: Any) -> str:
    if not isinstance(value, str):
        raise SupervisorContractError("Profile field_status must be a string")
    status = {"unknown": "missing", "not_registered": "missing"}.get(value, value)
    if status not in PROFILE_FIELD_STATUSES:
        raise SupervisorContractError("Unknown Profile field_status")
    return status


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
    if data.get("operation") == "inspect":
        if data["is_temporary"] is not True:
            raise SupervisorContractError("Inspect must be read-only (is_temporary=true)")
        if "fields" not in data:
            raise SupervisorContractError("Inspect requires explicit fields; use prepare for extraction")
    if "fields" in data:
        if data.get("operation") != "inspect" or data["is_temporary"] is not True:
            raise SupervisorContractError("fields are read-only inspect paths")
        validate_read_fields(data["fields"])
    if data.get("operation", "prepare") == "prepare":
        if "required_fields" in data:
            validate_read_fields(data["required_fields"])
        if "change_requested" in data and type(data["change_requested"]) is not bool:
            raise SupervisorContractError("Profile change_requested must be a boolean")
        if data.get("change_requested") is True and data["is_temporary"]:
            raise SupervisorContractError("Temporary Profile prepare cannot request persisted changes")
        allowed = {"operation", "instruction", "is_temporary", "required_fields", "change_requested"}
    else:
        allowed = {"operation", "instruction", "is_temporary", "fields"}
    if set(data) - allowed:
        raise SupervisorContractError("Profile 입력에 정의되지 않은 필드가 있습니다.")


def normalize_change_proposals(changes: Any) -> list[dict[str, Any]]:
    """Profile은 변경 후보만 반환한다. 승인 상태와 실제 적재는 서버 소유다."""
    if not isinstance(changes, list):
        raise SupervisorContractError("ProfileResult.changes must be a list")
    result = []
    seen = set()
    for change in changes:
        if not isinstance(change, dict):
            raise SupervisorContractError("Profile change must be an object")
        if set(change) - {"change_id", "domain", "field", "value", "remove_goal_ids", "status"}:
            raise SupervisorContractError("Profile change contains unsupported fields")
        ref, domain, field = (change.get(k) for k in ("change_id", "domain", "field"))
        if not isinstance(ref, str) or not ref.strip() or ref in seen:
            raise SupervisorContractError("Profile change_id must be nonempty and unique")
        if domain not in ("basic", "financial") or not isinstance(field, str):
            raise SupervisorContractError("Profile change domain/field is invalid")
        profile_domain = f"{domain}_profile"
        if field not in FIELDS[profile_domain] - METADATA or "value" not in change:
            raise SupervisorContractError("Profile change field is unknown or protected")
        if change.get("status") not in (None, "pending", "needs_confirmation"):
            raise SupervisorContractError("Profile cannot declare a change applied or resolved")
        validate_profile(profile_domain, {field: change["value"]})
        clean = {k: deepcopy(change[k]) for k in ("change_id", "domain", "field", "value")}
        if "remove_goal_ids" in change:
            ids = change["remove_goal_ids"]
            if field != "goals" or not isinstance(ids, list) or any(not isinstance(x, str) or not x.strip() for x in ids) or len(set(ids)) != len(ids):
                raise SupervisorContractError("remove_goal_ids is a unique string list for goals only")
            clean["remove_goal_ids"] = list(ids)
        result.append(clean)
        seen.add(ref)
    return result


# Profile income_extraction.py의 매핑 계약. 미지원 소득은 새 DB 필드/환산값이 아니다.
INCOME_TARGETS = {
    ("monthly", "net"): "financial_profile.monthly_net_income_krw",
    ("annual", "gross"): "financial_profile.annual_gross_income_krw",
}


def _income_record(record: Any, keys: set[str], label: str) -> None:
    if not isinstance(record, dict) or set(record) != keys:
        raise SupervisorContractError(f"ProfileResult.{label} has invalid fields")
    if type(record["amount_krw"]) is not int or record["amount_krw"] < 0:
        raise SupervisorContractError(f"ProfileResult.{label}.amount_krw must be a nonnegative integer")
    if not isinstance(record["raw_text"], str) or not record["raw_text"].strip():
        raise SupervisorContractError(f"ProfileResult.{label}.raw_text must be a nonempty string")


def _normalize_income_metadata(data: dict[str, Any], result: dict[str, Any], *, prepare: bool) -> None:
    """신규 소득 추출/모호함 메타데이터만 검증하여 관찰에 보존한다. 적재는 하지 않는다."""
    facts = data.get("extracted_info", [])
    ambiguities = data.get("ambiguities", [])
    if not isinstance(facts, list) or not isinstance(ambiguities, list):
        raise SupervisorContractError("ProfileResult.extracted_info/ambiguities must be lists")
    mapped: dict[str, int] = {}
    expected = []
    for fact in facts:
        _income_record(fact, {"type", "period", "amount_krw", "tax_basis", "target_field", "raw_text"}, "extracted_info")
        period, basis = fact["period"], fact["tax_basis"]
        if fact["type"] != "income" or period not in ("monthly", "annual") or basis not in ("net", "gross", "unknown"):
            raise SupervisorContractError("ProfileResult.extracted_info has invalid income type/period/tax_basis")
        path = INCOME_TARGETS.get((period, basis))
        if fact["target_field"] != path:
            raise SupervisorContractError("ProfileResult.extracted_info.target_field conflicts with income period/tax_basis")
        if path is None:
            expected.append(("tax_basis_unknown" if basis == "unknown" else "unsupported_income_field", fact["raw_text"], fact["amount_krw"]))
        else:
            if path in mapped and mapped[path] != fact["amount_krw"]:
                raise SupervisorContractError("ProfileResult.extracted_info has conflicting mapped income values")
            mapped[path] = fact["amount_krw"]
    actual = []
    for item in ambiguities:
        _income_record(item, {"type", "raw_text", "amount_krw"}, "ambiguities")
        if item["type"] not in ("tax_basis_unknown", "unsupported_income_field"):
            raise SupervisorContractError("ProfileResult.ambiguities has unknown type")
        actual.append((item["type"], item["raw_text"], item["amount_krw"]))
    if "extracted_info" in data and "ambiguities" in data and Counter(expected) != Counter(actual):
        raise SupervisorContractError("ProfileResult.ambiguities does not match unmapped income facts")
    # 새 계약을 쓰는 prepare의 소득 delta/변경안은 명확하게 매핑된 원값과 일치해야 한다.
    # 메타데이터가 없는 레거시 반환 및 별도의 DB read는 기존 계약으로 검증한다.
    if ("extracted_info" in data or "ambiguities" in data) and (prepare or facts or ambiguities):
        for path in INCOME_TARGETS.values():
            domain, field = path.split(".")
            delta = result["profile"].get(domain, {})
            if field in delta and delta[field] is not None and mapped.get(path) != delta[field]:
                raise SupervisorContractError("Profile income delta does not match a mapped extracted fact")
            for change in result.get("changes", []):
                if change["domain"] == "financial" and change["field"] == field and (path not in mapped or mapped[path] != change["value"]):
                    raise SupervisorContractError("Profile income change does not match a mapped extracted fact")
    for key in ("extracted_info", "ambiguities"):
        if key in data:
            result[key] = deepcopy(data[key])


def normalize_profile_result(data: Any, *, current_profile: dict[str, Any] | None = None,
                             required_fields: list[str] | None = None) -> dict[str, Any]:
    """prepare의 신규 필드 검증은 서버 snapshot, profile은 별도 추출 delta이다."""
    if not isinstance(data, dict) or not isinstance(data.get("profile"), dict):
        raise SupervisorContractError("ProfileResult.profile은 dict이어야 합니다.")
    missing = data.get("missing_info")
    if not isinstance(missing, list) or not all(isinstance(x, str) for x in missing):
        raise SupervisorContractError("ProfileResult.missing_info는 문자열 목록이어야 합니다.")
    missing = list(dict.fromkeys(canonical_profile_path(f) for f in missing))
    result = {"profile": normalize_profile(data["profile"]), "missing_info": missing}
    if "changes" in data:
        result["changes"] = normalize_change_proposals(data["changes"])
    if "baseline_ref" in data:
        if not isinstance(data["baseline_ref"], str) or not data["baseline_ref"]:
            raise SupervisorContractError("ProfileResult.baseline_ref가 올바르지 않습니다.")
        result["baseline_ref"] = data["baseline_ref"]
    statuses: dict[str, str] = {}
    if "field_status" in data:
        if not isinstance(data["field_status"], dict):
            raise SupervisorContractError("ProfileResult.field_status는 dict이어야 합니다.")
        for field, raw_status in data["field_status"].items():
            path = canonical_profile_path(field)
            status = normalize_field_status(raw_status)
            if path in statuses and statuses[path] != status:
                raise SupervisorContractError("Conflicting Profile field_status aliases")
            statuses[path] = status
    requested = set(validate_read_fields(required_fields)) if required_fields is not None else set()
    if requested and (current_profile is None or set(statuses) != requested):
        raise SupervisorContractError("Profile prepare must report field_status for exactly the requested required_fields")
    # 최신 Profile은 required_fields 및 기존 누락 검사에서 실제 current_profile만 검사한다.
    # 추출 delta를 이 상태와 비교하면 기존 null + 새 명시값 같은 정상 반환이 반려된다.
    snapshot_status = current_profile is not None and (bool(requested) or "extracted_info" in data or "ambiguities" in data)
    for path in missing:
        if statuses.get(path) == "available":
            raise SupervisorContractError("Profile cannot mark a missing field available")
        domain, field = path.split(".")
        default = "not_loaded" if snapshot_status and field not in current_profile.get(domain, {}) else "missing"
        statuses.setdefault(path, default)
    for path, status in statuses.items():
        domain, field = path.split(".")
        checked = current_profile if snapshot_status else result["profile"]
        value = checked.get(domain, {}).get(field)
        if status == "available" and value is None:
            raise SupervisorContractError("Profile available field must contain a non-null value in its checked snapshot/delta")
        if snapshot_status and status == "missing" and field not in checked.get(domain, {}):
            # Profile의 레거시 누락 검사에 미로딩 값이 포함되더라도 미등록으로 단정하지 않는다.
            statuses[path] = status = "not_loaded"
        if status != "available" and value is not None:
            raise SupervisorContractError("Unresolved Profile field must not carry a confirmed value")
        if status in {"ambiguous", "conflict", "invalid"} and any(
                ch.get("domain") == domain.removesuffix("_profile") and ch.get("field") == field
                for ch in result.get("changes", [])):
            raise SupervisorContractError("Unresolved Profile field cannot be a save proposal")
    if statuses:
        result["field_status"] = statuses
        result["missing_info"] = [path for path, status in statuses.items() if status == "missing"]
    if "branch_specs" in data:
        from .scenarios import normalize_holding_actions
        specs = data["branch_specs"]
        if not isinstance(specs, list) or len(specs) > 3:
            raise SupervisorContractError("Invalid Profile branch_specs")
        clean_specs, seen = [], set()
        for spec in specs:
            if (not isinstance(spec, dict) or set(spec) - {"id", "status", "holding_actions"}
                    or not isinstance(spec.get("id"), str) or not spec["id"].strip()
                    or spec["id"] in seen or spec.get("status") not in ("ready", "conditional")):
                raise SupervisorContractError("Invalid Profile branch_specs")
            actions = normalize_holding_actions(spec.get("holding_actions", []), result.get("baseline_ref"),
                                                conditional=spec["status"] == "conditional")
            clean_specs.append({"id": spec["id"], "status": spec["status"], "holding_actions": actions})
            seen.add(spec["id"])
        result["branch_specs"] = clean_specs
    if "missing_inputs" in data:
        if not isinstance(data["missing_inputs"], list) or not all(isinstance(item, str) for item in data["missing_inputs"]):
            raise SupervisorContractError("Invalid Profile missing_inputs")
        result["missing_inputs"] = list(dict.fromkeys(data["missing_inputs"]))
    _normalize_income_metadata(data, result, prepare=current_profile is not None)
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
    load_advice_snapshot: Callable[[str], dict[str, Any]] | None = None

    @classmethod
    def with_profile_mvp(cls, *, retrieve_memory: ToolFn | None = None,
                         load_memory_context: ToolFn | None = None,
                         load_advice_snapshot: Callable[[str], dict[str, Any]] | None = None,
                         get_profile_fields: Callable[[list[str]], dict[str, Any]] | None = None) -> "SupervisorPorts":
        from agent.profile.profile_agent import run_profile_agent

        def profile_adapter(payload: dict[str, Any]) -> dict[str, Any]:
            # 실제 ProfileRequest 계약만 전달한다. 새 옵션을 누락하지 않는다.
            request = {"instruction": payload["instruction"],
                       "is_temporary": payload["is_temporary"],
                       "current_profile": deepcopy(payload.get("current_profile", {})),
                       "change_requested": payload.get("change_requested", False)}
            if "required_fields" in payload:
                request["required_fields"] = deepcopy(payload["required_fields"])
            return run_profile_agent(request)

        return cls(profile_agent=profile_adapter, retrieve_memory=retrieve_memory,
                   load_memory_context=load_memory_context, get_profile_fields=get_profile_fields,
                   load_advice_snapshot=load_advice_snapshot)
