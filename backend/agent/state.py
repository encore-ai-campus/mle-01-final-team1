"""Supervisor 최상위 State(v1.2). 전문 Agent의 내부 State는 별도로 관리한다."""

from typing import Any, TypedDict


class SupervisorState(TypedDict, total=False):
    messages: list[dict[str, str]]
    conversation_summary: str
    consultation_id: str
    turn_id: str
    phase: str
    basic_profile: dict[str, Any]
    financial_profile: dict[str, Any]
    memory: dict[str, Any]
    intent: dict[str, Any]
    execution_plan: dict[str, Any]
    pending_input: dict[str, Any] | None
    agent_results: dict[str, dict[str, Any]]
    review_results: dict[str, dict[str, Any]]
    final_advice: dict[str, Any] | None
    basic_profile_changes: list[dict[str, Any]]
    financial_profile_changes: list[dict[str, Any]]
    memory_candidates: list[dict[str, Any]]
