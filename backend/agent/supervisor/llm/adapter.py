# 실제 LLM 연결용 선택적 어댑터. 패키지 설치·API Key는 서비스가 맡는다.
# 상위 State 계약과 실행 책임의 경계를 유지한다.
from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, ConfigDict

from .prompts import SYSTEM_PROMPT


class TaskDraftSchema(BaseModel):
    """고정 전문 Agent에 배정할 한 작업의 명령과 선행 의존성."""
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(description="Unique task ID; reuse on unchanged replan")
    agent: Literal["policy", "product", "fund"]
    objective: str = Field(min_length=1, description="Self-contained Korean work order: purpose, scope, required output, relevant constraints and use of predecessor results")
    depends_on: list[str] = Field(default_factory=list, description="IDs of ALL mandatory predecessor tasks; empty for independent tasks")
    scenario_id: str | None = Field(default=None, description="Own prepared scenario ID for every branch-dependent task; null ONLY for a non-scenario request or invariant product/policy fact lookup actually needed by at least two branches; never a shared Fund calculation")


class PlanDraftSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tasks: list[TaskDraftSchema] = Field(min_length=1, max_length=12, description="Complete DAG for the entire request: each scenario has its own full independent path by default; share only identical invariant factual product/policy lookups required by at least two branches; no cross-scenario comparison tasks")


class IntentDraftSchema(BaseModel):
    """공용 intent의 허용 필드. submit_plan은 코드에서 필수 필드를 재검증한다."""
    model_config = ConfigDict(extra="forbid")
    purpose: str | None = Field(default=None, description="Required for submit_plan: user's actual goal, not an agent name")
    operation: Literal["new", "continue", "revise", "compare", "explain"] | None = None
    analysis_mode: Literal["candidate_compare", "strategy_compare", "roadmap"] | None = Field(default=None, description="Required for compare plans: candidate_compare for same-condition product candidates; strategy_compare for alternative financial actions/assumptions needing separate scenario DAGs")
    strategy_preference: Any = None
    supplied_values: dict[str, Any] | None = Field(default=None, description="Required for submit_plan: only actual one-turn user values, {} if none; never scenario-specific assumptions")
    profile_update_status: Literal["not_needed", "pending", "waiting", "done"] | None = Field(default=None, description="Required for submit_plan: actual profile persistence status only; never invent done")
    target_advice_ref: str | None = None


class DecisionSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["call_tool", "submit_plan", "ask_user", "compose"]
    reason: str
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = Field(default=None, description="For prepare_scenarios: scenarios list with unique optional id, label, overrides, assumptions. Missing IDs are assigned by Python; use the exact returned IDs in subsequent task plans. Never invent baseline_ref or holding_ref.")
    intent: IntentDraftSchema | None = Field(default=None, description="MANDATORY for submit_plan: purpose, operation, supplied_values, profile_update_status; analysis_mode for compare")
    execution_plan: PlanDraftSchema | None = None
    pending_input: list[dict[str, Any]] | None = None
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
            HumanMessage(content=json.dumps(context, ensure_ascii=False, default=str)),
        ])
        if hasattr(result, "model_dump"):
            data = result.model_dump(exclude_none=True)
        elif isinstance(result, dict):
            data = {key: value for key, value in result.items() if value is not None}
        else:
            raise ValueError("LLM structured output 형식 오류")
        return data
