"""LangGraph 설치나 LLM 호출 없이 실행 엔진의 순수 상태 전이 규칙을 검사한다."""
from copy import deepcopy
import unittest

from agent.workflow_control import collect_results, is_current_result, new_report, normalize_current, select_wave


def task(tid, agent, deps=(), *, status="pending", run_id=None, scenario=None):
    return {"task_id": tid, "agent": agent, "objective": tid, "depends_on": list(deps),
            "scenario_id": scenario, "status": status, "run_id": run_id}


def plan(*tasks):
    return {"revision": 1, "review_retry_count": 0, "tasks": list(tasks)}


def result(t, *, revision=1, status="completed"):
    return {"task_id": t["task_id"], "run_id": t["run_id"], "plan_revision": revision,
            "scenario_id": t["scenario_id"], "status": status, "summary": "test contract fixture",
            "data": {}, "evidence_refs": [], "warnings": [], "needed_fields": []}


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


if __name__ == "__main__":
    unittest.main()
