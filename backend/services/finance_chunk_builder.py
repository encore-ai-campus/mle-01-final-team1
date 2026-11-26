"""금융 정제 JSON을 골든셋 근거용 evidence/chunk 행으로 변환한다."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from pathlib import Path
from typing import Any


PRODUCT_DATASETS = ("deposit", "savings", "loan")
REQUIRED_FILES = tuple(
    f"{dataset}_{kind}.json"
    for dataset in PRODUCT_DATASETS
    for kind in (("products", "rates") if dataset != "loan" else ("products", "rates"))
) + ("youth_policy.json",)
RAW_SOURCE_FILES = {
    "deposit": "정기예금.json",
    "savings": "적금.json",
    "loan": "전체자금 대출.json",
    "youth_policy": "청년정책.json",
}
CHUNK_ID_VERSIONS = ("v1", "stable-v2")


def _load_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"필수 데이터 파일이 없습니다: {path.name} ({path})")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("records"), list):
        return [row for row in payload["records"] if isinstance(row, dict)]
    return []


def _stable_id(prefix: str, *parts: Any) -> str:
    joined = "\x1f".join("" if part is None else str(part) for part in parts)
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest().upper()[:16]
    return f"{prefix}_{digest}"


def _display(value: Any) -> str:
    if value is None:
        return "미제공"
    if isinstance(value, list):
        return ", ".join(_display(item) for item in value) or "미제공"
    return str(value)


def _status(record: dict[str, Any], field: str) -> str | None:
    value = record.get(f"{field}_status")
    return str(value) if value is not None else None


def _rate_option_id(dataset: str, record: dict[str, Any]) -> str | None:
    product_id = record.get("product_id")
    disclosure_month = record.get("disclosure_month")
    if product_id is None or disclosure_month is None:
        return None
    if dataset == "loan":
        parts = (
            product_id,
            disclosure_month,
            record.get("rate_type_code"),
            record.get("repayment_type_code"),
        )
    else:
        parts = (product_id, disclosure_month, record.get("rate_type_code"), record.get("term_months"))
        if dataset == "savings":
            parts += (record.get("saving_type_code"),)
    if any(value is None for value in parts):
        return None
    return ":".join(str(value) for value in parts)


def _stable_rate_record_key(dataset: str, record: dict[str, Any]) -> str:
    """Return a position-independent identity for one processed rate record."""
    option_id = _rate_option_id(dataset, record)
    if option_id:
        return option_id
    values = [
        record.get("product_id"),
        record.get("disclosure_month"),
        record.get("rate_type_code"),
        record.get("repayment_type_code"),
        record.get("term_months"),
        record.get("saving_type_code"),
    ]
    return "|".join("" if value is None else str(value) for value in values)


def _raw_rate_key(
    dataset: str,
    product_id: Any,
    record: dict[str, Any],
) -> tuple[Any, ...]:
    if dataset == "loan":
        return (
            product_id,
            record.get("dcls_month"),
            record.get("lend_rate_type"),
            record.get("rpay_type"),
        )
    key = (
        product_id,
        record.get("dcls_month"),
        record.get("intr_rate_type"),
        int(record["save_trm"]) if record.get("save_trm") not in (None, "") else None,
    )
    if dataset == "savings":
        key += (record.get("rsrv_type"),)
    return key


def _raw_source_index(raw_dir: Path | None, dataset: str) -> dict[str, Any]:
    """Index raw JSON locators without changing the processed-source identity."""
    raw_file = RAW_SOURCE_FILES[dataset]
    result: dict[str, Any] = {
        "raw_file": raw_file,
        "status": "not_loaded" if raw_dir is None else "raw_file_missing",
        "products": {},
        "rates": {},
    }
    if raw_dir is None:
        return result
    path = Path(raw_dir) / raw_file
    if not path.exists():
        expected = unicodedata.normalize("NFC", raw_file)
        path = next(
            (
                candidate
                for candidate in Path(raw_dir).iterdir()
                if unicodedata.normalize("NFC", candidate.name) == expected
            ),
            path,
        )
    if not path.exists():
        return result
    payload = json.loads(path.read_text(encoding="utf-8"))
    items = payload.get("products" if dataset != "youth_policy" else "policies", [])
    result["status"] = "matched"
    if dataset == "youth_policy":
        for index, item in enumerate(items):
            result["products"][str(item.get("plcyNo"))] = {
                "file": raw_file,
                "path": f"/policies/{index}",
            }
        return result
    for product_index, product in enumerate(items):
        product_id = product.get("product_id")
        if product_id is None:
            continue
        result["products"][str(product_id)] = {
            "file": product.get("base_source_file") or raw_file,
            "path": f"/products/{product_index}",
        }
        for option_index, option in enumerate(product.get("rate_options", [])):
            data = option.get("data", {})
            key = _raw_rate_key(dataset, product_id, data)
            result["rates"][key] = {
                "file": option.get("source_file") or raw_file,
                "path": f"/products/{product_index}/rate_options/{option_index}/data",
            }
    return result


def _source_link(
    processed_file: str,
    processed_path: str,
    raw_index: dict[str, Any],
    raw_locator: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "processed": {"file": processed_file, "path": processed_path},
        "original": raw_locator,
        "original_file": raw_index.get("raw_file"),
        "status": "matched" if raw_locator else raw_index.get("status", "not_loaded"),
    }


def _emit(
    evidence_rows: list[dict[str, Any]],
    chunk_rows: list[dict[str, Any]],
    *,
    dataset: str,
    chunk_type: str,
    text: str,
    source_file: str,
    source_path: str,
    source_record_id: str,
    metadata: dict[str, Any],
    entity_ids: list[str] | None = None,
    relation_ids: list[str] | None = None,
    source_link: dict[str, Any] | None = None,
    stable_record_key: str | None = None,
    id_version: str = "v1",
) -> None:
    if id_version not in CHUNK_ID_VERSIONS:
        raise ValueError(f"지원하지 않는 chunk ID 버전입니다: {id_version}")
    record_key = stable_record_key or source_record_id
    if id_version == "v1":
        evidence_id = _stable_id(
            "EVD", dataset, source_file, source_path, source_record_id, chunk_type
        )
    else:
        evidence_id = _stable_id(
            "EVD", id_version, dataset, source_file, record_key, chunk_type
        )
    chunk_id = _stable_id("CHK", evidence_id, chunk_type)
    common = {
        "dataset": dataset,
        "chunk_type": chunk_type,
        "text": text.strip(),
        "source_file": source_file,
        "source_path": source_path,
        "source_record_id": source_record_id,
        "source_record_key": record_key,
        "chunk_id_version": id_version,
        "entity_ids": list(entity_ids or []),
        "relation_ids": list(relation_ids or []),
        "relation_verification": {
            "declared_relation_ids": list(relation_ids or []),
            "verified_relation_ids": [],
            "status": "not_verified",
        },
        "metadata": metadata,
        "source_link": source_link,
    }
    evidence_rows.append({"evidence_id": evidence_id, **common})
    chunk_rows.append({"chunk_id": chunk_id, "evidence_id": evidence_id, **common})


def _product_text(record: dict[str, Any], dataset: str) -> str:
    product_type = record.get("product_type") or {
        "deposit": "예금",
        "savings": "적금",
        "loan": "대출",
    }[dataset]
    return (
        f"금융상품 {record.get('product_name') or '상품명 미제공'}은 "
        f"{record.get('institution_name') or '금융기관 미제공'}의 "
        f"{product_type} 상품이다. "
        f"가입방법은 {_display(record.get('join_method'))}이며, "
        f"가입대상은 {_display(record.get('eligible_member'))}이다. "
        f"우대조건은 {_display(record.get('special_condition'))}이다. "
        f"기타 유의사항은 {_display(record.get('additional_note'))}이다."
    )


def _rate_text(
    dataset: str, record: dict[str, Any], product: dict[str, Any] | None
) -> str:
    product = product or {}
    product_id = record.get("product_id") or "상품 ID 미제공"
    product_name = product.get("product_name") or "상품명 미제공"
    institution_name = product.get("institution_name") or "금융기관 미제공"
    rate_type = _display(record.get("rate_type"))
    if dataset == "loan":
        return (
            f"금융상품 {product_name}({institution_name})의 상품 ID {product_id}, "
            f"{_display(record.get('disclosure_month'))} 공시 "
            f"{rate_type}·{_display(record.get('repayment_type'))} 금리옵션은 "
            f"{('기간 정보 미제공' if record.get('term_months') is None else str(record['term_months']) + '개월')} 기준 "
            f"최저금리 {_display(record.get('min_rate'))}%, "
            f"최고금리 {_display(record.get('max_rate'))}%, "
            f"평균금리 {_display(record.get('average_rate'))}이다. "
            f"평균금리 상태는 {_display(_status(record, 'average_rate'))}이다."
        )
    saving_type = ""
    if dataset == "savings":
        saving_type = f", {_display(record.get('saving_type'))}({_display(record.get('saving_type_code'))})"
    return (
        f"금융상품 {product_name}({institution_name})의 상품 ID {product_id}, "
        f"{_display(record.get('disclosure_month'))} 공시 "
        f"{_display(record.get('term_months'))}개월 {rate_type}{saving_type} 금리옵션의 기본금리는 "
        f"{_display(record.get('base_rate'))}%이고, 최고금리는 "
        f"{_display(record.get('max_rate'))}%이다. "
        f"최고금리 상태는 {_display(_status(record, 'max_rate'))}이다."
    )


def _build_product_chunks(
    processed_dir: Path,
    dataset: str,
    evidence_rows: list[dict[str, Any]],
    chunk_rows: list[dict[str, Any]],
    limit_per_dataset: int | None,
    raw_dir: Path | None,
    selected_product_ids: set[str] | None,
    id_version: str,
) -> None:
    product_file = f"{dataset}_products.json"
    rate_file = f"{dataset}_rates.json"
    products = _load_records(processed_dir / product_file)
    if selected_product_ids is not None:
        products = [
            row for row in products if str(row.get("product_id")) in selected_product_ids
        ]
    if limit_per_dataset is not None:
        products = products[:limit_per_dataset]
    product_ids = {str(row.get("product_id")) for row in products}
    raw_index = _raw_source_index(raw_dir, dataset)
    products_by_id = {
        str(row.get("product_id")): row for row in products if row.get("product_id")
    }

    for index, product in enumerate(products):
        product_id = str(product.get("product_id") or f"{dataset}-product-{index}")
        metadata = {
            "product_id": product_id,
            "institution_id": product.get("institution_id"),
            "institution_name": product.get("institution_name"),
            "product_name": product.get("product_name"),
            "product_type": product.get("product_type", dataset),
            "disclosure_month": product.get("disclosure_month"),
        }
        _emit(
            evidence_rows,
            chunk_rows,
            dataset=dataset,
            chunk_type="product_overview",
                text=_product_text(product, dataset),
            source_file=product_file,
            source_path=f"records[{index}]",
            source_record_id=product_id,
            metadata=metadata,
            entity_ids=[product_id],
            relation_ids=["OFFERED_BY", "HAS_DISCLOSURE"],
            source_link=_source_link(
                product_file,
                f"records[{index}]",
                raw_index,
                raw_index["products"].get(product_id),
            ),
            stable_record_key=product_id,
            id_version=id_version,
        )

    for index, rate in enumerate(_load_records(processed_dir / rate_file)):
        product_id = str(rate.get("product_id") or "")
        if selected_product_ids is not None and product_id not in product_ids:
            continue
        if selected_product_ids is None and product_ids and product_id not in product_ids:
            continue
        source_id = product_id or f"{dataset}-rate-{index}"
        metadata = {
            "product_id": product_id or None,
            "product_name": products_by_id.get(product_id, {}).get("product_name"),
            "institution_name": products_by_id.get(product_id, {}).get("institution_name"),
            "disclosure_id": (
                f"{product_id}:{rate.get('disclosure_month')}"
                if product_id and rate.get("disclosure_month") is not None
                else None
            ),
            "rate_option_id": _rate_option_id(dataset, rate),
            "disclosure_month": rate.get("disclosure_month"),
            "rate_type_code": rate.get("rate_type_code"),
            "term_months": rate.get("term_months"),
            "rate_type": rate.get("rate_type"),
            "saving_type_code": rate.get("saving_type_code"),
            "saving_type": rate.get("saving_type"),
            "repayment_type_code": rate.get("repayment_type_code"),
            "repayment_type": rate.get("repayment_type"),
            "base_rate": rate.get("base_rate"),
            "max_rate": rate.get("max_rate"),
            "min_rate": rate.get("min_rate"),
            "average_rate": rate.get("average_rate"),
            "max_rate_status": _status(rate, "max_rate"),
            "average_rate_status": _status(rate, "average_rate"),
            "rate_basis": rate.get("rate_basis"),
        }
        _emit(
            evidence_rows,
            chunk_rows,
            dataset=dataset,
            chunk_type="rate_option",
            text=_rate_text(dataset, rate, products_by_id.get(product_id)),
            source_file=rate_file,
            source_path=f"records[{index}]",
            source_record_id=source_id,
            metadata=metadata,
            entity_ids=[product_id] if product_id else [],
            relation_ids=["HAS_DISCLOSURE", "HAS_RATE_OPTION"],
            source_link=_source_link(
                rate_file,
                f"records[{index}]",
                raw_index,
                raw_index["rates"].get(
                    _raw_rate_key(
                        dataset,
                        product_id,
                        {
                            "dcls_month": rate.get("disclosure_month"),
                            "intr_rate_type": rate.get("rate_type_code"),
                            "save_trm": rate.get("term_months"),
                            "rsrv_type": rate.get("saving_type_code"),
                            "lend_rate_type": rate.get("rate_type_code"),
                            "rpay_type": rate.get("repayment_type_code"),
                        },
                    )
                ),
            ),
            stable_record_key=_stable_rate_record_key(dataset, rate),
            id_version=id_version,
        )


def _policy_text(record: dict[str, Any], chunk_type: str) -> str:
    policy_name = record.get("policy_name") or "정책명 미제공"
    if chunk_type == "policy_overview":
        return (
            f"청년정책 {policy_name}의 설명은 "
            f"{_display(record.get('policy_description'))}"
        )
    if chunk_type == "policy_eligibility":
        return (
            f"청년정책 {policy_name}의 대상 연령은 "
            f"{_display(record.get('min_age'))}세부터 "
            f"{_display(record.get('max_age'))}세까지이며, "
            f"소득 원천값은 최소 {_display(record.get('min_income'))} "
            f"({_display(_status(record, 'min_income'))}), "
            f"최대 {_display(record.get('max_income'))} "
            f"({_display(_status(record, 'max_income'))})이다. "
            f"소득 제한 여부는 원천 데이터 해석이 필요하다. "
            f"적용 지역은 {_display(record.get('region_code'))}이다."
        )
    if chunk_type == "policy_benefit":
        return (
            f"청년정책 {policy_name}의 지원내용은 "
            f"{_display(record.get('support_content'))}이며, "
            f"지원 규모는 {_display(record.get('support_scale_count'))}, "
            f"지원 방식은 {_display(record.get('policy_provision_method_code'))}이다."
        )
    return (
        f"청년정책 {policy_name}의 신청기간은 "
        f"{_display(record.get('application_start_date'))}부터 "
        f"{_display(record.get('application_end_date'))}까지이며, "
        f"기간 상태는 {_display(record.get('application_period_status'))}이다. "
        f"신청방법은 {_display(record.get('application_method'))}이고, "
        f"제출서류는 {_display(record.get('submission_document'))} "
        f"({_display(_status(record, 'submission_document'))})이다."
    )


def _build_policy_chunks(
    processed_dir: Path,
    evidence_rows: list[dict[str, Any]],
    chunk_rows: list[dict[str, Any]],
    limit_per_dataset: int | None,
    raw_dir: Path | None,
    selected_policy_ids: set[str] | None,
    id_version: str,
) -> None:
    source_file = "youth_policy.json"
    policies = _load_records(processed_dir / source_file)
    if selected_policy_ids is not None:
        policies = [
            row for row in policies if str(row.get("policy_id")) in selected_policy_ids
        ]
    raw_index = _raw_source_index(raw_dir, "youth_policy")
    if limit_per_dataset is not None:
        policies = policies[:limit_per_dataset]
    chunk_types = (
        "policy_overview",
        "policy_eligibility",
        "policy_benefit",
        "policy_application",
    )
    for index, policy in enumerate(policies):
        policy_id = str(policy.get("policy_id") or f"policy-{index}")
        for chunk_type in chunk_types:
            _emit(
                evidence_rows,
                chunk_rows,
                dataset="youth_policy",
                chunk_type=chunk_type,
                text=_policy_text(policy, chunk_type),
                source_file=source_file,
                source_path=f"records[{index}].{chunk_type}",
                source_record_id=policy_id,
                metadata={
                    "policy_id": policy_id,
                    "policy_name": policy.get("policy_name"),
                    "region_code": policy.get("region_code"),
                    "support_content": policy.get("support_content"),
                    "application_period": policy.get("application_period"),
                    "application_start_date": policy.get("application_start_date"),
                    "application_end_date": policy.get("application_end_date"),
                    "application_period_status": policy.get("application_period_status"),
                    "income_limit_interpretation": "unresolved"
                    if policy.get("min_income") in (None, 0)
                    or policy.get("max_income") in (None, 0)
                    else "raw_value_only",
                },
                entity_ids=[policy_id],
                relation_ids=["HAS_BENEFIT", "APPLIES_TO"],
                source_link=_source_link(
                    source_file,
                    f"records[{index}].{chunk_type}",
                    raw_index,
                    raw_index["products"].get(policy_id),
                ),
                stable_record_key=policy_id,
                id_version=id_version,
            )


def build_finance_chunks(
    processed_dir: Path,
    limit_per_dataset: int | None = None,
    raw_dir: Path | None = None,
    product_ids: set[str] | None = None,
    policy_ids: set[str] | None = None,
    id_version: str = "v1",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return deterministic evidence rows and retrieval chunks for a snapshot."""
    if id_version not in CHUNK_ID_VERSIONS:
        raise ValueError(f"지원하지 않는 chunk ID 버전입니다: {id_version}")
    processed_dir = Path(processed_dir)
    missing = [name for name in REQUIRED_FILES if not (processed_dir / name).exists()]
    if missing:
        raise FileNotFoundError(
            "필수 금융 데이터 파일이 없습니다: " + ", ".join(missing)
        )
    evidence_rows: list[dict[str, Any]] = []
    chunk_rows: list[dict[str, Any]] = []
    for dataset in PRODUCT_DATASETS:
        _build_product_chunks(
            processed_dir,
            dataset,
            evidence_rows,
            chunk_rows,
            limit_per_dataset,
            raw_dir,
            product_ids,
            id_version,
        )
    _build_policy_chunks(
        processed_dir,
        evidence_rows,
        chunk_rows,
        limit_per_dataset,
        raw_dir,
        policy_ids,
        id_version,
    )
    return evidence_rows, chunk_rows
