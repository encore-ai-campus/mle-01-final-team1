# Chunk ID 안정성 점검 결과

점검 대상은 전체 `data/evaluation/chunks/v1/chunks.jsonl`과 Neo4j 대조용 부분 샘플이다. 같은 원천 레코드는 `rate_option_id`, `policy_id`, `product_id`로 매칭했다.

## 확인 결과

- 비교 레코드: 13개
- 원천 레코드 매칭: 13/13
- 텍스트 동일: 13/13
- `records[index]` 변경: 5건
- 기존 v1 Chunk ID 동일: 8/13
- 기존 v1 Chunk ID 변경: 5/13
- stable-v2 동일 ID: 13/13

정책 샘플은 부분 데이터에서 `records[0]`, 전체 데이터에서 `records[33]`이었습니다. 기존 v1 ID가 `source_path`에 포함되어 있어 ID가 달라졌습니다. iM함께예금 상품 개요도 부분 샘플과 전체 데이터의 상품 위치가 달라 기존 ID가 달라졌습니다.

## 적용한 방식

- 기존 `v1` ID 생성 방식은 변경하지 않았습니다. 기존 청크·골든셋 매핑을 깨뜨리지 않기 위한 선택입니다.
- `stable-v2`를 추가했습니다. 상품은 `product_id`, 정책은 `policy_id`, 금리옵션은 `rate_option_id`를 ID 생성 기준으로 사용합니다.
- `source_path`는 현재 매니페스트의 위치 정보로 계속 보존하고, ID 생성에는 stable key를 사용합니다.
- `scripts/audit_chunk_id_stability.py`로 전체·부분 샘플을 비교할 수 있습니다.
- `scripts/remap_neo4j_mapping.py`로 기존 Neo4j 경로의 관계·Evidence ID는 그대로 두고, 대상 청크의 Chunk/EVD ID만 원천 ID 기준으로 다시 연결합니다.

현재는 기존 v1을 유지하고, 새 청킹 실험이나 새 골든셋 후보부터 stable-v2를 선택하는 방식이 안전합니다. v2를 기본값으로 바꾸는 것은 기존 골든셋의 Chunk ID 매핑을 재검증한 뒤 별도 버전으로 진행해야 합니다.
