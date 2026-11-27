// Run each statement separately in Neo4j Query.
// 1. Expected graph counts, relationship coverage, and linked Evidence values.
CALL () {
  MATCH (n)
  UNWIND labels(n) AS label
  WITH label, count(*) AS count
  RETURN collect({label: label, count: count}) AS actual_nodes_by_label
}
CALL () {
  MATCH ()-[r]->()
  WITH type(r) AS rel, count(*) AS count
  RETURN collect({rel: rel, count: count}) AS actual_relationships_by_type
}
CALL () {
  MATCH (p:FinancialProduct)-[:HAS_PRODUCT_TYPE]->(t:ProductType)
  WITH t.product_type_code AS product_type, count(DISTINCT p) AS count
  RETURN collect({type: product_type, count: count}) AS actual_products_by_type
}
CALL () {
  MATCH (p:FinancialProduct)
  WHERE NOT EXISTS { MATCH (p)-[:HAS_DISCLOSURE]->(:ProductDisclosure)-[:HAS_RATE_OPTION]->(:RateOption) }
     OR NOT EXISTS { MATCH (p)-[:HAS_PRODUCT_TYPE]->(:ProductType) }
     OR NOT EXISTS { MATCH (p)-[:OFFERED_BY]->(:Institution) }
  RETURN count(*) AS products_with_missing_paths
}
CALL () {
  MATCH (p:YouthPolicy)
  WHERE NOT EXISTS { MATCH (p)-[:HAS_BENEFIT]->(:Benefit)-[:SUPPORTED_BY]->(:Evidence) }
     OR NOT EXISTS { MATCH (p)-[:APPLIES_TO]->(:Region) }
  RETURN count(*) AS policies_with_missing_paths
}
CALL () {
  MATCH (e:Evidence)
  OPTIONAL MATCH (n)-[:SUPPORTED_BY]->(e)
  WITH e, collect(n) AS owners
  WITH e, owners, head(owners) AS n
  WHERE size(owners) <> 1
     OR e.source_file IS NULL OR e.source_field IS NULL OR e.source_locator IS NULL
     OR e.source_record_key IS NULL OR e.source_value IS NULL
     OR e.supports_property IS NULL
     OR NOT coalesce(
       CASE e.supports_property
         WHEN 'join_method' THEN n.join_method IS NOT NULL
         WHEN 'application_start_date' THEN toString(n.application_start_date) = e.source_value
         WHEN 'application_end_date' THEN toString(n.application_end_date) = e.source_value
         ELSE n[e.supports_property] = e.source_value
       END, false)
  RETURN count(*) AS invalid_evidence_links
}
WITH *, [{label: "FinancialProduct", count: 18}, {label: "ProductDisclosure", count: 18}, {label: "RateOption", count: 86}, {label: "ProductType", count: 3}, {label: "Institution", count: 16}, {label: "YouthPolicy", count: 12}, {label: "Benefit", count: 12}, {label: "Region", count: 51}, {label: "Evidence", count: 326}] AS expected_nodes,
     [{rel: "HAS_RATE_OPTION", count: 86}, {rel: "SUPPORTED_BY", count: 326}, {rel: "HAS_DISCLOSURE", count: 18}, {rel: "OFFERED_BY", count: 18}, {rel: "HAS_PRODUCT_TYPE", count: 18}, {rel: "APPLIES_TO", count: 78}, {rel: "OPERATED_BY", count: 9}, {rel: "HAS_BENEFIT", count: 12}] AS expected_relationships,
     [{type: "deposit", count: 7}, {type: "savings", count: 5}, {type: "loan", count: 6}] AS expected_products
RETURN size(actual_nodes_by_label) = size(expected_nodes) AND
       all(row IN expected_nodes WHERE row IN actual_nodes_by_label) AS node_counts_ok,
       size(actual_relationships_by_type) = size(expected_relationships) AND
       all(row IN expected_relationships WHERE row IN actual_relationships_by_type) AS relationship_counts_ok,
       size(actual_products_by_type) = size(expected_products) AND
       all(row IN expected_products WHERE row IN actual_products_by_type) AS product_type_counts_ok,
       reduce(total = 0, row IN actual_nodes_by_label | total + row.count) AS total_nodes,
       reduce(total = 0, row IN actual_relationships_by_type | total + row.count) AS total_relationships,
       products_with_missing_paths, policies_with_missing_paths, invalid_evidence_links;

// 2. Expected: one row, 202610, 12 months, base_rate 3.60, /records/3/base_rate.
MATCH (p:FinancialProduct {product_id: 'fss:deposit:0010001:WR0001B'})
  -[:HAS_DISCLOSURE]->(d:ProductDisclosure {disclosure_month: '202610'})
  -[:HAS_RATE_OPTION]->(r:RateOption {term_months: 12, rate_type_code: 'S'})
  -[:SUPPORTED_BY]->(e:Evidence {supports_property: 'base_rate'})
RETURN p.product_name, d.disclosure_month, r.rate_option_id, r.base_rate, e.source_file, e.source_locator;

// 3. Policy example, including the source for Benefit.description.
MATCH (p:YouthPolicy {policy_id: '20261111005400214817'})
  -[:HAS_BENEFIT]->(b:Benefit)-[:SUPPORTED_BY]->(e:Evidence {supports_property: 'description'})
MATCH (p)-[:APPLIES_TO]->(r:Region)
RETURN p.policy_name, p.application_start_date, p.application_end_date, r.region_code,
       b.description, e.source_file, e.source_locator;
