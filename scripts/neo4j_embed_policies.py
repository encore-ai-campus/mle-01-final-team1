"""Attach a precomputed policy embedding file to existing YouthPolicy nodes."""

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import re
import sys
import time

from neo4j_ingest import QueryAPI


MODEL = "text-embedding-3-small"
DIMENSIONS = 1536
INDEX_NAME = "youth_policy_semantic_embedding"
BATCH_SIZE = 25

WRITE = """
UNWIND $rows AS row
MATCH (p:YouthPolicy {policy_id: row.id})
CALL db.create.setNodeVectorProperty(p, 'semantic_embedding', row.vector)
SET p.embedding_model = $model, p.embedding_hash = row.hash
RETURN count(p) AS updated
"""

CREATE_INDEX = """
CREATE VECTOR INDEX youth_policy_semantic_embedding IF NOT EXISTS
FOR (p:YouthPolicy) ON (p.semantic_embedding)
OPTIONS {indexConfig: {
  `vector.dimensions`: 1536,
  `vector.similarity_function`: 'cosine'
}}
"""


def read_embeddings(path):
    source = json.loads(path.read_text(encoding="utf-8-sig"))["records"]
    rows = []
    seen = set()
    for row in source:
        policy_id = row["policy_id"]
        if not isinstance(policy_id, str) or not policy_id.strip() or policy_id in seen:
            raise ValueError("Missing or duplicate policy_id in embedding file")
        seen.add(policy_id)
        vector = row["semantic_embedding"]
        fingerprint = row["embedding_hash"]
        if row["embedding_model"] != MODEL or not re.fullmatch(r"[0-9a-f]{64}", fingerprint or ""):
            raise ValueError(f"Unexpected model or missing embedding hash for {policy_id}")
        if not isinstance(vector, list) or len(vector) != DIMENSIONS or not any(vector):
            raise ValueError(f"Invalid embedding length or zero vector for {policy_id}")
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in vector):
            raise ValueError(f"Invalid vector value for {policy_id}")
        rows.append({"id": policy_id, "vector": vector, "hash": fingerprint})
    if not rows:
        raise ValueError("The embedding file contains no policy records")
    return rows


def pending_embeddings(api, rows):
    stored, _ = api.query("""
        MATCH (p:YouthPolicy)
        RETURN p.policy_id, p.embedding_model, p.embedding_hash, size(p.semantic_embedding)
    """)
    metadata = {identifier: (model, fingerprint, dimensions) for identifier, model, fingerprint, dimensions in stored}
    requested_ids = {r["id"] for r in rows}
    if len(metadata) != len(stored) or None in metadata:
        raise RuntimeError("The database contains duplicate or missing policy IDs")
    missing = requested_ids - metadata.keys()
    extra = metadata.keys() - requested_ids
    if missing or extra:
        raise RuntimeError(f"Database policy IDs differ from the file: missing={len(missing)}, extra={len(extra)}")

    pending = []
    for row in rows:
        model, fingerprint, dimensions = metadata[row["id"]]
        if dimensions is None:
            pending.append(row)
        elif (model, fingerprint, dimensions) != (MODEL, row["hash"], DIMENSIONS):
            raise RuntimeError(f"Existing embedding metadata differs for {row['id']}")
    return pending


def find_index(api):
    indexes, _ = api.query("SHOW VECTOR INDEXES YIELD name, state, labelsOrTypes, properties, options")
    candidates = [r for r in indexes if r[0] == INDEX_NAME or (r[2], r[3]) == (["YouthPolicy"], ["semantic_embedding"])]
    if len(candidates) > 1:
        raise RuntimeError("Multiple vector indexes match the intended scope")
    if not candidates:
        return None
    name, state, labels, fields, options = candidates[0]
    config = options.get("indexConfig", {})
    if ((labels, fields) != (["YouthPolicy"], ["semantic_embedding"])
            or config.get("vector.dimensions") != DIMENSIONS
            or str(config.get("vector.similarity_function")).lower() != "cosine"):
        raise RuntimeError(f"Existing vector index {name} is incompatible")
    if state == "FAILED":
        raise RuntimeError(f"Existing vector index {name} has failed")
    return name, state


def attach_embeddings(api, rows, pending, index):
    for start in range(0, len(pending), BATCH_SIZE):
        batch = pending[start:start + BATCH_SIZE]
        values, _ = api.query(WRITE, {"rows": batch, "model": MODEL})
        if values != [[len(batch)]]:
            raise RuntimeError(f"Unexpected write count in embedding batch starting at {start}")
        complete = start + len(batch)
        if complete == len(pending) or complete % (BATCH_SIZE * 10) == 0:
            print(f"embeddings written: {complete}/{len(pending)}", flush=True)

    if pending_embeddings(api, rows):
        raise RuntimeError("Some embeddings are still missing after the write")
    if index is None:
        api.query(CREATE_INDEX)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        index = find_index(api)
        if index is not None and index[1] == "ONLINE":
            return index[0]
        time.sleep(2)
    raise RuntimeError("Embeddings are present but the vector index is not ONLINE yet")


def verify_search(api, rows, index_name):
    fingerprints = Counter(row["hash"] for row in rows)
    example = next((r for r in rows if fingerprints[r["hash"]] == 1), rows[0])
    neighbors, _ = api.query("""
        CALL db.index.vector.queryNodes($name, 10, $vector)
        YIELD node, score RETURN node.policy_id, score
    """, {"name": index_name, "vector": example["vector"]})
    if not any(identifier == example["id"] and score > 0.99 for identifier, score in neighbors):
        raise RuntimeError("An indexed policy was not found by its stored embedding")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--credentials", type=Path)
    parser.add_argument("--mode", choices=("dry-run", "check", "load"), default="dry-run")
    args = parser.parse_args()
    rows = read_embeddings(args.input)
    print(f"Input: {len(rows)} policies, {MODEL}, {DIMENSIONS} dimensions", flush=True)
    if args.mode == "dry-run":
        return

    api = QueryAPI(args.credentials)
    print(f"Target: {api.target}", flush=True)
    pending = pending_embeddings(api, rows)
    index = find_index(api)
    print(f"Matched: {len(rows)}, already embedded: {len(rows) - len(pending)}, pending: {len(pending)}", flush=True)
    print(f"Index: {index if index is not None else 'absent'}", flush=True)
    if args.mode == "load":
        name = attach_embeddings(api, rows, pending, index)
        verify_search(api, rows, name)
        print(f"Verified: {len(rows)} embeddings, index {name} ONLINE, self-query matched", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (KeyError, OSError, TypeError, ValueError, RuntimeError) as exc:
        sys.exit(str(exc))
