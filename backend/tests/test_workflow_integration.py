"""실제 LangGraph/Supervisor 계약 연결 검사. LLM API는 호출하지 않으며 전문 Agent를 구현하지 않는다."""
from copy import deepcopy
import unittest

from agent.supervisor import Supervisor, SupervisorContractError
from agent.supervisor.runtime import make_input
from agent.workflow import build_workflow
from agent.workflow_input import run_stream


def task(tid, agent, deps=(), scenario=None):
    return {"task_id": tid, "agent": agent, "objective": f"{tid} 작업", "depends_on": list(deps), "scenario_id": scenario}


def decision(*tasks):
    return {"kind": "submit_plan", "reason": "실행 구조 테스트", "intent": {"purpose": "저축 및 지원 조건 확인",
            "operation": "new", "supplied_values": {}, "profile_update_status": "not_needed"},
            "execution_plan": {"tasks": list(tasks)}}


def run(d):
    # 기존 Supervisor 전체 로직을 사용하되 API 대신 고정 테스트 판단만 주입한다.
    graph = build_workflow(supervisor_factory=lambda: Supervisor(decide=lambda _: deepcopy(d)))
    return graph.invoke(make_input(None, "저축 계획과 지원 정책을 알려줘"), config={"recursion_limit": 128})


class WorkflowIntegrationTests(unittest.TestCase):
    def test_single_fund_without_agent(self):
        result = run(decision(task("fund", "fund")))
        self.assertEqual(result["workflow_report"]["status"], "connection_check_completed")
        self.assertEqual(result["workflow_report"]["tasks"]["fund"]["reason"], "agent_not_registered")
        self.assertIsNone(result["final_advice"])
        self.assertEqual(result["agent_results"], {})
        self.assertEqual(result["review_results"], {})

    def test_dynamic_dependencies(self):
        result = run(decision(task("p", "product"), task("y", "policy"), task("f", "fund", ["p", "y"])))
        report = result["workflow_report"]
        self.assertEqual(report["planned_waves"], [["p", "y"], ["f"]])
        self.assertEqual(report["tasks"]["p"]["reason"], "agent_not_registered")
        self.assertEqual(report["tasks"]["y"]["reason"], "agent_not_registered")
        self.assertEqual(report["tasks"]["f"]["reason"], "dependency_unavailable")
        self.assertTrue(all(t["status"] == "skipped" and t["run_id"] is None for t in result["execution_plan"]["tasks"]))
        self.assertEqual(result["agent_outbox"], [])
        self.assertFalse(report["financial_advice_available"])

    def test_supervisor_ask_user(self):
        result = run({"kind": "ask_user", "reason": "소득 확인 필요", "pending_input": [
            {"question": "세후 월 소득은 얼마인가요?", "fields": ["financial_profile.monthly_net_income_krw"], "target": "profile"}]})
        self.assertEqual(result["phase"], "waiting_for_user")
        self.assertEqual(result["workflow_report"]["status"], "waiting_for_user")
        self.assertEqual(result["pending_input"][0]["status"], "pending")
        self.assertIsNone(result["final_advice"])
        self.assertNotIn("dispatcher", [x["stage"] for x in result["workflow_report"]["stages"]])

    def test_scenario_branches_and_comparison_slot(self):
        d = decision(task("a", "fund", scenario="s_a"), task("b", "fund", scenario="s_b"))
        d["intent"].update(operation="compare", analysis_mode="strategy_compare")
        d["scenario_state"] = {"scenarios": [
            {"id": "s_a", "label": "기준 소득", "overrides": {"monthly_net_income_krw": 3000000}, "assumptions": {"horizon_months": 12}},
            {"id": "s_b", "label": "가상 소득", "overrides": {"monthly_net_income_krw": 3500000}, "assumptions": {"horizon_months": 12}},
        ]}
        result = run(d)
        report = result["workflow_report"]
        self.assertEqual(report["planned_waves"], [["a", "b"]])
        self.assertEqual(report["tasks"]["a"]["scenario_id"], "s_a")
        self.assertEqual(report["tasks"]["b"]["scenario_id"], "s_b")
        self.assertIn({"stage": "comparison", "status": "skipped", "reason": "stage_not_implemented"}, report["stages"])

    def test_stream_reports_real_graph_execution(self):
        d = decision(task("p", "product"), task("f", "fund", ["p"]))
        graph = build_workflow(supervisor_factory=lambda: Supervisor(decide=lambda _: deepcopy(d)))
        lines = []
        result = run_stream(graph, make_input(None, "적금 검색 및 저축 계획"), emit=lines.append)
        self.assertEqual(result["workflow_report"]["status"], "connection_check_completed")
        self.assertIn("[NODE] supervisor", lines)
        self.assertIn("[NODE] dispatcher", lines)
        self.assertIn("       p: agent_not_registered", lines)
        self.assertIn("       f: dependency_unavailable", lines)
        self.assertIn("[NODE] renderer", lines)

    def test_invalid_cycle_rejected(self):
        with self.assertRaises(SupervisorContractError):
            run(decision(task("p", "product", ["f"]), task("f", "fund", ["p"])))


if __name__ == "__main__":
    unittest.main()
