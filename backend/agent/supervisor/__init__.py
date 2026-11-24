# Supervisor의 외부 공개 API. import만으로 자체 테스트를 실행하지 않는다.
# 상위 State 계약과 실행 책임의 경계를 유지한다.
from .contracts import (
    Decision, MemoryRequest, ProfileRequest, ProfileResult,
    SupervisorContractError, SupervisorOutcome,
)
from .state import SupervisorWorkingState
from .tools.ports import SupervisorPorts
from .tools.scenarios import prepare_scenarios


def __getattr__(name: str):
    """supervisor_main.py 모듈 직접 실행 시 중복 선행 import를 피한다."""
    if name == "Supervisor":
        from .supervisor_main import Supervisor
        return Supervisor
    if name in ("build_main_supervisor_node", "compile_graph"):
        from . import graph
        return getattr(graph, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")



__all__ = [
    "Decision", "MemoryRequest", "ProfileRequest", "ProfileResult",
    "Supervisor", "SupervisorContractError", "SupervisorOutcome", "SupervisorPorts",
    "SupervisorWorkingState",
    "build_main_supervisor_node", "compile_graph", "prepare_scenarios",
]
