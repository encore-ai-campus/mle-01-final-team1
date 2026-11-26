from services.golden_candidate_pipeline import RuleBasedCritic
from services.llm_candidate_adapter import (
    LLMGenerator,
    LLMCritic,
    LlmSettings,
    RetryingJsonClient,
    UsageLedger,
)

from test_golden_candidate_pipeline import chunk, graph_row


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def complete_json(self, *, system_prompt, user_prompt, model):
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_retry_records_attempts_and_cost():
    ledger = UsageLedger()
    client = FakeClient([RuntimeError("temporary"), {"data": {"ok": True}, "usage": {"total_tokens": 100}}])
    retrying = RetryingJsonClient(
        client,
        settings=LlmSettings(max_attempts=2, retry_base_seconds=0, input_cost_per_1k=1.0, output_cost_per_1k=2.0),
        ledger=ledger,
        sleeper=lambda _: None,
    )

    result = retrying.complete_json(system_prompt="s", user_prompt="u", model="test")

    assert result["data"] == {"ok": True}
    assert client.calls == 2
    assert ledger.retry_count == 1
    assert ledger.call_count == 2
    assert ledger.total_tokens == 100
    assert ledger.total_cost_usd == 0.2


def test_llm_generator_keeps_ids_and_ground_truth_facts_from_code():
    client = FakeClient([
        {
            "data": {
                "question": "12개월 금리가 얼마인지, 어느 공시 기준인지 알려줘.",
                "reference": "기본금리 3.6%이며 202610 공시 기준입니다.",
            },
            "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
        }
    ])
    generator = LLMGenerator(
        RetryingJsonClient(client, settings=LlmSettings(max_attempts=1), ledger=UsageLedger())
    )

    candidate = generator.generate("HYBRID", chunk("CHK_1"), graph_row(), "llm-1", "v1")

    assert candidate["question"].startswith("12개월 금리")
    assert candidate["reference_context_ids"] == ["CHK_1"]
    assert candidate["source_evidence_ids"] == ["EVD_CHK_1"]
    assert candidate["expected_facts"][0]["value"] == 3.6
    assert candidate["generator"]["provider"] == "llm"


def test_llm_critic_runs_only_after_local_grounding_passes():
    client = FakeClient([
        {
            "data": {"verdict": "pass", "reasons": []},
            "usage": {"total_tokens": 12},
        }
    ])
    critic = LLMCritic(
        RetryingJsonClient(client, settings=LlmSettings(max_attempts=1), ledger=UsageLedger())
    )
    candidate = LLMGenerator(
        RetryingJsonClient(
            FakeClient([{"data": {"question": "질문", "reference": "3.6%"}}]),
            settings=LlmSettings(max_attempts=1),
            ledger=UsageLedger(),
        )
    ).generate("VECTOR", chunk("CHK_1"), None, "llm-2", "v1")

    result = critic.critique(candidate, {"CHK_1": chunk("CHK_1")}, {})

    assert result["verdict"] == "pass"
    assert result["provider"] == "rules+llm"
    assert client.calls == 1
