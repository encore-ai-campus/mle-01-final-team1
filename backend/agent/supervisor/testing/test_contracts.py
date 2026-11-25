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

    def test_scenario_id_autofill_and_preservation(self):
        args = self._scenario_inputs()
        args["scenarios"][0].pop("id")
        args["scenarios"][1]["id"] = "s_01"  # 뒤쪽 명시 ID부터 예약해야 한다.
        args["scenarios"][2]["id"] = None
        original = deepcopy(args)
        prepared = prepare_scenarios(args)["scenario_state"]
        self.assertEqual([b["id"] for b in prepared["scenarios"]], ["s_02", "s_01", "s_03"])
        self.assertEqual(args, original)
        self.assertEqual(prepare_scenarios(prepared)["scenario_state"], prepared)
        prepared["scenarios"][0]["assumptions"]["holding_actions"][0]["target_criteria"]["monthly_contribution_krw"] = 1
        self.assertEqual(args, original)

    def test_duplicate_or_malformed_explicit_id_rejected(self):
        for value in ("s_b", " s_a", 1, [], {}):
            args = self._scenario_inputs()
            args["scenarios"][0]["id"] = value
            with self.subTest(value=value), self.assertRaises(SupervisorContractError):
                prepare_scenarios(args)
        args = self._scenario_inputs()
        args["scenarios"][0]["assumptions"]["holding_actions"][0]["action"] = []
        with self.assertRaises(SupervisorContractError):
            prepare_scenarios(args)

    def test_independent_paths_default_and_generated_ids_used(self):
        # 실제 로그에서 발생한 ID 누락을 재현한다. 도구가 반환한 ID로 바로 계획을 제출한다.
        args = self._scenario_inputs()
        args["scenarios"] = args["scenarios"][:2]
        for branch in args["scenarios"]:
            branch.pop("id")
            branch["assumptions"]["horizon_months"] = 60
            branch["assumptions"]["holding_actions"][0]["target_criteria"].pop("term_months")
        intent = {"purpose": "5년 뒤 납입 전략 비교", "operation": "compare", "analysis_mode": "strategy_compare", "supplied_values": {}, "profile_update_status": "not_needed"}
        seen = []
        trace = []
        def decide(context):
            seen.append(deepcopy(context))
            if len(seen) == 1:
                return {"kind": "call_tool", "reason": "분기 준비", "tool_name": "prepare_scenarios", "tool_input": args}
            branches = context["observations"][-1]["result"]["scenario_state"]["scenarios"]
            tasks = []
            for branch in branches:
                sid = branch["id"]
                tasks.extend([
                    {"task_id": f"product_{sid}", "agent": "product", "scenario_id": sid, "objective": "해당 분기의 적금 후보 조회", "depends_on": []},
                    {"task_id": f"fund_{sid}", "agent": "fund", "scenario_id": sid, "objective": "해당 분기 원금 및 확인된 조건으로 60개월 전망", "depends_on": [f"product_{sid}"]},
                ])
            return {"kind": "submit_plan", "reason": "분기별 독립 배정", "intent": intent, "execution_plan": {"tasks": tasks}}
        source = {"messages": [{"type": "human", "content": "30만원 적금과 40만원 적금의 5년 뒤 금액 비교"}], "turn_id": "t1", "consultation_id": "c1", "intent": None, "scenario_state": None}
        outcome = Supervisor(decide=decide, trace_sink=trace.append).run(source, use_graph=False)
        self.assertEqual(outcome.next_node, "plan_validator")
        self.assertEqual(len(seen), 2)
        self.assertEqual(outcome.tool_calls, 1)
        scenario = outcome.update["scenario_state"]
        plan = outcome.update["execution_plan"]
        self.assertEqual(execution_waves(plan, scenario), [["product_s_01", "product_s_02"], ["fund_s_01", "fund_s_02"]])
        self.assertEqual(scenario_plan_view(plan, scenario)["common_task_ids"], [])
        self.assertIsNone(source["scenario_state"])
        self.assertNotIn("scenario_execution", outcome.update)
        self.assertTrue(all("term_months" not in b["assumptions"]["holding_actions"][0]["target_criteria"] for b in scenario["scenarios"]))
        tool_events = [e for e in trace if e["node"] == "tools"]
        self.assertEqual(len(tool_events), 1)
        self.assertNotEqual(tool_events[0]["output"]["observation"]["result"].get("status"), "failed")

    def test_common_work_must_have_multiple_branch_consumers(self):
        scenario = prepare_scenarios(self._scenario_inputs())["scenario_state"]
        plan = normalize_plan(self._branch_plan(), None, True)
        # 공통 조회를 한 분기만 쓰는 경우와 아예 쓰지 않는 경우는 모두 반려한다.
        for kept in ({"s_a"}, set()):
            invalid = deepcopy(plan)
            for task in invalid["tasks"]:
                if task.get("scenario_id") not in kept:
                    task["depends_on"] = [d for d in task["depends_on"] if d != "common"]
            with self.subTest(kept=kept), self.assertRaisesRegex(SupervisorContractError, "두 개 이상의 분기"):
                validate_plan(invalid, scenario)
        # 조회 자체가 여러 분기에서 간접적으로 사용되면 허용한다.
        valid = deepcopy(plan)
        valid["tasks"].append({"task_id": "terms", "agent": "product", "scenario_id": None, "objective": "특정 계약 원문 조회", "depends_on": [], "status": "pending", "run_id": None})
        valid["tasks"][0]["depends_on"] = ["terms"]
        validate_plan(valid, scenario)

    def test_fund_calculation_cannot_be_shared(self):
        scenario = prepare_scenarios(self._scenario_inputs())["scenario_state"]
        plan = normalize_plan(self._branch_plan(), None, True)
        plan["tasks"][0]["agent"] = "fund"
        with self.assertRaisesRegex(SupervisorContractError, "Fund 계산"):
            validate_plan(plan, scenario)
        # 시나리오가 없는 일반 Fund 단독 계획은 그대로 허용한다.
        single = normalize_plan({"tasks": [{"task_id": "fund", "agent": "fund", "objective": "현재 재무 계산"}]}, None, False)
        validate_plan(single, None)

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

    def _combined_decision(self):
        # API 출력 계약 검사에만 사용되는 분기 초안이다.
        draft = self._scenario_inputs()
        draft["scenarios"] = draft["scenarios"][:2]
        tasks = []
        for branch in draft["scenarios"]:
            sid = branch["id"]
            tasks.extend([
                {"task_id": f"product_{sid}", "agent": "product", "scenario_id": sid, "objective": "해당 조건의 적금 후보와 금리 조사", "depends_on": []},
                {"task_id": f"fund_{sid}", "agent": "fund", "scenario_id": sid, "objective": "선행 상품 조건으로 36개월 원금·이자 산출", "depends_on": [f"product_{sid}"]},
            ])
        return {"kind": "submit_plan", "reason": "납입 전략별 독립 분석",
                "intent": {"purpose": "납입 전략 비교", "operation": "compare", "analysis_mode": "strategy_compare", "supplied_values": {}, "profile_update_status": "not_needed"},
                "scenario_state": draft, "execution_plan": {"tasks": tasks}}

    def test_combined_scenario_plan_one_http_call(self):
        decision = self._combined_decision()
        raw = {"status": "completed", "output": [{"type": "function_call", "name": "supervisor_decision", "arguments": json.dumps(decision)}],
               "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "input_tokens_details": {"cached_tokens": 2}, "output_tokens_details": {"reasoning_tokens": 1}}}
        def response(req, timeout):
            payload = json.loads(req.data)
            self.assertIn("scenario_state", payload["tools"][0]["parameters"]["properties"])
            self.assertEqual(json.loads(payload["input"])["main_state"]["messages"][-1]["content"], "월 30만 원과 40만 원 적금 비교")
            return BytesIO(json.dumps(raw).encode())
        source = {"messages": [{"type": "human", "content": "월 30만 원과 40만 원 적금 비교"}], "turn_id": "t1", "consultation_id": "c1", "scenario_state": None, "intent": None, "financial_profile": {"monthly_net_income_krw": 3200000}}
        before, trace = deepcopy(source), []
        with patch("agent.supervisor.llm.openai_api.urlopen", side_effect=response) as post:
            decider = OpenAIDecider("unit-test-key", "unit-test-model")
            outcome = Supervisor(decide=decider, trace_sink=trace.append).run(source, use_graph=False)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(decider.calls, 1)
        self.assertEqual(outcome.tool_calls, 0)
        self.assertEqual(decider.usage[0]["cached_input_tokens"], 2)
        self.assertEqual(decider.usage[0]["reasoning_tokens"], 1)
        self.assertGreater(decider.input_sizes[0]["schema_bytes"], 0)
        self.assertEqual(outcome.next_node, "plan_validator")
        self.assertEqual(execution_waves(outcome.update["execution_plan"], outcome.update["scenario_state"]), [["product_s_a", "product_s_b"], ["fund_s_a", "fund_s_b"]])
        self.assertEqual([e["node"] for e in trace], ["initial_memory", "reason", "plan", "finalize", "return"])
        self.assertEqual(source, before)
        self.assertNotIn("financial_profile", outcome.update)
        self.assertTrue(all(b["assumptions"]["holding_actions"][0]["status"] == "hypothetical" for b in outcome.update["scenario_state"]["scenarios"]))

    def test_combined_scenario_ids_and_repair(self):
        decision = self._combined_decision()
        for i, branch in enumerate(decision["scenario_state"]["scenarios"], 1):
            old = branch.pop("id")
            for task in decision["execution_plan"]["tasks"]:
                if task["scenario_id"] == old:
                    task["scenario_id"] = f"s_{i:02d}"
        outcome = Supervisor(decide=lambda _: deepcopy(decision)).run({}, use_graph=False)
        self.assertEqual([s["id"] for s in outcome.update["scenario_state"]["scenarios"]], ["s_01", "s_02"])
        bad = deepcopy(decision)
        bad["execution_plan"]["tasks"][0]["scenario_id"] = "unknown"
        calls = []
        def repair(context):
            calls.append(context)
            return deepcopy(bad if len(calls) == 1 else decision)
        fixed = Supervisor(decide=repair).run({}, use_graph=False)
        self.assertEqual(fixed.next_node, "plan_validator")
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1]["issues"])
        self.assertEqual(fixed.tool_calls, 0)

    def test_combined_scenario_baseline_cannot_bypass_verification(self):
        from ..tools.scenarios import prepare_verified_scenarios
        args = self._scenario_inputs()
        args["baseline_ref"] = "verified_snapshot"
        args["scenarios"][0]["assumptions"]["holding_actions"] = [{"action": "keep", "holding_ref": "holding_a"}]
        with self.assertRaisesRegex(SupervisorContractError, "Unverified baseline_ref"):
            prepare_verified_scenarios(args, {"source": {}, "observations": []})
        verified = {"source": {}, "observations": [{"name": "profile_agent", "request": {"operation": "inspect"}, "result": {"baseline_ref": "verified_snapshot", "profile": {"financial_profile": {"holdings": [{"holding_id": "holding_a", "status": "active"}]}}}}]}
        self.assertEqual(prepare_verified_scenarios(args, verified)["scenario_state"]["baseline_ref"], "verified_snapshot")
        bad = deepcopy(args)
        bad["scenarios"][0]["assumptions"]["holding_actions"][0]["holding_ref"] = "unknown"
        with self.assertRaisesRegex(SupervisorContractError, "holding_ref"):
            prepare_verified_scenarios(bad, verified)
        decision = self._combined_decision()
        decision["scenario_state"]["baseline_ref"] = "fabricated_snapshot"
        with self.assertRaisesRegex(SupervisorContractError, "Unverified baseline_ref"):
            Supervisor(decide=lambda _: deepcopy(decision), max_repairs=0).run({}, use_graph=False)

    def test_real_lookup_round_trip_retained(self):
        decision = self._combined_decision()
        seen = []
        calls = []
        def retrieve(request):
            calls.append(request)
            return {"retrieval_status": "found", "retrieved": [{"memory_id": "m1", "memory_type": "user_decision", "content": "기존 상품의 해지 조건을 확인", "source_ref": "a1"}]}
        def decide(context):
            seen.append(deepcopy(context))
            if len(seen) == 1:
                return {"kind": "call_tool", "reason": "관련 기억 확인", "tool_name": "retrieve_memory", "tool_input": {"query": "해지"}}
            self.assertEqual(context["main_state"]["memory"]["retrieved"][0]["memory_id"], "m1")
            self.assertNotIn("latest_memory_detail", context)
            self.assertNotIn("retrieved", context["observations"][-1]["result"])
            return deepcopy(decision)
        out = Supervisor(decide=decide, ports=SupervisorPorts(retrieve_memory=retrieve)).run({}, use_graph=False)
        self.assertEqual(out.next_node, "plan_validator")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(seen), 2)
        self.assertEqual(out.tool_calls, 1)

    def test_compact_context_keeps_current_facts_and_denied_memory_out(self):
        from ..nodes.reason import build_context
        source = {"conversation_summary": "이전 대화 요약", "messages": [{"type": "human", "content": "최신 질문"}] + [{"type": "ai", "content": f"설명 {i}"} for i in range(8)],
                  "financial_profile": {"monthly_net_income_krw": 0, "monthly_living_expenses_krw": None},
                  "pending_input": [{"id": "q1", "status": "pending", "question": "필수 질문"}],
                  "execution_plan": {"revision": 2, "tasks": [{"task_id": "fund_a", "run_id": "r2", "scenario_id": "s_a"}]},
                  "agent_results": {"fund_a": {"run_id": "r1", "plan_revision": 1, "scenario_id": "s_a", "summary": "구 실행"}},
                  "review_results": {"fund_a": {"target_run_id": "r2", "reflection": {"status": "revise", "issues": ["금리 전제 확인"]}}, "old": {"target_run_id": "r0"}}}
        request = self._scenario_inputs()
        prepared = prepare_scenarios(request)
        memory_item = {"memory_id": "m1", "memory_type": "user_preference", "content": "denied-sensitive-content", "source_ref": "a1"}
        observations = [{"name": "prepare_scenarios", "request": request, "result": prepared},
                        {"name": "retrieve_memory", "request": {"query": "선호"}, "result": {"retrieval_status": "found", "retrieved": [memory_item]}},
                        {"name": "retrieve_memory", "request": {"query": "다른 선호"}, "result": {"retrieval_status": "access_denied", "retrieved": []}}]
        state = {"source": source, "observations": observations, "issues": ["계획 수정"]}
        before = deepcopy(state)
        context = build_context(Supervisor(decide=lambda _: {}), state, 2)
        self.assertEqual(context["main_state"]["messages"][0]["content"], "최신 질문")
        self.assertEqual(len(context["main_state"]["messages"]), 5)
        self.assertEqual(context["main_state"]["pending_input"], source["pending_input"])
        self.assertNotIn("agent_results", context["main_state"])
        self.assertEqual(list(context["main_state"]["review_results"]), ["fund_a"])
        self.assertEqual(context["issues"], ["계획 수정"])
        avail = context["profile_availability"]["financial_profile"]
        self.assertIn("monthly_net_income_krw", avail["available"])
        self.assertIn("monthly_living_expenses_krw", avail["unknown"])
        self.assertIn("total_assets_krw", avail["not_loaded"])
        self.assertNotIn("request", context["observations"][0])
        self.assertEqual(context["observations"][0]["result"], prepared)
        self.assertNotIn("denied-sensitive-content", json.dumps(context))
        self.assertEqual(state, before)

    def test_latest_memory_overflow_only_once(self):
        from ..nodes.reason import build_context
        records = [{"memory_id": f"m{i}", "memory_type": "user_preference", "content": f"선호 {i}", "source_ref": f"a{i}"} for i in range(12)]
        state = {"source": {}, "observations": [{"name": "retrieve_memory", "request": {"query": "선호"}, "result": {"retrieval_status": "found", "retrieved": records}}]}
        context = build_context(Supervisor(decide=lambda _: {}), state, 2)
        merged = context["main_state"]["memory"]["retrieved"]
        extra = context["latest_memory_detail"]["retrieved"]
        self.assertEqual(len(merged), 10)
        self.assertEqual(len(extra), 2)
        self.assertEqual({i["memory_id"] for i in merged + extra}, {i["memory_id"] for i in records})
        self.assertTrue(set(i["memory_id"] for i in merged).isdisjoint(i["memory_id"] for i in extra))

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


class CandidateAndFollowupTests(unittest.TestCase):
    @staticmethod
    def _task(tid, agent="product", sid=None, deps=()):
        return {"task_id": tid, "agent": agent, "scenario_id": sid,
                "objective": f"{tid} 필요한 결과 조사", "depends_on": list(deps)}

    @staticmethod
    def _intent(operation="new", mode=None, ref=None):
        result = {"purpose": "재무 전략 분석", "operation": operation,
                  "supplied_values": {}, "profile_update_status": "not_needed"}
        if mode:
            result["analysis_mode"] = mode
        if ref:
            result["target_advice_ref"] = ref
        return result

    def _plan(self, tasks, scenarios=None, intent=None, **extra):
        return {"kind": "submit_plan", "reason": "계획", "intent": intent or self._intent(),
                "execution_plan": {"tasks": tasks}, **({"scenario_state": scenarios} if scenarios else {}), **extra}

    def _execute(self, decision, source=None, ports=None):
        calls = []
        def decide(context):
            calls.append(deepcopy(context))
            return deepcopy(decision)
        s = Supervisor(decide=decide, ports=ports, max_repairs=0)
        outcome = s.run(source or {"messages": [{"type": "human", "content": "계획"}]}, use_graph=False)
        self.assertEqual(len(calls), 1)
        return outcome.update, calls[0]

    def test_candidate_limits_zero_to_three_branches(self):
        for n, cap in [(0, 3), (1, 3), (2, 3), (3, 2)]:
            with self.subTest(n=n):
                ids = [f"s{i}" for i in range(n)] if n else [None]
                scenarios = {"scenarios": [{"id": sid, "label": sid} for sid in ids]} if n else None
                tasks = [self._task(f"{a}_{i}", a, sid) for i, sid in enumerate(ids) for a in ("product", "policy", "fund")]
                state, _ = self._execute(self._plan(tasks, scenarios))
                for task in state["execution_plan"]["tasks"]:
                    if task["agent"] == "fund":
                        self.assertNotIn("candidate_limit", task)
                    else:
                        self.assertEqual(task["candidate_limit"], cap)
                for agent in ("product", "policy"):
                    self.assertLessEqual(sum(t["candidate_limit"] for t in state["execution_plan"]["tasks"] if t["agent"] == agent), 6)

    def test_multiple_searches_share_branch_budget(self):
        scenarios = {"scenarios": [{"id": f"s{i}", "label": f"s{i}"} for i in range(3)]}
        tasks = [self._task("a1", sid="s0"), self._task("a2", sid="s0"), self._task("b", sid="s1"), self._task("c", sid="s2")]
        state, _ = self._execute(self._plan(tasks, scenarios))
        self.assertEqual({t["task_id"]: t["candidate_limit"] for t in state["execution_plan"]["tasks"]}, {"a1": 1, "a2": 1, "b": 2, "c": 2})
        with self.assertRaisesRegex(SupervisorContractError, "후보 슬롯"):
            self._execute(self._plan(tasks + [self._task("a3", sid="s0")], scenarios))

    def test_shared_query_charged_to_each_consumer(self):
        state, _ = self._execute(self._plan(ContractTests._branch_plan()["tasks"], ContractTests._scenario_inputs()))
        self.assertEqual({t["task_id"]: t["candidate_limit"] for t in state["execution_plan"]["tasks"] if "candidate_limit" in t}, {"common": 1, "policy_c": 2, "product_c": 1})

    def test_server_limit_not_llm_owned(self):
        from ..candidate_budget import validate_candidate_limits
        draft = self._task("p")
        draft["candidate_limit"] = 999
        state, _ = self._execute(self._plan([draft]))
        plan = state["execution_plan"]
        self.assertEqual(plan["tasks"][0]["candidate_limit"], 3)
        plan["tasks"][0]["candidate_limit"] = 999
        with self.assertRaisesRegex(SupervisorContractError, "변조"):
            validate_candidate_limits(plan, None)

    def test_dispatch_and_collect_limits(self):
        from ..candidate_budget import search_agent_input, validate_candidate_output
        state, _ = self._execute(self._plan([self._task("p")]))
        task = state["execution_plan"]["tasks"][0]
        task["run_id"] = "r1"
        original = {"task_id": "p", "scenario_id": None, "run_id": "r1", "objective": "상품 조사"}
        payload = search_agent_input(task, original)
        self.assertEqual(payload["candidate_limit"], 3)
        self.assertNotIn("candidate_limit", original)
        self.assertEqual(payload["objective"], original["objective"])
        result = {"task_id": "p", "scenario_id": None, "run_id": "r1", "status": "completed", "data": {"candidates": [{"ref": str(i)} for i in range(3)]}, "evidence_refs": [str(i) for i in range(10)]}
        validate_candidate_output(task, result)
        result["data"]["candidates"].append({"ref": "extra"})
        with self.assertRaisesRegex(SupervisorContractError, "상한 초과"):
            validate_candidate_output(task, result)
        result["data"]["candidates"] = []
        validate_candidate_output(task, result)
        result["scenario_id"] = "other"
        with self.assertRaises(SupervisorContractError):
            validate_candidate_output(task, result)

    def test_candidate_compare_not_split(self):
        with self.assertRaisesRegex(SupervisorContractError, "단일 전략"):
            self._execute(self._plan([self._task("a", sid="s_a"), self._task("b", sid="s_b")],
                {"scenarios": [{"id": "s_a", "label": "A"}, {"id": "s_b", "label": "B"}]},
                self._intent("compare", "candidate_compare")))

    def _snapshot_source(self, snapshot=None):
        snapshot = snapshot or {"status": "found", "advice_ref": "a1", "scenario_state": ContractTests._scenario_inputs()}
        source = {"messages": [{"type": "human", "content": "B안으로 상품을 더 찾아줘"}],
                  "memory": {"last_advice_ref": "a1"}, "turn_id": "new", "consultation_id": "c1"}
        calls = []
        def load(ref):
            calls.append(ref)
            return deepcopy(snapshot)
        return source, SupervisorPorts(load_advice_snapshot=load), calls

    def test_select_previous_branch_raises_limit_to_three(self):
        source, ports, loads = self._snapshot_source()
        original = deepcopy(source)
        state, context = self._execute(self._plan([self._task("p", sid="s_b")], intent=self._intent("continue", ref="a1"), selected_scenario_id="s_b"), source, ports)
        self.assertEqual(loads, ["a1"])
        self.assertEqual(source, original)
        self.assertEqual(state["scenario_state"]["scenarios"], [prepare_scenarios(ContractTests._scenario_inputs())["scenario_state"]["scenarios"][1]])
        self.assertEqual(state["execution_plan"]["tasks"][0]["candidate_limit"], 3)
        self.assertEqual(len([o for o in context["observations"] if o["name"] == "prior_advice_snapshot"]), 1)

    def test_selected_branch_refinement_preserves_conditions(self):
        source, ports, _ = self._snapshot_source()
        branches = {"scenarios": [{"id": f"new{i}", "label": f"비중 {a}", "assumptions": {"allocation_pct": {"저축": a, "상환": 100-a}}} for i, a in enumerate((70, 50, 30))]}
        state, _ = self._execute(self._plan([self._task(f"p{i}", sid=f"new{i}") for i in range(3)], branches,
                    self._intent("compare", "strategy_compare", "a1"), selected_scenario_id="s_b"), source, ports)
        self.assertEqual(len(state["scenario_state"]["scenarios"]), 3)
        for i, branch in enumerate(state["scenario_state"]["scenarios"]):
            self.assertEqual(branch["assumptions"]["horizon_months"], 36)
            self.assertEqual(branch["assumptions"]["holding_actions"][0]["target_criteria"]["monthly_contribution_krw"], 400000)
            self.assertEqual(branch["assumptions"]["allocation_pct"]["저축"], (70, 50, 30)[i])
        self.assertEqual([t["candidate_limit"] for t in state["execution_plan"]["tasks"]], [2, 2, 2])

    def test_selection_needs_verified_snapshot_and_valid_id(self):
        source, _, _ = self._snapshot_source()
        decision = self._plan([self._task("p", sid="s_b")], intent=self._intent("continue", ref="a1"), selected_scenario_id="s_b")
        with self.assertRaisesRegex(SupervisorContractError, "스냅샷"):
            self._execute(decision, source)
        _, ports, _ = self._snapshot_source({"status": "found", "advice_ref": "other", "scenario_state": ContractTests._scenario_inputs()})
        with self.assertRaisesRegex(SupervisorContractError, "스냅샷"):
            self._execute(decision, source, ports)
        _, ports, _ = self._snapshot_source()
        decision["selected_scenario_id"] = "missing"
        with self.assertRaisesRegex(SupervisorContractError, "존재하지"):
            self._execute(decision, source, ports)

    def test_prior_contract_is_not_current_verified_baseline(self):
        scenario = {"baseline_ref": "old_snapshot", "scenarios": [{"id": "s_b", "label": "유지", "assumptions": {"holding_actions": [{"action": "keep", "holding_ref": "h1"}]}}]}
        source, ports, _ = self._snapshot_source({"status": "found", "advice_ref": "a1", "scenario_state": scenario})
        with self.assertRaisesRegex(SupervisorContractError, "Unverified baseline_ref"):
            self._execute(self._plan([self._task("p", sid="s_b")], intent=self._intent("continue", ref="a1"), selected_scenario_id="s_b"), source, ports)

    def test_allocation_validation(self):
        for invalid in [{"a": 80, "b": 80}, {"a": -10, "b": 110}, {"a": True, "b": 99}, {"a": float("nan"), "b": 100}, {"a": 50}]:
            with self.subTest(invalid=invalid), self.assertRaisesRegex(SupervisorContractError, "allocation_pct"):
                prepare_scenarios({"scenarios": [{"label": "비중", "assumptions": {"allocation_pct": invalid}}]})
        valid = {"a": 33.3, "b": 66.7}
        state = prepare_scenarios({"scenarios": [{"label": "비중", "assumptions": {"allocation_pct": valid}}]})["scenario_state"]
        self.assertEqual(state["scenarios"][0]["assumptions"]["allocation_pct"], valid)
        with self.assertRaisesRegex(SupervisorContractError, "동일한 배분 항목"):
            prepare_scenarios({"scenarios": [{"label": "A", "assumptions": {"allocation_pct": valid}}, {"label": "B", "assumptions": {"allocation_pct": {"c": 100}}}]})

    def test_limit_changes_invalidate_old_completed_runs(self):
        from ..candidate_budget import apply_candidate_limits
        draft = {"tasks": [self._task("p")]}
        old = normalize_plan(draft, None, False)
        old["tasks"][0].update(status="completed", run_id="old_run")
        new = apply_candidate_limits(normalize_plan(draft, old, False), None, old)
        self.assertEqual(new["revision"], old["revision"] + 1)
        self.assertEqual(new["tasks"][0]["status"], "pending")
        self.assertIsNone(new["tasks"][0]["run_id"])
        new["tasks"][0].update(status="completed", run_id="new_run")
        again = apply_candidate_limits(normalize_plan(draft, new, False), None, new)
        self.assertEqual(again, new)


class ClarificationTests(unittest.TestCase):
    """질문 분기와 Profile 필드 계약만 검사하며 실제 모델 판단을 모의 평가하지 않는다."""
    income = "financial_profile.monthly_net_income_krw"
    living = "financial_profile.monthly_living_expenses_krw"
    debt = "financial_profile.monthly_debt_payment_krw"
    assets = "financial_profile.available_assets_krw"
    gross = "financial_profile.annual_gross_income_krw"

    def _source(self, text="매달 얼마를 저축할 수 있어?"):
        return {"messages": [{"type": "human", "content": text}],
                "turn_id": "t1", "consultation_id": "c1", "phase": "planning"}

    def _inspect(self, fields):
        return {"kind": "call_tool", "reason": "필수 값 확인", "tool_name": "profile_agent",
                "tool_input": {"operation": "inspect", "instruction": "지정 필드 조회", "is_temporary": True, "fields": fields}}

    def _ask(self, fields, *, target="profile", scenario_id=None, **extra):
        return {"kind": "ask_user", "reason": "필수 정보 확인", "pending_input": [
            {"question": "필요한 조건을 알려주세요.", "fields": fields, "target": target,
             "scenario_id": scenario_id, **extra}]}

    def _plan(self, *, supplied=None, task_id="fund_1"):
        return {"kind": "submit_plan", "reason": "현재 범위에서 진행",
                "intent": {"purpose": "저축 가능액", "operation": "new", "supplied_values": supplied or {},
                           "profile_update_status": "not_needed"},
                "execution_plan": {"tasks": [{"task_id": task_id, "agent": "fund", "objective": "가용 금액 계산", "depends_on": []}]}}

    def test_ambiguous_intent_does_not_call_profile(self):
        calls = []
        source = self._source("그거 하는 게 좋을까?")
        before = deepcopy(source)
        def read(fields):
            calls.append(fields)
            raise AssertionError("의도 확인에 프로필 조회 불필요")
        out = Supervisor(decide=lambda _: self._ask(["intended_subject"], target="supervisor"),
                         ports=SupervisorPorts(get_profile_fields=read)).run(source, use_graph=False)
        self.assertEqual(out.next_node, "ask_user")
        self.assertEqual(out.update["phase"], "waiting_for_user")
        self.assertEqual(out.update["pending_input"][0]["target"], "supervisor")
        self.assertEqual(out.update["pending_input"][0]["id"], "ask_01")
        self.assertEqual(out.tool_calls, 0)
        self.assertEqual(calls, [])
        self.assertEqual(source, before)
        self.assertNotIn("execution_plan", out.update)

    def test_inspection_batches_reuses_and_distinguishes_statuses(self):
        source = self._source()
        source["financial_profile"] = {"monthly_debt_payment_krw": 0}
        source["basic_profile"] = {"owns_home": False, "goals": []}
        fields = [self.debt, "basic_profile.owns_home", "basic_profile.goals", self.income, self.living, self.assets, self.gross]
        calls = []
        def read(paths):
            calls.append(paths)
            return {"profile": {"financial_profile": {"monthly_net_income_krw": 3000000}},
                    "missing_info": ["monthly_living_expenses_krw"],
                    "field_status": {self.income: "available", self.living: "missing",
                                     self.assets: "ambiguous", self.gross: "access_denied"}}
        contexts = []
        def decide(context):
            contexts.append(deepcopy(context))
            return self._inspect(fields) if len(contexts) <= 2 else self._ask([self.living])
        out = Supervisor(decide=decide, ports=SupervisorPorts(get_profile_fields=read)).run(source, use_graph=False)
        self.assertEqual(calls, [[self.income, self.living, self.assets, self.gross]])
        for ctx in contexts[1:]:
            obs = ctx["observations"][-1]["result"]
            self.assertEqual(obs["missing_info"], [self.living])
            self.assertEqual(obs["field_status"][self.debt], "available")
            self.assertEqual(obs["field_status"]["basic_profile.owns_home"], "available")
            self.assertEqual(obs["field_status"]["basic_profile.goals"], "available")
            self.assertEqual(obs["field_status"][self.assets], "ambiguous")
            self.assertEqual(obs["field_status"][self.gross], "access_denied")
        self.assertEqual(out.next_node, "ask_user")
        self.assertEqual(out.update["financial_profile"], {"monthly_debt_payment_krw": 0,
                         "monthly_net_income_krw": 3000000, "monthly_living_expenses_krw": None})
        self.assertNotIn("available_assets_krw", source["financial_profile"])

    def test_legacy_read_omission_not_missing_and_optional_missing_not_question(self):
        contexts = []
        def decide(context):
            contexts.append(context)
            if len(contexts) == 1:
                return self._inspect([self.income, self.assets, self.living])
            return self._plan()
        out = Supervisor(decide=decide, ports=SupervisorPorts(get_profile_fields=lambda _: {
            "financial_profile": {"monthly_net_income_krw": 3000000, "monthly_living_expenses_krw": None}
        })).run(self._source("저축 상품을 일반적으로 설명"), use_graph=False)
        obs = contexts[1]["observations"][-1]["result"]
        self.assertEqual(obs["field_status"][self.assets], "not_loaded")
        self.assertEqual(obs["missing_info"], [self.living])
        self.assertEqual(out.next_node, "plan_validator")
        self.assertNotIn("pending_input", out.update)

    def test_read_error_and_denial_not_missing_or_retried(self):
        for mode in ("failed", "access_denied", "unavailable", "not_loaded"):
            with self.subTest(mode=mode):
                from ..tools.profile_inspection import inspect_fields
                calls = []
                def read(fields):
                    calls.append(fields)
                    if mode == "failed":
                        raise RuntimeError("sensitive error")
                    return {"status": mode, "profile": {"financial_profile": {"monthly_net_income_krw": 999}}}
                ports = SupervisorPorts(get_profile_fields=None if mode == "not_loaded" else read)
                sup = Supervisor(decide=lambda _: {}, ports=ports)
                state = {"source": self._source(), "observations": []}
                result = inspect_fields(sup, state, [self.income])
                self.assertEqual(result["field_status"], {self.income: mode})
                self.assertEqual(result["missing_info"], [])
                self.assertEqual(result["profile"], {})
                state["observations"].append({"name": "profile_agent", "request": self._inspect([self.income])["tool_input"], "result": result})
                self.assertEqual(inspect_fields(sup, state, [self.income]), result)
                self.assertEqual(len(calls), 0 if mode == "not_loaded" else 1)

    def test_profile_status_normalization_and_unresolved_values_rejected(self):
        from ..tools.ports import normalize_profile_result
        result = normalize_profile_result({"profile": {}, "missing_info": ["monthly_net_income_krw", self.income],
                                           "field_status": {self.income: "ambiguous"}})
        self.assertEqual(result["missing_info"], [])
        self.assertEqual(result["field_status"], {self.income: "ambiguous"})
        for raw in (
            {"profile": {"financial_profile": {"monthly_net_income_krw": 3000000}}, "missing_info": [], "field_status": {self.income: "ambiguous"}},
            {"profile": {}, "missing_info": [self.income], "field_status": {self.income: "available"}},
            {"profile": {}, "missing_info": [], "field_status": {self.income: "made_up"}},
            {"profile": {}, "missing_info": [], "field_status": {self.income: "conflict"}, "changes": [{"domain": "financial", "field": "monthly_net_income_krw", "value": 3000000}]},
        ):
            with self.subTest(raw=raw), self.assertRaises(SupervisorContractError):
                normalize_profile_result(raw)

    def test_prepare_ambiguity_does_not_hydrate_or_save(self):
        calls = []
        def prepare(request):
            calls.append(request)
            return {"profile": {}, "missing_info": [], "field_status": {self.income: "ambiguous", self.gross: "ambiguous"}}
        decisions = iter([
            {"kind": "call_tool", "reason": "소득 정보 정리", "tool_name": "profile_agent",
             "tool_input": {"operation": "prepare", "is_temporary": True, "instruction": "월급 300만 원의 의미 확인"}},
            self._ask(["income_basis"], target="supervisor"),
        ])
        out = Supervisor(decide=lambda _: next(decisions), ports=SupervisorPorts(profile_agent=prepare)).run(self._source("월급 300만원이야. 얼마 저축할까?"), use_graph=False)
        self.assertEqual(out.next_node, "ask_user")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("financial_profile", out.update)
        self.assertNotIn("financial_profile_changes", out.update)

    def test_question_dedup_and_id_reservation(self):
        from ..clarification import merge_pending_questions
        source = {"pending_input": [{"id": "ask_01", "question": "소득?", "fields": ["monthly_net_income_krw"],
                                     "target": "profile", "scenario_id": None, "status": "pending"},
                                    {"id": "ask_02", "question": "목표?", "fields": ["purpose"], "target": "supervisor",
                                     "scenario_id": None, "status": "resolved", "answers": {"purpose": "저축"}}]}
        before = deepcopy(source)
        new = [*self._ask([self.income, "monthly_net_income_krw"])["pending_input"], *self._ask([self.living])["pending_input"]]
        merged = merge_pending_questions(new, source, None)
        self.assertEqual(len(merged), 3)
        self.assertEqual(merged[:2], source["pending_input"])
        self.assertNotIn(merged[2]["id"], {"ask_01", "ask_02"})
        self.assertEqual(merged[2]["fields"], [self.living])
        self.assertEqual(source, before)

    def test_invalid_questions_rejected(self):
        from ..clarification import merge_pending_questions
        src = {"pending_input": [{"id": "ask_01", "question": "소득?", "fields": [self.income], "target": "profile", "scenario_id": None, "status": "resolved"}]}
        for draft in (self._ask([self.income], id="ask_01"), self._ask(["fake_field"]),
                      self._ask([self.income], target="unknown_task"), self._ask([self.income], scenario_id="unknown"),
                      self._ask([self.income], answers={self.income: 1}), self._ask([self.income], status="resolved")):
            with self.subTest(draft=draft), self.assertRaises(SupervisorContractError):
                merge_pending_questions(draft["pending_input"], src, None)

    def test_specialist_needs_input_scoped_to_existing_task(self):
        source = self._source()
        source["scenario_state"] = prepare_scenarios({"scenarios": [{"id": "s1", "label": "가정"}]})["scenario_state"]
        source["execution_plan"] = {"revision": 1, "tasks": [{"task_id": "fund_a", "agent": "fund", "scenario_id": "s1", "run_id": "r1", "status": "needs_input"}]}
        source["agent_results"] = {"fund_a": {"run_id": "r1", "plan_revision": 1, "scenario_id": "s1", "status": "needs_input", "needed_fields": [self.living]}}
        def decide(context):
            self.assertEqual(context["main_state"]["agent_results"]["fund_a"]["needed_fields"], [self.living])
            return self._ask([self.living], target="fund_a")
        out = Supervisor(decide=decide).run(source, use_graph=False)
        self.assertEqual(out.update["pending_input"][0]["scenario_id"], "s1")
        self.assertEqual(out.update["pending_input"][0]["target"], "fund_a")

    def test_validated_answer_resume_keeps_turn_and_invalidates_prior_run(self):
        from ..clarification import resolve_validated_answers
        source = self._source()
        source["phase"] = "waiting_for_user"
        source["intent"] = self._plan()["intent"]
        source["execution_plan"] = normalize_plan(self._plan()["execution_plan"], None, False)
        source["execution_plan"]["tasks"][0].update(status="needs_input", run_id="r1")
        source["pending_input"] = [{"id": "q1", "question": "부채 상환액?", "fields": [self.debt], "target": "fund_1", "scenario_id": None, "status": "pending"}]
        before = deepcopy(source)
        for bad in ({"unknown": {self.debt: 0}}, {"q1": {}}, {"q1": {self.debt: None}}, {"q1": {self.debt: -1}}):
            with self.assertRaises(SupervisorContractError):
                resolve_validated_answers(source, bad)
        resumed = {**source, **resolve_validated_answers(source, {"q1": {self.debt: 0}})}
        self.assertEqual(source, before)
        self.assertEqual(resumed["turn_id"], "t1")
        self.assertEqual(resumed["pending_input"][0]["status"], "resolved")
        self.assertNotIn("financial_profile", resumed)
        def decide(context):
            self.assertEqual(context["main_state"]["pending_input"][0]["answers"], {self.debt: 0})
            return self._plan()
        out = Supervisor(decide=decide).run(resumed, use_graph=False)
        self.assertEqual(out.next_node, "plan_validator")
        self.assertEqual(out.update["execution_plan"]["revision"], 2)
        self.assertEqual(out.update["execution_plan"]["tasks"][0]["status"], "pending")
        self.assertIsNone(out.update["execution_plan"]["tasks"][0]["run_id"])
        self.assertNotIn("financial_profile", out.update)
        self.assertNotIn("pending_input", out.update)

    def test_pending_questions_block_plan_until_resolved(self):
        src = self._source()
        src["pending_input"] = [{"id": "q1", "question": "월 생활비?", "fields": [self.living], "target": "profile", "scenario_id": None, "status": "pending"}]
        with self.assertRaises(SupervisorContractError):
            Supervisor(decide=lambda _: self._plan(), max_repairs=0).run(src, use_graph=False)

    def test_rephrase_same_id_and_reject_overlapping_questions(self):
        from ..clarification import merge_pending_questions
        src = {"pending_input": [{"id": "q1", "question": "월 소득?", "fields": [self.income],
                                  "target": "profile", "scenario_id": None, "status": "pending"}]}
        draft = self._ask([self.income], id="q1")["pending_input"]
        draft[0]["question"] = "세후 월 실수령액을 원 단위로 알려주세요."
        result = merge_pending_questions(draft, src, None)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "q1")
        self.assertEqual(result[0]["question"], draft[0]["question"])
        with self.assertRaises(SupervisorContractError):
            merge_pending_questions(self._ask([self.income, self.living])["pending_input"], src, None)

    def test_prepare_service_denial_drops_payload(self):
        contexts = []
        def decide(context):
            contexts.append(context)
            if len(contexts) == 1:
                return {"kind": "call_tool", "reason": "정보 준비", "tool_name": "profile_agent",
                        "tool_input": {"operation": "prepare", "instruction": "값 확인", "is_temporary": True}}
            return self._ask(["income_basis"], target="supervisor")
        ports = SupervisorPorts(profile_agent=lambda _: {"status": "access_denied",
            "profile": {"financial_profile": {"monthly_net_income_krw": 9876543}}, "missing_info": []})
        out = Supervisor(decide=decide, ports=ports).run(self._source(), use_graph=False)
        self.assertEqual(contexts[1]["observations"][-1]["result"], {"status": "failed", "error_code": "profile_access_denied"})
        self.assertNotIn("9876543", json.dumps(contexts[1]))
        self.assertNotIn("financial_profile", out.update)


class SuppliedValuesRegressionTests(unittest.TestCase):
    """v23 실제 LLM 로그에서 관측한 반환 형식 문제의 오프라인 회귀 검사."""

    @staticmethod
    def _plan(supplied=None):
        return {"kind": "submit_plan", "reason": "재무 전망 및 적금 조사",
                "intent": {"purpose": "2년 내 2천만 원 목표 가능성 및 적금 조사",
                           "operation": "new", "analysis_mode": "roadmap",
                           "supplied_values": supplied if supplied is not None else {},
                           "profile_update_status": "not_needed"},
                "execution_plan": {"tasks": [
                    {"task_id": "feasibility", "agent": "fund", "objective": "목표 달성 가능성 분석", "depends_on": []},
                    {"task_id": "savings", "agent": "product", "objective": "적합한 적금 조사", "depends_on": []}]}}

    @staticmethod
    def _source():
        return {"turn_id": "t1", "consultation_id": "c1", "phase": "planning",
                "messages": [{"type": "human", "content": "2년 안에 2천만 원 가능할까? 적금도 추천해 줘."}],
                "financial_profile": {"monthly_net_income_krw": 2800000, "monthly_living_expenses_krw": 1500000,
                                      "available_assets_krw": 5000000, "monthly_debt_payment_krw": 0}}

    def test_schema_and_validator_share_canonical_keys(self):
        from ..llm.adapter import DecisionSchema
        from ..tools.profile_fields import SUPPLIED_FIELDS, RECORD_FIELDS
        schema = DecisionSchema.model_json_schema()["$defs"]["IntentDraftSchema"]["properties"]["supplied_values"]
        object_schema = next(s for s in schema["anyOf"] if s.get("type") == "object")
        self.assertFalse(object_schema["additionalProperties"])
        self.assertEqual(set(object_schema["properties"]), SUPPLIED_FIELDS)
        goals = object_schema["properties"]["goals"]["items"]
        self.assertFalse(goals["additionalProperties"])
        self.assertEqual(set(goals["properties"]), RECORD_FIELDS["goals"])
        self.assertNotIn("relative", goals["properties"]["target_date_precision"]["enum"])
        self.assertNotIn("horizon_months", object_schema["properties"])

    def test_legacy_g03_api_draft_keeps_goal_and_parallel_tasks(self):
        # 사용자 실제 G-03 오류: top-level 목표액/기간은 상담 조건이지 프로필이 아니다.
        decision = self._plan({"target_amount_krw": 20000000, "horizon_months": 24})
        raw = {"status": "completed", "output": [{"type": "function_call", "name": "supervisor_decision",
                "arguments": json.dumps(decision)}]}
        source = self._source()
        before = deepcopy(source)
        draft_before = deepcopy(decision)
        with patch("agent.supervisor.llm.openai_api.urlopen", return_value=BytesIO(json.dumps(raw).encode())):
            decider = OpenAIDecider("unit-test-key", "unit-test-model")
            out = Supervisor(decide=decider, max_repairs=0).run(source, use_graph=False)
        self.assertEqual(decider.calls, 1)
        self.assertEqual(out.next_node, "plan_validator")
        self.assertEqual(out.update["intent"]["supplied_values"], {})
        for text in [out.update["intent"]["purpose"], *[t["objective"] for t in out.update["execution_plan"]["tasks"]]]:
            self.assertIn("20000000원", text)
            self.assertIn("24개월", text)
        self.assertEqual(execution_waves(out.update["execution_plan"]), [["feasibility", "savings"]])
        self.assertEqual(out.update["execution_plan"]["tasks"][1]["candidate_limit"], 3)
        self.assertNotIn("financial_profile", out.update)
        self.assertNotIn("basic_profile_changes", out.update)
        self.assertEqual(source, before)
        self.assertEqual(decision, draft_before)

    def test_legacy_t01_ask_preserves_supplied_profile_without_saving(self):
        # 로그에 관측된 다른 목표 별칭과 ask_user 경계도 같은 정규화를 사용한다.
        supplied = {"monthly_net_income_krw": 2700000, "monthly_living_expenses_krw": 1500000,
                    "total_debt_krw": 8000000, "available_assets_krw": 5000000,
                    "goal_target_amount_krw": 30000000, "goal_horizon_months": 36}
        draft = {"kind": "ask_user", "reason": "부족한 조건 확인",
                 "intent": {"purpose": "3년 내 3천만 원 목표에서 저축/상환 비교", "operation": "compare",
                            "analysis_mode": "strategy_compare", "supplied_values": supplied,
                            "profile_update_status": "not_needed"},
                 "pending_input": [{"question": "상환 수수료를 아시나요?", "fields": ["early_repayment_fee"],
                                    "target": "supervisor"}]}
        out = Supervisor(decide=lambda _: draft, max_repairs=0).run(self._source(), use_graph=False)
        self.assertEqual(out.next_node, "ask_user")
        self.assertEqual(out.update["intent"]["supplied_values"], {k: v for k, v in supplied.items() if not k.startswith("goal_")})
        self.assertIn("30000000원", out.update["intent"]["purpose"])
        self.assertIn("36개월", out.update["intent"]["purpose"])
        self.assertNotIn("execution_plan", out.update)
        self.assertNotIn("financial_profile_changes", out.update)

    def test_canonical_dotted_fields_normalize_without_coercion(self):
        from ..tools.profile_fields import normalize_supplied_values
        raw = {"financial_profile.monthly_net_income_krw": 2700000,
               "monthly_net_income_krw": 2700000, "basic_profile.owns_home": False,
               "financial_profile.monthly_debt_payment_krw": 0,
               "monthly_living_expenses_krw": None}
        before = deepcopy(raw)
        actual, conditions = normalize_supplied_values(raw)
        self.assertEqual(actual, {"monthly_net_income_krw": 2700000, "owns_home": False,
                                  "monthly_debt_payment_krw": 0, "monthly_living_expenses_krw": None})
        self.assertEqual(conditions, {})
        self.assertEqual(raw, before)
        for bad in ({"monthly_salary_krw": 2700000}, {"basic_profile.monthly_net_income_krw": 2700000},
                    {"financial_profile.holdings": []}, {"financial_profile.version": 1},
                    {"monthly_net_income_krw": True}, {"monthly_net_income_krw": "2700000"},
                    {"monthly_net_income_krw": 2700000, "financial_profile.monthly_net_income_krw": 2800000},
                    {"target_amount_krw": 1000000, "goal_target_amount_krw": 2000000},
                    {"horizon_months": 0}, {"horizon_months": True}):
            with self.subTest(bad=bad), self.assertRaises(SupervisorContractError):
                normalize_supplied_values(bad)

    def test_invalid_relative_goal_is_not_guessed_or_silently_dropped(self):
        from ..tools.profile_fields import normalize_supplied_values
        draft = {"financial_profile.monthly_net_income_krw": 2700000,
                 "goals": [{"goal_id": None, "goal_type": "savings", "target_amount_krw": 30000000,
                            "target_amount_type": "total", "target_date": "3년 이내",
                            "target_date_precision": "relative", "priority": None, "status": "active"}]}
        with self.assertRaisesRegex(SupervisorContractError, r"supplied_values\.goals:.*relative periods belong in purpose/objective"):
            normalize_supplied_values(draft)
        with self.assertRaisesRegex(SupervisorContractError, r"supplied_values.*'goal'"):
            normalize_supplied_values({"goal": draft["goals"][0]})

    def test_error_reports_bad_key_and_repair_can_keep_dag(self):
        decisions = []
        def decide(context):
            decisions.append(deepcopy(context))
            if len(decisions) == 1:
                return self._plan({"monthly_salary_krw": 2700000})
            self.assertIn("monthly_salary_krw", context["issues"][-1])
            self.assertIn("purpose/objective", context["issues"][-1])
            return self._plan()
        out = Supervisor(decide=decide).run(self._source(), use_graph=False)
        self.assertEqual(out.next_node, "plan_validator")
        self.assertEqual(len(decisions), 2)
        self.assertEqual(execution_waves(out.update["execution_plan"]), [["feasibility", "savings"]])

    def test_structured_goal_and_save_boundaries_are_preserved(self):
        from ..nodes.plan import merged_intent
        from ..tools.profile_fields import validate_overlay
        source = {"basic_profile": {"goals": [{"goal_id": "old", "goal_type": "emergency_fund",
                  "status": "active", "target_amount_krw": 3000000}]}}
        before = deepcopy(source)
        draft = self._plan({"goals": [{"goal_id": "old", "target_amount_krw": 4000000}]})
        merged = merged_intent(source, draft)
        self.assertEqual(merged["supplied_values"]["goals"][0]["target_amount_krw"], 4000000)
        self.assertEqual(source, before)
        for status, mode in (("pending", None), ("waiting", None), ("not_needed", "replace")):
            bad = self._plan({"target_amount_krw": 20000000})
            bad["intent"]["profile_update_status"] = status
            if mode: bad["goal_mode"] = mode
            with self.subTest(status=status, mode=mode), self.assertRaisesRegex(SupervisorContractError, "separate consultation"):
                merged_intent(source, bad)
        with self.assertRaises(SupervisorContractError):
            validate_overlay({"target_amount_krw": 20000000}, supplied=True)
        with self.assertRaises(SupervisorContractError):
            validate_overlay({"horizon_months": 24})
        with self.assertRaises(SupervisorContractError):
            validate_overlay({"goals": None}, supplied=True)

    def test_normal_t01_independent_strategy_plan_without_profile_prepare(self):
        # 계약 검사이며 실제 모델의 판단 성공을 의미하지 않는다.
        source = self._source()
        source["financial_profile"].update(monthly_net_income_krw=2700000, total_debt_krw=8000000,
            monthly_debt_payment_krw=200000, liabilities=[{"liability_id": "debt_1", "liability_type": "credit_loan",
            "balance_krw": 8000000, "annual_rate_pct": 6.0, "monthly_payment_krw": 200000, "status": "active"}])
        before = deepcopy(source)
        draft = self._plan({"monthly_net_income_krw": 2700000})
        draft["intent"].update(purpose="3년 내 3천만 원 목표 상환/청년 적금 전략 비교", operation="compare", analysis_mode="strategy_compare")
        draft["scenario_state"] = {"scenarios": [
            {"id": "repay_first", "label": "상환 우선", "assumptions": {"horizon_months": 36}},
            {"id": "save_first", "label": "저축 우선", "assumptions": {"horizon_months": 36}}]}
        draft["execution_plan"]["tasks"] = [
            {"task_id": "repay", "agent": "fund", "scenario_id": "repay_first", "objective": "기존 대출 조건으로 36개월 전망; 수수료 미확인 조건부", "depends_on": []},
            {"task_id": "search", "agent": "product", "scenario_id": "save_first", "objective": "청년 적금 조건과 자격 조사", "depends_on": []},
            {"task_id": "save", "agent": "fund", "scenario_id": "save_first", "objective": "상품 조건을 반영한 36개월 전망", "depends_on": ["search"]}]
        out = Supervisor(decide=lambda _: draft, max_repairs=0).run(source, use_graph=False)
        self.assertEqual(out.next_node, "plan_validator")
        self.assertEqual(out.tool_calls, 0)
        self.assertEqual(out.update["intent"]["supplied_values"], {"monthly_net_income_krw": 2700000})
        self.assertEqual(execution_waves(out.update["execution_plan"], out.update["scenario_state"]), [["repay", "search"], ["save"]])
        self.assertEqual(source, before)


class StateBoundaryTests(unittest.TestCase):
    """Supervisor 전용 입력·반환·State 권한의 무과금 회귀 검사."""

    def test_tool_input_and_question_schemas_are_explicit(self):
        from ..llm.adapter import DecisionSchema
        from ..tools.profile_fields import READ_FIELDS, REPRESENTATIVE
        from ..tools.scenarios import ASSUMPTION_PROPERTIES
        defs = DecisionSchema.model_json_schema()["$defs"]
        for name in ("ProfileInspectSchema", "ProfilePrepareSchema", "MemorySearchSchema", "PendingQuestionSchema"):
            self.assertFalse(defs[name]["additionalProperties"])
        self.assertEqual(set(defs["ProfileInspectSchema"]["properties"]["fields"]["items"]["enum"]), READ_FIELDS)
        self.assertNotIn("current_profile", defs["ProfilePrepareSchema"]["properties"])
        self.assertEqual(set(defs["ScenarioBranchSchema"]["properties"]["overrides"]["properties"]), REPRESENTATIVE)
        self.assertEqual(set(defs["ScenarioBranchSchema"]["properties"]["assumptions"]["properties"]), set(ASSUMPTION_PROPERTIES))
        self.assertNotIn("answers", defs["PendingQuestionSchema"]["properties"])

    def test_decision_rejects_unrelated_fields(self):
        decision = {"kind": "ask_user", "reason": "대상 확인", "pending_input": [{"question": "어떤 상품?", "fields": ["product_name"]}]}
        for key, value in (("execution_plan", {}), ("tool_input", {}), ("agent_results", {}), ("intent", {"write_state": True})):
            with self.subTest(key=key), self.assertRaises(SupervisorContractError):
                validate_decision({**decision, key: value})

    def test_inspect_without_explicit_fields_never_calls_prepare(self):
        from ..tools.ports import validate_profile_request
        bad = {"operation": "inspect", "instruction": "조회", "is_temporary": True}
        with self.assertRaisesRegex(SupervisorContractError, "explicit fields"):
            validate_profile_request(bad)
        calls = []
        decisions = iter([{"kind": "call_tool", "reason": "조회", "tool_name": "profile_agent", "tool_input": bad},
                          SuppliedValuesRegressionTests._plan()])
        out = Supervisor(decide=lambda _: next(decisions), ports=SupervisorPorts(profile_agent=lambda args: calls.append(args))).run(
            SuppliedValuesRegressionTests._source(), use_graph=False)
        self.assertEqual(out.next_node, "plan_validator")
        self.assertEqual(calls, [])

    def test_profile_changes_are_validated_before_observation(self):
        from ..tools.ports import normalize_profile_result
        change = {"change_id": "ch1", "domain": "financial", "field": "monthly_net_income_krw", "value": 3000000}
        def result(items):
            return {"profile": {}, "missing_info": [], "changes": items}
        self.assertEqual(normalize_profile_result(result([{**change, "status": "pending"}]))["changes"], [change])
        for bad in ([{**change, "status": "applied"}], [{**change, "field": "version"}],
                    [{**change, "value": True}], [change, change], [{**change, "write_state": {}}]):
            with self.subTest(bad=bad), self.assertRaises(SupervisorContractError):
                normalize_profile_result(result(bad))

    def test_temporary_prepare_cannot_emit_save_proposals(self):
        change = {"change_id": "ch1", "domain": "financial", "field": "monthly_net_income_krw", "value": 3000000}
        decisions = iter([{"kind": "call_tool", "reason": "가정", "tool_name": "profile_agent", "tool_input":
                           {"operation": "prepare", "instruction": "가상 소득", "is_temporary": True}}, SuppliedValuesRegressionTests._plan()])
        trace = []
        source = SuppliedValuesRegressionTests._source()
        before = deepcopy(source)
        out = Supervisor(decide=lambda _: next(decisions), ports=SupervisorPorts(profile_agent=lambda _: {
            "profile": {}, "missing_info": [], "changes": [change]}), trace_sink=trace.append).run(source, use_graph=False)
        self.assertEqual(source, before)
        self.assertNotIn("financial_profile_changes", out.update)
        self.assertNotIn("financial_profile", out.update)
        self.assertEqual(next(x for x in trace if x["node"] == "tools")["output"]["observation"]["result"]["status"], "failed")

    def test_scenario_contract_rejects_unregistered_keys_but_preserves_notes(self):
        result = prepare_scenarios({"scenarios": [{"label": "소득 변동", "assumptions": {
            "horizon_months": 36, "notes": "6개월 뒤 취업 가능성을 조건부 분석"}}]})
        self.assertEqual(result["scenario_state"]["scenarios"][0]["assumptions"]["notes"], "6개월 뒤 취업 가능성을 조건부 분석")
        for draft in ({"scenarios": [{"label": "가정", "assumptions": {"write_state": {}}}]},
                      {"scenarios": [{"label": "가정", "financial_profile": {}}]},
                      {"scenarios": [{"label": "가정", "assumptions": {"holding_actions": [{"action": "subscribe", "status": "applied"}]}}]},
                      {"scenarios": [{"label": "가정", "assumptions": {"holding_actions": [{"action": "subscribe", "target_criteria": {"state_patch": {}}}]}}]}):
            with self.subTest(draft=draft), self.assertRaises(SupervisorContractError):
                prepare_scenarios(draft)

    def test_node_write_scopes_block_source_and_cross_node_updates(self):
        from ..state_policy import checked_node_update
        for node, key in (("reason", "source"), ("tools", "main_patch"), ("plan", "observations"), ("finalize", "source")):
            with self.subTest(node=node), self.assertRaises(SupervisorContractError):
                checked_node_update(node, {key: {}})
        data = {"decision": {"kind": "ask_user"}, "reason_calls": 1, "retry": False}
        result = checked_node_update("reason", data)
        result["decision"]["kind"] = "invalid"
        self.assertEqual(data["decision"]["kind"], "ask_user")

    def test_main_patch_route_permissions_and_read_hydration(self):
        from ..state_policy import finalize_patch
        source = {"financial_profile": {"monthly_net_income_krw": 2800000}}
        obs = {"name": "profile_agent", "request": {"operation": "inspect", "is_temporary": True,
               "fields": ["financial_profile.monthly_living_expenses_krw"]}, "result": {
               "verified_read": True, "verified_fields": {"financial_profile": {"monthly_living_expenses_krw": 1500000}},
               "profile": {"financial_profile": {"monthly_living_expenses_krw": 1500000}},
               "field_status": {"financial_profile.monthly_living_expenses_krw": "available"}}}
        state = {"source": source, "observations": [obs]}
        valid = {"phase": "waiting_for_user", "pending_input": []}
        for key in ("financial_profile", "agent_results", "execution_plan", "final_advice", "messages"):
            with self.subTest(key=key), self.assertRaises(SupervisorContractError):
                finalize_patch(state, {**valid, key: {}}, "ask_user")
        returned = finalize_patch(state, valid, "ask_user")
        self.assertEqual(returned["financial_profile"], {"monthly_net_income_krw": 2800000, "monthly_living_expenses_krw": 1500000})
        self.assertEqual(source, {"financial_profile": {"monthly_net_income_krw": 2800000}})
        for corrupt in ("unrequested", "overwrite", "prepare"):
            forged = deepcopy(state)
            x = forged["observations"][0]
            if corrupt == "unrequested": x["request"]["fields"] = ["financial_profile.monthly_net_income_krw"]
            elif corrupt == "overwrite": forged["source"]["financial_profile"]["monthly_living_expenses_krw"] = 1
            else: x["request"]["operation"] = "prepare"
            with self.subTest(corrupt=corrupt), self.assertRaises(SupervisorContractError):
                finalize_patch(forged, valid, "ask_user")

    def test_raw_read_response_only_hydrates_requested_fields(self):
        source = SuppliedValuesRegressionTests._source()
        before = deepcopy(source)
        calls = []
        def read(fields):
            calls.append(list(fields))
            fields.append("financial_profile.total_debt_krw")
            return {"financial_profile": {"annual_gross_income_krw": 34000000, "total_debt_krw": 999, "monthly_net_income_krw": 999}}
        decisions = iter([{"kind": "call_tool", "reason": "세전 연소득 조회", "tool_name": "profile_agent", "tool_input": {
            "operation": "inspect", "instruction": "연소득 확인", "is_temporary": True, "fields": ["financial_profile.annual_gross_income_krw"]}},
            SuppliedValuesRegressionTests._plan()])
        out = Supervisor(decide=lambda _: next(decisions), ports=SupervisorPorts(get_profile_fields=read)).run(source, use_graph=False)
        self.assertEqual(calls, [["financial_profile.annual_gross_income_krw"]])
        self.assertEqual(out.update["financial_profile"], {**source["financial_profile"], "annual_gross_income_krw": 34000000})
        self.assertEqual(source, before)


class ProfileIncomeContractTests(unittest.TestCase):
    """고도화 Profile의 실제 반환 계약과 Supervisor의 읽기/변경 경계를 검증한다."""
    net = "financial_profile.monthly_net_income_krw"
    gross = "financial_profile.annual_gross_income_krw"
    living = "financial_profile.monthly_living_expenses_krw"

    @staticmethod
    def _fact(period="monthly", basis="net", amount=3000000, target="financial_profile.monthly_net_income_krw", raw="세후 월급 300만 원"):
        return {"type": "income", "period": period, "amount_krw": amount,
                "tax_basis": basis, "target_field": target, "raw_text": raw}

    @staticmethod
    def _ambiguity(fact):
        return {"type": "tax_basis_unknown" if fact["tax_basis"] == "unknown" else "unsupported_income_field",
                "raw_text": fact["raw_text"], "amount_krw": fact["amount_krw"]}

    @staticmethod
    def _result(facts, ambiguities=None, **kw):
        return {"profile": {}, "missing_info": [], "changes": [], "extracted_info": facts,
                "ambiguities": [] if ambiguities is None else ambiguities, **kw}

    @staticmethod
    def _prepare(text, *, temporary=True, change=False, required=None):
        args = {"operation": "prepare", "instruction": text, "is_temporary": temporary, "change_requested": change}
        if required is not None:
            args["required_fields"] = required
        return {"kind": "call_tool", "reason": "소득 입력 추출 및 필요한 기존 정보 검증",
                "tool_name": "profile_agent", "tool_input": args}

    @staticmethod
    def _ask(question="입력한 소득의 기준을 확인해 주세요.", fields=None):
        return {"kind": "ask_user", "reason": "필수 정보/승인 확인",
                "pending_input": [{"question": question, "fields": fields or ["income_basis"], "target": "supervisor"}]}

    def _real_ports(self):
        try:
            from agent.profile.profile_agent import run_profile_agent
        except ModuleNotFoundError as exc:
            if exc.name in {"agent.profile", "agent.profile.profile_agent"}:
                self.skipTest("고도화된 팀 Profile 모듈이 설치된 프로젝트에서 실행하세요.")
            raise
        return SupervisorPorts.with_profile_mvp()

    def test_legacy_and_new_income_metadata_preserved(self):
        from ..tools.ports import normalize_profile_result
        legacy = {"profile": {"financial_profile": {"monthly_net_income_krw": 3000000}}, "missing_info": []}
        self.assertEqual(normalize_profile_result(legacy), legacy)
        net = self._fact()
        annual = self._fact("annual", "gross", 45000000, self.gross, "세전 연봉 4,500만 원")
        monthly_gross = self._fact("monthly", "gross", 3500000, None, "세전 월급 350만 원")
        ambiguous = self._fact("monthly", "unknown", 3500000, None, "월급 350만 원")
        raw = self._result([net, annual, monthly_gross, ambiguous],
                           [self._ambiguity(monthly_gross), self._ambiguity(ambiguous)],
                           profile={"financial_profile": {"monthly_net_income_krw": 3000000, "annual_gross_income_krw": 45000000}})
        before = deepcopy(raw)
        result = normalize_profile_result(raw)
        self.assertEqual(result, raw)
        result["extracted_info"][0]["amount_krw"] = 0
        self.assertEqual(raw, before)
        self.assertEqual(normalize_profile_result(self._result([self._fact(amount=0)]))["extracted_info"][0]["amount_krw"], 0)

    def test_income_metadata_rejects_bad_shape_and_mismatch(self):
        from ..tools.ports import normalize_profile_result
        fact = self._fact()
        for change in ({"amount_krw": True}, {"amount_krw": -1}, {"amount_krw": 3.0}, {"period": "week"},
                       {"tax_basis": "unknown"}, {"target_field": self.gross}, {"raw_text": ""},
                       {"write_state": {}}):
            with self.subTest(change=change), self.assertRaises(SupervisorContractError):
                normalize_profile_result(self._result([{**fact, **change}]))
        unsupported = self._fact("monthly", "gross", 3500000, None, "세전 월급 350만 원")
        for bad in ([{**self._ambiguity(unsupported), "amount_krw": 42}],
                    [{**self._ambiguity(unsupported), "type": "tax_basis_unknown"}], [], "invalid"):
            with self.subTest(bad=bad), self.assertRaises(SupervisorContractError):
                normalize_profile_result(self._result([unsupported], bad))
        with self.assertRaises(SupervisorContractError):
            normalize_profile_result(self._result({"fake": fact}))

    def test_unsupported_or_conflicting_income_cannot_be_mapped(self):
        from ..tools.ports import normalize_profile_result
        unsupported = self._fact("monthly", "gross", 3500000, None, "세전 월급 350만 원")
        raw = self._result([unsupported], [self._ambiguity(unsupported)])
        for patch_value in ({"profile": {"financial_profile": {"annual_gross_income_krw": 42000000}}},
                            {"changes": [{"change_id": "bad", "domain": "financial", "field": "annual_gross_income_krw", "value": 42000000}]}):
            with self.subTest(patch=patch_value), self.assertRaises(SupervisorContractError):
                normalize_profile_result({**raw, **patch_value}, current_profile={})
        with self.assertRaisesRegex(SupervisorContractError, "conflicting mapped income"):
            normalize_profile_result(self._result([self._fact(), self._fact(amount=3500000)]))

    def test_required_fields_status_checks_snapshot_not_delta(self):
        from ..tools.ports import normalize_profile_result
        base = {"financial_profile": {"monthly_net_income_krw": None, "monthly_living_expenses_krw": 1500000}}
        raw = self._result([self._fact()], profile={"financial_profile": {"monthly_net_income_krw": 3000000}},
                           field_status={self.net: "unknown", self.living: "available", self.gross: "not_loaded"},
                           missing_info=["monthly_net_income_krw", self.gross])
        result = normalize_profile_result(raw, current_profile=base, required_fields=[self.net, self.living, self.gross])
        self.assertEqual(result["field_status"], {self.net: "missing", self.living: "available", self.gross: "not_loaded"})
        self.assertEqual(result["missing_info"], [self.net])
        self.assertEqual(result["profile"], raw["profile"])
        self.assertNotIn("verified_read", result)
        for statuses in ({self.net: "available", self.living: "available", self.gross: "not_loaded"},
                         {self.net: "missing", self.gross: "not_loaded"}):
            with self.subTest(statuses=statuses), self.assertRaises(SupervisorContractError):
                normalize_profile_result({**raw, "field_status": statuses}, current_profile=base, required_fields=[self.net, self.living, self.gross])

    def test_new_prepare_request_schema_and_scope(self):
        from ..llm.adapter import DecisionSchema
        from ..tools.ports import validate_profile_request
        decision = self._prepare("세후 월급 300만 원으로 저장해 줘", temporary=False, change=True, required=[self.net])
        parsed = DecisionSchema.model_validate(decision).model_dump(exclude_none=True)
        self.assertEqual(parsed["tool_input"], decision["tool_input"])
        validate_profile_request(parsed["tool_input"])
        legacy = {"instruction": "소득 추출", "is_temporary": True}
        validate_profile_request(legacy)
        for bad in ({**legacy, "change_requested": True}, {**legacy, "change_requested": 1},
                    {**legacy, "required_fields": ["financial_profile.monthly_gross_income_krw"]},
                    {**legacy, "current_profile": {}}, {**legacy, "required_fields": []},
                    {**legacy, "operation": "inspect", "fields": [self.net], "required_fields": [self.net]},
                    {**legacy, "operation": "inspect", "fields": [self.net], "change_requested": False}):
            with self.subTest(bad=bad), self.assertRaises(SupervisorContractError):
                validate_profile_request(bad)

    def test_real_profile_prepare_flags_and_extraction(self):
        from ..tools.ports import normalize_profile_result
        ports = self._real_ports()
        base = {"financial_profile": {"monthly_net_income_krw": 2800000, "monthly_living_expenses_krw": 0}}
        for question, basis, target, amount in (("세후 월급 300만 원", "net", self.net, 3000000),
                                                ("세전 연봉 4,500만 원", "gross", self.gross, 45000000),
                                                ("세전 월급 350만 원", "gross", None, 3500000),
                                                ("월급 350만 원", "unknown", None, 3500000)):
            with self.subTest(question=question):
                raw = ports.profile_agent({"instruction": question, "is_temporary": True, "current_profile": base,
                                           "required_fields": [self.living], "change_requested": False})
                result = normalize_profile_result(raw, current_profile=base, required_fields=[self.living])
                self.assertEqual(result["field_status"], {self.living: "available"})
                self.assertEqual(result["extracted_info"][0]["tax_basis"], basis)
                self.assertEqual(result["extracted_info"][0]["target_field"], target)
                self.assertEqual(result["extracted_info"][0]["amount_krw"], amount)
                self.assertFalse(result["changes"])
                self.assertEqual(bool(result["ambiguities"]), target is None)
        # Adapter는 기존 텍스트 기반 저장 추론을 끈다.
        raw = ports.profile_agent({"instruction": "세후 월급 300만 원 저장해 줘", "is_temporary": False, "current_profile": base})
        self.assertFalse(raw["changes"])
        self.assertEqual(base["financial_profile"]["monthly_net_income_krw"], 2800000)

    def test_real_profile_metadata_reaches_reason_without_hydration(self):
        ports = self._real_ports()
        source = SuppliedValuesRegressionTests._source()
        before = deepcopy(source)
        contexts = []
        def decide(ctx):
            contexts.append(deepcopy(ctx))
            if len(contexts) == 1:
                return self._prepare("월급 350만 원이야. 매달 얼마 저축할 수 있어?", required=[self.net, self.living])
            return self._ask()
        out = Supervisor(decide=decide, ports=ports).run(source, use_graph=False)
        obs = contexts[1]["observations"][-1]["result"]
        self.assertEqual(obs["extracted_info"][0]["tax_basis"], "unknown")
        self.assertEqual(obs["ambiguities"][0]["type"], "tax_basis_unknown")
        self.assertEqual(obs["field_status"][self.net], "available")
        self.assertEqual(out.next_node, "ask_user")
        for key in ("extracted_info", "ambiguities", "financial_profile", "financial_profile_changes"):
            self.assertNotIn(key, out.update)
        self.assertEqual(source, before)

    def test_real_profile_explicit_change_only_produces_approval(self):
        ports = self._real_ports()
        source = SuppliedValuesRegressionTests._source()
        before = deepcopy(source)
        decisions = iter([self._prepare("세후 월급 300만 원으로 저장해 줘", temporary=False, change=True),
                          self._ask("세후 월급 300만 원으로 변경할까요?", ["confirm_income_change"])])
        out = Supervisor(decide=lambda _: next(decisions), ports=ports).run(source, use_graph=False)
        self.assertEqual(out.next_node, "ask_user")
        changes = out.update["financial_profile_changes"]
        self.assertEqual(len(changes), 1)
        self.assertEqual((changes[0]["field"], changes[0]["value"], changes[0]["status"]),
                         ("monthly_net_income_krw", 3000000, "needs_confirmation"))
        self.assertEqual(out.update["intent"]["profile_update_status"], "waiting")
        self.assertNotIn("financial_profile", out.update)
        self.assertEqual(source, before)
        # 미지원 소득을 명시적으로 저장하라고 해도 임의 연환산 후보는 나오지 않는다.
        raw = ports.profile_agent({"instruction": "세전 월급 350만 원 저장해 줘", "is_temporary": False, "change_requested": True})
        self.assertFalse(raw["changes"])
        self.assertEqual(raw["ambiguities"][0]["type"], "unsupported_income_field")

    def test_non_temporary_without_change_flag_cannot_propose_changes(self):
        source = SuppliedValuesRegressionTests._source()
        decision = self._prepare("소득 추출", temporary=False, change=False)
        trace = []
        decisions = iter([decision, SuppliedValuesRegressionTests._plan()])
        calls = []
        def service(request):
            calls.append(deepcopy(request))
            return {"profile": {}, "missing_info": [], "changes": [{"change_id": "bad", "domain": "financial",
                                                                      "field": "monthly_net_income_krw", "value": 3000000}]}
        out = Supervisor(decide=lambda _: next(decisions), ports=SupervisorPorts(profile_agent=service), trace_sink=trace.append).run(source, use_graph=False)
        self.assertIs(calls[0]["change_requested"], False)
        self.assertEqual(next(x for x in trace if x["node"] == "tools")["output"]["observation"]["result"]["status"], "failed")
        self.assertNotIn("financial_profile_changes", out.update)
        self.assertNotIn("financial_profile", out.update)

    def test_read_port_cannot_inject_extracted_facts(self):
        source = SuppliedValuesRegressionTests._source()
        decision = {"kind": "call_tool", "reason": "기존 연소득 조회", "tool_name": "profile_agent", "tool_input": {
            "operation": "inspect", "instruction": "연소득 조회", "is_temporary": True, "fields": [self.gross]}}
        decisions = iter([decision, SuppliedValuesRegressionTests._plan()])
        trace = []
        raw = self._result([self._fact()], profile={"financial_profile": {"annual_gross_income_krw": 42000000}})
        out = Supervisor(decide=lambda _: next(decisions), ports=SupervisorPorts(get_profile_fields=lambda _: raw), trace_sink=trace.append).run(source, use_graph=False)
        self.assertEqual(next(x for x in trace if x["node"] == "tools")["output"]["observation"]["result"]["status"], "failed")
        self.assertNotIn("financial_profile", out.update)
