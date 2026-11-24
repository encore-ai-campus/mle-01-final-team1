# Supervisor Subgraph 전용 내부 State와 초기화 규칙을 정의한다.
# MainState의 읽기 복사본, 도구 관찰, 판단·계획·검증 상태를 분리해 관리한다.
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, NotRequired, TypedDict


# 상위 MainState에서 Supervisor가 읽을 수 있는 필드만 선택한다.
# 부모 State의 타입을 가져오거나 변경하지 않는다.
MAIN_STATE_READ_KEYS = (
    "messages", "conversation_summary", "consultation_id", "turn_id", "phase",
    "basic_profile", "financial_profile", "intent", "scenario_state", "execution_plan",
    "pending_input", "memory", "agent_results", "review_results", "superseded_results",
    "final_advice", "post_response", "basic_profile_changes", "financial_profile_changes",
)


class ToolObservation(TypedDict):
    """도구 1회 호출의 구조화된 관찰. MainState에는 직접 저장하지 않는다."""

    name: str
    result: dict[str, Any]
    request: NotRequired[dict[str, Any]]


class SupervisorWorkingState(TypedDict, total=False):
    """Supervisor 내부 4개 노드가 공유하는 요청 단위 작업 상태."""

    # 1. 입력: 읽기 전용 MainState 복사본. 초기 기억 조회 결과를 포함한다.
    source: dict[str, Any]

    # 2. Reason / Tools: 최신 구조화 결정과 누적 도구 관찰.
    decision: dict[str, Any]
    observations: list[ToolObservation]
    reason_calls: int
    tool_calls: int

    # 3. Plan: 정규화된 MainState 변경 후보와 생성 오류.
    candidate_patch: dict[str, Any]
    candidate_error: str | None

    # 4. Finalize: 검증 오류, 재판단 제한 및 실행 제어.
    issues: list[str]
    repair_count: int
    retry: bool
    done: bool

    # 5. 최종 반환: 검증된 부분 업데이트와 다음 상위 노드.
    main_patch: dict[str, Any]
    next_node: str


def copy_main_source(main_state: Mapping[str, Any]) -> dict[str, Any]:
    """허용된 부모 입력만 깊게 복사하여 원본 변경을 방지한다."""
    return {key: deepcopy(main_state[key]) for key in MAIN_STATE_READ_KEYS if key in main_state}


def create_working_state(source: dict[str, Any]) -> SupervisorWorkingState:
    """새 실행마다 내부 제어값을 초기화한다. 미생성 결과는 미로딩으로 둔다."""
    return {
        "source": source,
        "observations": [],
        "tool_calls": 0,
        "reason_calls": 0,
        "repair_count": 0,
        "issues": [],
        "done": False,
        "retry": False,
    }
