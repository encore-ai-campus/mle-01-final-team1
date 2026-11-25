# 실제 LLM 연결용 선택적 어댑터. 패키지 설치·API Key는 서비스가 맡는다.
# 상위 State 계약과 실행 책임의 경계를 유지한다.
from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, ConfigDict, WithJsonSchema

from .prompts import SYSTEM_PROMPT
from ..tools.profile_fields import READ_FIELDS, overlay_json_schema
from ..tools.ports import MEMORY_TYPES
from ..tools.scenarios import assumption_json_schema

# API에는 제한된 필드 스키마를 노출하되 raw dict를 보존해 Plan에서 안전한 형식 정규화가 가능하게 한다.
SuppliedValuesSchema = Annotated[dict[str, Any], WithJsonSchema(overlay_json_schema(supplied=True))]
OverrideSchema = Annotated[dict[str, Any], WithJsonSchema(overlay_json_schema(supplied=False))]
AssumptionsSchema = Annotated[dict[str, Any], WithJsonSchema(assumption_json_schema())]
ReadField = Annotated[str, WithJsonSchema({"type": "string", "enum": sorted(READ_FIELDS)})]
MemoryType = Annotated[str, WithJsonSchema({"type": "string", "enum": list(MEMORY_TYPES)})]


class ProfileInspectSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    operation: Literal["inspect"]
    instruction: str = Field(min_length=1)
    is_temporary: Literal[True]
    fields: list[ReadField] = Field(min_length=1)


class ProfilePrepareSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    operation: Literal["prepare"] = "prepare"
    instruction: str = Field(min_length=1)
    is_temporary: bool


class MemorySearchSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=1)
    limit: int | None = Field(default=None, ge=1, le=20)
    memory_types: list[MemoryType] | None = Field(default=None, min_length=1)


class PendingQuestionSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str | None = None
    question: str = Field(min_length=1)
    fields: list[str] = Field(min_length=1)
    target: str = "supervisor"
    scenario_id: str | None = None
    status: Literal["pending"] | None = None


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
    overrides: OverrideSchema = Field(default_factory=dict, description="평면 프로필 가정만")
    assumptions: AssumptionsSchema = Field(default_factory=dict, description="가상 금융 행동·배분·기간·추가 가정 notes")


class ScenarioDraftSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenarios: list[ScenarioBranchSchema] = Field(min_length=1, max_length=3)
    baseline_ref: str | None = Field(default=None, description="검증된 계약 스냅샷 참조")


class IntentDraftSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    purpose: str | None = Field(default=None, description="사용자 목적과 상담 목표 금액·기간·제약")
    operation: Literal["new", "continue", "revise", "compare", "explain"] | None = None
    analysis_mode: Literal["candidate_compare", "strategy_compare", "roadmap"] | None = None
    strategy_preference: Any = None
    supplied_values: SuppliedValuesSchema | None = Field(default=None, description="이번에 명시한 실제 프로필 값만(평면 키). 상담 목표 금액/기간은 purpose와 objective. 없으면 {}.")
    profile_update_status: Literal["not_needed", "pending", "waiting", "done"] | None = None
    target_advice_ref: str | None = None


class DecisionSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["call_tool", "submit_plan", "ask_user", "compose"]
    reason: str
    tool_name: Literal["profile_agent", "retrieve_memory", "prepare_scenarios"] | None = None
    tool_input: ProfileInspectSchema | ProfilePrepareSchema | MemorySearchSchema | ScenarioDraftSchema | None = None
    intent: IntentDraftSchema | None = None
    execution_plan: PlanDraftSchema | None = None
    scenario_state: ScenarioDraftSchema | None = Field(default=None, description="submit_plan과 함께 제출할 분기 초안")
    selected_scenario_id: str | None = Field(default=None, description="이전 상담에서 선택한 분기 ID")
    pending_input: list[PendingQuestionSchema] | None = Field(default=None, description="ask_user만: 질문 초안; 답변 및 해결 상태 금지")
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
