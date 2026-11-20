"""Agent 공통 입출력 타입. 세부 결과 구조는 각 전문 Agent에서 정의한다."""

from typing import Any, Literal, NotRequired, TypedDict


class AgentInput(TypedDict):
    task_id: str
    turn_id: str
    objective: str
    basic_profile_slice: NotRequired[dict[str, Any]]
    financial_profile_slice: NotRequired[dict[str, Any]]
    relevant_memories: NotRequired[list[dict[str, Any]]]
    upstream_results: NotRequired[dict[str, Any]]
    constraints: NotRequired[dict[str, Any]]


class AgentOutput(TypedDict):
    task_id: str
    turn_id: str
    agent: str
    status: Literal["completed", "needs_input", "failed"]
    payload: dict[str, Any]
    target_task_id: NotRequired[str]
    evidence_refs: NotRequired[list[str]]
    graph_refs: NotRequired[list[str]]  # 향후 지식그래프 근거 화면에 사용할 선택 항목.
    missing_fields: NotRequired[list[str]]
    warnings: NotRequired[list[str]]
