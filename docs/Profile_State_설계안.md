# 청년 금융 서비스 — Profile State 상세 설계안

> **기준일:** 2026-11-24. 기존 20개 `MainState`와 `policy/product/fund`의 최신 실행 설계를 유지하면서, 기본·금융 프로필의 세부 필드를 이 문서로 **분리한 단일 정본**이다.
>
> **관련 문서:** [`State_설계안.md`](./State_설계안.md) — 20개 최상위 Key·실행·시나리오·검증 계약 / [`멀티에이전트_설계안.md`](./멀티에이전트_설계안.md) — 노드·전문 Agent 역할과 메인 흐름.
>
> **목적:** 영속 프로필과 LangGraph State의 **필드명·타입·누락 의미를 통일**하되, **Agent 입력/LLM 프롬프트에는 업무별 필요한 부분만** 전달한다. 실제 저장 DB가 원본이며 LangGraph State는 현재 상담의 스냅샷이다.

## 1. 결정 사항

1. `MainState`는 전체 상담 상태다. `basic_profile`, `financial_profile`은 그 안의 **두 필드**다. 기본 프로필이 MainState 전체를 의미하지 않는다.
2. Neo4j에 저장하는 `BasicProfile`/`FinancialProfile`과 `MainState`의 동명 필드에 **동일한 타입/키 계약**을 적용한다. Neo4j의 노드/관계 물리 구조는 자유롭게 구현할 수 있다.
3. State는 현재 상담의 스냅샷이다. DB는 영속 원본이다. 사용자 승인 없는 실제 DB 갱신은 금지한다.
4. `profile_node`는 Supervisor 내부 조건부 노드로 유지한다. 프로필 조회·변경의 규칙적인 처리는 **코드/서비스 함수**가 담당하고, 모호한 자연어 해석에만 LLM을 사용한다.
5. 처음부터 금융 상세 내역을 요구하지 않는다. `holdings`·`liabilities`는 선택 정보다.
6. **필요한 필드를 한 번에 묶어서 조회**한다. LLM에 모든 State를 전달하거나 필드 하나당 별도 LLM 호출을 수행하지 않는다.
7. 공용 `MainState`의 나머지 키(`scenario_state`, `intent`, `execution_plan`, `agent_results`, `*_profile_changes` 등)는 변경하지 않는다.

## 2. BasicProfile — 기본 프로필 스키마

### 2.1 `basic_profile`

| 키 | 타입 | 의미 | 수집 |
|---|---|---|---|
| `birth_date` | `date \| null` | 정확한 생년월일. 나이는 필요할 때 계산 | 연령 확인 시 |
| `region_sido` | `str \| null` | 거주 시·도 | 개인화 추천 시 |
| `region_sigungu` | `str \| null` | 거주 시·군·구 | 지역별 정책 시 |
| `employment_status` | `str \| null` | 현재 주된 상태: `employed`, `self_employed`, `student`, `job_seeking`, `unemployed`, `other` | 개인화 시 |
| `has_regular_income` | `bool \| null` | 정기적인 소득 유무. 재직 상태로 단정하지 않음 | 자금 계획 시 |
| `household_size` | `int \| null` | 가구원 수 | 가구 자격 시 |
| `owns_home` | `bool \| null` | 주택 소유 여부 | 주거·정책 시 |
| `marital_status` | `str \| null` | 예: `single`, `married`, `other` | 혼인 조건 시 |
| `risk_tolerance` | `str \| null` | 예: `conservative`, `moderate`, `aggressive` | 투자 위험 평가 시 |
| `goals` | `list[Goal] \| null` | 복수 금융 목표 | 목표 상담 시 |
| `snapshot_at` | `datetime \| null` | 현재 스냅샷 생성·확인 시점 | 시스템 |
| `version` | `int \| null` | DB의 프로필 버전 | 시스템 |

- 위에서 `필요할 때`는 회원가입 필수 입력을 뜻하지 않는다.
- 사용자가 나이만 제공한 경우 **정확한 생년월일을 추정하여 저장하지 않는다.** 상담의 일회성 `intent.supplied_values.age`로 사용하고, 정책상 정확한 연령이 필요하면 다시 확인한다.
- 직업/학적/가구 소득에 대한 특수 자격은 전문 Agent가 추가 질문할 수 있다. 이를 이유로 기본 프로필 필드를 무한히 확장하지 않는다.
- `has_regular_income`과 `employment_status`는 독립적이다. `monthly_net_income_krw`가 양수라고 정기 소득이 보장되는 것은 아니다.

### 2.2 `Goal` — `basic_profile.goals[]`

| 키 | 타입 | 의미 |
|---|---|---|
| `goal_id` | `str \| null` | 저장 서비스의 고유 ID. 결과·실적의 `goal_ref`와 동일 값. 일회성 목표는 `null` 가능 |
| `goal_type` | `str` | 예: `marriage`, `home_purchase`, `rental_deposit`, `savings`, `emergency_fund`, `debt_payoff`, `other` |
| `target_amount_krw` | `int \| null` | 목표 금액 |
| `target_amount_type` | `str \| null` | 예: `required_equity`, `total_purchase_price`, `target_balance` |
| `target_date` | `date \| null` | 목표 예정일. 모르면 `null` |
| `target_date_precision` | `str \| null` | 목표일의 표현 정밀도: `year`, `month`, `day` |
| `priority` | `int \| null` | 우선순위. 없으면 `null` |
| `status` | `str` | `active`, `paused`, `completed` |

**기간 관리 원칙**
- `target_date`가 저장 기준이다. 연도만 지정하면 해당 연도 12월 31일(`year`), 연·월만 지정하면 해당 월 말일(`month`), 정확한 일자이면 해당 날짜(`day`)로 정규화한다. `null`이면 정밀도도 `null`이다. 연·월 단위 날짜는 계산용 기준이며 화면에서 실제 확정 마감일처럼 표시하지 않는다.
- "5년 뒤"처럼 상대 기간을 명시하면 상담 기준일에 60개월을 더한 날짜를 사용한다(말일 초과 시 해당 월 말일). 이 경우 `day`는 계산상 날짜의 해상도이지 사용자가 특정 일자를 확정했다는 뜻이 아니다. 대략적인 기간은 연·월 정밀도를 유지한다.
- `target_period_months`는 상담 기준일부터 목표일까지의 **완전한 달력 개월 수**(말일 보정, 부분월 내림, 과거 기한은 0)로 코드에서 계산하며 중복 저장하지 않는다. 실제 납입 가능 횟수는 납입일·만기일로 별도 계산한다. 정확한 기한에 따라 결과가 달라지면 사용자에게 확인한다.
- 기존 `goals[].type` → `goal_type`, `amount_krw` → `target_amount_krw`, `amount_type` → `target_amount_type`. `target_year` 단독 데이터는 연도 정밀도를 보존하여 전환한다.
- 복수 목표가 가능하며 목표 등록·변경·삭제는 `goal_id` 기준이다. 기존 결과·대시보드의 `goal_ref`는 저장된 `Goal.goal_id`를 그대로 참조한다(별도 ID 생성 금지). 일회성 목표는 영속 `goal_ref`를 만들지 않는다. 상담 제안만으로 실제 목표를 변경하지 않는다.

## 3. FinancialProfile — 금융 프로필 스키마

| 키 | 타입 | 의미 |
|---|---|---|
| `monthly_net_income_krw` | `int \| null` | 세후 월평균 소득 |
| `annual_gross_income_krw` | `int \| null` | 세전 연간 소득. 정책 기준에 따라 별도 확인 |
| `monthly_living_expenses_krw` | `int \| null` | 일반 생활비. 부채 원리금·저축 납입액 제외 |
| `total_assets_krw` | `int \| null` | 사용자가 알고 있는 전체 자산 |
| `available_assets_krw` | `int \| null` | 계획에 실제 사용 가능한 자금. 비상자금·제약 고려 |
| `total_debt_krw` | `int \| null` | 전체 남은 부채 |
| `monthly_debt_payment_krw` | `int \| null` | 월 부채 원리금·할부 상환 부담 |
| `holdings` | `list[Holding] \| null` | 사용자가 보유한 금융상품 상세. 선택 |
| `liabilities` | `list[Liability] \| null` | 대출·할부 등의 상세. 선택 |
| `snapshot_at` | `datetime \| null` | 현재 스냅샷 생성·확인 시점 |
| `version` | `int \| null` | DB의 금융 프로필 버전 |

**대표값과 상세 목록 관계**
- `total_assets_krw`는 `holdings[]` 단순 합계와 같을 필요가 없다. 현금·보증금·기타 자산이 포함될 수 있고 보유상품 목록이 부분 등록일 수 있다.
- `total_debt_krw`도 `liabilities[]` 합계로 무조건 덮어쓰지 않는다. 상세 등록 범위를 확인한 경우만 대조하거나 계산한다.
- `available_assets_krw`를 `total_assets_krw`와 동일하게 추정하지 않는다.
- `monthly_living_expenses_krw`는 월세 등 생활비를 포함하지만 원리금과 저축 납입액을 제외한다.
- 현재 월 가용액 = `monthly_net_income_krw - monthly_living_expenses_krw - monthly_debt_payment_krw`. 이는 **신규 저축에 전액 투입해도 되는 금액이 아니다**. 기존 저축 납입액과 예비비 등을 추가 고려한다.
- 기존 저축 납입액은 `holdings[].monthly_contribution_krw`에서 가능한 범위만 합산한다. 보유상품 입력이 불완전하면 총액을 확정하지 않고 질문한다.
- 세전 연소득과 세후 월소득은 서로 확정 환산하지 않는다.
- 시계열 집계, 거래기록, 계좌 비밀번호, 실제 계좌번호, 인증 토큰은 공용 프로필에 넣지 않는다.

### 3.1 `Holding` — `financial_profile.holdings[]`

| 키 | 타입 | 의미 |
|---|---|---|
| `holding_id` | `str` | 사용자 계약/보유상품의 안정적 고유 ID. `holding_ref`와 동일 값 |
| `product_type` | `str` | `installment_savings`, `deposit`, `investment`, `other` 등 |
| `product_ref` | `str \| null` | 상품 DB에서 조회할 수 있는 참조 ID |
| `balance_krw` | `int \| null` | 현재 잔액/평가금액 |
| `monthly_contribution_krw` | `int \| null` | 월 납입액. 정기 납입 상품만 해당 |
| `annual_rate_pct` | `float \| null` | 사용자 계약에 적용되는 연 금리 |
| `opened_at` | `date \| null` | 가입일 |
| `maturity_date` | `date \| null` | 만기일 |
| `status` | `str` | `active`, `matured`, `closed` |

- 투자상품에는 확정 금리를 임의로 채우지 않는다(`annual_rate_pct=null`).
- 중도해지 이율, 우대금리, 계약 특약 등은 `product_ref`로 상품 원본을 조회한다. 개인 계약이 다르면 본인 계약정보를 확인한다.
- `balance_krw`만으로 해지 환급액을 확정하지 않는다. 원금 납입 내역과 해지 조건이 없으면 추가 확인이 필요하다.

### 3.2 `Liability` — `financial_profile.liabilities[]`

| 키 | 타입 | 의미 |
|---|---|---|
| `liability_id` | `str` | 개별 부채 고유 ID |
| `liability_type` | `str` | `credit_loan`, `rent_loan`, `mortgage`, `installment`, `other` 등 |
| `balance_krw` | `int \| null` | 남은 원금 |
| `annual_rate_pct` | `float \| null` | 개인에게 적용된 연 금리 |
| `monthly_payment_krw` | `int \| null` | 현재 월 상환액 |
| `repayment_method` | `str \| null` | 원리금균등·원금균등·만기일시·기타 |
| `maturity_date` | `date \| null` | 만기일 |
| `status` | `str` | `active`, `paid_off` |

- `liabilities[]`를 전부 등록하지 않아도 `total_debt_krw`, `monthly_debt_payment_krw` 대표값으로 기본 상담이 가능하다.
- 실제 대출 가능 여부·대출 조건은 금융기관 심사와 상품 자격 기준에 좌우된다. 프로필 값만으로 확정 판정하지 않는다.

### 3.3 누락·0·목록의 의미 (필수)

| 데이터 | 의미 |
|---|---|
| State에 키 자체가 없음 | 아직 이번 상담 State로 로드하지 않은 필드/섹션일 수 있음 |
| `null` | 해당 값이 미확인 또는 명시적으로 비어 있음. 절대 0으로 간주하지 않음 |
| `0` | 사용자가 명확히 없다고 답했거나 확인된 영 값 |
| `goals=[]` | 저장되거나 확인된 금융 목표가 없음. 새 목표를 자동 생성하지 않음 |
| `holdings=[]` | 등록된 상세 보유상품 항목이 없음. 전체 자산이 0이라는 뜻 아님 |
| `liabilities=[]` | 등록된 개별 부채 항목이 없음. 총부채가 0이라는 뜻 아님 |
| `version` | 낙관적 잠금 등 저장 충돌 확인에 이용하는 버전 |

부분 로딩 시 **로드되지 않은 키와 저장되어 있으나 값이 `null`인 키를 구분**한다. 임의 기본값(`0`, `[]`)으로 먼저 채우지 않는다.


### 3.4 DB/State 공통 프로필 구조 예시

다음은 **일부 필드만 확인한 사용자**의 프로필 스냅샷 예시다. 날짜·금액·상품명은 가상값이다. 저장 서비스와 MainState가 동일한 필드명을 사용하며, **미조회 상세 필드 전체를 프롬프트에 넣는 것은 아니다**.

```json
{
  "basic_profile": {
    "birth_date": "1999-06-15",
    "region_sido": "서울특별시",
    "region_sigungu": null,
    "employment_status": "employed",
    "has_regular_income": true,
    "household_size": null,
    "owns_home": null,
    "marital_status": null,
    "risk_tolerance": null,
    "goals": [
      {
        "goal_id": "goal_001",
        "goal_type": "home_purchase",
        "target_amount_krw": 80000000,
        "target_amount_type": "required_equity",
        "target_date": "2031-12-31",
        "target_date_precision": "year",
        "priority": 1,
        "status": "active"
      }
    ],
    "snapshot_at": "2026-11-24T09:00:00+09:00",
    "version": 2
  },
  "financial_profile": {
    "monthly_net_income_krw": 3200000,
    "annual_gross_income_krw": null,
    "monthly_living_expenses_krw": 1800000,
    "total_assets_krw": 15000000,
    "available_assets_krw": 12000000,
    "total_debt_krw": 10000000,
    "monthly_debt_payment_krw": 350000,
    "holdings": [
      {
        "holding_id": "holding_01",
        "product_type": "installment_savings",
        "product_ref": "product_001",
        "balance_krw": 3000000,
        "monthly_contribution_krw": 300000,
        "annual_rate_pct": 3.0,
        "opened_at": "2025-11-01",
        "maturity_date": "2027-10-31",
        "status": "active"
      }
    ],
    "liabilities": null,
    "snapshot_at": "2026-11-24T09:00:00+09:00",
    "version": 3
  }
}
```

**해석:** 총부채는 1,000만 원으로 알려져 있으나 `liabilities=null`이므로 개별 대출 세부 정보는 아직 미확인이다. 이 예시에서 `holdings`가 한 개만 있다고 전체 자산이 300만 원인 것은 아니다. `target_date_precision="year"`이면 실제 목표 의미는 **2031년까지**이며 날짜의 월·일은 저장 정규화를 위한 임시 표현이므로 사용자에게 정확한 12월 31일이라고 단정하지 않는다.

### 3.5 가입 계약 정본과 `holdings[]`의 연결

- `financial_profile.holdings[]`는 기존 Neo4j의 사용자별 계약/보유상품 레코드에서 투영한 **현재 보유상품의 선택적 요약**이다. 가입·기납입 원금·거래 이력·계약 유효 여부의 정본도 같은 사용자 계약 서비스에 둔다. 별도 프로필용 계약 원장을 중복 저장하지 않는다.
- **`holding_ref = Holding.holding_id`**: 저장된 계약을 가리키는 경우 두 필드는 동일한 안정적 ID를 사용한다. `product_ref`는 금융상품 원본의 별도 ID다. 조회 시 인증 사용자 소유권과 `baseline_ref`에 해당 ID가 포함되는지 검증한다. 미등록 사용자 진술은 일회성 스냅샷 안의 임시 참조로만 다루며 실제 계약 ID로 저장하거나 검증된 가입으로 간주하지 않는다.
- `get_holdings_snapshot(user_context, as_of)`가 **동일 기준일·버전의 `baseline_ref`**를 반환하면, Supervisor/Dispatcher는 그 참조를 공통으로 사용한다. 한 분기에는 새 내역, 다른 분기에는 과거 내역을 섞지 않는다.
- `get_contribution_history(holding_ref)`는 요청의 고정 `baseline_ref`에 해당하는 기납입 금액·가입/만기일 등 필요한 사실만 제공한다. 기준이 일치하지 않으면 재조회하거나 조건부 비교한다. 전체 거래내역은 LLM에 전달하지 않는다.
- 보유상품 내역을 포함한 `financial_profile` 스키마는 DB와 State가 같지만, **정밀 가입·납입 원장까지 State에 복제하는 것은 아니다.** 필요한 부분만 도구로 조회한다.
- 적금 잔액이 `total_assets_krw`에 이미 포함되었다면 Fund가 이를 별도 자산으로 중복 가산하지 않는다. `liabilities[]`도 `total_debt_krw`와 중복 계산하지 않는다.

### 3.6 공통 타입 선언 — DB 서비스와 MainState에서 재사용

`backend/agent/shared/profile_models.py`에 아래 공통 `TypedDict`를 한 번 정의한다(권장 경로). `total=False`로 부분 로딩을 허용하고 날짜는 Python `date`/`datetime`, JSON은 ISO 8601로 직렬화한다. 저장 서비스는 같은 타입을 기준으로 입력 검증을 수행한다. `TypedDict` 자체가 런타임 검증을 대신하지는 않는다.

```python
from datetime import date, datetime
from typing import Required, TypedDict


class Goal(TypedDict, total=False):
    goal_id: str | None
    goal_type: Required[str]
    target_amount_krw: int | None
    target_amount_type: str | None
    target_date: date | None
    target_date_precision: str | None
    priority: int | None
    status: Required[str]


class Holding(TypedDict, total=False):
    holding_id: Required[str]
    product_type: Required[str]
    product_ref: str | None
    balance_krw: int | None
    monthly_contribution_krw: int | None
    annual_rate_pct: float | None
    opened_at: date | None
    maturity_date: date | None
    status: Required[str]


class Liability(TypedDict, total=False):
    liability_id: Required[str]
    liability_type: Required[str]
    balance_krw: int | None
    annual_rate_pct: float | None
    monthly_payment_krw: int | None
    repayment_method: str | None
    maturity_date: date | None
    status: Required[str]


class BasicProfile(TypedDict, total=False):
    birth_date: date | None
    region_sido: str | None
    region_sigungu: str | None
    employment_status: str | None
    has_regular_income: bool | None
    household_size: int | None
    owns_home: bool | None
    marital_status: str | None
    risk_tolerance: str | None
    goals: list[Goal] | None
    snapshot_at: datetime | None
    version: int | None


class FinancialProfile(TypedDict, total=False):
    monthly_net_income_krw: int | None
    annual_gross_income_krw: int | None
    monthly_living_expenses_krw: int | None
    total_assets_krw: int | None
    available_assets_krw: int | None
    total_debt_krw: int | None
    monthly_debt_payment_krw: int | None
    holdings: list[Holding] | None
    liabilities: list[Liability] | None
    snapshot_at: datetime | None
    version: int | None
```

저장된 목표는 실제 `goal_id`가 있어야 한다. 영속 저장·조회·State·시나리오 참조가 서로 다른 스키마를 만들지 않도록 한다. `age`는 아래의 상담 전용 View 필드이지 `BasicProfile`의 영속 필드가 아니다.

## 4. 프로필 저장·State·LLM 전달은 서로 다른 동작

**같은 스키마란 동일한 필드명과 타입을 공유한다는 뜻이다. 매 Agent에 전체 데이터를 전달하라는 뜻이 아니다.**

1. `Context Loader`가 현재 상담에 필요한 기본 스냅샷을 DB에서 읽어 `MainState.basic_profile`과 `MainState.financial_profile`에 적재한다. 일반 안내에는 개인정보 조회가 필요하지 않을 수 있다.
2. Supervisor는 `execution_plan.tasks[]`에 업무 유형과 목적을 정한다.
3. Dispatcher 또는 전문 Agent 입력 어댑터가 **업무별 필드 집합**을 코드에서 선택한다. `task_type`은 업무/템플릿으로 결정하는 서버 내부 값이며 새 MainState 필드는 아니다.
4. State에 없는 필요한 값만 읽기 전용 Tool로 일괄 조회하고, 실제값 → `intent.supplied_values` → 해당 `scenario_state.scenarios[].overrides` 순으로 임시 유효값을 구성한다. 목표는 승인된 저장 변경과 별개로 이번 상담 의도에 따라 병합하며, 계약 스냅샷·가상 행동은 별도 View 속성으로 붙인다.
5. **병합 후 허용된 필드만 `profile_view`에 투영**하여 LLM에 전달한다. 전체 State를 직렬화하지 않는다. 일회성 `age`는 Policy View에서 사용할 수 있으나 DB의 생년월일을 추정해 수정하지 않는다.
6. Tool의 출력은 해당 요청의 임시 입력이나 Agent local context로 사용한다. 실제 프로필 저장은 승인된 `profile_node`를 통해서만 한다.

```text
Neo4j (공통 프로필 스키마)
       |
       v
Context Loader / Profile Service  -- 필요할 때 1회 조회 -->
       |
       v
MainState.basic_profile / financial_profile  (동일한 스키마, 현재 스냅샷)
       |
       v
Dispatcher: task_type => required_fields (규칙 / 코드)
       |
       +--> State에 있음: 실제값 재사용 -----+
       |                                    |
       +--> 미로딩 값: Read Tool(batch) ----+--> 일회성 입력 / 분기 overrides 병합
                                                |
                                                v
                                           허용 필드 투영 (profile_view)
                                                |
                                                v
                                           전문 Agent의 LLM 입력
```

### 4.1 작업별 권장 필드 뷰

| 작업 | 기본 프로필에서 전달 | 금융 프로필에서 전달 | 추가 Tool |
|---|---|---|---|
| 일반 정보 안내 | 없음 | 없음 | 공개 정책·상품 조회 |
| Policy — 자격 검색 | `birth_date` 또는 일회성 `age`, `region_sido`, `region_sigungu`, `employment_status`, `household_size`, `owns_home`, 관련 `goals` | `annual_gross_income_krw` 등 해당 정책에 필요한 값 | 정책별 예외 조건만 추가 질문 |
| Product — 적금·예금 | `goals`, `risk_tolerance` | `available_assets_krw`, `monthly_net_income_krw`, 필요시 `holdings` | 상품 금리·특약 조회 |
| Product — 대출 | 관련 `goals`, `employment_status`, `owns_home` | `annual_gross_income_krw`, `total_debt_krw`, `monthly_debt_payment_krw`, 필요시 `liabilities` | 대출 금리·상환 조건 조회 |
| Fund — 기본 로드맵 | `goals`, `has_regular_income`, 필요시 `risk_tolerance` | 7개 대표값 중 작업에 필요한 값 | 예산 계산 도구 |
| Fund — 갈아타기/상세 비교 | `goals` | 대표값 + 관련 `holdings`, 필요시 `liabilities` | 상품 상세·해지 조건 조회 |

위 표는 **초기 기본 집합**이지 회원가입 필수 정보가 아니다. 일반 안내는 `required=[]`이며 구체적인 Fund 월 현금흐름 계산은 월소득·생활비·부채상환액의 확인이 필요하다. Policy의 `age`는 이번 상담에 명시된 값만 전달하거나 생년월일로 해당 정책 기준일의 나이를 계산한다. 입력 나이와 저장 생년월일이 충돌하면 확인하고, 생년월일을 알 수 없거나 정확한 기준일 판정이 불가능하면 `needs_check`로 둔다. `age`를 DB 조회 경로에 포함하지 않는다.

## 5. Tool 입출력 계약

### 5.1 Profile Read Tool (조회) 최소 계약

- `get_profile_fields(fields: list[str])`: 허용된 경로의 값을 **한 번의 호출로** 묶어서 반환. 예: `["basic_profile.goals", "financial_profile.available_assets_krw"]`.
- `list_holdings(product_types: list[str] | None, active_only: bool)`: 필요한 보유상품만 반환. `holding_id`를 기존 계약의 `holding_ref`로 그대로 사용.
- `list_liabilities(liability_types: list[str] | None, active_only: bool)`: 필요한 부채만 반환.
- `get_holdings_snapshot(user_context, as_of)`: 인증된 현재 사용자에 대한 실제 계약/가입 현황을 **동일 기준일**로 고정하고 `baseline_ref`를 반환. 이력은 서비스에서 관리.
- `get_contribution_history(holding_ref)`: 요청에 고정된 `baseline_ref`의 확인된 계약에 대해 가입일·납입원금·납입 내역 중 필요한 사실만 반환.
- `get_product_terms(product_ref: str)`: **Product 상품 DB 읽기 Tool**. 금리/만기/중도해지 조건을 조회하지만 Profile 저장 Tool은 아님.

Tool은 서버가 인증한 현재 사용자로 범위를 제한한다. LLM이 `user_id`, Neo4j Cypher, 임의 속성 경로를 직접 지정하게 하지 않는다. 필드명과 반환 범위는 **허용 목록(allowlist)** 으로 검증한다.

### 5.2 Profile Write Tool (수정) 최소 계약

- `apply_profile_changes(changes, change_id, expected_version)` — **Supervisor 내부 profile_node만 사용**
- 실제 저장 의사 또는 명시적 승인 확인 → 입력 검증 → DB 버전 확인 → 반영 → 재조회 → `MainState` 스냅샷 갱신
- `financial_profile_changes`, `basic_profile_changes`는 **기존 MainState 키**를 재사용한다.
- User가 "월급이 350만 원이면?"이라고 가정할 때는 `scenario_state.scenarios[].overrides`에만 기록하고 저장하지 않는다.
- 추천안 수락 후 목표를 추가할 때도 사용자의 명확한 저장 승인을 받고 `goals[]`에 저장한다. 상품 가입이나 금융 거래를 자동 실행하는 것은 아니다.
- 저장 실패, 권한 오류, 버전 충돌 시 `applied`라고 표시하지 않는다.

### 5.3 간단한 코드 흐름 (개념 예시)

```python
# 업무 유형별로 허용한 필드 묶음. LLM이 키를 하나씩 고르는 방식이 아니다.
PROFILE_FIELD_SETS = {
    "policy": {
        "basic": ("birth_date", "age", "region_sido", "region_sigungu", "employment_status",
                  "household_size", "owns_home", "goals"),
        "financial": ("annual_gross_income_krw",),
    },
    "product_savings": {
        "basic": ("goals", "risk_tolerance"),
        "financial": ("available_assets_krw", "monthly_net_income_krw"),
    },
    "product_loan": {
        "basic": ("goals", "employment_status", "owns_home"),
        "financial": ("annual_gross_income_krw", "total_debt_krw",
                      "monthly_debt_payment_krw"),
    },
    "fund": {
        "basic": ("goals", "has_regular_income"),
        "financial": ("monthly_net_income_krw", "monthly_living_expenses_krw",
                      "total_assets_krw", "available_assets_krw",
                      "total_debt_krw", "monthly_debt_payment_krw"),
    },
}

def pick_present(profile: dict, allowed_fields: tuple[str, ...]) -> dict:
    # 아직 로딩하지 않은 필드는 임의로 null/0으로 만들지 않음
    return {name: profile[name] for name in allowed_fields if name in profile}

def build_profile_view(state: dict, task_type: str, *,
                       overrides: dict | None = None,
                       goal_view: dict | None = None) -> dict:
    fields = PROFILE_FIELD_SETS[task_type]
    # 먼저 필요한 미로딩 DB 값은 읽기 전용으로 보완한 state를 전달한다.
    # supplied_values/overrides는 기존 State 계약의 평평한 필드명이다.
    supplied = (state.get("intent") or {}).get("supplied_values", {})
    overlay = {**supplied, **(overrides or {})}
    if "goals" in overlay and "goals" in fields["basic"]:
        if goal_view is None or "goals" not in goal_view:
            raise ValueError("목표 변경 의도에 따른 병합 결과가 필요합니다.")
    overlay.pop("goals", None)  # 목표 배열은 단순 덮어쓰기 금지
    basic = {**state.get("basic_profile", {}),
             **pick_present(overlay, fields["basic"])}
    financial = {**state.get("financial_profile", {}),
                 **pick_present(overlay, fields["financial"])}
    # 목표 변경 요청이 있으면 의도에 따라 미리 병합한 {"goals": ...} 전달.
    if goal_view is not None and "goals" in goal_view:
        basic["goals"] = goal_view["goals"]
    return {
        "basic_profile": pick_present(basic, fields["basic"]),
        "financial_profile": pick_present(financial, fields["financial"]),
    }

# age는 상담용 allowlist에만 포함, DB 조회·저장 대상 아님.
# 선택 분기의 overrides 및 검증된 goal_view를 인자로 전달한다.
# 일반적인 작업은 위 함수가 만든 profile_view만 LLM에 전달.
# 적금 갈아타기라면 Agent가 필요할 때만:
#   list_holdings(product_types=["installment_savings"], active_only=True)
#   get_product_terms(product_ref=...)
```

이 코드는 **병합 후 투영하는 실행 패턴**의 예시다. 실제 DB 조회·인증·나이 충돌 확인·목표 병합·시나리오 입력 검증·기존 계약 스냅샷 조립은 Dispatcher/서비스의 선행 처리에서 수행한다. 일반 안내는 View 생성 자체를 생략할 수 있다.

## 6. 요청 유형별 예시

### A. "청년 정책 종류 알려줘"
- 프로필 조회하지 않아도 일반 정책 안내 가능.
- 조건 검색이 필요할 때만 `get_profile_fields([...])`로 연령·지역·소득 등 일괄 조회.
- 정책 원문에 필요한 특수 자격만 Agent가 추가 질문.

### B. "매달 얼마를 더 저축할 수 있어?"
- `fund` 필드 집합만 전달.
- 코드 계산으로 `세후 월소득 - 생활비 - 부채 상환액` 산출.
- 기존 납입액을 고려해야 하고 아직 모르면 관련 `holdings`만 조회. 등록 범위가 불완전하면 사용자 확인.
- 근거·가정·단위를 Agent 결과에 보존한다.

### C. "3% 적금을 깨고 5%로 갈아탈까?"
- Product/Fund가 적금 `holdings`만 선택 조회.
- `product_ref`로 중도해지 이율, 우대조건, 만기/납입 조건을 상품 DB에서 조회.
- 아직 없는 개인 약정 조건은 모름으로 처리.
- 유지/해지 후 변경 시나리오를 `scenario_state`에서 비교. 실제 보유내역이나 목표 자동 변경 금지.

### D. "내 월급을 350만 원으로 저장해줘"
- 내부 `profile_node`에서 저장 의사를 확인 → DB 갱신 → 버전 재확인 → State 갱신.
- 저장 이력은 서비스 계층에서 관리. 전체 MainState 복사 저장이 아님.

### E. "내년 월급이 350만 원이면?"
- `scenario_state`에만 임시 가정을 둔다. 실제 프로필 변경 및 저장 없음.

## 7. 기존 설계안과의 차이

| 기존 압축 설계 | 공통 스키마 확정안 | 변경 이유 |
|---|---|---|
| `basic_profile.age` | `birth_date` (정확 생년월일 확인 시 저장) | 정책 연령 기준일 계산 |
| `basic_profile.region` | `region_sido`, `region_sigungu` | 지역 정책 정밀도 |
| 정기소득 여부 중심 | `employment_status` 추가, `has_regular_income` 유지 | 재직/구직 자격과 소득 지속성 분리 |
| `goals[].type/amount_krw/target_year` | 목표 ID·종류·금액·날짜·정밀도·상태 | 복수 목표 업데이트 및 대시보드 연계 |
| 금융 요약 7개 | 7개 유지 | 단순 상담 유지 |
| 보유상품 상세 없음 | `holdings[]` 선택 추가 | 보유 적금·예금 비교 |
| 부채별 상세 없음 | `liabilities[]` 선택 추가 | 대출·할부 상세 분석 |
| 프로필 기준 시점 메타 최소 | 각 프로필 `snapshot_at`, `version` | 갱신·동시 수정 관리 |
| Agent에 공통 프로필 Slice 전달 | **코드 기반 업무별 Field Set + 필요 시 Tool** | LLM 비용·개인정보 노출·복잡도 감소 |

**역할 정합성:** `Housing Agent`는 두지 않는다. 대출은 `Product`, 지원정책은 `Policy`, 자산관리와 목표 로드맵은 `Fund`가 담당한다. Agent 역할의 정본은 `멀티에이전트_설계안.md`다.

## 8. 개발 완료 기준

- [ ] `BasicProfile`, `FinancialProfile`, `Goal`, `Holding`, `Liability` 공통 모델이 DB 서비스와 State에서 동일하게 사용됨
- [ ] 프로필 필드 누락·`null`·`0`·`[]` 의미가 구분됨
- [ ] Dispatcher가 실제값 → 일회성 입력 → 가정 순으로 병합한 뒤 필요한 필드만 전달함. 일회성 `age`와 목표의 부분 병합이 누락되지 않음
- [ ] 저장 `goal_id`와 결과 `goal_ref`, 저장 `holding_id`와 계약 `holding_ref`가 동일 ID로 연결됨
- [ ] 연·월 목표일 정규화와 달력 개월 수 계산이 일치하며 날짜 정밀도가 유지됨
- [ ] 일반 정책 안내가 프로필 정보 없이도 동작함
- [ ] 상세 `holdings`/`liabilities` 조회는 필요한 작업에서만 발생함
- [ ] 프로필 Tool이 요청 사용자를 서버 인증 컨텍스트로 제한함
- [ ] 사용자 승인 없는 영속 저장이 발생하지 않음
- [ ] 저장 성공 후 State 스냅샷이 갱신되며 버전 충돌은 거절됨
- [ ] `scenario_state` 가정이 실제 프로필을 오염시키지 않음
- [ ] Fund가 보유상품 없이도 기본 로드맵을 작성할 수 있음
- [ ] 상세 누락 시 Agent가 자격/해지 수익 등을 확정값으로 단정하지 않음

**권장 구현 순서:** 공통 모델 → ProfileService read/write → Context Loader → 작업별 Field Set 투영 → 상세 조회 Tool → profile_node 저장/승인 규칙 → 테스트.
