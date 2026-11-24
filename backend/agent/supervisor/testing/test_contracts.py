# 실제 운영 실행과 분리된 최소 계약 검사다. API 호출/DB 저장은 수행하지 않는다.
# DAG, 스키마, 메모리 접근 경계와 HTTP 요청 형식만 검증한다.
from __future__ import annotations

import json
import unittest
from copy import deepcopy
from io import BytesIO
from typing import Any, TypedDict
from unittest.mock import patch
from urllib.error import HTTPError

from ..contracts import (SupervisorContractError, execution_waves, normalize_plan,
                         validate_plan, validate_decision, validate_intent_scenarios, scenario_plan_view)
from ..llm.openai_api import OpenAIDecider, OpenAIConnectionError
from ..state import copy_main_source
from ..supervisor_main import Supervisor
from ..tools.ports import MEMORY_TYPES, SupervisorPorts, normalize_memory_result, validate_memory_request
from ..tools.memory import initial_memory, merge_memory
from ..tools.scenarios import prepare_scenarios


class ContractTests(unittest.TestCase):
    def test_dag(self):
        drafts = [{"task_id": "budget", "agent": "fund", "objective": "월 가용자금 확인", "depends_on": []},
                  {"task_id": "benefit", "agent": "policy", "objective": "지원 조건 확인", "depends_on": []},
                  {"task_id": "products", "agent": "product", "objective": "선행 예산과 지원 조건 활용", "depends_on": ["budget", "benefit"]},
                  {"task_id": "roadmap", "agent": "fund", "objective": "세 선행 결과 종합", "depends_on": ["budget", "benefit", "products"]}]
        plan = normalize_plan({"tasks": drafts}, None, False)
        self.assertEqual(execution_waves(plan), [["budget", "benefit"], ["products"], ["roadmap"]])
        self.assertTrue(all(t["status"] == "pending" and t["run_id"] is None for t in plan["tasks"]))
        plan["tasks"][0]["depends_on"] = ["roadmap"]
        with self.assertRaises(SupervisorContractError):
            execution_waves(plan)

    def test_state_isolation(self):
        original = {"financial_profile": {"monthly_net_income_krw": 3200000}, "agent_outbox": [], "private": "ignored"}
        source = copy_main_source(original)
        result = prepare_scenarios({"scenarios": [{"id": "lower", "label": "가정", "overrides": {"monthly_net_income_krw": 2500000}, "assumptions": {"horizon_months": 60}}]})
        self.assertEqual(result["scenario_state"]["scenarios"][0]["overrides"]["monthly_net_income_krw"], 2500000)
        source["financial_profile"]["monthly_net_income_krw"] = 1
        self.assertEqual(original["financial_profile"]["monthly_net_income_krw"], 3200000)
        self.assertNotIn("private", source)
        self.assertNotIn("agent_outbox", source)

    def test_memory_schema_and_denial(self):
        item = {"memory_id": "m1", "memory_type": "user_preference", "content": "대출 제외", "source_ref": "a1", "metadata": {"status": "active"}}
        raw = {"retrieval_status": "found", "retrieved": [item, deepcopy(item)]}
        result = normalize_memory_result(raw)
        self.assertEqual(result["retrieved"], [item])
        self.assertEqual(normalize_memory_result({"retrieval_status": "access_denied", "retrieved": [item]})["retrieved"], [])
        self.assertEqual(merge_memory({"memory": result}, [{"name": "retrieve_memory", "result": {"retrieval_status": "access_denied", "retrieved": []}}])["retrieved"], [])
        validate_memory_request({"query": "적금", "memory_types": list(MEMORY_TYPES)})
        with self.assertRaises(SupervisorContractError):
            validate_memory_request({"query": "적금", "scope": "all"})
        with self.assertRaises(SupervisorContractError):
            validate_memory_request({"query": "적금", "user_id": "other"})

    def test_prefetch_same_turn(self):
        # 검색 호출 횟수 검사 전용 스파이. 실제 실행 경로에서는 팀 서비스가 연결된다.
        calls = []
        def service(request):
            calls.append(request)
            return {"retrieval_status": "no_match", "retrieved": []}
        ports = SupervisorPorts(load_memory_context=service)
        source = {"messages": [{"type": "human", "content": "저축 계획"}], "turn_id": "t1", "consultation_id": "c1"}
        source["memory"] = initial_memory(source, ports)
        self.assertEqual(initial_memory(source, ports), source["memory"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(set(calls[0]["memory_types"]), set(MEMORY_TYPES))
        source["turn_id"] = "t2"
        initial_memory(source, ports)
        self.assertEqual(len(calls), 2)

    def test_http_contract(self):
        # HTTP 전송 경계만 검사하며 실제 OpenAI API 호출 성공을 뜻하지 않는다.
        decision = {"kind": "submit_plan", "reason": "일반 상품 조회", "intent": {"purpose": "deposit_search", "operation": "new", "supplied_values": {}, "profile_update_status": "not_needed"}, "execution_plan": {"tasks": [{"task_id": "product", "agent": "product", "objective": "예금 상품 조회", "depends_on": []}]}}
        raw = {"status": "completed", "output": [{"type": "function_call", "name": "supervisor_decision", "arguments": json.dumps(decision)}]}
        def response(req, timeout):
            payload = json.loads(req.data)
            self.assertEqual(req.full_url, "https://api.openai.com/v1/responses")
            self.assertEqual(req.get_header("Authorization"), "Bearer unit-test-key")
            self.assertEqual(payload["model"], "unit-test-model")
            self.assertEqual(payload["tool_choice"]["name"], "supervisor_decision")
            self.assertFalse(payload["store"])
            return BytesIO(json.dumps(raw).encode())
        with patch("agent.supervisor.llm.openai_api.urlopen", side_effect=response):
            decider = OpenAIDecider("unit-test-key", "unit-test-model")
            source = {"messages": [{"type": "human", "content": "예금 종류 알려줘"}], "turn_id": "t1", "consultation_id": "c1"}
            original = deepcopy(source)
            outcome = Supervisor(decide=decider).run(source, use_graph=False)
            self.assertEqual(outcome.next_node, "plan_validator")
            self.assertEqual(outcome.update["execution_plan"]["tasks"][0]["agent"], "product")
            self.assertEqual(source, original)
            self.assertEqual(outcome.update["intent"]["purpose"], "deposit_search")
            self.assertEqual(decider.calls, 1)

    def test_http_error_no_fallback(self):
        failure = HTTPError("https://api.openai.com/v1/responses", 401, "secret raw body", {}, None)
        with patch("agent.supervisor.llm.openai_api.urlopen", side_effect=failure):
            with self.assertRaisesRegex(OpenAIConnectionError, "401") as ctx:
                OpenAIDecider("unit-test-key", "unit-test-model")({})
            self.assertNotIn("secret raw body", str(ctx.exception))


    def test_intent_required_for_plan(self):
        decision = {"kind": "submit_plan", "reason": "검사", "execution_plan": {"tasks": []}}
        with self.assertRaisesRegex(SupervisorContractError, "intent"):
            validate_decision(decision)
        decision["intent"] = {"purpose": "savings_comparison", "operation": "compare", "supplied_values": {}, "profile_update_status": "not_needed"}
        with self.assertRaisesRegex(SupervisorContractError, "analysis_mode"):
            validate_decision(decision)
        decision["intent"]["analysis_mode"] = "strategy_compare"
        validate_decision(decision)
        with self.assertRaisesRegex(SupervisorContractError, "2~3"):
            validate_intent_scenarios(decision["intent"], None)
        decision["intent"]["analysis_mode"] = "candidate_compare"
        validate_intent_scenarios(decision["intent"], None)

    @staticmethod
    def _scenario_inputs():
        return {"scenarios": [
            {"id": sid, "label": label, "overrides": {}, "assumptions": {"horizon_months": 36,
                "holding_actions": [{"action": "subscribe", "target_criteria": {"monthly_contribution_krw": amount, "term_months": 36}}]}}
            for sid, label, amount in (("s_a", "30만 원", 300000), ("s_b", "40만 원", 400000), ("s_c", "50만 원", 500000))
        ]}

    @staticmethod
    def _branch_plan():
        # A/B는 동일 경로와 목적도 허용, C는 다른 경로. 공통 조회는 한 번만 실행.
        return {"tasks": [
            {"task_id": "common", "agent": "product", "objective": "공통 36개월 조건 확인", "depends_on": []},
            {"task_id": "fund_a", "agent": "fund", "scenario_id": "s_a", "objective": "해당 분기 납입액 계산", "depends_on": ["common"]},
            {"task_id": "fund_b", "agent": "fund", "scenario_id": "s_b", "objective": "해당 분기 납입액 계산", "depends_on": ["common"]},
            {"task_id": "policy_c", "agent": "policy", "scenario_id": "s_c", "objective": "해당 전략 자격 확인", "depends_on": []},
            {"task_id": "product_c", "agent": "product", "scenario_id": "s_c", "objective": "해당 정책과 납입 조건 확인", "depends_on": ["policy_c", "common"]},
            {"task_id": "fund_c", "agent": "fund", "scenario_id": "s_c", "objective": "해당 분기 납입액 계산", "depends_on": ["policy_c", "product_c", "common"]},
        ]}

    def test_scenario_dags_shared_and_distinct(self):
        scenario = prepare_scenarios(self._scenario_inputs())["scenario_state"]
        plan = normalize_plan(self._branch_plan(), None, True)
        self.assertEqual(execution_waves(plan, scenario), [["common", "policy_c"], ["fund_a", "fund_b", "product_c"], ["fund_c"]])
        view = scenario_plan_view(plan, scenario)
        self.assertEqual(view["common_task_ids"], ["common"])
        self.assertEqual(view["branches"][0]["waves"], [["common"], ["fund_a"]])
        self.assertEqual(view["branches"][1]["waves"], [["common"], ["fund_b"]])
        self.assertEqual(view["branches"][2]["waves"], [["common", "policy_c"], ["product_c"], ["fund_c"]])
        self.assertEqual(view["branches"][2]["terminal_task_ids"], ["fund_c"])
        self.assertTrue(all(s["assumptions"]["holding_actions"][0]["status"] == "hypothetical" for s in scenario["scenarios"]))
        self.assertEqual(scenario["scenarios"][1]["assumptions"]["holding_actions"][0]["target_criteria"]["monthly_contribution_krw"], 400000)

    def test_missing_scenario_and_cross_branch_rejected(self):
        scenario = prepare_scenarios(self._scenario_inputs())["scenario_state"]
        plan = normalize_plan(self._branch_plan(), None, True)
        missing = deepcopy(plan)
        missing["tasks"] = [t for t in missing["tasks"] if t["scenario_id"] != "s_b"]
        with self.assertRaisesRegex(SupervisorContractError, "s_b"):
            validate_plan(missing, scenario)
        crossing = deepcopy(plan)
        crossing["tasks"][1]["depends_on"].append("fund_b")
        with self.assertRaisesRegex(SupervisorContractError, "다른 시나리오"):
            validate_plan(crossing, scenario)
        shared = deepcopy(plan)
        shared["tasks"].append({"task_id": "compare", "agent": "fund", "scenario_id": None, "objective": "전체 분기 비교", "depends_on": ["fund_a", "fund_b"], "status": "pending", "run_id": None})
        with self.assertRaisesRegex(SupervisorContractError, "다른 시나리오"):
            validate_plan(shared, scenario)

    def test_invalid_subscription_assumption(self):
        for field, value in (("monthly_contribution_krw", -1), ("monthly_contribution_krw", True), ("term_months", 0), ("annual_rate_pct", float("inf"))):
            args = self._scenario_inputs()
            args["scenarios"][0]["assumptions"]["holding_actions"][0]["target_criteria"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(SupervisorContractError):
                prepare_scenarios(args)

    def test_branch_plan_repair_and_return(self):
        # 계약 경계 검사: 잘못된 분기 누락 제출이 실제 finalize에서 반려되고 재제출되는지 확인.
        # 이 결정열은 단위 검사에만 쓰며 기본 실행에는 사용되지 않는다.
        intent = {"purpose": "savings_strategy_comparison", "operation": "compare", "analysis_mode": "strategy_compare", "supplied_values": {}, "profile_update_status": "not_needed"}
        first = {"kind": "submit_plan", "reason": "검사", "intent": intent, "execution_plan": self._branch_plan()}
        prepare = {"kind": "call_tool", "reason": "분기 준비", "tool_name": "prepare_scenarios", "tool_input": self._scenario_inputs()}
        decisions = iter([first, prepare, first])
        seen = []
        def decider(context):
            seen.append(deepcopy(context))
            return deepcopy(next(decisions))
        trace = []
        source = {"messages": [{"type": "human", "content": "월 30/40/50만 원 납입 전략 비교"}], "financial_profile": {"monthly_net_income_krw": 3200000}, "turn_id": "t1", "consultation_id": "c1", "intent": None, "scenario_state": None}
        original = deepcopy(source)
        outcome = Supervisor(decide=decider, trace_sink=trace.append).run(source, use_graph=False)
        self.assertEqual(outcome.next_node, "plan_validator")
        self.assertEqual(outcome.update["intent"], intent)
        self.assertEqual(len(outcome.update["scenario_state"]["scenarios"]), 3)
        self.assertEqual(len(outcome.update["execution_plan"]["tasks"]), 6)
        self.assertTrue(seen[1]["issues"])
        self.assertNotIn("financial_profile", outcome.update)
        self.assertNotIn("scenario_execution", outcome.update)
        self.assertNotIn("observations", outcome.update)
        self.assertEqual(original, source)
        self.assertTrue(any(e["node"] == "plan" and "scenario_execution" in e["output"] for e in trace))

    def test_parent_graph_command_if_installed(self):
        # LangGraph 설치 환경에서 실제 내부 Graph + 부모 Command 이동만 검사한다.
        try:
            from langgraph.graph import START, END, StateGraph
        except ModuleNotFoundError as exc:
            if exc.name == "langgraph":
                self.skipTest("LangGraph 설치 환경에서 부모 Command 검사를 실행하세요.")
            raise
        from ..graph import build_main_supervisor_node

        class ParentTestState(TypedDict, total=False):
            messages: list[dict[str, Any]]
            turn_id: str
            consultation_id: str
            phase: str
            intent: dict[str, Any]
            scenario_state: dict[str, Any]
            execution_plan: dict[str, Any]
            memory: dict[str, Any]
            arrived: str

        intent = {"purpose": "savings_strategy_comparison", "operation": "compare", "analysis_mode": "strategy_compare", "supplied_values": {}, "profile_update_status": "not_needed"}
        decisions = iter([
            {"kind": "call_tool", "reason": "분기 준비", "tool_name": "prepare_scenarios", "tool_input": self._scenario_inputs()},
            {"kind": "submit_plan", "reason": "검사", "intent": intent, "execution_plan": self._branch_plan()},
        ])
        supervisor = Supervisor(decide=lambda _: deepcopy(next(decisions)))
        parent = StateGraph(ParentTestState)
        parent.add_node("supervisor", build_main_supervisor_node(supervisor))
        def terminal(destination):
            def node(_):
                return {"arrived": destination}
            return node
        for name in ("plan_validator", "ask_user", "final_advice"):
            parent.add_node(name, terminal(name))
            parent.add_edge(name, END)
        parent.add_edge(START, "supervisor")
        result = parent.compile().invoke({"messages": [{"type": "human", "content": "월 납입 전략 비교"}], "turn_id": "t1", "consultation_id": "c1"})
        self.assertEqual(result["arrived"], "plan_validator")
        self.assertEqual(result["intent"], intent)
        self.assertEqual(len(result["scenario_state"]["scenarios"]), 3)
        self.assertEqual(len(result["execution_plan"]["tasks"]), 6)
        self.assertNotIn("scenario_execution", result)

    def test_actual_memory_mvp_if_installed(self):
        try:
            from agent.memory.memory_service import MemoryService
        except ModuleNotFoundError as exc:
            if exc.name in {"agent.memory", "agent.memory.memory_service"}:
                self.skipTest("팀 Memory MVP가 설치된 프로젝트에서 실행하세요.")
            raise
        from ..tools.integration import memory_ports
        service = MemoryService()
        self.assertEqual(service.save_memory({"memory_type": "user_preference", "content": "대출을 선호하지 않음", "source_ref": "a1"}, authenticated_user_id="owner")["status"], "saved")
        service.save_memory({"memory_type": "recommendation", "content": "적금 추천", "source_ref": "a2"}, authenticated_user_id="other")
        prefetch, detail = memory_ports(service, authenticated_user_id="owner")
        result = prefetch({"query": "목돈 마련 방법", "limit": 10, "memory_types": list(MEMORY_TYPES)})
        self.assertEqual(result["retrieval_status"], "found")
        self.assertEqual(result["retrieved"][0]["memory_type"], "user_preference")
        self.assertEqual(detail({"query": "적금"})["retrieval_status"], "no_match")
        denied, _ = memory_ports(service, authenticated_user_id="")
        self.assertEqual(denied({"query": "대출"})["retrieval_status"], "access_denied")

    def test_actual_profile_mvp_if_installed(self):
        try:
            from agent.profile.profile_agent import run_profile_agent
        except ModuleNotFoundError as exc:
            if exc.name in {"agent.profile", "agent.profile.profile_agent"}:
                self.skipTest("팀 Profile MVP가 설치된 프로젝트에서 실행하세요.")
            raise
        ports = SupervisorPorts.with_profile_mvp()
        original = {"financial_profile": {"monthly_net_income_krw": 3200000}}
        result = ports.profile_agent({"instruction": "세후 월급 250만 원으로 가정", "is_temporary": True, "current_profile": original})
        self.assertEqual(result["profile"]["financial_profile"]["monthly_net_income_krw"], 2500000)
        self.assertFalse(result.get("changes"))
        self.assertEqual(original["financial_profile"]["monthly_net_income_krw"], 3200000)
