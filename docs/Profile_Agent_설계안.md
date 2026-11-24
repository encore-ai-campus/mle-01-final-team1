# Profile Agent 설계안

**작성 기준:** 2026-11-24  
**상태:** 설계 초안 — 구현 및 테스트 미완료  
**연동 대상:** Supervisor v6

## 1. 목적

Profile Agent는 사용자 발화와 기존 프로필을 바탕으로 필요한 금융·기본 정보를 확인하고, 누락 정보 및 프로필 변경 후보를 추출한다.

Profile Agent는 실제 프로필 변경을 직접 확정하지 않는다. 처리 결과를 `ProfileResult`로 반환하고, Supervisor가 저장 의도·승인·MainState 반영을 관리한다.

## 2. 책임 범위

### Profile Agent 담당

- 사용자 발화의 정보·의도 추출
- 기본·금융 프로필 정보 확인
- 필요한 누락 정보 식별
- 실제 저장 변경 후보 생성
- 실제 정보와 가상 시뮬레이션 정보 구분
- 필요 시 계약 스냅샷 참조 반환

### Supervisor 담당

- Profile Agent 호출 여부 결정
- 변경 후보의 실제 저장 필요성 판단
- `basic_profile_changes` 및 `financial_profile_changes` 구성
- `pending_input` 구성
- 가상 시나리오 최종 확정
- 승인된 저장 절차 조율

## 3. 입출력 계약

### 3.1 입력

기존 논의의 임시 인터페이스:

```python
class ProfileRequest(TypedDict):
    instruction: str
    is_temporary: bool
```

실제 Supervisor v6 연동 시 입력 구조를 확인한다.

### 3.2 출력

팀 협의로 확정된 `ProfileResult` 계약:

```python
from typing import Any, NotRequired, TypedDict

class ProfileResult(TypedDict):
    profile: dict[str, Any]
    missing_info: list[str]
    changes: NotRequired[list[dict[str, Any]]]
    baseline_ref: NotRequired[str]
    field_status: NotRequired[dict[str, str]]
```

- `profile`: 조회 또는 처리한 프로필 결과
- `missing_info`: 현재 요청에서 확인되지 않은 필드 목록
- `changes`: 실제 저장 변경 후보
- `baseline_ref`: 필요한 경우 실제 계약 스냅샷 참조
- `field_status`: 선택적 필드별 상태 정보

`changes`는 `list[dict]` 형식이다.

## 4. 내부 처리 설계

### 4.1 Resolver

사용자 발화를 다음 세 종류로 구분한다.

| 유형 | 예시 | 처리 |
|---|---|---|
| 일회성 상담 정보 | 세후 월급 300만 원으로 계산해 줘 | 이번 상담에만 활용 |
| 실제 저장 요청 | 월급을 350만 원으로 저장해 줘 | 변경 후보 반환 |
| 가상 시뮬레이션 | 내년 월급이 350만 원이라면? | 임시 프로필로만 처리 |

분류값과 내부 구현 방식은 개발 시 구체화한다.

### 4.2 Missing Info Checker

현재 요청에 필요한 정보 중 확인되지 않은 필드를 식별한다.

원칙:

1. 이미 확인한 정보를 불필요하게 다시 요청하지 않는다.
2. `null`을 `0`으로 간주하지 않는다.
3. 일반적인 정보 안내에는 불필요한 개인정보를 요구하지 않는다.
4. 누락 정보를 실제로 질문할지는 Supervisor가 결정한다.

### 4.3 Change Builder

실제 저장 변경 후보를 다음 구조로 반환한다.

```json
{
  "profile": {},
  "missing_info": [],
  "changes": [
    {
      "change_id": "change_001",
      "domain": "financial",
      "field": "monthly_net_income_krw",
      "value": 3500000
    }
  ]
}
```

`domain`은 `basic` 또는 `financial`이다.

Supervisor는 `domain`에 따라 변경 후보를 `basic_profile_changes` 또는 `financial_profile_changes`로 분류한다.

변경 후보 반환은 DB 저장 성공을 의미하지 않는다.

## 5. 데이터 안전 규칙

- 명시적인 저장 의사나 승인 없이 실제 DB를 변경하지 않는다.
- 가상 시뮬레이션 값은 실제 프로필 변경 후보와 구분한다.
- 기존 금융 목표는 추가 요청으로 인해 삭제되거나 덮어써지지 않아야 한다.
- 저장된 목표는 `goal_id`를 기준으로 식별한다.
- 저장 계약과 금융상품 원본의 참조를 혼동하지 않는다.
- 사용자별 프로필 조회는 인증된 사용자 범위로 제한한다.
- 실제 저장 단계에서는 `change_id`, 프로필 버전, 사용자 권한을 검증한다.

## 6. 단위 테스트 설계

| ID | 테스트 | 핵심 기대 결과 |
|---|---|---|
| TC-01 | 세후 월급 300만 원 추출 | `monthly_net_income_krw = 3000000` |
| TC-02 | 저축 가능 금액 문의 | 필요한 누락 필드 반환 |
| TC-03 | 월급 350만 원 저장 요청 | 금융 프로필 `changes` 반환 |
| TC-04 | 내년 월급 350만 원 가정 | 실제 저장 변경 후보 생성 금지 |
| TC-05 | 비상금 목표 추가 요청 | 기존 목표 보존 및 신규 목표 변경 후보 생성 |

### 평가 기준

- 필드 추출 정확도
- 금액 정규화 정확도
- 누락 정보 탐지 정확도
- 변경 후보 구조 정확도
- 실제/가상 정보 구분 정확도
- 기존 목표 보존 여부

Gold JSON 5개는 초기 테스트 데이터로 사용하며, 실제 코드 연동 시 `profile` 반환 세부 구조를 확정한다.

## 7. 구현 상태 및 다음 단계

### 완료

- Profile Agent 역할 및 책임 정리
- Resolver 설계 초안
- Missing Info Checker 설계 초안
- Change Builder 설계 초안
- Supervisor와 `ProfileResult` 계약 협의
- 테스트 시나리오 및 Gold JSON 초안

### 미완료

- 실제 Profile Agent 코드
- Supervisor v6 코드 연동
- DB 조회 및 저장 서비스 연동
- pytest 테스트 작성 및 실행
- 예외 상황 추가 평가

## 8. 관련 문서

- `멀티에이전트_설계안.md`
- `Profile_State_설계안.md`
- `State_설계안.md`

이 문서는 Profile Agent의 기능과 구현·평가 범위를 설명한다. 공통 프로필 필드와 MainState의 상세 정의는 위 정본 문서를 따른다.