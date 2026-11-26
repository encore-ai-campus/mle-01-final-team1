# 골든셋 후보 시험 실행 보고서

실행 배치: `trial_v1`  
스냅샷: `v1`  
시험 배치는 본 목표 120개에 포함하지 않음: `included_in_final_target=false`

## 실행 결과

| 유형 | 요청 | 생성·Critic 통과 | 부족분 |
| --- | ---: | ---: | ---: |
| VECTOR | 5 | 5 | 0 |
| GRAPH | 5 | 2 | 3 |
| HYBRID | 5 | 2 | 3 |
| 합계 | 15 | 9 | 6 |

9개 모두 코드 기반 Critic을 통과했고, 중복 질문은 0건이다. Generator는 `template`, Critic은 `rules`를 사용했으므로 외부 LLM 비용은 0달러이며 LLM 호출도 0회다.

## 왜 15개를 채우지 않았는가

Neo4j 검증 보고서에는 4개 행이 있지만, 이 중 전체 청크 v1의 `chunks.jsonl`과 `chunk_id`가 연결되는 것은 금리옵션 2개뿐이다.

- 정책 혜택 `CHK_102EB2E5E4801A0D`: Neo4j 대조 샘플에는 있으나 전체 청크 v1에는 없음
- 정책 신청기간 `CHK_AA9AB9F730474EC6`: Neo4j 대조 샘플에는 있으나 전체 청크 v1에는 없음
- 금리옵션 2개: 전체 청크 v1과 연결되어 GRAPH·HYBRID 후보에 사용

따라서 연결되지 않은 정책 행을 억지로 사용하지 않고 GRAPH·HYBRID를 각각 2개에서 멈췄다. 이 매핑을 먼저 해결하면 정책 혜택 경로는 후보로 사용할 수 있지만, 신청기간 경로는 현재 1홉이라 HYBRID 조건에는 부족하다.

## 생성 파일

- `data/evaluation/candidates/trial_v1/candidates.jsonl`
- `data/evaluation/candidates/trial_v1/review.csv`
- `data/evaluation/candidates/trial_v1/candidate_report.json`
- 검수 후 생성될 `reviewed_candidates.jsonl`, `review_history.jsonl`

## 검증 결과

- 후보 스키마 필수 필드 수동 검사: 9/9 통과
- 코드 기반 근거·출처·질문·관계 검사: 9/9 통과
- 중복 질문: 0건
- 초기 상태: 모두 `pending`
- 자동으로 `golden_set_final.jsonl`을 만들거나 확정하지 않음

## 다음에 필요한 자료

1. Neo4j 대조 샘플의 정책 혜택·신청기간을 전체 v1 `chunks.jsonl`의 실제 `chunk_id`와 연결하는 매핑표 또는 재생성된 매핑 결과
2. GRAPH·HYBRID 후보를 20개씩 만들 수 있을 정도의 추가 검증된 Neo4j 경로
3. 실제 Generator·Critic LLM 호출 함수, 모델명, 토큰 비용, 반환 JSON 예시
4. Retriever의 검색 결과 JSON과 최종 답변 JSON 계약
