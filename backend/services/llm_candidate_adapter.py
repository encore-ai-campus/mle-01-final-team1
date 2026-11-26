"""실제 LLM Generator·Critic을 연결하기 위한 선택형 어댑터.

기본 시험 실행은 외부 호출 없이 TemplateGenerator/RuleBasedCritic을 사용한다.
이 모듈은 API 키가 준비된 경우에만 명시적으로 선택해 사용한다.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .golden_candidate_pipeline import RuleBasedCritic, TemplateGenerator


class JsonCompletionClient(Protocol):
    def complete_json(
        self, *, system_prompt: str, user_prompt: str, model: str
    ) -> dict[str, Any]: ...


@dataclass
class LlmSettings:
    provider: str = "openai"
    model: str = "gpt-4o-mini"
    api_key: str | None = None
    max_attempts: int = 3
    retry_base_seconds: float = 1.0
    input_cost_per_1k: float = 0.0
    output_cost_per_1k: float = 0.0

    @classmethod
    def from_env(cls) -> "LlmSettings":
        return cls(
            provider=os.getenv("FINANCE_LLM_PROVIDER", "openai"),
            model=os.getenv("FINANCE_LLM_MODEL", "gpt-4o-mini"),
            api_key=os.getenv("FINANCE_LLM_API_KEY"),
            max_attempts=int(os.getenv("FINANCE_LLM_MAX_ATTEMPTS", "3")),
            retry_base_seconds=float(os.getenv("FINANCE_LLM_RETRY_BASE_SECONDS", "1")),
            input_cost_per_1k=float(os.getenv("FINANCE_LLM_INPUT_COST_PER_1K", "0")),
            output_cost_per_1k=float(os.getenv("FINANCE_LLM_OUTPUT_COST_PER_1K", "0")),
        )


@dataclass
class UsageLedger:
    call_count: int = 0
    retry_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0

    def record(self, usage: dict[str, Any], settings: LlmSettings) -> None:
        prompt = int(usage.get("prompt_tokens", 0) or 0)
        completion = int(usage.get("completion_tokens", 0) or 0)
        total = int(usage.get("total_tokens", prompt + completion) or 0)
        if total and not prompt and not completion:
            completion = total
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.total_tokens += total or prompt + completion
        self.total_cost_usd += (
            prompt / 1000 * settings.input_cost_per_1k
            + completion / 1000 * settings.output_cost_per_1k
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "llm_calls": self.call_count,
            "retry_count": self.retry_count,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost_usd": round(self.total_cost_usd, 8),
        }


class RetryingJsonClient:
    def __init__(
        self,
        client: JsonCompletionClient,
        *,
        settings: LlmSettings,
        ledger: UsageLedger,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.settings = settings
        self.ledger = ledger
        self.sleeper = sleeper

    def complete_json(
        self, *, system_prompt: str, user_prompt: str, model: str | None = None
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        attempts = max(1, self.settings.max_attempts)
        for attempt in range(attempts):
            self.ledger.call_count += 1
            try:
                result = self.client.complete_json(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    model=model or self.settings.model,
                )
                self.ledger.record(result.get("usage") or {}, self.settings)
                if not isinstance(result.get("data"), dict):
                    raise ValueError("LLM JSON 응답에 data 객체가 없습니다.")
                return result
            except Exception as exc:  # noqa: BLE001 - 마지막 오류를 호출자에게 전달
                last_error = exc
                if attempt + 1 >= attempts:
                    break
                self.ledger.retry_count += 1
                self.sleeper(self.settings.retry_base_seconds * (2**attempt))
        raise RuntimeError(f"LLM JSON 호출이 {attempts}회 실패했습니다: {last_error}") from last_error


class OpenAIJsonClient:
    """OpenAI SDK를 선택적으로 로드하는 JSON 응답 클라이언트."""

    def __init__(self, settings: LlmSettings) -> None:
        if not settings.api_key:
            raise ValueError("FINANCE_LLM_API_KEY가 설정되지 않았습니다.")
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - 설치 환경에서만 실행
            raise RuntimeError("실제 LLM 사용에는 openai 패키지가 필요합니다.") from exc
        self.client = OpenAI(api_key=settings.api_key)

    def complete_json(
        self, *, system_prompt: str, user_prompt: str, model: str
    ) -> dict[str, Any]:
        response = self.client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or "{}"
        usage = response.usage.model_dump() if response.usage else {}
        return {"data": json.loads(content), "usage": usage}


class LLMGenerator:
    provider = "llm"
    prompt_version = "finance-candidate-llm-v1"

    def __init__(self, client: RetryingJsonClient) -> None:
        self.client = client
        self.ledger = client.ledger

    def generate(
        self,
        route: str,
        context: dict[str, Any],
        graph_row: dict[str, Any] | None,
        candidate_id: str,
        snapshot_version: str,
    ) -> dict[str, Any]:
        grounded = TemplateGenerator().generate(
            route, context, graph_row, candidate_id, snapshot_version
        )
        prompt = {
            "route": route,
            "context": context,
            "graph_path": (graph_row or {}).get("graph_path", []),
            "instruction": "자연스러운 사회초년생 금융상담 질문과 기준 정답 문장만 작성하세요.",
        }
        response = self.client.complete_json(
            system_prompt=(
                "실제 근거만 사용합니다. ID, 수치, 관계를 새로 만들지 말고 JSON으로 반환하세요."
            ),
            user_prompt=json.dumps(prompt, ensure_ascii=False),
        )
        data = response["data"]
        if not str(data.get("question", "")).strip() or not str(data.get("reference", "")).strip():
            raise ValueError("LLM Generator는 question과 reference를 모두 반환해야 합니다.")
        grounded["question"] = data["question"]
        grounded["reference"] = data["reference"]
        grounded["generator"] = {
            "provider": self.provider,
            "model": self.client.settings.model,
            "prompt_version": self.prompt_version,
            "status": "generated",
            "usage": response.get("usage", {}),
            "grounding_policy": "ids_and_expected_facts_from_code",
        }
        return grounded


class LLMCritic:
    provider = "rules+llm"

    def __init__(self, client: RetryingJsonClient) -> None:
        self.client = client
        self.ledger = client.ledger

    def critique(
        self,
        candidate: dict[str, Any],
        contexts: dict[str, dict[str, Any]],
        graph_rows: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        local = RuleBasedCritic().critique(candidate, contexts, graph_rows)
        if local["verdict"] != "pass":
            local["provider"] = "rules"
            local["llm_skipped"] = True
            return local
        response = self.client.complete_json(
            system_prompt=(
                "후보 질문의 자연스러움과 route 분류만 평가하세요. 근거 ID와 수치는 코드 검사를 우선합니다."
            ),
            user_prompt=json.dumps(
                {
                    "question": candidate["question"],
                    "reference": candidate["reference"],
                    "route": candidate["expected_route"],
                    "difficulty": candidate["difficulty"],
                    "hop_count": candidate["hop_count"],
                },
                ensure_ascii=False,
            ),
        )
        data = response["data"]
        llm_pass = data.get("verdict") == "pass"
        return {
            "provider": self.provider,
            "verdict": "pass" if llm_pass else "fail",
            "checks": {**local["checks"], "llm_naturalness": llm_pass},
            "reasons": [] if llm_pass else data.get("reasons", ["llm_naturalness"]),
            "usage": response.get("usage", {}),
        }
