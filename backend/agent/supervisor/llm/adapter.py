# 실제 LLM 연결용 선택적 어댑터. 패키지 설치·API Key는 서비스가 맡는다.
# 상위 State 계약과 실행 책임의 경계를 유지한다.
from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, ConfigDict

from .prompts import SYSTEM_PROMPT


class TaskDraftSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(description="고유 작업 ID")
    agent: Literal["policy", "product", "fund"]
    objective: str = Field(min_length=1, description="한국어 1~2문장: 이번 작업 목적·필요 결과·고유 제약")
    depends_on: list[str] = Field(default_factory=list, description="직접 선행 task_id")
    scenario_id: str | None = Field(default=None, description="해당 분기 ID; 비분기 또는 공통 조회는 null")


class PlanDraftSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tasks: list[TaskDraftSchema] = Field(min_length=1, max_length=12, description="전체 작업 DAG")


class ScenarioBranchSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = Field(default=None, description="작업이 참조할 고유 분기 ID")
    label: str = Field(min_length=1)
    overrides: dict[str, Any] = Field(default_factory=dict, description="임시 프로필 값")
    assumptions: dict[str, Any] = Field(default_factory=dict, description="horizon_months, holding_actions, allocation_pct 등 가정")


class ScenarioDraftSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenarios: list[ScenarioBranchSchema] = Field(min_length=1, max_length=3)
    baseline_ref: str | None = Field(default=None, description="검증된 계약 스냅샷 참조")


class IntentDraftSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    purpose: str | None = Field(default=None, description="사용자 목적")
    operation: Literal["new", "continue", "revise", "compare", "explain"] | None = None
    analysis_mode: Literal["candidate_compare", "strategy_compare", "roadmap"] | None = None
    strategy_preference: Any = None
    supplied_values: dict[str, Any] | None = Field(default=None, description="이번 실제 제공값")
    profile_update_status: Literal["not_needed", "pending", "waiting", "done"] | None = None
    target_advice_ref: str | None = None


class DecisionSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["call_tool", "submit_plan", "ask_user", "compose"]
    reason: str
    tool_name: Literal["profile_agent", "retrieve_memory", "prepare_scenarios"] | None = None
    tool_input: dict[str, Any] | None = None
    intent: IntentDraftSchema | None = None
    execution_plan: PlanDraftSchema | None = None
    scenario_state: ScenarioDraftSchema | None = Field(default=None, description="submit_plan과 함께 제출할 분기 초안")
    selected_scenario_id: str | None = Field(default=None, description="이전 상담에서 선택한 분기 ID")
    pending_input: list[dict[str, Any]] | None = Field(default=None, description="ask_user만: question, fields, target, 필요시 id/scenario_id")
    goal_mode: Literal["merge", "replace"] | None = None


class StructuredLLMDecider:
    """ChatModel.with_structured_output()을 지원하는 모델에 연결한다.

    사용 예: StructuredLLMDecider(ChatOpenAI(model=..., temperature=0))
    """
    def __init__(self, model: Any, **structured_options: Any):
        self._chain = model.with_structured_output(DecisionSchema, **structured_options)

    def __call__(self, context: dict[str, Any]) -> dict[str, Any]:
        from langchain_core.messages import HumanMessage, SystemMessage

        result = self._chain.invoke([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=json.dumps(context, ensure_ascii=False, default=str, separators=(",", ":"))),
        ])
        if hasattr(result, "model_dump"):
            data = result.model_dump(exclude_none=True)
        elif isinstance(result, dict):
            data = {key: value for key, value in result.items() if value is not None}
        else:
            raise ValueError("LLM structured output 형식 오류")
        return data
