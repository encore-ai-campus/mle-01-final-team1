"""실제 금융 chunk와 검증된 Neo4j 경로로 골든셋 후보를 생성한다."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from services.golden_candidate_pipeline import (  # noqa: E402
    RuleBasedCritic,
    TemplateGenerator,
    generate_candidates,
    write_candidate_outputs,
)
from services.llm_candidate_adapter import (  # noqa: E402
    LLMGenerator,
    LLMCritic,
    LlmSettings,
    OpenAIJsonClient,
    RetryingJsonClient,
    UsageLedger,
)


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest-dir", type=Path, required=True)
    parser.add_argument("--neo4j-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--snapshot-version", default="v1")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--batch-id", default="candidate-sample-v1")
    parser.add_argument("--batch-kind", choices=["trial", "production"], default="trial")
    parser.add_argument(
        "--include-in-final-target",
        action="store_true",
        help="시험 배치를 120개 목표 집계에 포함할지 명시합니다. 기본값은 제외입니다.",
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--vector", type=int, default=2)
    parser.add_argument("--graph", type=int, default=2)
    parser.add_argument("--hybrid", type=int, default=2)
    parser.add_argument(
        "--generator-provider",
        choices=("template", "openai"),
        default="template",
        help="기본은 로컬 TemplateGenerator이며 openai 선택 시 환경변수 기반 LLM을 사용합니다.",
    )
    parser.add_argument(
        "--critic-provider",
        choices=("rules", "openai"),
        default="rules",
        help="기본은 로컬 RuleBasedCritic이며 openai 선택 시 코드 검사 후 LLM 검사를 추가합니다.",
    )
    parser.add_argument("--llm-model", default=None)
    args = parser.parse_args()
    if min(args.vector, args.graph, args.hybrid) < 0:
        parser.error("후보 수는 0 이상이어야 합니다.")

    chunks = _read_jsonl(args.manifest_dir / "chunks.jsonl")
    graph_report = json.loads(args.neo4j_report.read_text(encoding="utf-8"))
    run_id = args.run_id or args.batch_id
    generator = TemplateGenerator()
    critic = RuleBasedCritic()
    ledger = None
    if args.generator_provider == "openai" or args.critic_provider == "openai":
        settings = LlmSettings.from_env()
        if args.llm_model:
            settings.model = args.llm_model
        ledger = UsageLedger()
        client = OpenAIJsonClient(settings)
        retrying_client = RetryingJsonClient(client, settings=settings, ledger=ledger)
        if args.generator_provider == "openai":
            generator = LLMGenerator(retrying_client)
        if args.critic_provider == "openai":
            critic = LLMCritic(retrying_client)
    candidates, report = generate_candidates(
        chunks,
        graph_report.get("rows", []),
        counts={"VECTOR": args.vector, "GRAPH": args.graph, "HYBRID": args.hybrid},
        snapshot_version=args.snapshot_version,
        run_id=run_id,
        generator=generator,
        critic=critic,
    )
    report.update(
        {
            "batch_id": args.batch_id,
            "batch_kind": args.batch_kind,
            "included_in_final_target": args.include_in_final_target,
            "target_plan": {"VECTOR": 40, "GRAPH": 40, "HYBRID": 40},
            "providers": {
                "generator": args.generator_provider,
                "critic": args.critic_provider,
            },
            "cost": ledger.as_dict() if ledger else {
                "llm_calls": 0,
                "retry_count": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "estimated_cost_usd": 0.0,
            },
        }
    )
    write_candidate_outputs(args.output_dir, candidates, report, overwrite=args.overwrite)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
