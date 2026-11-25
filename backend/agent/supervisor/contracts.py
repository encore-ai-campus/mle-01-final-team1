# Supervisor의 LLM 결정·도구 입출력·부모 그래프 반환 계약을 정의한다.
# 작업 DAG 정규화와 계약 검증을 담당하며 내부 State는 state.py에 둔다.
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, NotRequired, TypedDict

# Legacy imports remain valid; the actual internal State is defined in state.py.
from .state import SupervisorWorkingState, ToolObservation


class SupervisorContractError(ValueError):
    """LLM 판단 또는 도구 반환이 공용 경계의 제약을 위반했다."""


class ProfileRequest(TypedDict):
    instruction: str
    is_temporary: bool
    current_profile: NotRequired[dict[str, Any]]  # injected by server, not LLM
    operation: NotRequired[Literal["inspect", "prepare"]]
    fields: NotRequired[list[str]]  # inspect: authenticated read paths
    required_fields: NotRequired[list[str]]  # prepare: validate the injected snapshot, not a DB read
    change_requested: NotRequired[bool]  # explicit user request; never an approval


class ExtractedIncome(TypedDict):
    type: Literal["income"]
    period: Literal["monthly", "annual"]
    amount_krw: int
    tax_basis: Literal["net", "gross", "unknown"]
    target_field: str | None  # canonical supported profile path, otherwise null
    raw_text: str


class IncomeAmbiguity(TypedDict):
    type: Literal["tax_basis_unknown", "unsupported_income_field"]
    raw_text: str
    amount_krw: int


class ProfileResult(TypedDict):
    profile: dict[str, Any]
    missing_info: list[str]  # canonical missing fields after normalization; not user questions
    changes: NotRequired[list[dict[str, Any]]]
    baseline_ref: NotRequired[str]
    field_status: NotRequired[dict[str, str]]  # per-field validity/read status (tools/ports.py)
    branch_specs: NotRequired[list[dict[str, Any]]]
    missing_inputs: NotRequired[list[str]]
    extracted_info: NotRequired[list[ExtractedIncome]]
    ambiguities: NotRequired[list[IncomeAmbiguity]]


class MemoryRequest(TypedDict):
    query: str
    limit: NotRequired[int]
    memory_types: NotRequired[list[str]]


class TaskDraft(TypedDict):
    """Supervisor가 작성하는 작업 지시. 실행 ID와 상태는 서버 소유다."""
    task_id: str
    agent: Literal["policy", "product", "fund"]
    objective: str
    depends_on: list[str]
    scenario_id: str | None


class PlanDraft(TypedDict):
    tasks: list[TaskDraft]


class Decision(TypedDict):
    kind: Literal["call_tool", "submit_plan", "ask_user", "compose"]
    reason: str
    tool_name: NotRequired[str]
    tool_input: NotRequired[dict[str, Any]]
    intent: NotRequired[dict[str, Any]]
    execution_plan: NotRequired[PlanDraft]
    scenario_state: NotRequired[dict[str, Any]]  # submit_plan과 함께 제출할 내부 분기 초안
    selected_scenario_id: NotRequired[str]  # verified prior advice branch
    retry_task_ids: NotRequired[list[str]]  # same-plan execution retry request, not status/run_id
    pending_input: NotRequired[list[dict[str, Any]]]
    goal_mode: NotRequired[Literal["merge", "replace"]]


@dataclass(frozen=True)
class SupervisorOutcome:
    """부모 Graph가 사용할 최소 반환. goto는 MainState의 새 키가 아니다."""

    update: dict[str, Any]
    next_node: Literal["plan_validator", "ask_user", "final_advice"]
    tool_calls: int = 0


ALLOWED_TOOLS = frozenset({"profile_agent", "retrieve_memory", "prepare_scenarios"})
ALLOWED_AGENTS = frozenset({"product", "policy", "fund"})
MAX_SCENARIOS = 3
MAX_TASKS = 12
INTENT_OPERATIONS = frozenset({"new", "continue", "revise", "compare", "explain"})
ANALYSIS_MODES = frozenset({"candidate_compare", "strategy_compare", "roadmap"})
PROFILE_UPDATE_STATUSES = frozenset({"not_needed", "pending", "waiting", "done"})
INTENT_FIELDS = frozenset({"purpose", "operation", "analysis_mode", "strategy_preference",
                           "supplied_values", "profile_update_status", "target_advice_ref"})


def validate_plan_intent(intent: Any) -> None:
    """계획 제출 시 최소 의도 계약을 강제한다. 임의 기본 의도를 만들지 않는다."""
    if not isinstance(intent, dict):
        raise SupervisorContractError("submit_plan에는 intent가 반드시 필요합니다.")
    if set(intent) - INTENT_FIELDS:
        raise SupervisorContractError("intent에는 공용 계약의 필드만 사용할 수 있습니다.")
    purpose = intent.get("purpose")
    if not isinstance(purpose, str) or not purpose.strip() or purpose.strip().lower() in ALLOWED_AGENTS:
        raise SupervisorContractError("intent.purpose에 사용자 목적을 명시하세요(Agent 이름 아님).")
    if not isinstance(intent.get("operation"), str) or intent["operation"] not in INTENT_OPERATIONS:
        raise SupervisorContractError("intent.operation은 new/continue/revise/compare/explain 중 하나여야 합니다.")
    if not isinstance(intent.get("supplied_values"), dict):
        raise SupervisorContractError("intent.supplied_values는 필수 dict입니다(없으면 {}).")
    if not isinstance(intent.get("profile_update_status"), str) or intent["profile_update_status"] not in PROFILE_UPDATE_STATUSES:
        raise SupervisorContractError("intent.profile_update_status가 누락되었거나 올바르지 않습니다.")
    mode = intent.get("analysis_mode")
    if mode is not None and (not isinstance(mode, str) or mode not in ANALYSIS_MODES):
        raise SupervisorContractError("intent.analysis_mode가 올바르지 않습니다.")
    if intent["operation"] == "compare" and mode is None:
        raise SupervisorContractError("compare 계획은 intent.analysis_mode로 후보/경제 전략/로드맵 비교를 구분하세요.")
    ref = intent.get("target_advice_ref")
    if ref is not None and (not isinstance(ref, str) or not ref.strip()):
        raise SupervisorContractError("intent.target_advice_ref는 유효한 참조 또는 null이어야 합니다.")


def validate_intent_scenarios(intent: dict[str, Any], scenario: dict[str, Any] | None) -> None:
    """경제적 행동/가정별 비교로 분류된 계획은 독립 분기 없이 발행할 수 없다."""
    validate_plan_intent(intent)
    mode = intent.get("analysis_mode")
    if mode == "candidate_compare" and len((scenario or {}).get("scenarios", [])) > 1:
        raise SupervisorContractError("상품·정책 후보 비교는 단일 전략 내부에서 처리하세요. 경제 전략 비교만 분기합니다.")
    comparative = mode == "strategy_compare" or (mode == "roadmap" and intent["operation"] == "compare")
    if comparative and len((scenario or {}).get("scenarios", [])) < 2:
        raise SupervisorContractError(
            "전략/재무 로드맵 비교는 2~3개 독립 시나리오와 각각의 작업 DAG가 필요합니다. submit_plan에 scenario_state를 함께 제출하거나 준비된 분기를 참조하세요."
        )


def validate_decision(data: dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise SupervisorContractError("Supervisor LLM은 dict 의사결정을 반환해야 합니다.")
    kind = data.get("kind")
    if not isinstance(kind, str) or kind not in {"call_tool", "submit_plan", "ask_user", "compose"}:
        raise SupervisorContractError("알 수 없는 Supervisor kind입니다.")
    if not isinstance(data.get("reason"), str):
        raise SupervisorContractError("판단 이유 reason이 필요합니다.")
    allowed = {"kind", "reason", "intent"} | {
        "call_tool": {"tool_name", "tool_input"},
        "submit_plan": {"execution_plan", "scenario_state", "selected_scenario_id", "goal_mode", "retry_task_ids"},
        "ask_user": {"pending_input", "goal_mode"},
        "compose": {"goal_mode"},
    }[kind]
    if set(data) - allowed:
        raise SupervisorContractError(f"{kind}: unrelated or protected Decision fields: " + ", ".join(sorted(map(str, set(data) - allowed))))
    if isinstance(data.get("intent"), dict) and set(data["intent"]) - INTENT_FIELDS:
        raise SupervisorContractError("intent contains unsupported fields")
    if kind == "call_tool":
        if data.get("tool_name") not in ALLOWED_TOOLS:
            raise SupervisorContractError("허용되지 않은 내부 도구입니다.")
        if not isinstance(data.get("tool_input"), dict):
            raise SupervisorContractError("tool_input은 dict이어야 합니다.")
    if data.get("goal_mode", "merge") not in {"merge", "replace"}:
        raise SupervisorContractError("Invalid goal_mode")
    if "intent" in data and not isinstance(data["intent"], dict):
        raise SupervisorContractError("intent는 dict이어야 합니다.")
    if "scenario_state" in data and (kind != "submit_plan" or not isinstance(data["scenario_state"], dict)):
        raise SupervisorContractError("scenario_state 초안은 submit_plan의 dict로만 제출하세요.")
    if "selected_scenario_id" in data:
        sid = data["selected_scenario_id"]
        if kind != "submit_plan" or not isinstance(sid, str) or not sid.strip():
            raise SupervisorContractError("selected_scenario_id는 submit_plan의 이전 분기 ID입니다.")
    if "retry_task_ids" in data:
        ids = data["retry_task_ids"]
        if (not isinstance(ids, list) or not 1 <= len(ids) <= MAX_TASKS
                or any(not isinstance(t, str) or not t.strip() or t != t.strip() for t in ids)
                or len(set(ids)) != len(ids)):
            raise SupervisorContractError("retry_task_ids must be unique nonempty current task IDs")
    if "intent" in data:
        intent = data["intent"]
        if "supplied_values" in intent and not isinstance(intent["supplied_values"], dict):
            raise SupervisorContractError("intent.supplied_values must be a dict when provided")
        if "profile_update_status" in intent and (not isinstance(intent["profile_update_status"], str)
                or intent["profile_update_status"] not in PROFILE_UPDATE_STATUSES):
            raise SupervisorContractError("intent.profile_update_status is invalid")
    if kind == "submit_plan":
        # The draft may omit server-owned/default fields; the published intent must not.
        intent = data.get("intent")
        if not isinstance(intent, dict):
            raise SupervisorContractError("submit_plan에는 intent가 반드시 필요합니다.")
        validate_plan_intent({"supplied_values": {}, "profile_update_status": "not_needed", **intent})


def validate_questions(items: Any, *, reserved_ids: set[str] | None = None) -> list[dict[str, Any]]:
    """질문 초안을 정규화한다. 답변/해결 상태는 LLM이 만들 수 없다."""
    from .tools.profile_fields import canonical_profile_path

    if not isinstance(items, list) or not items:
        raise SupervisorContractError("필수 확인 질문 pending_input이 없습니다.")
    explicit_ids: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise SupervisorContractError("질문은 dict이어야 합니다.")
        sid = item.get("id")
        if sid is not None:
            if not isinstance(sid, str) or not sid.strip() or sid != sid.strip():
                raise SupervisorContractError("질문 ID가 올바르지 않습니다.")
            if sid in explicit_ids:
                raise SupervisorContractError("명시 질문 ID가 중복되었습니다.")
            explicit_ids.add(sid)
    used_ids = set(reserved_ids or ()) | explicit_ids
    result = []
    counter = 1
    for item in items:
        if set(item) - {"id", "question", "fields", "target", "scenario_id", "status"}:
            raise SupervisorContractError("질문 초안에 답변 또는 정의되지 않은 필드를 넣을 수 없습니다.")
        question, fields = item.get("question"), item.get("fields")
        if not isinstance(question, str) or not question.strip():
            raise SupervisorContractError("비어 있는 사용자 질문을 요청했습니다.")
        if not isinstance(fields, list) or not fields or not all(isinstance(f, str) and f.strip() for f in fields):
            raise SupervisorContractError("질문의 fields[]가 올바르지 않습니다.")
        target = item.get("target", "supervisor")
        if not isinstance(target, str) or not target.strip() or target != target.strip():
            raise SupervisorContractError("질문 target이 올바르지 않습니다.")
        scenario_id = item.get("scenario_id")
        if scenario_id is not None and (not isinstance(scenario_id, str) or not scenario_id.strip()):
            raise SupervisorContractError("질문의 scenario_id가 올바르지 않습니다.")
        if item.get("status", "pending") != "pending":
            raise SupervisorContractError("ask_user 질문은 pending 상태여야 합니다.")
        normalized_fields = []
        for field in fields:
            field = field.strip()
            try:
                field = canonical_profile_path(field)
            except SupervisorContractError:
                # 의도·대상·전문 계산 조건은 프로필 필드가 아니다.
                if target == "profile" or field.startswith(("basic_profile.", "financial_profile.")):
                    raise
            if field not in normalized_fields:
                normalized_fields.append(field)
        sid = item.get("id")
        if sid is None:
            while f"ask_{counter:02d}" in used_ids:
                counter += 1
            sid = f"ask_{counter:02d}"
            used_ids.add(sid)
            counter += 1
        result.append({"id": sid, "question": question.strip(), "fields": normalized_fields,
                       "target": target, "scenario_id": scenario_id, "status": "pending"})
    return result


def validate_plan(plan: Any, scenario: dict[str, Any] | None) -> None:
    if not isinstance(plan, dict) or not isinstance(plan.get("tasks"), list):
        raise SupervisorContractError("execution_plan.tasks[]가 필요합니다.")
    tasks = plan["tasks"]
    if not tasks or len(tasks) > MAX_TASKS:
        raise SupervisorContractError(f"작업은 1~{MAX_TASKS}개여야 합니다.")
    if type(plan.get("revision")) is not int or plan["revision"] < 1:
        raise SupervisorContractError("계획 revision이 올바르지 않습니다.")
    ids: set[str] = set()
    scenario_ids = {s["id"] for s in (scenario or {}).get("scenarios", [])}
    tasks_by_id: dict[str, dict[str, Any]] = {}
    for task in tasks:
        if not isinstance(task, dict) or not isinstance(task.get("agent"), str) or task["agent"] not in ALLOWED_AGENTS:
            raise SupervisorContractError("전문 작업은 policy/product/fund만 허용합니다.")
        task_id = task.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip() or task_id != task_id.strip() or task_id in ids:
            raise SupervisorContractError("task_id가 중복되거나 비었습니다.")
        ids.add(task_id)
        tasks_by_id[task_id] = task
        if not isinstance(task.get("objective"), str) or not task["objective"].strip():
            raise SupervisorContractError("작업 objective가 없습니다.")
        if task.get("scenario_id") is not None and (not isinstance(task["scenario_id"], str) or task["scenario_id"] not in scenario_ids):
            raise SupervisorContractError("존재하지 않는 scenario_id를 참조합니다.")
        if not isinstance(task.get("depends_on"), list):
            raise SupervisorContractError("depends_on은 list이어야 합니다.")
        if task.get("status") not in ("pending", "running", "completed", "needs_input", "failed", "skipped"):
            raise SupervisorContractError("작업 상태가 올바르지 않습니다.")
    # 선언된 각 분기에 독립 작업이 최소 하나 있어야 한다. 공통 조회만으로 대체 불가.
    assigned = {task.get("scenario_id") for task in tasks if task.get("scenario_id") is not None}
    if scenario_ids - assigned:
        missing = ", ".join(sorted(scenario_ids - assigned))
        raise SupervisorContractError(f"독립 실행 작업이 배정되지 않은 시나리오: {missing}. 각 분기의 전체 경로를 배정하세요.")
    edges = {task["task_id"]: task.get("depends_on", []) for task in tasks}
    for task_id, deps in edges.items():
        if any(not isinstance(d, str) for d in deps):
            raise SupervisorContractError("작업 의존성은 문자열 task_id 목록이어야 합니다.")
        if len(deps) != len(set(deps)) or any(d not in ids or d == task_id for d in deps):
            raise SupervisorContractError("잘못된 작업 의존성입니다.")
        own_scenario = tasks_by_id[task_id].get("scenario_id")
        for dep in deps:
            parent_scenario = tasks_by_id[dep].get("scenario_id")
            # 분기 작업은 같은 분기나 공통 조회 결과에만 의존할 수 있다.
            if parent_scenario is not None and parent_scenario != own_scenario:
                raise SupervisorContractError("다른 시나리오의 전문 결과에 의존할 수 없습니다.")
    visited: set[str] = set()
    active: set[str] = set()

    def dfs(node: str) -> None:
        if node in active:
            raise SupervisorContractError("작업 DAG에 순환 의존성이 있습니다.")
        if node in visited:
            return
        active.add(node)
        for parent in edges[node]:
            dfs(parent)
        active.remove(node)
        visited.add(node)

    for task_id in ids:
        dfs(task_id)

    # 독립 분기 우선: 공통 작업은 실제 두 개 이상 분기가 필요로 하는 사실 조회에 한정한다.
    # 자연어 objective의 의미적 동일성은 LLM/Evidence가 확인하며, 여기서는 구조만 강제한다.
    if scenario_ids:
        common_ids = {tid for tid, task in tasks_by_id.items() if task.get("scenario_id") is None}
        common_consumers: dict[str, set[str]] = {tid: set() for tid in common_ids}
        for task_id, task in tasks_by_id.items():
            sid = task.get("scenario_id")
            if sid is None:
                if task["agent"] == "fund":
                    raise SupervisorContractError("시나리오가 있으면 Fund 계산은 각 scenario_id에 배정하세요. 공통 작업은 분기 무관 사실 조회만 허용합니다.")
                continue
            # 직접 또는 간접적으로 이 분기에 필요한 공통 조회를 기록한다.
            stack = list(edges[task_id])
            seen_deps: set[str] = set()
            while stack:
                dep = stack.pop()
                if dep in seen_deps:
                    continue
                seen_deps.add(dep)
                if dep in common_consumers:
                    common_consumers[dep].add(sid)
                stack.extend(edges[dep])
        for task_id in sorted(common_ids):
            if len(common_consumers[task_id]) < 2:
                raise SupervisorContractError(
                    f"공통 작업 {task_id}는 두 개 이상의 분기에서 사용되어야 합니다. "
                    "한 분기의 조회는 해당 scenario_id에 배정하고, 불필요한 작업은 제거하세요."
                )

    # 같은 작업을 ID만 바꿔 중복 발행하는 경우를 차단한다. 동일 Agent의 다른 업무는 허용.
    signatures = set()
    for task in tasks:
        signature = (task["agent"], task.get("scenario_id"),
                     " ".join(task["objective"].split()).casefold(),
                     tuple(sorted(task["depends_on"])))
        if signature in signatures:
            raise SupervisorContractError("동일 목적·분기·선행 조건의 중복 작업입니다.")
        signatures.add(signature)


def execution_waves(plan: dict[str, Any], scenario: dict[str, Any] | None = None) -> list[list[str]]:
    """검증된 DAG를 실행 가능 단계로 표현한다. State에는 저장하지 않는 읽기 전용 뷰."""
    validate_plan(plan, scenario)
    remaining = list(plan["tasks"])
    done: set[str] = set()
    waves: list[list[str]] = []
    while remaining:
        batch = [t["task_id"] for t in remaining if set(t["depends_on"]) <= done]
        if not batch:
            raise SupervisorContractError("실행 가능한 작업이 없는 DAG입니다.")
        waves.append(batch)
        done.update(batch)
        remaining = [t for t in remaining if t["task_id"] not in done]
    return waves


def scenario_plan_view(plan: dict[str, Any], scenario: dict[str, Any] | None) -> dict[str, Any] | None:
    """분기별 계획을 진단용으로 표시한다. 공용 State·운영 실행에는 새 필드를 추가하지 않는다."""
    validate_plan(plan, scenario)
    if not scenario:
        return None
    tasks = plan["tasks"]
    by_id = {t["task_id"]: t for t in tasks}
    common = [t["task_id"] for t in tasks if t.get("scenario_id") is None]
    branches = []
    for branch in scenario["scenarios"]:
        sid = branch["id"]
        own = [t["task_id"] for t in tasks if t.get("scenario_id") == sid]
        required = set(own)
        stack = list(own)
        while stack:
            for dep in by_id[stack.pop()]["depends_on"]:
                if dep not in required:
                    required.add(dep)
                    stack.append(dep)
        used_common = [tid for tid in common if tid in required]
        # 분기에 필요한 공통 선행 작업만 포함한 국소 위상 단계(정적 진단용).
        remaining = [t for t in tasks if t["task_id"] in required]
        done: set[str] = set()
        waves = []
        while remaining:
            batch = [t["task_id"] for t in remaining if set(t["depends_on"]) <= done]
            waves.append(batch)
            done.update(batch)
            remaining = [t for t in remaining if t["task_id"] not in done]
        consumed = {dep for tid in own for dep in by_id[tid]["depends_on"]}
        branches.append({"scenario_id": sid, "label": branch["label"],
                         "task_ids": own, "common_dependency_ids": used_common,
                         "terminal_task_ids": [tid for tid in own if tid not in consumed],
                         "waves": waves})
    return {"common_task_ids": common, "branches": branches}


def normalize_plan(
    raw: Any,
    previous: dict[str, Any] | None,
    scenario_changed: bool,
    *,
    input_changed: bool = False,
    review_rejected: bool = False,
    retry_task_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise SupervisorContractError("계획 생성 결과는 dict이어야 합니다.")
    tasks = raw.get("tasks")
    if not isinstance(tasks, list):
        raise SupervisorContractError("계획의 tasks[]가 필요합니다.")
    cleaned_tasks = []
    for task in tasks:
        if not isinstance(task, dict):
            raise SupervisorContractError("계획 작업 형식이 잘못되었습니다.")
        cleaned_tasks.append({
            "task_id": task.get("task_id"),
            "agent": task.get("agent"),
            "objective": task.get("objective"),
            "scenario_id": task.get("scenario_id"),
            "depends_on": task.get("depends_on", []),
            "status": "pending",
            "run_id": None,
        })
    old = previous if isinstance(previous, dict) else {}
    old_sig = [{k: t.get(k) for k in ("task_id", "agent", "objective", "scenario_id", "depends_on")}
               for t in old.get("tasks", []) if isinstance(t, dict)]
    new_sig = [{k: t[k] for k in ("task_id", "agent", "objective", "scenario_id", "depends_on")}
               for t in cleaned_tasks]
    old_revision = old.get("revision") if type(old.get("revision")) is int else 0
    changed = old_sig != new_sig or scenario_changed or input_changed
    revision = (old_revision + 1) if changed or not old_revision else old_revision
    if old_revision and not changed and (not review_rejected or retry_task_ids):
        # 동일 계획 재방문은 이미 끝난 전문 작업을 다시 실행하지 않는다.
        # 실행 입력이 바뀌었다면 input_changed/scenario_changed로 새 revision을 만든다.
        for new_task, old_task in zip(cleaned_tasks, old.get("tasks", [])):
            old_status = old_task.get("status")
            old_run_id = old_task.get("run_id")
            if (old_status in {"pending", "running", "completed", "needs_input", "failed", "skipped"}
                    and (old_run_id is None or isinstance(old_run_id, str))):
                new_task["status"] = old_status
                new_task["run_id"] = old_run_id
        # Same plan/input: invalidate only selected roots and their transitive dependents.
        affected = set(retry_task_ids)
        if affected - {t["task_id"] for t in cleaned_tasks}:
            raise SupervisorContractError("Retry target is absent from the submitted plan")
        while True:
            expanded = affected | {t["task_id"] for t in cleaned_tasks
                                   if isinstance(t.get("depends_on"), list)
                                   and any(isinstance(dep, str) and dep in affected for dep in t["depends_on"])}
            if expanded == affected:
                break
            affected = expanded
        for task in cleaned_tasks:
            if task["task_id"] in affected:
                task.update(status="pending", run_id=None)
    retry_count = old.get("review_retry_count", 0)
    if type(retry_count) is not int or not 0 <= retry_count <= 1:
        raise SupervisorContractError("외부 Reflection 재계획 상한을 초과했습니다.")
    if review_rejected:
        if retry_count >= 1:
            raise SupervisorContractError("외부 Reflection 재계획 1회 상한을 초과했습니다.")
        retry_count += 1
    result = {
        "revision": revision,
        "review_retry_count": retry_count,
        "tasks": cleaned_tasks,
    }
    return result
