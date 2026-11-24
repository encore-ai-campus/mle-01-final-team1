
import os
from pathlib import Path
from uuid import uuid4

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .contracts import (
    MemorySearchRequest,
    MemorySearchResult,
    MemoryWriteRequest,
    MemoryWriteResult,
)


class PostgresMemoryService:
    """PostgreSQL에 사용자별 기억을 저장하고 검색한다."""

    def __init__(self):
        env_path = Path(__file__).resolve().parents[2] / ".env"
        load_dotenv(env_path)

        self.connection_config = {
            "host": os.getenv("DB_HOST", "localhost"),
            "port": os.getenv("DB_PORT", "5432"),
            "dbname": os.getenv("DB_NAME", "finance_memory"),
            "user": os.getenv("DB_USER", "postgres"),
            "password": os.getenv("DB_PASSWORD"),
            "connect_timeout": 5,
        }

    def save_memory(
        self,
        request: MemoryWriteRequest,
        *,
        authenticated_user_id: str,
    ) -> MemoryWriteResult:
        if not authenticated_user_id:
            return {"status": "access_denied"}

        if not request["content"].strip() or not request["source_ref"].strip():
            return {"status": "failed"}

        try:
            with psycopg.connect(**self.connection_config) as conn:
                with conn.cursor() as cur:
                    # 동일 사용자의 중복 기억 확인
                    cur.execute(
                        """
                        SELECT memory_id
                        FROM memories
                        WHERE user_id = %s
                          AND memory_type = %s
                          AND content = %s
                          AND source_ref = %s
                          AND target_ref IS NOT DISTINCT FROM %s
                        LIMIT 1
                        """,
                        (
                            authenticated_user_id,
                            request["memory_type"],
                            request["content"],
                            request["source_ref"],
                            request.get("target_ref"),
                        ),
                    )

                    existing = cur.fetchone()

                    if existing:
                        return {
                            "status": "duplicate",
                            "memory_id": str(existing[0]),
                        }

                    memory_id = str(uuid4())

                    cur.execute(
                        """
                        INSERT INTO memories (
                            memory_id, user_id, memory_type,
                            content, source_ref, target_ref, metadata
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            memory_id,
                            authenticated_user_id,
                            request["memory_type"],
                            request["content"],
                            request["source_ref"],
                            request.get("target_ref"),
                            Jsonb(request.get("metadata", {})),
                        ),
                    )

                conn.commit()

            return {"status": "saved", "memory_id": memory_id}

        except psycopg.Error:
            return {"status": "failed"}

    def search_memory(
        self,
        request: MemorySearchRequest,
        *,
        authenticated_user_id: str,
    ) -> MemorySearchResult:
        if not authenticated_user_id:
            return {
                "retrieval_status": "access_denied",
                "retrieved": [],
            }

        query = request["query"].strip()

        if not query:
            return {
                "retrieval_status": "no_match",
                "retrieved": [],
            }

        limit = max(1, min(request.get("limit", 5), 20))
        allowed_types = request.get("memory_types")

        try:
            with psycopg.connect(**self.connection_config) as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    sql = """
                        SELECT memory_id, memory_type, content,
                               source_ref, target_ref, metadata
                        FROM memories
                        WHERE user_id = %s
                          AND POSITION(LOWER(%s) IN LOWER(content)) > 0
                    """

                    params = [authenticated_user_id, query]

                    if allowed_types:
                        sql += " AND memory_type = ANY(%s)"
                        params.append(allowed_types)

                    sql += " ORDER BY created_at DESC LIMIT %s"
                    params.append(limit)

                    cur.execute(sql, params)
                    rows = cur.fetchall()

            memories = [
                {
                    **row,
                    "memory_id": str(row["memory_id"]),
                }
                for row in rows
            ]

            return {
                "retrieval_status": "found" if memories else "no_match",
                "retrieved": memories,
            }

        except psycopg.Error:
            return {
                "retrieval_status": "failed",
                "retrieved": [],
            }
