# 골든셋 후보 시험 v2 보고서

`trial_v1`의 ID 매핑 문제와 내부 경로 중심 질문을 수정한 뒤, 기존 배치를 덮어쓰지 않고 `trial_v5`로 다시 실행했다.

| 유형 | 요청 | 생성 | Critic 통과 |
| --- | ---: | ---: | ---: |
| VECTOR | 5 | 5 | 5 |
| GRAPH | 5 | 3 | 3 |
| HYBRID | 5 | 3 | 3 |
| 합계 | 15 | **11** | **11** |

GRAPH·HYBRID 3개는 두 개의 금리옵션 경로와 새로 전체 v1에 매핑한 정책 혜택 경로다. 정책 신청기간 경로는 1홉이라 HYBRID에 사용하지 않았다.

## 질문 품질 변화

변경 전 GRAPH:

> WON플러스예금의 12개월 금리옵션은 어떤 공시·금리옵션 경로로 연결되어 있나요?

변경 후 GRAPH:

> WON플러스예금의 12개월 금리 정보가 어떤 공시 기준으로 등록되어 있는지 확인해줘.

변경 전 HYBRID:

> WON플러스예금의 12개월 기본금리와 이를 뒷받침하는 원본 근거 경로를 알려주세요.

변경 후 HYBRID:

> WON플러스예금 12개월 금리가 얼마인지, 원본 근거가 어느 공시에서 나온 것인지 함께 알려줘.

내부 ID와 관계 타입은 질문에 넣지 않고, 기준 정답·근거 필드에 보존한다.

## 비용과 상태

- Generator: `template`
- Critic: `rules`
- LLM 호출: 0회
- 예상 비용: 0달러
- 시험 배치 포함 여부: `false`
- `golden_set_final.jsonl`: 미생성

## 출력

- `data/evaluation/candidates/trial_v5/candidates.jsonl`
- `data/evaluation/candidates/trial_v5/review.csv`
- `data/evaluation/candidates/trial_v5/candidate_report.json`
