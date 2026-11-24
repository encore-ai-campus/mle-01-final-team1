# 직접 입력한 질문 또는 MainState JSON을 실제 Supervisor에 전달한다.
# 실제 API 결과, 작업 DAG, 다음 노드와 합성된 State를 JSON으로 출력한다.
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4
from typing import Any

from .llm.openai_api import OpenAIDecider, OpenAIConnectionError
from .contracts import SupervisorContractError, execution_waves, scenario_plan_view
from .tools.integration import bind_mvp_ports
from .tools.ports import MEMORY_TYPES
from .tools.memory import latest_user_text
from .supervisor_main import Supervisor


# 새 요청의 초기화 범위. 영속 프로필·대화·last_advice_ref는 보존한다.
TURN_RESET = {"scenario_state": None, "intent": None, "execution_plan": None,
              "pending_input": [], "agent_results": {}, "review_results": {},
              "superseded_results": {}, "final_advice": None, "post_response": None,
              "basic_profile_changes": [], "financial_profile_changes": [], "memory_candidates": []}


def as_jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): as_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [as_jsonable(v) for v in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(getattr(value, "type", None), str) and hasattr(value, "content"):
        result = {"type": value.type, "content": as_jsonable(value.content)}
        if getattr(value, "id", None):
            result["id"] = value.id
        return result
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError(f"JSON으로 표현할 수 없는 State 타입: {type(value).__name__}")


def make_input(raw: dict[str, Any] | None, question: str | None) -> dict[str, Any]:
    state = deepcopy(raw or {})
    if question is not None:
        if not question.strip():
            raise ValueError("질문을 입력하세요.")
        # 질문을 함께 주면 새 turn. 같은 turn 재개는 Entry Router가 만든 JSON을 그대로 입력한다.
        state.update(deepcopy(TURN_RESET), turn_id=f"turn_{uuid4().hex}", phase="planning")
        state["messages"] = [*(state.get("messages") or []), {"type": "human", "content": question.strip()}]
        old_memory = state.get("memory") or {}
        state["memory"] = {"retrieval_status": "not_requested", "retrieved": []}
        if old_memory.get("last_advice_ref"):
            state["memory"]["last_advice_ref"] = old_memory["last_advice_ref"]
        if "agent_outbox" in state:
            state["agent_outbox"] = []
    state.setdefault("consultation_id", f"consultation_{uuid4().hex}")
    state.setdefault("turn_id", f"turn_{uuid4().hex}")
    state.setdefault("phase", "planning")
    if not latest_user_text(state):
        raise ValueError("MainState.messages에 사용자 질문이 없습니다.")
    # 실제 LangGraph / 부모 State의 메시지 계약을 따른다.
    from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
    messages = []
    for msg in state["messages"]:
        if not isinstance(msg, dict):
            messages.append(msg)
            continue
        cls = {"human": HumanMessage, "user": HumanMessage, "ai": AIMessage,
               "assistant": AIMessage, "system": SystemMessage}.get(msg.get("type", msg.get("role")))
        if cls is None or not isinstance(msg.get("content"), str):
            raise ValueError("messages 항목은 human/ai/system 문자열 메시지여야 합니다.")
        args = {"content": msg["content"]}
        if msg.get("id") or msg.get("message_id"):
            args["id"] = msg.get("id") or msg["message_id"]
        messages.append(cls(**args))
    state["messages"] = messages
    return state


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def local_memory(memory_file: Path | None):
    """실제 MVP의 로컬 전용 인스턴스. 기본 빈 저장소; 입력 파일의 기록만 명시적으로 적재."""
    try:
        from agent.memory.memory_service import MemoryService
    except ModuleNotFoundError as exc:
        if exc.name not in {"agent.memory", "agent.memory.memory_service"} or memory_file:
            raise
        return None, "", 0
    service = MemoryService()
    namespace = f"local_cli_{uuid4().hex}"
    loaded = 0
    if memory_file:
        records = read_json(memory_file)
        if not isinstance(records, list):
            raise ValueError("--memory-file은 MemoryWriteRequest의 JSON 배열입니다.")
        for record in records:
            if (not isinstance(record, dict) or record.get("memory_type") not in MEMORY_TYPES
                    or any(not isinstance(record.get(k), str) or not record[k].strip() for k in ("content", "source_ref"))
                    or set(record) - {"memory_type", "content", "source_ref", "target_ref", "metadata"}):
                raise ValueError("MemoryWriteRequest 형식과 실제 기억 유형을 확인하세요.")
            result = service.save_memory(record, authenticated_user_id=namespace)
            if result.get("status") not in {"saved", "duplicate"}:
                raise ValueError("로컬 Memory 입력 적재에 실패했습니다.")
            loaded += result["status"] == "saved"
    return service, namespace, loaded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Supervisor 실제 OpenAI 실행 (고정 Mock 출력 없음)")
    parser.add_argument("--question", help="새 사용자 질문; 생략하면 직접 입력")
    parser.add_argument("--state-file", type=Path, help="실제 MainState JSON 입력")
    parser.add_argument("--memory-file", type=Path, help="로컬 MVP에 명시적으로 적재할 MemoryWriteRequest 배열")
    parser.add_argument("--output", type=Path, help="실행 결과/trace JSON 저장")
    parser.add_argument("--state-output", type=Path, help="다음 Graph에 전달할 합성 MainState만 별도 저장")
    parser.add_argument("--quiet", action="store_true", help="실시간 로그 생략 (최종 JSON은 유지)")
    parser.add_argument("--regression", action="store_true", help="API를 호출하지 않는 소수의 계약 단위 검사")
    args = parser.parse_args(argv)
    if args.regression:
        import unittest
        suite = unittest.defaultTestLoader.loadTestsFromName("agent.supervisor.testing.test_contracts")
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
    events = []
    def trace(event: dict[str, Any]):
        entry = {"step": len(events) + 1, **as_jsonable(event)}
        events.append(entry)
        if not args.quiet:
            print(f"\n[{entry['step']:02d} | {entry['node']}] -> {entry.get('route', '-')}", file=sys.stderr, flush=True)
            print(json.dumps(entry['output'], ensure_ascii=False, indent=2), file=sys.stderr, flush=True)
    try:
        raw = read_json(args.state_file) if args.state_file else None
        if args.state_file and not isinstance(raw, dict):
            raise ValueError("--state-file은 MainState JSON 객체여야 합니다.")
        question = args.question
        if question is None and raw is None:
            print("질문을 입력하세요 (실제 OpenAI API 사용):", file=sys.stderr, flush=True)
            question = input().strip()
        state = make_input(raw, question)
        # .env는 실제 실행에서 읽고, 인증 실패를 가짜 응답으로 대체하지 않는다.
        decider = OpenAIDecider.from_env()
        service, namespace, loaded = local_memory(args.memory_file)
        ports = bind_mvp_ports(memory_service=service, authenticated_user_id=namespace)
        supervisor = Supervisor(decider, ports, trace_sink=trace)
        original = as_jsonable(state)
        result = supervisor.run(state, use_graph=True)
        if as_jsonable(state) != original:
            raise SupervisorContractError("Supervisor가 입력 State를 변경했습니다.")
        final = {**deepcopy(state), **deepcopy(result.update)}
        output = {
            "status": "executed", "execution_mode": "langgraph_supervisor_only",
            "decider": "actual_openai_api", "model": decider.model,
            "llm_calls": decider.calls, "llm_usage": decider.usage,
            "llm_input_sizes": decider.input_sizes,
            "llm_usage_totals": {k: sum(u[k] for u in decider.usage)
                                 for k in ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens", "reasoning_tokens")
                                 if len(decider.usage) == decider.calls and decider.usage and all(k in u for u in decider.usage)},
            "services": {"profile": "actual_mvp" if ports.profile_agent else "unconnected",
                         "profile_db_read": "connected" if ports.get_profile_fields else "unconnected", "memory": "actual_local_in_memory_mvp" if service is not None else "unconnected",
                         "memory_imported_records": loaded, "memory_persistent": False},
            "main_state_input": original,
            "supervisor_return": {"next_node": result.next_node, "tool_calls": result.tool_calls,
                                  "state_patch": as_jsonable(result.update)},
            "main_state_after_supervisor": as_jsonable(final),
            "node_trace": events,
        }
        if result.next_node == "plan_validator":
            output["execution_waves"] = execution_waves(final["execution_plan"], final.get("scenario_state"))
            if final.get("scenario_state"):
                output["scenario_execution"] = scenario_plan_view(final["execution_plan"], final["scenario_state"])
        if args.state_output:
            args.state_output.write_text(json.dumps(output["main_state_after_supervisor"], ensure_ascii=False, indent=2), encoding="utf-8")
    except (KeyboardInterrupt, EOFError):
        print("실행을 종료했습니다.", file=sys.stderr)
        return 1
    except Exception as exc:
        # API 응답/키/개인정보가 담길 수 있는 외부 예외 원문은 노출하지 않는다.
        safe_message = str(exc) if isinstance(exc, (OpenAIConnectionError, SupervisorContractError, ValueError, ModuleNotFoundError)) else "실행 실패. 연결 및 입력을 확인하세요."
        output = {"status": "failed", "error": {"type": type(exc).__name__, "message": safe_message},
                  "supervisor_return": None, "node_trace": events}
    rendered = json.dumps(output, ensure_ascii=False, indent=2)
    print(rendered, flush=True)
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    return 0 if output["status"] == "executed" else 1
