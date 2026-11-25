"""개발용 실행점의 출력/스트림 경계 검사. 실제 모델 API나 가짜 운영 Agent를 만들지 않는다."""
import unittest
from agent.workflow_input import run_stream, show_update, result_view, environment_info


class WorkflowInputTests(unittest.TestCase):
    def test_events_and_root_final_state(self):
        # LangGraph 공식 v2 StreamPart 구조를 재현하는 테스트 전용 경계 객체.
        class StreamFixture:
            calls = 0
            def stream(self, state, **kwargs):
                self.calls += 1
                assert kwargs == {"stream_mode": ["updates", "values"], "subgraphs": True, "version": "v2"}
                yield {"ns": (), "type": "values", "data": state}
                yield {"ns": ("supervisor:123",), "type": "updates", "data": {"reason": {"decision": {"kind": "submit_plan"}}}}
                yield {"ns": ("supervisor:123",), "type": "values", "data": {"private": "inner_state"}}
                yield {"ns": (), "type": "updates", "data": {"renderer": {"workflow_report": {"stages": [{"stage": "renderer", "status": "skipped", "reason": "stage_not_implemented"}]}}}}
                yield {"ns": (), "type": "values", "data": {"phase": "failed", "workflow_report": {"status": "connection_check_completed"}}}
        graph = StreamFixture()
        lines = []
        result = run_stream(graph, {"messages": []}, emit=lines.append)
        self.assertEqual(graph.calls, 1)
        self.assertEqual(result["workflow_report"]["status"], "connection_check_completed")
        self.assertIn("[NODE] supervisor/reason", lines)
        self.assertIn("       decision: submit_plan", lines)
        self.assertIn("[NODE] renderer", lines)
        self.assertNotIn("inner_state", str(lines))

    def test_missing_final_report_rejected(self):
        class EmptyStream:
            def stream(self, *args, **kwargs):
                yield {"ns": (), "type": "values", "data": {}}
        with self.assertRaises(RuntimeError):
            run_stream(EmptyStream(), {}, emit=lambda _: None)

    def test_dispatcher_emits_actual_actions(self):
        lines = []
        show_update((), {"dispatcher": {"workflow_report": {"waves": [[{"task_id": "p", "action": "agent_not_registered"}]], "stages": []}}}, lines.append)
        self.assertIn("       p: agent_not_registered", lines)

    def test_result_excludes_entire_profile_and_message_history(self):
        output = result_view({"financial_profile": {"private": 123}, "messages": ["private"], "workflow_report": {"status": "waiting_for_user"}}, 0.125, langfuse_enabled=False)
        self.assertNotIn("financial_profile", output)
        self.assertNotIn("messages", output)
        self.assertEqual(output["status"], "waiting_for_user")
        self.assertFalse(output["langfuse_configured"])

    def test_environment_check(self):
        info = environment_info()
        self.assertTrue(info["python"])
        self.assertIn("langgraph", info["packages"])
        self.assertIn("langfuse", info["packages"])


if __name__ == "__main__":
    unittest.main()
