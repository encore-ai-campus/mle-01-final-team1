"""실제 금융정보 없이 Langfuse 연결과 최상위 그래프 추적을 점검한다."""

from langfuse import get_client

from agent.runtime import get_workflow
from services.observability import is_langfuse_enabled


def main() -> int:
    if not is_langfuse_enabled():
        print("Set LANGFUSE_ENABLED=true in backend/.env first.")
        return 1

    # Obtain the workflow first to register export-stage masking.
    graph = get_workflow()
    client = get_client()
    if not client.auth_check():
        print("Langfuse authentication failed; check keys and region URL.")
        return 2

    # Synthetic state only: no user data, no financial profile.
    result = graph.invoke({"phase": "initial"})
    if result.get("phase") != "planning":
        raise AssertionError(f"Unexpected workflow result: {result!r}")
    client.flush()
    print("Langfuse test trace sent: main_supervisor (bootstrap -> END).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
