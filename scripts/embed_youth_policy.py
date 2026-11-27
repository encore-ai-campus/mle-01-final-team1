#!/usr/bin/env python3
"""청년정책의 이름·설명·지원 내용을 통합 임베딩하여 별도 JSON에 저장한다.

scripts/에 배치하며, 실행 위치와 무관하게 프로젝트의 data/processed와
backend/.env를 참조한다. 원본 JSON은 수정하지 않는다. 배치별 SQLite 캐시로
중단 후 재실행 및 정책 변경 시 필요한 텍스트만 다시 임베딩할 수 있다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any, Iterator

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODEL = "text-embedding-3-small"
DIMENSIONS = 1536
BATCH_SIZE = 64
PRICE_PER_MILLION_TOKENS = 0.02
FIELDS = (
    ("policy_name", "정책명"),
    ("policy_description", "정책 설명"),
    ("support_content", "지원 내용"),
)
EMBEDDING_FIELDS = ("semantic_embedding", "embedding_model", "embedding_hash")


def build_embedding_text(record: dict[str, Any]) -> str:
    """정해진 세 필드만 결합하고 원문 내부의 숫자·부정 표현은 보존한다."""
    parts = []
    for key, label in FIELDS:
        value = record.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError(f"{record.get('policy_id')}: {key} 값이 문자열이 아닙니다.")
        if value.strip():
            parts.append(f"{label}: {value.strip()}")
    return "\n".join(parts)


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_payload(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8-sig") as f:
        payload = json.load(f)
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        raise ValueError('입력 JSON은 {"records": [...]} 형식이어야 합니다.')
    if not payload["records"]:
        raise ValueError("정책 레코드가 없습니다.")
    seen = set()
    for record in payload["records"]:
        if not isinstance(record, dict):
            raise ValueError("정책 레코드가 JSON 객체가 아닙니다.")
        pid = record.get("policy_id")
        if not isinstance(pid, str) or not pid.strip() or pid in seen:
            raise ValueError(f"누락되었거나 중복된 policy_id: {pid!r}")
        seen.add(pid)
    return payload


def prepare(records: list[dict[str, Any]]) -> tuple[list[str | None], dict[str, str]]:
    """각 레코드의 해시와 중복 제거된 입력을 생성한다."""
    hashes = []
    unique = {}
    for record in records:
        text = build_embedding_text(record)
        if not text:
            hashes.append(None)
            continue
        digest = text_hash(text)
        hashes.append(digest)
        unique.setdefault(digest, text)
    return hashes, unique


def input_size(text: str) -> int:
    # UTF-8 바이트 수는 일반 BPE 토큰 수에 대한 보수적인 상한으로 사용한다.
    # 현재 데이터는 이 상한만으로도 모든 입력이 제한 이내인지 확인 가능하다.
    size = len(text.encode("utf-8"))
    if size > 8192:
        raise ValueError(
            f"입력의 UTF-8 크기({size:,} bytes)가 보수적 한도 8,192를 초과했습니다. "
            "해당 정책의 실제 토큰 수를 확인 후 처리 방식을 결정하세요. "
            "원문을 임의로 자르지 않습니다."
        )
    return size


def make_batches(items: list[tuple[str, str]], batch_size: int) -> Iterator[list[tuple[str, str]]]:
    """개별 입력 및 요청 전체 크기를 제한하며 원문을 임의로 자르지 않는다."""
    batch = []
    total = 0
    for item in items:
        size = input_size(item[1])
        if batch and (len(batch) >= batch_size or total + size > 300_000):
            yield batch
            batch, total = [], 0
        batch.append(item)
        total += size
    if batch:
        yield batch


def valid_vector(vector: Any) -> bool:
    return (
        isinstance(vector, list)
        and len(vector) == DIMENSIONS
        and all(type(x) in (float, int) and math.isfinite(x) for x in vector)
        and any(x != 0 for x in vector)
    )


def open_cache(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS embeddings (
            model TEXT NOT NULL,
            dimensions INTEGER NOT NULL,
            text_hash TEXT NOT NULL,
            vector_json TEXT NOT NULL,
            PRIMARY KEY (model, dimensions, text_hash)
        )
    """)
    conn.commit()
    return conn


def get_cached_vector(conn: sqlite3.Connection, digest: str) -> list[float] | None:
    row = conn.execute(
        "SELECT vector_json FROM embeddings WHERE model=? AND dimensions=? AND text_hash=?",
        (MODEL, DIMENSIONS, digest),
    ).fetchone()
    if row is None:
        return None
    try:
        vector = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    return vector if valid_vector(vector) else None


def make_client(root: Path) -> Any:
    """기존 백엔드 환경변수를 사용하며 실제 API 호출이 필요한 경우에만 로드한다."""
    try:
        from dotenv import load_dotenv
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError(
            "backend 가상환경에 openai, python-dotenv를 설치해 주세요. "
            "backend 폴더에서 uv add openai"
        ) from exc
    load_dotenv(root / "backend" / ".env", override=False)
    load_dotenv(root / ".env", override=False)
    if not os.getenv("OPENAI_API_KEY", "").strip():
        raise RuntimeError("backend/.env 또는 환경변수에 OPENAI_API_KEY가 필요합니다.")
    return OpenAI(max_retries=4, timeout=90.0)


def request_batch(client: Any, batch: list[tuple[str, str]]) -> tuple[list[tuple[str, int, str, str]], int]:
    response = client.embeddings.create(
        model=MODEL,
        dimensions=DIMENSIONS,
        encoding_format="float",
        input=[text for _, text in batch],
    )
    by_index = {item.index: item.embedding for item in response.data}
    if len(response.data) != len(batch) or set(by_index) != set(range(len(batch))):
        raise RuntimeError("API 응답의 벡터 개수 또는 인덱스가 입력과 일치하지 않습니다.")
    rows = []
    for index, (digest, _) in enumerate(batch):
        vector = by_index[index]
        if not valid_vector(vector):
            raise RuntimeError(f"유효하지 않은 임베딩 응답: {digest[:12]}")
        rows.append((MODEL, DIMENSIONS, digest, json.dumps(vector, separators=(",", ":"), allow_nan=False)))
    return rows, int(response.usage.total_tokens)


def fill_cache(conn: sqlite3.Connection, pending: list[tuple[str, str]], batch_size: int, client: Any) -> int:
    """배치가 성공할 때마다 저장하여 중단된 작업을 이어서 실행할 수 있다."""
    done = tokens = 0
    for batch in make_batches(pending, batch_size):
        rows, used = request_batch(client, batch)
        with conn:
            conn.executemany("INSERT OR REPLACE INTO embeddings VALUES (?, ?, ?, ?)", rows)
        done += len(batch)
        tokens += used
        print(f"[임베딩] 신규 {done:,}/{len(pending):,}개 | 누적 API 토큰 {tokens:,}", flush=True)
    return tokens


def write_output(output: Path, payload: dict[str, Any], hashes: list[str | None], conn: sqlite3.Connection) -> None:
    """전체 기존 속성을 유지하고, 파일 작성이 끝난 뒤 원자적으로 결과를 교체한다."""
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=output.parent,
            prefix=f".{output.name}.", suffix=".tmp", delete=False,
        ) as f:
            temporary = Path(f.name)
            f.write("{\n")
            for key_index, (key, value) in enumerate(payload.items()):
                if key_index:
                    f.write(",\n")
                f.write(json.dumps(key, ensure_ascii=False) + ": ")
                if key != "records":
                    json.dump(value, f, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                    continue
                f.write("[\n")
                for index, (record, digest) in enumerate(zip(value, hashes, strict=True)):
                    augmented = {k: v for k, v in record.items() if k not in EMBEDDING_FIELDS}
                    if digest is not None:
                        vector = get_cached_vector(conn, digest)
                        if vector is None:
                            raise RuntimeError(f"캐시 벡터 누락: {record['policy_id']}")
                        augmented.update(semantic_embedding=vector, embedding_model=MODEL, embedding_hash=digest)
                    if index:
                        f.write(",\n")
                    json.dump(augmented, f, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                f.write("\n]")
            f.write("\n}\n")
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run(input_path: Path, output_path: Path, cache_path: Path, *, root: Path = PROJECT_ROOT,
        batch_size: int = BATCH_SIZE, limit: int | None = None, dry_run: bool = False,
        client: Any = None) -> None:
    if not 1 <= batch_size <= 2048:
        raise ValueError("배치 크기는 1~2048이어야 합니다.")
    if limit is not None and limit <= 0:
        raise ValueError("--limit은 양수여야 합니다.")
    if len({input_path.resolve(), output_path.resolve(), cache_path.resolve()}) != 3:
        raise ValueError("입력·출력·캐시 경로는 서로 달라야 합니다.")

    payload = load_payload(input_path)
    if limit is not None:
        payload["records"] = payload["records"][:limit]
    hashes, unique = prepare(payload["records"])
    # 모든 입력을 API 호출 전에 검사한다. 실패 시 일부만 과금되는 상황을 방지한다.
    sizes = [input_size(text) for text in unique.values()]
    nonempty = sum(digest is not None for digest in hashes)
    print(f"입력: {input_path}\n출력: {output_path}\n모델: {MODEL} / {DIMENSIONS}차원")
    print(f"정책 {len(hashes):,}개 | 대상 {nonempty:,}개 | 고유 텍스트 {len(unique):,}개 | 빈 텍스트 {len(hashes) - nonempty:,}개")
    print(f"중복 제거: {nonempty - len(unique):,}개 | 고유 입력 UTF-8 크기: {sum(sizes):,} bytes | 최대: {max(sizes, default=0):,} bytes")
    if dry_run:
        print("[DRY RUN] API 호출·파일 생성 없이 입력 검사만 완료했습니다.")
        if unique:
            print("\n[첫 번째 임베딩 입력 예시]\n" + next(iter(unique.values())))
        return

    with closing(open_cache(cache_path)) as conn:
        pending = [(digest, text) for digest, text in unique.items() if get_cached_vector(conn, digest) is None]
        print(f"캐시 재사용: {len(unique) - len(pending):,}개 | 신규 API 입력: {len(pending):,}개")
        # 토큰 수가 아니라 보수적인 UTF-8 바이트 상한을 사용한다.
        cost_upper = sum(input_size(text) for _, text in pending) * PRICE_PER_MILLION_TOKENS / 1_000_000
        print(f"신규 임베딩 입력 비용 상한 추정: ${cost_upper:.4f} (재시도 제외)")
        tokens = 0
        if pending:
            if client is None:
                with make_client(root) as sdk:
                    tokens = fill_cache(conn, pending, batch_size, sdk)
            else:
                tokens = fill_cache(conn, pending, batch_size, client)
        write_output(output_path, payload, hashes, conn)
    print(f"[완료] {output_path} ({output_path.stat().st_size / 1_000_000:.2f} MB)")
    print(f"이번 실행 API 사용량: {tokens:,} tokens | 성공 응답 기준 비용 추정: ${tokens * PRICE_PER_MILLION_TOKENS / 1_000_000:.4f}")
    print(f"캐시: {cache_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=PROJECT_ROOT / "data" / "processed" / "youth_policy.json", help="원본 정책 JSON")
    parser.add_argument("--output", type=Path, help="결과 JSON (기본: 원본과 같은 폴더의 youth_policy_embedded.json)")
    parser.add_argument("--cache", type=Path, help="SQLite 캐시 (기본: 원본과 같은 폴더의 youth_policy_embeddings.db)")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="API 요청당 최대 입력 수 (기본 64)")
    parser.add_argument("--limit", type=int, help="첫 N개만 테스트 (기본 출력 파일명에 _sample 추가)")
    parser.add_argument("--dry-run", action="store_true", help="API 호출·파일 생성 없이 입력 검사")
    args = parser.parse_args()
    input_path = args.input.resolve()
    name = "youth_policy_embedded_sample.json" if args.limit is not None else "youth_policy_embedded.json"
    output_path = (args.output or input_path.with_name(name)).resolve()
    cache_path = (args.cache or input_path.with_name("youth_policy_embeddings.db")).resolve()
    try:
        run(input_path, output_path, cache_path, batch_size=args.batch_size, limit=args.limit, dry_run=args.dry_run)
    except KeyboardInterrupt:
        print("\n중단했습니다. 저장된 배치 캐시를 이용해 재실행할 수 있습니다.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"오류: {exc}", file=sys.stderr)
        print("성공한 배치는 캐시에 보존됩니다. 원인 해결 후 동일 명령을 재실행하세요.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
