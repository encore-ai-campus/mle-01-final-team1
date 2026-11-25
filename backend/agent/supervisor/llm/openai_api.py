# OPENAI_API_KEY와 OPENAI_MODEL을 사용해 실제 OpenAI Responses API를 호출한다.
# 외부 SDK 없이 구조화 결정만 반환하며 키나 원본 API 오류 본문을 출력하지 않는다.
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from typing import Any

from pydantic import ValidationError
from .adapter import DecisionSchema
from .prompts import SYSTEM_PROMPT
from ..contracts import SupervisorContractError


class OpenAIConnectionError(RuntimeError):
    """실제 API 실패. Mock 또는 추정 결과로 대체하지 않는다."""


class OpenAIDecider:
    def __init__(self, api_key: str, model: str, *, timeout: float = 120):
        if not api_key.strip() or not model.strip():
            raise ValueError("OPENAI_API_KEY와 OPENAI_MODEL을 설정하세요.")
        self._api_key = api_key.strip()
        self.model = model.strip()
        self.timeout = timeout
        self.calls = 0
        self.usage: list[dict[str, Any]] = []
        self.input_sizes: list[dict[str, int]] = []

    @classmethod
    def from_env(cls) -> "OpenAIDecider":
        from dotenv import load_dotenv
        backend = Path(__file__).resolve().parents[3]
        # Shell에 설정된 환경변수 우선. 백엔드 .env만 읽고 변경하지 않는다.
        load_dotenv(backend / ".env", override=False)
        return cls(os.getenv("OPENAI_API_KEY", ""), os.getenv("OPENAI_MODEL", ""))

    def __call__(self, context: dict[str, Any]) -> dict[str, Any]:
        # Compact JSON removes incidental whitespace. Byte counts are diagnostics, not tokens.
        context_json = json.dumps(context, ensure_ascii=False, default=str, separators=(",", ":"))
        schema = DecisionSchema.model_json_schema()
        self.input_sizes.append({"prompt_bytes": len(SYSTEM_PROMPT.encode("utf-8")),
                                 "context_bytes": len(context_json.encode("utf-8")),
                                 "schema_bytes": len(json.dumps(schema, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))})
        payload = {
            "model": self.model,
            "instructions": SYSTEM_PROMPT,
            "input": context_json,
            "tools": [{"type": "function", "name": "supervisor_decision",
                       "description": "Return one structured Supervisor decision, not financial advice.",
                       "parameters": schema, "strict": False}],
            "tool_choice": {"type": "function", "name": "supervisor_decision"},
            "parallel_tool_calls": False,
            "store": False,
        }
        request = Request("https://api.openai.com/v1/responses",
                          data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                          headers={"Authorization": f"Bearer {self._api_key}",
                                   "Content-Type": "application/json"}, method="POST")
        self.calls += 1
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            hints = {400: "모델의 Responses API/함수 호출 지원 및 요청 형식을 확인하세요.",
                     401: "API 키를 확인하세요.", 403: "프로젝트의 API 권한을 확인하세요.",
                     404: "OPENAI_MODEL과 모델 접근 권한을 확인하세요.",
                     429: "API 사용량 한도 또는 결제 상태를 확인하세요."}
            raise OpenAIConnectionError(f"OpenAI HTTP {exc.code}: {hints.get(exc.code, 'API 요청 실패')}") from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise OpenAIConnectionError("OpenAI API 통신 또는 응답 읽기에 실패했습니다.") from None
        if not isinstance(raw, dict) or raw.get("status") != "completed":
            raise OpenAIConnectionError("OpenAI 응답이 정상 완료되지 않았습니다.")
        if isinstance(raw.get("usage"), dict):
            usage = raw["usage"]
            item = {k: usage[k] for k in ("input_tokens", "output_tokens", "total_tokens")
                    if type(usage.get(k)) is int}
            for group, field, label in (("input_tokens_details", "cached_tokens", "cached_input_tokens"),
                                         ("output_tokens_details", "reasoning_tokens", "reasoning_tokens")):
                details = usage.get(group)
                if isinstance(details, dict) and type(details.get(field)) is int:
                    item[label] = details[field]
            self.usage.append(item)
        calls = [item for item in raw.get("output", []) if isinstance(item, dict)
                 and item.get("type") == "function_call" and item.get("name") == "supervisor_decision"]
        if len(calls) != 1:
            raise SupervisorContractError("모델이 supervisor_decision을 정확히 한 번 반환하지 않았습니다.")
        try:
            parsed = DecisionSchema.model_validate_json(calls[0]["arguments"])
        except ValidationError as exc:
            # 값 또는 전체 응답 대신 스키마상의 위치와 오류 종류만 전달한다.
            errors = exc.errors(include_input=False, include_context=False, include_url=False)
            locations = [".".join(str(part)[:48] for part in e["loc"])[:160] + ": " + e["type"] for e in errors[:5]]
            raise SupervisorContractError("Decision 계약 오류: " + "; ".join(locations)) from None
        except (KeyError, TypeError, ValueError):
            raise SupervisorContractError("모델의 구조화 판단 출력이 Decision 계약과 일치하지 않습니다.") from None
        return parsed.model_dump(exclude_none=True)
