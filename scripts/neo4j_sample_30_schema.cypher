CREATE CONSTRAINT sample_product_id_unique IF NOT EXISTS
FOR (n:FinancialProduct) REQUIRE n.product_id IS UNIQUE;

CREATE CONSTRAINT sample_disclosure_id_unique IF NOT EXISTS
FOR (n:ProductDisclosure) REQUIRE n.disclosure_id IS UNIQUE;

CREATE CONSTRAINT sample_rate_option_id_unique IF NOT EXISTS
FOR (n:RateOption) REQUIRE n.rate_option_id IS UNIQUE;

CREATE CONSTRAINT sample_product_type_code_unique IF NOT EXISTS
FOR (n:ProductType) REQUIRE n.product_type_code IS UNIQUE;

CREATE CONSTRAINT sample_institution_id_unique IF NOT EXISTS
FOR (n:Institution) REQUIRE n.institution_id IS UNIQUE;

CREATE CONSTRAINT sample_policy_id_unique IF NOT EXISTS
FOR (n:YouthPolicy) REQUIRE n.policy_id IS UNIQUE;

CREATE CONSTRAINT sample_benefit_id_unique IF NOT EXISTS
FOR (n:Benefit) REQUIRE n.benefit_id IS UNIQUE;

CREATE CONSTRAINT sample_region_code_unique IF NOT EXISTS
FOR (n:Region) REQUIRE n.region_code IS UNIQUE;

CREATE CONSTRAINT sample_evidence_id_unique IF NOT EXISTS
FOR (n:Evidence) REQUIRE n.evidence_id IS UNIQUE;

CREATE RANGE INDEX financial_product_name_range IF NOT EXISTS
FOR (n:FinancialProduct) ON (n.product_name);
