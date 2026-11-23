"""Langfuse 연결과 민감정보 제거를 담당한다. 실행 추적은 명시적으로 활성화할 때만 전송한다."""

from __future__ import annotations

import json
import math
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import load_dotenv

if TYPE_CHECKING:
    from langfuse.langchain import CallbackHandler
    from langfuse.types import MaskOtelSpansParams, MaskOtelSpansResult


# Load only the backend's own environment file (not the repository root).
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off", ""})

# Export a small allowlist of operational attributes, never arbitrary state.
_SAFE_TEXT_FIELDS = frozenset({
    "langfuse.trace.name",
    "langfuse.observation.name",
    "langfuse.observation.type",
    "langfuse.observation.level",
    "langfuse.observation.model.name",
    "gen_ai.operation.name",
    "gen_ai.system",
    "gen_ai.request.model",
    "gen_ai.response.model",
})
_SAFE_METRIC_FIELDS = frozenset({
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "gen_ai.usage.total_tokens",
    "gen_ai.usage.cost",
    "gen_ai.usage.cache_read_input_tokens",
    "gen_ai.usage.cache_creation_input_tokens",
    "llm.token_usage.prompt_tokens",
    "llm.token_usage.completion_tokens",
    "llm.token_usage.total_tokens",
    "langfuse.observation.usage.input",
    "langfuse.observation.usage.output",
    "langfuse.observation.usage.total",
})
_SAFE_DIAGNOSTIC_TEXT = re.compile(r"^[a-zA-Z0-9_.:/ -]{1,128}$")
_SAFE_JSON_METRIC_FIELDS = frozenset({
    "langfuse.observation.usage_details",
    "langfuse.observation.cost_details",
})
_SAFE_JSON_METRIC_KEYS = frozenset({
    "input", "output", "total", "input_cached", "input_cache_creation",
    "output_reasoning", "prompt_tokens", "completion_tokens", "total_tokens",
})


def is_langfuse_enabled() -> bool:
    """Return whether sending execution traces is explicitly enabled."""
    flag = os.getenv("LANGFUSE_ENABLED", "false").strip().lower()
    if flag in _TRUE_VALUES:
        return True
    if flag in _FALSE_VALUES:
        return False
    raise ValueError("LANGFUSE_ENABLED must be true or false")


def _safe_attribute(name: str, value: object) -> bool:
    if name in _SAFE_METRIC_FIELDS:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name in _SAFE_TEXT_FIELDS:
        return isinstance(value, str) and _SAFE_DIAGNOSTIC_TEXT.fullmatch(value) is not None
    return False


def _clean_metric_json(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        decoded = json.loads(value)
    except (ValueError, TypeError):
        return None
    if not isinstance(decoded, dict):
        return None
    clean = {
        key: number
        for key, number in decoded.items()
        if key in _SAFE_JSON_METRIC_KEYS
        and isinstance(number, (int, float))
        and not isinstance(number, bool)
        and math.isfinite(number)
    }
    return json.dumps(clean, separators=(",", ":")) if clean else None


def _mask_otel_spans(*, params: MaskOtelSpansParams) -> MaskOtelSpansResult | None:
    """Drop unapproved attributes before traces are exported to Langfuse."""
    from langfuse.types import MaskOtelSpansResult, OtelSpanPatch

    patches = {}
    for identifier, span in params.spans.items():
        remove = []
        replace = {}
        for key, value in span.attributes.items():
            if _safe_attribute(key, value):
                continue
            if key in _SAFE_JSON_METRIC_FIELDS:
                clean = _clean_metric_json(value)
                if clean is not None:
                    replace[key] = clean
                    continue
            remove.append(key)

        if remove or replace:
            patches[identifier] = OtelSpanPatch(
                delete_attributes=tuple(remove),
                set_attributes=replace,
            )

    if not patches:
        return None
    return MaskOtelSpansResult(span_patches=patches)


@lru_cache(maxsize=1)
def get_langfuse_handler() -> CallbackHandler | None:
    """Create one callback handler per application process when enabled."""
    if not is_langfuse_enabled():
        return None

    if not os.getenv("LANGFUSE_PUBLIC_KEY") or not os.getenv("LANGFUSE_SECRET_KEY"):
        raise RuntimeError(
            "LANGFUSE_ENABLED=true requires LANGFUSE_PUBLIC_KEY "
            "and LANGFUSE_SECRET_KEY"
        )

    from langfuse import Langfuse
    from langfuse.langchain import CallbackHandler

    # Initialize the client before the handler, so the handler reuses the
    # singleton client with privacy masking installed.
    Langfuse(mask_otel_spans=_mask_otel_spans)
    return CallbackHandler()
