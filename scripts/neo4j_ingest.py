"""Load the validated sample or all processed catalog data into Neo4j."""

import argparse
import base64
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "neo4j_sample_30_load.cypher"
TYPES = {"deposit": "정기예금", "savings": "적금", "loan": "전세자금대출"}
PRODUCT_EVIDENCE = (
    "join_method", "special_condition", "min_subscription_amount_krw", "additional_note", "eligible_member",
)
RATE_EVIDENCE = ("base_rate", "min_rate", "max_rate", "average_rate")
POLICY_EVIDENCE = (
    "application_period", "application_start_date", "application_end_date",
    "application_method", "min_age", "max_age", "income_condition_note", "application_url",
)
DISCLOSURE_FIELDS = (
    "product_id", "disclosure_month", "disclosure_start_date", "disclosure_end_date", "disclosure_end_date_status",
)
RATE_FIELDS = (
    "rate_type_code", "rate_type", "saving_type_code", "saving_type", "repayment_type_code",
    "repayment_type", "term_months", "average_rate_status", *RATE_EVIDENCE,
)
KEYS = {
    "FinancialProduct": "product_id", "ProductDisclosure": "disclosure_id", "RateOption": "rate_option_id",
    "ProductType": "product_type_code", "Institution": "institution_id", "YouthPolicy": "policy_id",
    "Benefit": "benefit_id", "Region": "region_code", "Evidence": "evidence_id",
}


@dataclass
class ImportData:
    institutions: list
    products: list
    policies: list
    nodes: dict
    relationships: dict
    name_conflicts: list


def read_rows(folder, name):
    return json.loads((folder / f"{name}.json").read_text(encoding="utf-8-sig"))["records"]


def props(row, fields):
    return {key: row[key] for key in fields if row.get(key) is not None}


def require_id(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing or invalid {label}")
    return value


def verify_dates(values, *keys):
    for key in keys:
        if key in values:
            date.fromisoformat(values[key])


def prepare(folder, sample):
    template = TEMPLATE.read_text(encoding="utf-8")
    selected = set(re.findall(r'\{id: "([^"]+)", idx: \d+', template)) if sample else None
    if sample and len(selected) != 30:
        raise ValueError("Expected the 30 existing sample IDs")

    products, policies = [], []
    rates_seen, product_ids, policy_ids, region_codes = set(), set(), set(), set()
    institution_types, institution_names = {}, defaultdict(set)
    relationships = Counter()
    kinds_loaded = set()

    def remember_institution(identifier, name, kind):
        require_id(identifier, "institution_id")
        if institution_types.setdefault(identifier, kind) != kind:
            raise ValueError(f"Institution code belongs to two domains: {identifier}")
        if name:
            institution_names[identifier].add(name)

    for kind, type_name in TYPES.items():
        rates = defaultdict(list)
        for idx, row in enumerate(read_rows(folder, f"{kind}_rates")):
            product_id = require_id(row["product_id"], "rate product_id")
            if selected is not None and product_id not in selected:
                continue
            disclosure_id = f'{product_id}:{row["disclosure_month"]}'
            code = require_id(row["rate_type_code"], "rate_type_code")
            if kind == "loan":
                suffix = f'{code}:{require_id(row["repayment_type_code"], "repayment_type_code")}'
            else:
                term = row["term_months"]
                if type(term) is not int or term <= 0:
                    raise ValueError(f"Invalid rate term for {product_id}")
                suffix = f"{code}:{term}"
                if kind == "savings":
                    suffix += f':{require_id(row["saving_type_code"], "saving_type_code")}'
            rate_id = f"{disclosure_id}:{suffix}"
            if rate_id in rates_seen:
                raise ValueError(f"Duplicate rate option ID: {rate_id}")
            rates_seen.add(rate_id)
            values = props(row, RATE_FIELDS)
            values.update({key + "_basis": value for key, value in row["rate_basis"].items() if value is not None})
            relationships["SUPPORTED_BY"] += len(props(row, RATE_EVIDENCE))
            rates[disclosure_id].append({"rate_id": rate_id, "idx": idx, "props": values})

        for idx, row in enumerate(read_rows(folder, f"{kind}_products")):
            product_id = require_id(row["product_id"], "product_id")
            if selected is not None and product_id not in selected:
                continue
            if product_id in product_ids:
                raise ValueError(f"Duplicate product ID: {product_id}")
            product_ids.add(product_id)
            month = row["disclosure_month"]
            if not re.fullmatch(r"\d{6}", month):
                raise ValueError(f"Invalid disclosure month: {product_id}")
            disclosure_id = f"{product_id}:{month}"
            disclosure = props(row, DISCLOSURE_FIELDS)
            verify_dates(disclosure, "disclosure_start_date", "disclosure_end_date")
            values = props(row, ("product_name", "product_code", "join_restriction_code", *PRODUCT_EVIDENCE))
            channels = values.get("join_method")
            if channels is not None and (
                not isinstance(channels, list) or not all(isinstance(channel, str) for channel in channels)
            ):
                raise ValueError(f"Invalid join_method for {product_id}")
            institution_id = row["institution_id"]
            remember_institution(institution_id, row.get("institution_name"), "financial")
            products.append({
                "id": product_id, "idx": idx, "dataset": kind, "institution_id": institution_id,
                "disclosure_id": disclosure_id, "product_type_name": type_name,
                "props": values, "join_method_json": json.dumps(channels, ensure_ascii=False, separators=(",", ":")),
                "disclosure": disclosure, "rates": rates.pop(disclosure_id, []),
            })
            kinds_loaded.add(kind)
            relationships["SUPPORTED_BY"] += len(props(row, PRODUCT_EVIDENCE))
        if rates:
            raise ValueError(f"Rate options without matching disclosures: {kind}, {len(rates)}")

    for idx, row in enumerate(read_rows(folder, "youth_policy")):
        policy_id = require_id(row["policy_id"], "policy_id")
        if selected is not None and policy_id not in selected:
            continue
        if policy_id in policy_ids:
            raise ValueError(f"Duplicate policy ID: {policy_id}")
        policy_ids.add(policy_id)
        values = props(row, ("policy_name", "policy_description", "application_period_status", "age_limit_flag", *POLICY_EVIDENCE))
        verify_dates(values, "application_start_date", "application_end_date")
        codes = list(dict.fromkeys(require_id(code, "region_code") for code in row.get("region_code") or []))
        region_codes.update(codes)
        relationships["APPLIES_TO"] += len(codes)
        institution_id = row.get("operating_institution_id") or None
        if institution_id is not None:
            remember_institution(institution_id, row.get("operating_institution"), "policy_operator")
            relationships["OPERATED_BY"] += 1
        if not row.get("support_content"):
            raise ValueError(f"Missing support_content for {policy_id}")
        benefit = {"benefit_index": 1, "description": row["support_content"], "delivery_method": row.get("policy_provision_method_code")}
        policies.append({
            "id": policy_id, "idx": idx, "institution_id": institution_id, "regions": codes,
            "props": values, "benefit_id": f"{policy_id}:001", "benefit": benefit,
        })
        relationships["SUPPORTED_BY"] += len(props(row, POLICY_EVIDENCE)) + len(props(benefit, ("description", "delivery_method")))

    if selected is not None and product_ids | policy_ids != selected:
        raise ValueError("The processed inputs are missing one or more existing sample IDs")

    institutions = [
        {"institution_id": identifier, "institution_type": kind,
         "institution_name": next(iter(institution_names[identifier])) if len(institution_names[identifier]) == 1 else None}
        for identifier, kind in institution_types.items()
    ]
    nodes = {
        "FinancialProduct": len(products), "ProductDisclosure": len(products), "RateOption": len(rates_seen),
        "ProductType": len(kinds_loaded), "Institution": len(institutions), "YouthPolicy": len(policies),
        "Benefit": len(policies), "Region": len(region_codes), "Evidence": relationships["SUPPORTED_BY"],
    }
    relationships.update({
        "HAS_DISCLOSURE": len(products), "HAS_PRODUCT_TYPE": len(products), "OFFERED_BY": len(products),
        "HAS_RATE_OPTION": len(rates_seen), "HAS_BENEFIT": len(policies),
    })
    conflicts = sorted(identifier for identifier, names in institution_names.items() if len(names) > 1)
    return ImportData(institutions, products, policies, nodes, dict(relationships), conflicts)


def load_queries():
    template = TEMPLATE.read_text(encoding="utf-8")
    # Keep the verified Cypher bodies. Only substitute the three literal input tables.
    bodies = re.findall(r"^CALL \(\) \{\n  UNWIND \[\n.*?^  \] AS row\n(.*?)^\}\n", template, re.M | re.S)
    if len(bodies) != 3:
        raise ValueError("Unexpected sample Cypher layout")
    return ["UNWIND $rows AS row\n" + body for body in bodies]


class QueryAPI:
    def __init__(self, file):
        settings = dict(os.environ)
        if file is not None:
            for line in file.read_text(encoding="utf-8-sig").splitlines():
                key, sep, value = line.partition("=")
                if sep and key.strip() in {"NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD", "NEO4J_DATABASE"}:
                    settings[key.strip()] = value.strip().strip('"\'')
        uri = urlsplit(settings["NEO4J_URI"])
        db = settings.get("NEO4J_DATABASE") or "neo4j"
        if not uri.hostname or uri.username or uri.password or uri.path not in {"", "/"} or uri.query or uri.fragment:
            raise ValueError("Unexpected Neo4j URI format")
        self.headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if uri.scheme == "neo4j+s" and uri.hostname.endswith(".databases.neo4j.io") and uri.port is None:
            secret = f'{settings["NEO4J_USERNAME"]}:{settings["NEO4J_PASSWORD"]}'
            self.headers["Authorization"] = "Basic " + base64.b64encode(secret.encode()).decode("ascii")
            self.endpoint = f"https://{uri.hostname}/db/{quote(db, safe='')}/query/v2"
        elif uri.scheme == "http" and uri.hostname in {"localhost", "127.0.0.1"} and uri.port:
            self.endpoint = f"http://{uri.hostname}:{uri.port}/db/{quote(db, safe='')}/query/v2"
        else:
            raise ValueError("Use a Neo4j Aura URI or a loopback HTTP endpoint for local verification")
        self.target = f"{uri.hostname}/{db}"
        self.bookmarks = []

    def query(self, statement, parameters=None):
        payload = {"statement": statement, "parameters": parameters or {}, "includeCounters": True}
        if self.bookmarks:
            payload["bookmarks"] = self.bookmarks
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        result = self.send(body)
        if result.get("errors"):
            error = result["errors"][0]
            raise RuntimeError(f'{error.get("code")}: {error.get("message")}')
        self.bookmarks = result.get("bookmarks", self.bookmarks)
        return result.get("data", {}).get("values", []), result.get("counters", {})

    def send(self, body):
        if os.name == "nt" and self.endpoint.startswith("https:"):
            # Use Windows' native trust store through Schannel. Keep credentials off the command line.
            escape = lambda value: json.dumps(value, ensure_ascii=False)
            config = [
                f"url = {escape(self.endpoint)}",
                *(f"header = {escape(key + ': ' + value)}" for key, value in self.headers.items()),
                f"data-binary = {escape(body)}",
            ]
            result = subprocess.run(
                ["curl.exe", "--silent", "--show-error", "--fail-with-body", "--max-time", "90", "--config", "-"],
                input="\n".join(config).encode("utf-8"),
                capture_output=True,
                timeout=95,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode:
                raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip() or f"curl error {result.returncode}")
            return json.loads(result.stdout)
        request = Request(self.endpoint, data=body.encode("utf-8"), headers=self.headers)
        try:
            with urlopen(request, timeout=90) as response:
                return json.load(response)
        except HTTPError as exc:
            raise RuntimeError(f"Neo4j Query API returned HTTP {exc.code}") from None
        except URLError as exc:
            raise RuntimeError(f"Cannot connect to {self.target}: {exc.reason}") from None


def graph_counts(api):
    values, _ = api.query("""
        CALL () { MATCH (n) UNWIND labels(n) AS k WITH k, count(*) AS c RETURN collect([k, c]) AS nodes }
        CALL () { MATCH ()-[r]->() WITH type(r) AS k, count(*) AS c RETURN collect([k, c]) AS relationships }
        RETURN nodes, relationships
    """)
    return dict(values[0][0]), dict(values[0][1])


def run(api, data, batch_size):
    schema, _ = api.query("SHOW CONSTRAINTS YIELD type, labelsOrTypes, properties RETURN type, labelsOrTypes, properties")
    unique = {
        (labels[0], props_[0]) for kind, labels, props_ in schema
        if kind in {"UNIQUENESS", "NODE_PROPERTY_UNIQUENESS"} and len(labels) == len(props_) == 1
    }
    missing = set(KEYS.items()) - unique
    if missing:
        raise RuntimeError(f"Missing required uniqueness constraints: {sorted(missing)}")

    totals = Counter()
    groups = (data.institutions, data.products, data.policies)
    for kind, rows, query in zip(("institutions", "products", "policies"), groups, load_queries(), strict=True):
        for offset in range(0, len(rows), batch_size):
            batch = rows[offset:offset + batch_size]
            values, counters = api.query(query, {"rows": batch})
            if values[0][0] != len(batch):
                raise RuntimeError(f"Incomplete {kind} batch starting at row {offset}")
            totals.update({key: counters.get(key, 0) for key in ("nodesCreated", "relationshipsCreated")})
            if offset + len(batch) == len(rows) or (offset // batch_size + 1) % 10 == 0:
                print(f"{kind}: {offset + len(batch)}/{len(rows)}", flush=True)
    return totals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--credentials", type=Path)
    parser.add_argument("--sample", action="store_true", help="Use the existing sample IDs")
    parser.add_argument("--batch-size", type=int, default=60)
    parser.add_argument("--mode", choices=("dry-run", "probe", "load", "verify"), default="dry-run")
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size must be positive")
    data = prepare(args.data_dir, args.sample)
    print(json.dumps({
        "expected_nodes": data.nodes, "expected_relationships": data.relationships,
        "operator_name_conflicts": len(data.name_conflicts),
    }, ensure_ascii=False), flush=True)
    if args.mode == "dry-run":
        return
    api = QueryAPI(args.credentials)
    print(f"target: {api.target}", flush=True)
    if args.mode == "load":
        print("write counters:", json.dumps(run(api, data, args.batch_size)), flush=True)
    nodes, relationships = graph_counts(api)
    print(json.dumps({
        "nodes": nodes, "relationships": relationships,
        "total_nodes": sum(nodes.values()), "total_relationships": sum(relationships.values()),
    }), flush=True)
    if args.mode != "probe" and (nodes != data.nodes or relationships != data.relationships):
        raise RuntimeError("Graph counts differ from the processed data for this scope")


if __name__ == "__main__":
    try:
        main()
    except (KeyError, OSError, ValueError, RuntimeError) as exc:
        sys.exit(str(exc))
