# 지정 필드의 조회 상태만 처리한다. 상담 의도와 질문 여부는 Reason이 결정한다.
from __future__ import annotations

from copy import deepcopy
from typing import Any, TYPE_CHECKING

from ..contracts import SupervisorContractError
from .ports import normalize_profile_result
from .profile_fields import FIELDS, METADATA, select_fields, source_with_reads, validate_read_fields

if TYPE_CHECKING:
    from ..supervisor_main import Supervisor


def _value(profile: dict[str, Any], path: str) -> Any:
    domain, field = path.split(".")
    return profile.get(domain, {}).get(field)


def _has(profile: dict[str, Any], path: str) -> bool:
    domain, field = path.split(".")
    return field in profile.get(domain, {})


def _assign(profile: dict[str, Any], path: str, value: Any) -> None:
    domain, field = path.split(".")
    profile.setdefault(domain, {})[field] = deepcopy(value)


def inspect_fields(supervisor: "Supervisor", state: dict[str, Any], fields: list[str]) -> dict[str, Any]:
    """현재 스냅샷과 같은 요청의 조회 기록을 재사용하고 미로딩 필드만 읽는다.

    get_profile_fields는 레거시 nested profile 또는 ProfileResult envelope를 반환한다.
    실패/권한 없음/모호함은 미등록으로 바꾸지 않으며 재조회하지 않는다.
    """
    paths = validate_read_fields(fields)
    current = source_with_reads(state)
    profile = select_fields(current, paths)
    statuses = {path: "available" if _value(profile, path) is not None else "missing"
                for path in paths if _has(profile, path)}
    # 같은 turn 내부에서 조회한 필드 상태를 재사용한다(값이 없거나 서비스 오류여도).
    for obs in state.get("observations", []):
        if obs.get("name") != "profile_agent" or obs.get("request", {}).get("operation") != "inspect":
            continue
        old = obs.get("result", {})
        for path in obs.get("request", {}).get("fields", []):
            if path not in paths or path in statuses:
                continue
            if path in old.get("field_status", {}):
                statuses[path] = old["field_status"][path]
            elif old.get("status") == "failed":
                statuses[path] = "failed"
    needed = [path for path in paths if path not in statuses]
    verified: dict[str, Any] = {}
    baseline_ref: str | None = None
    if needed and supervisor.ports.get_profile_fields is None:
        statuses.update(dict.fromkeys(needed, "not_loaded"))
    elif needed:
        try:
            raw = supervisor.ports.get_profile_fields(needed)
        except Exception:
            # 서비스 예외의 원문/인증정보를 모델에 보내지 않는다.
            statuses.update(dict.fromkeys(needed, "failed"))
        else:
            if not isinstance(raw, dict):
                raise SupervisorContractError("Profile read response must be a dict")
            if raw.get("status") in ("failed", "access_denied", "unavailable"):
                statuses.update(dict.fromkeys(needed, raw["status"]))
            else:
                wrapped = raw if "profile" in raw else {"profile": raw, "missing_info": []}
                if wrapped.get("changes"):
                    raise SupervisorContractError("Read-only Profile response cannot propose changes")
                read = normalize_profile_result(wrapped)
                fresh = read["profile"]
                for domain in FIELDS:
                    for key in METADATA:
                        old, new = current.get(domain, {}).get(key), fresh.get(domain, {}).get(key)
                        if old is not None and new is not None and old != new:
                            raise SupervisorContractError("Profile read snapshot mismatch; refresh via Context Loader")
                for path in needed:
                    value = _value(fresh, path)
                    # Omitted key is not proof of missing. Explicit null or missing status is.
                    status = read.get("field_status", {}).get(path)
                    if status is None:
                        status = ("available" if value is not None else "missing" if _has(fresh, path) else "not_loaded")
                    if status == "available" and value is None:
                        raise SupervisorContractError("Profile available field must contain a non-null value")
                    statuses[path] = status
                    if status in {"available", "missing"}:
                        _assign(verified, path, value)
                        _assign(profile, path, value)
                if ("financial_profile.holdings" in needed
                        and statuses.get("financial_profile.holdings") == "available"):
                    baseline_ref = read.get("baseline_ref")
    result = {"profile": profile,
              "missing_info": [path for path in paths if statuses[path] == "missing"],
              "field_status": {path: statuses[path] for path in paths}}
    unloaded = [path for path in paths if statuses[path] == "not_loaded"]
    if unloaded:
        result["unloaded_fields"] = unloaded
    if baseline_ref:
        result["baseline_ref"] = baseline_ref
    if verified:
        result.update(verified_read=True, verified_fields=verified)
    return result
