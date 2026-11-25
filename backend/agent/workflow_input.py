"""개발용 실행점. 실제 Supervisor와 최상위 LangGraph를 한 번 실행하고 과정과 결과를 표시한다.

VSCode 파일 직접 실행 및 python -m agent.workflow_input 지원. 서비스용 chat/API는 이 파일에 의존하지 않는다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Callable
from time import perf_counter

BACKEND = Path(__file__).resolve().parents[1]
# 직접 실행 시 agent/가 sys.path[0]에 놓이는 문제도 방지한다.
# agent/profile 등의 디렉터리가 표준 라이브러리 이름을 가리지 않게 한다.
if __name__ == "__main__" and not __package__:
    script_dir = Path(__file__).resolve().parent
    sys.path[:] = [p for p in sys.path if Path(p or os.getcwd()).resolve() != script_dir]
    sys.path.insert(0, str(BACKEND))


PACKAGES = ("langgraph", "langchain-core", "langfuse", "pydantic", "python-dotenv")


def environment_info() -> dict[str, Any]:
    packages = {}
    for name in PACKAGES:
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = "not installed"
    return {"python": sys.executable, "python_version": sys.version.split()[0], "packages": packages}


def check_environment() -> dict[str, Any]:
    """실제 import와 설정 존재만 확인한다. API 연결/추적 전송은 수행하지 않는다."""
    info = environment_info()
    checks = {}
    modules = (
        "agent.runtime", "agent.supervisor.llm.openai_api",
        "agent.supervisor.tools.integration", "agent.profile.profile_agent",
        "services.observability", "langfuse.langchain",
    )
    for name in modules:
        try:
            import_module(name)
            checks[name] = "ok"
        except Exception as exc:
            checks[name] = {"error_type": type(exc).__name__,
                            "missing_module": getattr(exc, "name", None)}
    info["imports"] = checks
    info["env_present"] = {key: bool(os.getenv(key, "").strip()) for key in (
        "OPENAI_API_KEY", "OPENAI_MODEL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY",
    )}
    info["langfuse_enabled_setting"] = os.getenv("LANGFUSE_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
    return info


def safe_error(exc: Exception) -> None:
    """오류의 파일/행/누락 모듈을 표시하되 전체 입력, 키, 원시 응답은 노출하지 않는다."""
    print(f"\n[ERROR] {type(exc).__name__}", file=sys.stderr)
    if isinstance(exc, ModuleNotFoundError):
        print(f"누락 모듈: {exc.name or '(SDK에서 이름 없이 발생: 아래 위치와 버전 확인)'}", file=sys.stderr)
        print("backend에서 uv sync --locked 실행 후 동일 Python으로 재실행하세요.", file=sys.stderr)
        print(json.dumps(environment_info(), ensure_ascii=False, indent=2), file=sys.stderr)
    elif type(exc).__name__ == "OpenAIConnectionError":
        # 기존 Supervisor가 정제한 오류만 출력한다.
        print(str(exc), file=sys.stderr)
    else:
        print("환경 설정 또는 그래프/State 계약을 확인하세요. 원본 예외 본문은 표시하지 않습니다.", file=sys.stderr)
    for frame in traceback.extract_tb(exc.__traceback__)[-12:]:
        file = Path(frame.filename)
        try:
            name = str(file.resolve().relative_to(BACKEND))
        except (ValueError, OSError):
            name = file.name
        print(f"  {name}:{frame.lineno} ({frame.name})", file=sys.stderr)


def show_update(namespace: tuple, updates: dict[str, Any], emit: Callable[[str], None]) -> None:
    """LangGraph가 실제로 내보낸 노드 업데이트의 필요한 부분만 로컬에 표시한다."""
    for node, patch in updates.items():
        if node.startswith("__"):
            continue
        # namespace에는 LangGraph 내부 실행 ID가 포함된다. 사람이 읽을 노드 이름만 표시한다.
        path = "/".join([*(str(x).split(":", 1)[0] for x in namespace), node])
        emit(f"[NODE] {path}")
        if not isinstance(patch, dict):
            continue
        decision = patch.get("decision")
        if isinstance(decision, dict):
            kind = decision.get("kind", "unknown")
            tool = decision.get("tool_name")
            emit(f"       decision: {kind}" + (f" / {tool}" if tool else ""))
        if patch.get("next_node"):
            emit(f"       next: {patch['next_node']}")
        if namespace:
            continue  # 내부 working state 전체를 출력하지 않는다.
        report = patch.get("workflow_report")
        if not isinstance(report, dict):
            continue
        if node == "supervisor" and report.get("supervisor_route"):
            emit(f"       route: {report['supervisor_route']}")
        if node == "plan_validator":
            emit(f"       planned waves: {json.dumps(report.get('planned_waves', []), ensure_ascii=False)}")
        if node == "dispatcher" and report.get("waves"):
            for item in report["waves"][-1]:
                emit(f"       {item['task_id']}: {item['action']}")
        stages = report.get("stages") or []
        if stages:
            stage = stages[-1]
            if stage.get("status") == "skipped":
                emit(f"       skipped: {stage.get('reason', 'unavailable')}")


def run_stream(graph: Any, state: dict[str, Any], *, emit: Callable[[str], None]) -> dict[str, Any]:
    """단일 stream 실행으로 실제 노드 이벤트와 마지막 병합 State를 함께 수집한다.

    잠금 파일의 LangGraph 1.2.x에서 지원하는 v2 StreamPart를 사용한다.
    invoke를 다시 호출하지 않으므로 LLM 중복 호출이 없다.
    """
    final = None
    for part in graph.stream(
        state, stream_mode=["updates", "values"], subgraphs=True, version="v2"
    ):
        namespace, mode, data = part["ns"], part["type"], part["data"]
        if mode == "updates" and isinstance(data, dict):
            show_update(namespace, data, emit)
        elif mode == "values" and not namespace and isinstance(data, dict):
            final = data
    if final is None or not isinstance(final.get("workflow_report"), dict):
        raise RuntimeError("No final workflow report")
    return final


def result_view(result: dict[str, Any], elapsed: float, *, langfuse_enabled: bool) -> dict[str, Any]:
    """요청별 개발 보고서. 전체 프로필과 대화 원문은 기본 출력에서 제외한다."""
    report = result.get("workflow_report") or {}
    return {
        "status": report.get("status"),
        "phase": result.get("phase"),
        "elapsed_seconds": round(elapsed, 3),
        "langfuse_configured": langfuse_enabled,
        "consultation_id": result.get("consultation_id"),
        "turn_id": result.get("turn_id"),
        "execution_plan": result.get("execution_plan"),
        "scenario_state": result.get("scenario_state"),
        "pending_input": result.get("pending_input", []),
        "final_advice": result.get("final_advice"),
        "workflow_report": report,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="최상위 LangGraph 개발용 입력/진행/결과 확인")
    parser.add_argument("--question", help="새 질문 (생략하면 직접 입력)")
    parser.add_argument("--state-file", type=Path, help="신뢰할 수 있는 로컬 MainState JSON")
    parser.add_argument("--state-output", type=Path, help="전체 MainState를 지정 파일에 저장 (민감정보 포함 가능)")
    parser.add_argument("--no-langfuse", action="store_true", help="이번 프로세스의 Langfuse만 끄고 원인 분리")
    parser.add_argument("--check-env", action="store_true", help="Python 버전, 모듈 import 및 환경 설정 확인 (API 호출 없음)")
    parser.add_argument("--quiet", action="store_true", help="진행 이벤트 생략, 결과 JSON만 출력")
    args = parser.parse_args(argv)
    if args.no_langfuse:
        # observability 모듈의 dotenv 로딩은 override=False이므로 CLI 선택이 우선한다.
        os.environ["LANGFUSE_ENABLED"] = "false"
    if args.check_env:
        print(json.dumps(check_environment(), ensure_ascii=False, indent=2))
        return 0
    tracing = False
    graph = None
    def emit(line: str) -> None:
        if not args.quiet:
            print(line, file=sys.stderr, flush=True)
    try:
        # 의존성 누락도 동일한 오류 경로에서 진단한다.
        from agent.runtime import get_workflow
        from agent.state import MainState
        from agent.supervisor.runtime import as_jsonable, make_input, read_json
        from services.observability import is_langfuse_enabled

        raw = read_json(args.state_file) if args.state_file else None
        if args.state_file and not isinstance(raw, dict):
            raise ValueError("MainState JSON object required")
        question = args.question
        if raw is None and question is None:
            print("[Workflow Input] 질문을 입력하세요:", file=sys.stderr, flush=True)
            question = input().strip()
        state = make_input(raw, question)
        tracing = is_langfuse_enabled()
        required_env = ["OPENAI_API_KEY", "OPENAI_MODEL"]
        if tracing:
            required_env.extend(["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"])
        missing_env = [name for name in required_env if not os.getenv(name, "").strip()]
        if missing_env:
            print("[ENV] backend/.env에 필요한 설정이 없습니다: " + ", ".join(missing_env), file=sys.stderr)
            return 1
        emit(f"[START] Langfuse: {'enabled' if tracing else 'disabled'}")
        graph = get_workflow()
        started = perf_counter()
        result = run_stream(graph, state, emit=emit)
        output = result_view(result, perf_counter() - started, langfuse_enabled=tracing)
        print(json.dumps(as_jsonable(output), ensure_ascii=False, indent=2), flush=True)
        if args.state_output:
            saved = {k: v for k, v in result.items() if k in MainState.__annotations__}
            args.state_output.write_text(json.dumps(as_jsonable(saved), ensure_ascii=False, indent=2), encoding="utf-8")
            emit("[STATE] 지정한 로컬 파일에 MainState 저장 완료 (민감정보 포함 가능)")
        return 0 if output["status"] in {"connection_check_completed", "waiting_for_user"} else 1
    except (EOFError, KeyboardInterrupt):
        print("\n실행을 중단했습니다.", file=sys.stderr)
        return 130
    except Exception as exc:
        safe_error(exc)
        if tracing:
            print("Langfuse 연결 오류 여부는 --no-langfuse 옵션으로 구분할 수 있습니다.", file=sys.stderr)
        return 1
    finally:
        if tracing and graph is not None:
            try:
                from langfuse import get_client
                get_client().flush()
                emit("[LANGFUSE] flush 요청 완료 (서버 수신은 대시보드에서 확인)")
            except Exception as exc:
                print(f"[LANGFUSE] flush 오류: {type(exc).__name__}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
