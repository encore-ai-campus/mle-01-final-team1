# Generator·Critic 입출력 계약

## Generator 입력

Generator는 후보 유형, 하나 이상의 검증된 청크, 그리고 검증된 그래프 경로만 입력으로 받는다.

```json
{
  "route": "VECTOR | GRAPH | HYBRID",
  "contexts": ["chunks.jsonl의 실제 레코드"],
  "graph_path": [
    {"from_id": "...", "relation_type": "...", "to_id": "..."}
  ],
  "source_snapshot_version": "v1",
  "candidate_id": "trial-v1_VECTOR_0001"
}
```

Generator가 새로 작성하는 값은 질문 문장, 기준 정답 문장, 기대 사실, 난이도, 홉 수다. `chunk_id`, `EVD_*`, Neo4j Evidence ID, 엔터티 ID, 관계 ID는 입력 근거에서 복사하며 LLM이 새로 만들지 않는다.

## 후보 출력

후보 1건은 `schema/golden_candidate.schema.json`을 따른다. 특히 다음을 분리한다.

- `reference_context_ids`: 검색 평가에서 정답 근거로 사용할 내부 Chunk ID
- `source_evidence_ids`: Work 내부 `EVD_*` ID
- `neo4j_evidence_ids`: Neo4j Evidence ID
- `expected_relation_ids`: 관계 고유 ID가 없으므로 `(from_id, relation_type, to_id)` 객체 배열
- `expected_route`, `difficulty`, `hop_count`: 판정 기준 문서에 따른 값

## Critic 출력

Critic은 다음 형식으로 반환한다.

```json
{
  "provider": "rules | llm",
  "verdict": "pass | fail",
  "checks": {
    "context_exists": true,
    "source_is_traceable": true,
    "facts_in_context": true,
    "reference_contains_expected_facts": true,
    "question_and_reference_present": true,
    "route_shape": true,
    "graph_path_verified": true,
    "relation_identity_is_explicit": true
  },
  "reasons": []
}
```

현재 시험 구현은 외부 LLM 호출 없이 `TemplateGenerator`와 `RuleBasedCritic`으로 실행한다. 실제 Generator·Critic LLM을 연결할 때도 이 출력 계약과 코드 기반 검사를 통과해야 하며, Critic만으로 최종 승인하지 않는다.

## 실제 LLM 연결 설정

실행 파일에서 `--generator-provider openai` 또는 `--critic-provider openai`를 명시할 때만 LLM을 호출한다. 다음 환경변수를 사용한다.

```text
FINANCE_LLM_API_KEY=비밀키
FINANCE_LLM_MODEL=gpt-4o-mini
FINANCE_LLM_MAX_ATTEMPTS=3
FINANCE_LLM_RETRY_BASE_SECONDS=1
FINANCE_LLM_INPUT_COST_PER_1K=0
FINANCE_LLM_OUTPUT_COST_PER_1K=0
```

API 키는 후보 파일·리포트에 저장하지 않는다. 호출 실패는 지수 대기 후 재시도하고, 배치 리포트에는 호출 횟수·재시도 횟수·토큰 수·예상 비용을 기록한다. 실제 호출을 사용하려면 `openai` 패키지를 설치해야 하며, 현재 시험 배치에는 외부 호출이 없었다.
