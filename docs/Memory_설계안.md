# Memory 시스템 설계안 v1.0

**작성일:** 2026-11-24  
**상태:** 1차 설계 초안  
**연동 대상:** Supervisor / Memory Retrieval / Memory Writer

## 1. 목적

Memory 시스템은 사용자의 과거 금융 상담, 추천 이력, 사용자 결정과 선호사항을 저장하고 이후 상담에서 관련 정보를 검색해 제공한다.

Memory는 과거 상담의 맥락을 제공하는 기능이며, 현재 금융정보를 관리하는 Profile DB나 실제 금융상품 정보를 제공하는 Product DB를 대체하지 않는다.

## 2. 책임 범위

| 구성 요소 | 책임 |
|---|---|
| Supervisor | Memory 호출 여부 판단, 검색 결과 활용 |
| Memory Retrieval | 과거 상담 및 결정 검색 |
| Memory Writer | 검증된 상담 결과와 사용자 결정 저장 |
| Memory Store | 사용자별 기억 저장 및 조회 |
| Profile / 계약 서비스 | 현재 금융정보와 실제 가입 사실 확인 |

Memory Retrieval과 Writer는 Supervisor의 판단을 대신하지 않는다.

## 3. Memory 저장 대상

### 3.1 기억 유형

| memory_type | 내용 |
|---|---|
| `consultation_summary` | 과거 상담의 핵심 요약 |
| `recommendation` | 이전에 추천한 상품·정책과 참조 |
| `user_decision` | 추천 수락·거절·변경 및 이유 |
| `user_preference` | 사용자가 명시적으로 밝힌 상담 선호 |

### 3.2 저장 제한

- 모든 대화를 자동으로 장기기억에 저장하지 않는다.
- 사용자에게 실제 전달되지 않은 답변은 저장하지 않는다.
- 현재 월급·자산·부채의 확정값은 Profile DB를 기준으로 한다.
- 실제 상품 가입·해지 여부는 계약 서비스에서 확인한다.
- 계좌번호, 비밀번호, 인증 토큰 등 불필요한 민감정보는 저장하지 않는다.
- 금융상품의 과거 금리와 자격을 현재 조건으로 재사용하지 않는다.

## 4. 저장소 설계

### 4.1 기술 선택

**1차 제안:** PostgreSQL 일반 테이블  
**확장안:** PostgreSQL + pgvector

Memory 기능을 위해 별도의 데이터베이스 제품을 도입하지 않고 기존 PostgreSQL 환경을 재사용하는 방향을 우선 검토한다.

pgvector는 의미 기반 유사도 검색이 필요할 때 적용한다. PostgreSQL 및 pgvector의 실제 설치·운영 구성은 구현 시 확인한다.

### 4.2 memories 테이블

| 필드 | 타입 | 설명 |
|---|---|---|
| `memory_id` | UUID | 기억 식별자 |
| `user_id` | 기존 인증 ID와 동일 타입 | 기억 소유자 |
| `memory_type` | VARCHAR | 기억 유형 |
| `content` | TEXT | 기억 내용 |
| `source_ref` | VARCHAR | 원본 상담·답변 참조 |
| `target_ref` | VARCHAR, nullable | 관련 상품·정책·목표 참조 |
| `metadata` | JSONB | 결정 이유 등 부가정보 |
| `created_at` | TIMESTAMPTZ | 생성 시각 |
| `updated_at` | TIMESTAMPTZ | 수정 시각 |

추가로 실제 운영에서는 삭제 상태, 중복 저장 방지 키, 접근 권한 정책과 관련 인덱스를 설계한다.

## 5. Memory Retrieval

### 5.1 입력 계약 — 제안

```python
class MemorySearchRequest(TypedDict):
    query: str
    limit: NotRequired[int]
    memory_types: NotRequired[list[str]]
```

사용자 식별자는 요청 객체에서 임의로 받지 않고 서버 인증 컨텍스트에서 확보한다.

### 5.2 출력 계약 — 제안

```python
class MemorySearchResult(TypedDict):
    retrieval_status: Literal[
        "found", "no_match", "failed", "access_denied"
    ]
    retrieved: list[dict[str, Any]]
```

### 5.3 검색 규칙

1. Supervisor가 과거 상담이 필요한지 판단한다.
2. 인증된 사용자 범위에서만 검색한다.
3. 현재 요청과 관련된 기억만 반환한다.
4. 관련 기록이 없으면 `no_match`로 반환한다.
5. 조회 오류와 접근 거부를 구분한다.
6. 검색된 과거 금융상품 정보는 필요한 경우 전문 Agent가 최신 정보를 다시 확인한다.

## 6. Memory Writer

### 6.1 입력 계약 — 제안

```python
class MemoryWriteRequest(TypedDict):
    memory_type: Literal[
        "consultation_summary",
        "recommendation",
        "user_decision",
        "user_preference",
    ]
    content: str
    source_ref: str
    target_ref: NotRequired[str | None]
    metadata: NotRequired[dict[str, Any]]
```

### 6.2 출력 계약 — 제안

```python
class MemoryWriteResult(TypedDict):
    status: Literal[
        "saved", "duplicate", "failed", "access_denied"
    ]
    memory_id: NotRequired[str]
```

### 6.3 저장 규칙

- Final Advice와 Output Guard를 통과하고 실제 사용자에게 전달된 답변을 기준으로 저장한다.
- 사용자 결정은 실제로 확인된 선택만 저장한다.
- 같은 요청 재실행으로 기억이 중복 생성되지 않도록 멱등성을 보장한다.
- `source_ref` 등 참조의 유효성과 사용자 소유권을 검사한다.
- Memory 저장 실패를 상담 답변 성공이나 기억 저장 성공으로 표시하지 않는다.
- 사용자가 정정·삭제한 기억이 이후 검색에서 잘못 사용되지 않도록 관리한다.

## 7. Supervisor 연동

기존 MainState의 `memory` 필드를 재사용한다.

```json
{
  "memory": {
    "last_advice_ref": "advice_001",
    "retrieval_status": "found",
    "retrieved": []
  }
}
```

### 7.1 검색 흐름

사용자 요청 → Supervisor의 호출 판단 → Memory Retrieval → Supervisor의 `memory` State 갱신 → 필요한 전문 Agent 실행

### 7.2 저장 흐름

전문 Agent 실행 → Evidence / Reflection → Final Advice → Output Guard → Renderer → 사용자에게 실제 전달 → Memory Writer

Memory Writer는 실제 전달이 확인된 답변과 사용자 결정을 저장한다.

## 8. 기억의 갱신·삭제

- 기억의 변경 시각과 원본 참조를 보존한다.
- 사용자가 선호나 결정을 바꾸면 이전 정보가 현재 선호인 것처럼 활용되지 않도록 한다.
- 삭제 요청은 인증 사용자 소유권을 확인한 뒤 처리한다.
- 삭제된 기억은 일반 검색 결과에서 제외한다.
- 저장 데이터뿐 아니라 임베딩, 캐시 등 관련 사본에 대한 삭제 처리도 고려한다.

## 9. 평가 및 테스트

| ID | 테스트 | 기대 결과 |
|---|---|---|
| MEM-01 | 정상적인 상담 기록 저장 | `saved` |
| MEM-02 | 동일 요청 중복 저장 | `duplicate` |
| MEM-03 | 관련 상담 검색 | `found` |
| MEM-04 | 관련 기억 없음 | `no_match` |
| MEM-05 | 다른 사용자 기억 접근 | 타인 정보 반환 금지 |
| MEM-06 | 이전 결정 정정 | 오래된 결정의 잘못된 사용 방지 |
| MEM-07 | 사용자 기억 삭제 | 이후 검색에서 제외 |
| MEM-08 | DB 조회 실패 | `failed` |

### 핵심 평가 지표

- Retrieval 성공률
- 관련 기억 검색 정확도
- 사용자 간 정보 누출 건수
- 중복 저장 건수
- 기억 정정·삭제 반영률
- 불필요한 Memory 호출률

## 10. MVP 개발 범위

### 1차

- Memory Writer 기본 인터페이스
- Memory Retrieval 기본 인터페이스
- 사용자별 기억 저장·조회
- 중복 저장 방지
- Supervisor `memory` State에 연결 가능한 반환 구조
- 기본 pytest 테스트

### 2차

- pgvector 의미 기반 검색
- 복합 검색·재정렬
- 기억 자동 병합
- 장기기억 품질 평가 및 최적화

## 11. 완료 및 미완료 상태

**완료된 설계**

- Memory 목적 및 책임 범위
- 기억 유형
- 저장소 기본 구조
- Writer/Retrieval 입출력 초안
- Supervisor 연동 흐름
- 1차 평가 시나리오

**미완료**

- 실제 PostgreSQL 테이블 생성
- Memory Writer/Retrieval 코드
- Supervisor 실제 인터페이스 연동
- pytest 작성 및 실행
- 기억 삭제 및 갱신 세부 구현
- pgvector 적용 여부 최종 결정

## 12. 관련 문서

- `멀티에이전트_설계안.md`
- `State_설계안.md`
- `Profile_State_설계안.md`
- `Profile_Agent_설계안.md`

이 문서는 Memory 시스템의 책임과 구현 방향을 정의하며, Supervisor의 기존 MainState 구조를 변경하지 않는다.