"""v3.0 전문 Agent 공통 입출력 계약을 정의한다.

전문 Agent별 후보·검색·계산 결과의 상세 스키마는 각 Agent가 정의한다.
Supervisor 내부 profile_node에는 이 전문 Agent 계약을 강제하지 않는다.
"""

from typing import Any, Literal, NotRequired, TypedDict


class RevisionFeedback(TypedDict):
    """같은 요청에서 검증 반려 후 재실행할 때만 전달하는 최소 피드백."""

    previous_run_id: str
    issues: list[str]
    previous_output_ref: NotRequired[str]


class AgentInput(TypedDict):
    """Dispatcher가 전문 Agent에 전달하는 작업 단위 입력."""

    task_id: str
    run_id: str
    plan_revision: int
    scenario_id: str | None
    objective: str
    user_query: str
    profile_view: dict[str, Any]

    # 실제로 필요한 경우에만 전달하는 선택 입력.
    upstream_results: NotRequired[dict[str, Any]]
    extra_answers: NotRequired[dict[str, Any]]
    prior_advice: NotRequired[dict[str, Any]]
    revision_feedback: NotRequired[RevisionFeedback]


class AgentOutput(TypedDict):
    """전문 Agent가 내부 수집 채널로 반환하는 공통 결과 외피."""

    task_id: str
    run_id: str
    plan_revision: int
    scenario_id: str | None
    status: Literal["completed", "needs_input", "failed"]
    summary: str
    data: dict[str, Any]
    evidence_refs: list[str]
    warnings: list[str]
    needed_fields: list[str]
