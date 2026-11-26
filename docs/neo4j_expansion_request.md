# Neo4j GRAPH·HYBRID 추가 검증 요청 목록

현재 확인된 경로는 4개뿐이다.

- `FinancialProduct -HAS_DISCLOSURE-> ProductDisclosure -HAS_RATE_OPTION-> RateOption -SUPPORTED_BY-> Evidence`
- `YouthPolicy -HAS_BENEFIT-> Benefit -SUPPORTED_BY-> Evidence`
- `YouthPolicy -SUPPORTED_BY-> Evidence` (신청기간, 1홉)

이 자료만으로는 GRAPH·HYBRID를 각각 40개 만들 수 없다. 아래 항목을 Neo4j 담당자에게 read-only 조회 결과 JSON으로 요청한다.

## 필요한 노드·관계 유형

1. 예금·적금: `FinancialProduct`, `Institution`, `ProductDisclosure`, `RateOption`, `Evidence`
2. 대출: `FinancialProduct`, `ProductDisclosure`, `RateOption`, `Evidence`와 상환방식·금리유형 연결
3. 청년정책: `YouthPolicy`, `Eligibility`, `Benefit`, `ApplicationPeriod`, `Region`, `Evidence`
4. 관계: 상품-기관, 상품-공시, 공시-금리옵션, 정책-자격, 정책-혜택, 정책-신청기간, 각 원문 노드-Evidence

관계 고유 ID가 없으면 모든 경로를 다음 객체로 반환한다.

```json
{
  "from_id": "...",
  "relation_type": "HAS_RATE_OPTION",
  "to_id": "..."
}
```

## 요청할 조회 결과

- 상품 유형별 최소 20개 이상의 검증된 경로
- 적금의 12·24·36개월 및 정액적립식·자유적립식이 구분된 경로
- 전세자금대출의 금리·상환방식·기간 근거 경로
- 청년정책의 자격·혜택·신청기간을 각각 Evidence까지 연결한 경로
- 각 행의 `product_id`, `disclosure_id`, `rate_option_id`, `policy_id`, `benefit_id`, Neo4j Evidence ID
- 원본 `source_file`, `source_locator`, 원본 값
- 조회 시점과 데이터 스냅샷 버전

## 담당자에게 요청할 read-only Cypher 예시

```cypher
MATCH (p:FinancialProduct)-[:HAS_DISCLOSURE]->(d:ProductDisclosure)
      -[:HAS_RATE_OPTION]->(r:RateOption)-[:SUPPORTED_BY]->(e:Evidence)
RETURN p.id AS product_id, d.id AS disclosure_id, r.id AS rate_option_id,
       type((r)-[:SUPPORTED_BY]->(e)) AS relation_type, e.id AS evidence_id,
       p.product_type AS product_type, r.term_months AS term_months;
```

```cypher
MATCH (p:YouthPolicy)-[:HAS_BENEFIT]->(b:Benefit)-[:SUPPORTED_BY]->(e:Evidence)
RETURN p.id AS policy_id, b.id AS benefit_id, e.id AS evidence_id,
       p.policy_name AS policy_name, b.support_content AS support_content;
```

```cypher
MATCH (p:YouthPolicy)-[r]->(n)-[:SUPPORTED_BY]->(e:Evidence)
WHERE type(r) IN ['HAS_ELIGIBILITY', 'HAS_BENEFIT', 'HAS_APPLICATION_PERIOD']
RETURN p.id AS policy_id, type(r) AS relation_type, n.id AS to_id,
       e.id AS evidence_id, n.source_locator AS source_locator;
```

실제 라벨·속성명이 다르면 담당자가 사용하는 스키마 이름으로 바꾸어 실행해야 한다. 이 프로젝트는 Neo4j DB에 직접 적재하거나 관계를 수정하지 않는다.
