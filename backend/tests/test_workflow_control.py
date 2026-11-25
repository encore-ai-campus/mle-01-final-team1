"""LangGraph 설치나 LLM 호출 없이 실행 엔진의 순수 상태 전이 규칙을 검사한다."""
from copy import deepcopy
import unittest

from agent.workflow_control import (collect_results, is_current_result, new_report, normalize_current,
                                    select_wave, required_upstream, resolved_task_answers,
                                    archive_review_feedback, revision_feedback, work_replan_tasks,
                                    review_replan_tasks)
from agent.supervisor.contracts import SupervisorContractError


def task(tid, agent, deps=(), *, status="pending", run_id=None, scenario=None):
    result = {"task_id": tid, "agent": agent, "objective": tid, "depends_on": list(deps),
              "scenario_id": scenario, "status": status, "run_id": run_id}
    if agent in {"product", "policy"}:
        result["candidate_limit"] = 3
    return result


def plan(*tasks):
    return {"revision": 1, "review_retry_count": 0, "tasks": list(tasks)}


def result(t, *, revision=1, status="completed"):
    return {"task_id": t["task_id"], "run_id": t["run_id"], "plan_revision": revision,
            "scenario_id": t["scenario_id"], "status": status, "summary": "test contract fixture",
            "data": {"candidates": []} if t["agent"] in {"product", "policy"} and status == "completed" else {},
            "evidence_refs": [], "warnings": [], "needed_fields": []}


class WorkflowControlTests(unittest.TestCase):
    def test_empty_registry_only_independent_tasks_first(self):
        p = plan(task("p", "product"), task("f", "fund", ["p"]), task("y", "policy"))
        self.assertEqual(select_wave(p, set()), [("p", "agent_not_registered"), ("y", "agent_not_registered")])

    def test_skipped_dependency_does_not_enable_fund(self):
        p = plan(task("p", "product", status="skipped"), task("f", "fund", ["p"]), task("y", "policy"))
        self.assertEqual(select_wave(p, {"fund", "policy"}), [("f", "dependency_unavailable"), ("y", "execute")])

    def test_fund_can_execute_alone(self):
        self.assertEqual(select_wave(plan(task("f", "fund")), {"fund"}), [("f", "execute")])

    def test_needs_input_blocks_only_dependent_tasks(self):
        p = plan(task("p", "product", status="needs_input"), task("f", "fund", ["p"]), task("y", "policy"))
        self.assertEqual(select_wave(p, {"fund", "policy"}), [("y", "execute")])

    def test_successful_dependency_enables_next_wave(self):
        p = plan(task("p", "product", status="completed"), task("f", "fund", ["p"]))
        self.assertEqual(select_wave(p, {"fund"}), [("f", "execute")])

    def test_old_revision_invalidates_dependents_and_reviews(self):
        p1 = task("p", "product", status="completed", run_id="r1")
        p2 = task("f", "fund", ["p"], status="completed", run_id="r2")
        p = plan(p1, p2)
        old = {"p": result(p1, revision=0), "f": result(p2)}
        actual, accepted, reviews = normalize_current(p, old, {"f": {"target_run_id": "r2"}})
        self.assertEqual([t["status"] for t in actual["tasks"]], ["pending", "pending"])
        self.assertEqual(accepted, {})
        self.assertEqual(reviews, {})
        self.assertEqual(p1["status"], "completed")

    def test_parallel_collection_only_current_envelope(self):
        a = task("a", "fund", status="running", run_id="r1", scenario="s_a")
        b = task("b", "fund", status="running", run_id="r2", scenario="s_b")
        p = plan(a, b)
        original = deepcopy(p)
        wrong = {**result(b), "scenario_id": "s_a"}
        actual, accepted, report = collect_results(p, {}, [result(a), wrong], new_report())
        self.assertEqual([t["status"] for t in actual["tasks"]], ["completed", "failed"])
        self.assertEqual(set(accepted), {"a"})
        self.assertEqual(report["tasks"]["b"]["reason"], "missing_or_duplicate_current_result")
        self.assertEqual(p, original)

    def test_duplicate_result_rejected(self):
        t = task("p", "product", status="running", run_id="r")
        actual, accepted, _ = collect_results(plan(t), {}, [result(t), result(t)], new_report())
        self.assertEqual(actual["tasks"][0]["status"], "failed")
        self.assertEqual(accepted, {})

    def test_no_run_id_no_success(self):
        t = task("p", "product", status="skipped")
        self.assertFalse(is_current_result(result(t), t, 1))


    def test_candidate_overflow_not_accepted_or_dispatched(self):
        p = task("p", "product", status="running", run_id="r")
        f = task("f", "fund", ["p"])
        output = result(p)
        output["data"]["candidates"] = [{"candidate_id": str(i)} for i in range(4)]
        actual, accepted, report = collect_results(plan(p, f), {}, [output], new_report())
        self.assertEqual(actual["tasks"][0]["status"], "failed")
        self.assertEqual(accepted["p"]["data"], {})
        self.assertIn("CANDIDATE_CONTRACT_VIOLATION", accepted["p"]["warnings"])
        self.assertEqual(report["tasks"]["p"]["reason"], "candidate_contract_violation")
        self.assertEqual(select_wave(actual, {"fund"}), [("f", "dependency_unavailable")])

    def test_missing_candidate_array_rejected(self):
        p = task("p", "policy", status="running", run_id="r")
        output = result(p)
        output["data"] = {}
        actual, accepted, report = collect_results(plan(p), {}, [output], new_report())
        self.assertEqual(actual["tasks"][0]["status"], "failed")
        self.assertEqual(report["tasks"]["p"]["reason"], "candidate_contract_violation")
        self.assertEqual(accepted["p"]["data"], {})

    def test_empty_candidate_array_is_success(self):
        p = task("p", "product", status="running", run_id="r")
        actual, accepted, _ = collect_results(plan(p), {}, [result(p)], new_report())
        self.assertEqual(actual["tasks"][0]["status"], "completed")
        self.assertEqual(accepted["p"]["data"], {"candidates": []})

    def test_cached_overflow_invalidates_dependents(self):
        p = task("p", "product", status="completed", run_id="r1")
        f = task("f", "fund", ["p"], status="completed", run_id="r2")
        output = result(p)
        output["data"]["candidates"] = [{}] * 4
        actual, accepted, reviews = normalize_current(plan(p, f), {"p": output, "f": result(f)},
                                                        {"p": {"target_run_id": "r1"}})
        self.assertEqual([t["status"] for t in actual["tasks"]], ["pending", "pending"])
        self.assertEqual(accepted, {})
        self.assertEqual(reviews, {})

    def test_required_upstream_exact_direct_dependencies(self):
        p = task("p", "product", status="completed", run_id="r1")
        f = task("f", "fund", ["p"], scenario="s_a")
        other = task("other", "fund", status="completed", run_id="r2", scenario="s_b")
        outputs = {"p": result(p), "other": result(other)}
        actual = required_upstream(plan(p, f, other), outputs, f)
        self.assertEqual(set(actual), {"p"})
        actual["p"]["data"]["candidates"].append({})
        self.assertEqual(outputs["p"]["data"]["candidates"], [])
        with self.assertRaises(SupervisorContractError):
            required_upstream(plan(p, f), {}, f)
        with self.assertRaises(SupervisorContractError):
            required_upstream(plan(p, f), {"p": result(p, revision=0)}, f)
        f["depends_on"] = ["other"]
        with self.assertRaises(SupervisorContractError):
            required_upstream(plan(p, f, other), outputs, f)

    def test_resolved_answers_are_task_scoped(self):
        f = task("f", "fund", scenario="s_a")
        state = {"pending_input": [
            {"id": "q1", "target": "f", "scenario_id": "s_a", "status": "resolved", "answers": {"x": 0}},
            {"id": "q2", "target": "f", "scenario_id": "s_b", "status": "resolved", "answers": {"x": 5}},
            {"id": "q3", "target": "f", "scenario_id": "s_a", "status": "pending", "answers": {"x": 6}},
        ]}
        self.assertEqual(resolved_task_answers(state, f), {"q1": {"x": 0}})

    def test_replan_only_for_real_failure_or_need(self):
        p = task("p", "product", status="skipped")
        f = task("f", "fund", status="failed", run_id="r1")
        y = task("y", "policy", status="needs_input", run_id="r2")
        self.assertEqual(work_replan_tasks({"execution_plan": plan(p, f, y)}), ["f", "y"])
        self.assertEqual(work_replan_tasks({"execution_plan": plan(p)}), [])

    def test_review_replan_and_archive_current_run_only(self):
        f = task("f", "fund", status="completed", run_id="r1", scenario="s_a")
        old = {"f": result(f)}
        reviews = {"f": {"target_run_id": "r1", "reflection": {"status": "revise", "issues": ["check term"]}}}
        state = {"execution_plan": plan(f), "agent_results": old, "review_results": reviews}
        self.assertEqual(review_replan_tasks(state), ["f"])
        archive = archive_review_feedback(old, reviews, {}, {})
        self.assertEqual(archive["r1"]["issues"], ["check term"])
        self.assertNotIn("data", archive["r1"])
        self.assertNotIn("previous_output_ref", archive["r1"])
        new_task = task("f", "fund", scenario="s_a")
        next_state = {"execution_plan": {**plan(new_task), "revision": 2}, "superseded_results": archive}
        self.assertEqual(revision_feedback(next_state, new_task), {"previous_run_id": "r1", "issues": ["check term"]})
        self.assertIsNone(revision_feedback(next_state, task("f", "fund", scenario="s_b")))
        reviews["f"]["target_run_id"] = "old"
        self.assertEqual(review_replan_tasks(state), [])
        self.assertEqual(archive_review_feedback(old, reviews, {}, {}), {})


if __name__ == "__main__":
    unittest.main()
