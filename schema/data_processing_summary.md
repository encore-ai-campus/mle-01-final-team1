# 금융·청년정책 데이터 처리 전체 정리

이 문서는 현재 프로젝트에서 원본 JSON 4개를 어떻게 확인하고 정제했는지 전체 과정을 정리한 문서다.

## 1. 처리 대상

| 데이터셋 | 원본 파일 | 원본 레코드 |
|---|---|---:|
| 청년정책 | `data/청년정책.json` | 정책 4,018건 |
| 적금 | `data/적금.json` | 상품 338건 |
| 정기예금 | `data/정기예금.json` | 상품 434건 |
| 전세자금대출 | `data/전체자금 대출.json` | 상품 58건 |

## 2. 전체 처리 흐름

```text
원본 JSON 읽기
  ↓
파일별 스키마 확인
  ↓
원본 필드명과 표준 필드명 매핑
  ↓
문자열·숫자·날짜·다중값 변환
  ↓
결측값 의미 및 상태 필드 적용
  ↓
상품정보와 금리정보 분리
  ↓
중복·변환 오류·금리범위 검증
  ↓
data/processed/에 정제 데이터 저장
```

원본 `data/*.json` 파일은 수정하지 않고 정제 결과를 별도 저장한다.

## 3. 스키마 확인

### 청년정책

```text
root
└── policies[]
```

- 정책 레코드: 4,018건
- 정책 필드: 60개
- 정책 ID: `plcyNo`

### 금융상품

```text
root
└── products[]
    ├── product_id
    ├── financial_group
    ├── base
    └── rate_options[]
        └── data
```

금융상품은 `base`와 `rate_options[].data`를 별도 분석 구간으로 분리했다.

| 데이터 | 상품 기본정보 | 금리옵션 |
|---|---:|---:|
| 적금 | 338건·15개 필드 | 1,539건·10개 필드 |
| 정기예금 | 434건·15개 필드 | 2,808건·8개 필드 |
| 전세자금대출 | 58건·13개 필드 | 106건·10개 필드 |

관련 파일:

- `scripts/inspect_schema.py`
- `schema/schema_profile.json`
- `schema/schema_report.md`

## 4. 필드명 표준화

필드명 변환 기준은 `schema/field_mapping.csv`에 저장했다.

| 원본 필드 | 표준 필드 | 의미 |
|---|---|---|
| `plcyNo` | `policy_id` | 정책 고유번호 |
| `plcyNm` | `policy_name` | 정책명 |
| `inqCnt` | `inquiry_count` | 조회수 |
| `fin_co_no` | `institution_id` | 금융기관 코드 |
| `kor_co_nm` | `institution_name` | 금융기관명 |
| `fin_prdt_cd` | `product_code` | 상품 코드 |
| `fin_prdt_nm` | `product_name` | 상품명 |
| `intr_rate` | `base_rate` | 기본금리 |
| `intr_rate2` | `max_rate` | 최고금리 |
| `lend_rate_min` | `min_rate` | 대출 최저금리 |
| `lend_rate_max` | `max_rate` | 대출 최고금리 |
| `lend_rate_avg` | `average_rate` | 대출 평균금리 |

현재 원본 스키마의 필드 131개가 매핑표에 등록되어 있다.

## 5. 문자열 처리

문자열에는 다음 규칙을 적용했다.

- 앞뒤 일반 공백 제거
- 전각 공백을 일반 공백으로 변환
- 공백만 있는 값은 `null`로 변환
- 설명문 내부의 줄바꿈은 의미 보존을 위해 유지

예시:

```text
"  정책명  " → "정책명"
"       "    → null
```

## 6. 숫자·금리 처리

### 정수형

- 천 단위 쉼표 제거
- 숫자로 변환
- 정수로 저장

```text
"1,234" → 1234
```

### 금리·실수형

- 숫자로 변환
- 소수점 이하가 없으면 정수 형태로 저장

```text
"3.50" → 3.5
```

### 변환 실패

변환할 수 없는 값은 임의로 추정하지 않는다.

- 정제 필드: `null`
- 원본값: `<standard_field>_raw`
- 품질보고서: `conversion_issues`

현재 실행 결과 변환 실패는 0건이다.

## 7. 날짜 처리

다음 형식을 ISO 형식으로 변환했다.

```text
YYYYMMDD → YYYY-MM-DD
YYYY-MM-DD → YYYY-MM-DD
YYYYMMDDHHMM → YYYY-MM-DDTHH:MM:SS
YYYY-MM-DD HH:MM:SS → YYYY-MM-DDTHH:MM:SS
```

청년정책의 `aplyYmd`처럼 기간 문자열은 단일 날짜로 강제 변환하지 않고 `application_period` 문자열로 유지했다.

```text
20261102 ~ 20261211
```

## 8. 다중값 처리

`string_or_array`로 지정된 필드는 쉼표·줄바꿈·가운뎃점 기준으로 배열화했다.

예시:

```text
"영업점, 인터넷\n스마트폰"
→ ["영업점", "인터넷", "스마트폰"]
```

주요 대상:

- 청년정책 지역 코드
- 청년정책 학교 코드
- 청년정책 직업 코드
- 청년정책 산업 코드
- 정책 키워드
- 금융상품 가입방법

## 9. 특수값 처리

청년정책의 최대연령 `999`는 실제 연령으로 사용하지 않는다.

```json
{
  "max_age": null,
  "age_unlimited": true,
  "max_age_status": "unknown"
}
```

소득금액 `0`은 제한 없음인지 실제 0원인지 원본만으로 확정할 수 있으므로 숫자 0으로 유지한다.

## 10. 결측값 의미 처리

결측값 정책은 `schema/missing_value_policy.csv`에 정의했다.

현재 결측값 처리에는 두 단계가 있다.

### 값 처리

```text
빈 문자열·공백 → null
원본에 없는 정책 필드 → null로 생성 가능
숫자·날짜 결측 → null
```

### 상태 필드 처리

결측 정책표에 등록된 필드에는 상태 필드를 추가한다.

```json
{
  "average_rate": null,
  "average_rate_status": "not_provided"
}
```

값이 있으면:

```json
{
  "average_rate": 4.2,
  "average_rate_status": "present"
}
```

상태값 의미:

| 상태 | 의미 |
|---|---|
| `present` | 값이 존재함 |
| `not_provided` | 원천 데이터에서 제공되지 않음 |
| `unknown` | 값이 없어 의미나 조건을 확정할 수 없음 |

파생 필드의 전체 목록과 구현 여부는 `schema/derived_fields.csv`에서 관리한다.

- `implemented`: 현재 전처리 코드가 실제로 생성하는 필드
- `planned`: 향후 온톨로지·조건 구조화·시뮬레이션 연동 시 추가할 필드
- `candidate`: 추가 여부를 검토하기 위해 후보로 등록한 필드. 현재 데이터에는 적용하지 않음

따라서 `field_mapping.csv`는 원천 필드명과 표준 필드명의 매핑만 담당하고, 상태 필드와 향후 계산·분류 필드는 `derived_fields.csv`에서 별도로 관리한다.

현재 구현된 주요 파생 필드:

- 청년정책 `application_start_date`, `application_end_date`
- 청년정책 `income_condition_normalized`
- 금융상품 금리옵션 `rate_basis`
- 금융상품 상품유형 `product_type` (`savings`·`deposit`·`loan`)
- 적금·예금 최소 가입금액 `min_subscription_amount_krw`

### 주요 결측 처리

| 필드 | 처리 기준 |
|---|---|
| `average_rate` | `null` 유지·평균금리 비교 제외 |
| `max_limit` | `null` 유지·0원이나 제한 없음으로 해석하지 않음 |
| `disclosure_end_date` | `null` 유지·계속 공시 중이라고 확정하지 않음 |
| `min_income`, `max_income` | `null` 유지·가입 가능 여부 확정 금지 |
| `min_age`, `max_age` | `null` 유지·연령 제한 없음으로 단정하지 않음 |
| `application_url` | URL 미제공으로 표시 |
| `submission_document` | 제출서류 확인 필요로 표시 |

결측 정책 적용 대상은 현재 4개 데이터셋의 42개 필드다.

## 11. 상품과 금리정보 분리

금융상품의 기본정보와 금리옵션을 별도 파일로 저장했다.

```text
products[].base
→ *_products.json

products[].rate_options[].data
→ *_rates.json
```

금리 레코드에는 상품 연결을 위한 `product_id`를 추가했다.

## 12. 중복 및 품질검증

### ID 중복

| 데이터 | ID | 결과 |
|---|---|---:|
| 청년정책 | `policy_id` | 0건 |
| 금융상품 | `product_id` | 0건 |

### 대출 금리 검증

다음 기준을 검사했다.

```text
min_rate <= max_rate
min_rate <= average_rate <= max_rate
```

현재 결과:

- 평균금리 결측: 52건
- 평균금리 범위 이탈: 27건
- 최저금리 > 최고금리: 0건

평균금리 범위 이탈은 원본값을 수정하지 않고 `rate_range_violations`에 상세 기록했다.

평균금리와 최저·최고금리는 산정 기준이 다를 수 있으므로, 이상값을 삭제하거나 평균금리를 보정하지 않는다.

## 13. 생성된 정제 파일

```text
data/processed/
├── youth_policy.json
├── savings_products.json
├── savings_rates.json
├── deposit_products.json
├── deposit_rates.json
├── loan_products.json
├── loan_rates.json
└── data_quality_report.json
```

## 14. 현재 완료된 부분

- 원본 JSON 스키마 확인
- 전체 필드 매핑
- 문자열 공백 정리
- 빈 문자열·공백 결측 처리
- 숫자·금리·날짜 변환
- 다중값 배열화
- 특수값 처리
- 의미 기반 결측 상태 필드 적용
- 상품·금리정보 분리
- ID 중복 검증
- 대출 금리범위 검증
- 품질보고서 생성

## 15. 아직 추가로 필요한 부분

현재 전처리와 결측 처리는 완료되었지만, 다음은 별도 설계 단계다.

### 도메인·온톨로지 분류

정책 데이터와 금융상품 데이터를 다음과 같이 구분해야 한다.

```text
financial_product
public_policy
benefit_program
loan_support
savings_support
```

### 정책 코드명 매핑

현재 지역·직업·학교·혼인상태 코드는 원본 코드로 보존되어 있다. 사람이 읽는 코드명 매핑표는 별도 작성이 필요하다.

### 대출 유형 필드

현재 전세자금대출 파일은 전세대출이라는 파일 단위 정보가 있지만, 내부에 공통 `loan_type` 필드는 추가로 정리할 수 있다.

### 신청기간 구조화

현재 `application_period`는 기간 문자열이다. 향후 다음 필드로 분리할 수 있다.

```text
application_period_raw
application_start_date
application_end_date
application_period_status
```

### 금리조건 구조화

현재 우대조건·중도해지 조건은 설명문에 포함되어 있다. 향후 다음 필드로 분리할 수 있다.

```text
rate_conditions
preferential_rate
preferential_condition
early_termination_rate
early_termination_condition
```

## 16. 재실행 명령

```bash
python3 scripts/inspect_schema.py
python3 scripts/preprocess_data.py
python3 scripts/report_missing_values.py
python3 -m unittest discover -s tests
```

전처리 기준을 바꾸면 `field_mapping.csv`, `missing_value_policy.csv`, `derived_fields.csv`, 전처리 스크립트, 품질보고서를 함께 확인해야 한다.
