"""청년 금융 Multi-Agent v3.0의 공용 Main State 타입을 정의한다.

개별 전문 Agent의 Local State와 영속 저장 데이터는 이 파일에서 정의하지 않는다.
"""

from typing import Annotated, Any, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


# 병렬 전문 Agent가 반환한 결과를 Collector에 전달하기 위한 내부 채널 초기화 신호.
RESET_OUTBOX = {"__reset__": True}


def append_outbox(
    old: list[dict[str, Any]] | None,
    new: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """병렬 결과를 누적하고, 초기화 신호가 오면 이전 결과를 비운다."""
    old_items = list(old or [])
    new_items = list(new or [])
    reset_positions = [
        index for index, item in enumerate(new_items) if item.get("__reset__")
    ]
    if reset_positions:
        return new_items[reset_positions[-1] + 1 :]
    return old_items + new_items


class MainState(TypedDict, total=False):
    """최상위 공용 Key 20개와 별도의 내부 결과 수집 채널."""

    # 상담 및 요청 식별·진행 상태
    messages: Annotated[list[BaseMessage], add_messages]
    conversation_summary: str
    consultation_id: str
    turn_id: str
    phase: str

    # 상담에서 사용할 간단한 프로필·요청·가정·기억
    basic_profile: dict[str, Any]
    financial_profile: dict[str, Any]
    scenario_state: dict[str, Any] | None
    memory: dict[str, Any]
    intent: dict[str, Any] | None
    execution_plan: dict[str, Any] | None
    pending_input: list[dict[str, Any]]

    # Collector가 확정하는 전문 결과 및 검수 기록
    agent_results: dict[str, dict[str, Any]]
    review_results: dict[str, dict[str, Any]]
    superseded_results: dict[str, dict[str, Any]]
    final_advice: dict[str, Any] | None
    post_response: dict[str, Any] | None

    # 실제 저장 변경 후보 및 응답 이후 장기 기억 후보
    basic_profile_changes: list[dict[str, Any]]
    financial_profile_changes: list[dict[str, Any]]
    memory_candidates: list[dict[str, Any]]

    # 공용 20개 Key에 포함되지 않는 병렬 결과 수집 전용 내부 채널.
    # 전문 Agent는 agent_results를 직접 수정하지 않고 여기에만 결과를 쓴다.
    agent_outbox: Annotated[list[dict[str, Any]], append_outbox]
