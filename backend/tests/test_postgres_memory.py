
from uuid import uuid4

import psycopg
import pytest

from agent.memory.postgres_memory_service import PostgresMemoryService


@pytest.fixture
def service():
    return PostgresMemoryService()


@pytest.fixture
def test_user_id(service):
    # Each test gets a unique user ID.
    user_id = f"postgres_test_{uuid4().hex}"

    yield user_id

    # Cleanup runs even if the test assertion fails.
    # Only remove records belonging to this test user.
    with psycopg.connect(**service.connection_config) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM memories WHERE user_id = %s",
                (user_id,),
            )
        conn.commit()


@pytest.fixture
def sample_request():
    return {
        "memory_type": "user_decision",
        "content": "Test user rejected savings product B",
        "source_ref": f"test_{uuid4().hex}",
        "target_ref": "product_B",
        "metadata": {"decision": "rejected"},
    }


def test_save_and_search(service, test_user_id, sample_request):
    saved = service.save_memory(
        sample_request,
        authenticated_user_id=test_user_id,
    )

    assert saved["status"] == "saved"

    another_service = PostgresMemoryService()

    result = another_service.search_memory(
        {"query": "savings product B"},
        authenticated_user_id=test_user_id,
    )

    assert result["retrieval_status"] == "found"
    assert len(result["retrieved"]) == 1
    assert result["retrieved"][0]["memory_id"] == saved["memory_id"]


def test_duplicate_prevention(service, test_user_id, sample_request):
    first = service.save_memory(
        sample_request,
        authenticated_user_id=test_user_id,
    )

    second = service.save_memory(
        sample_request,
        authenticated_user_id=test_user_id,
    )

    assert first["status"] == "saved"
    assert second["status"] == "duplicate"
    assert first["memory_id"] == second["memory_id"]


def test_user_isolation(service, test_user_id, sample_request):
    saved = service.save_memory(
        sample_request,
        authenticated_user_id=test_user_id,
    )

    assert saved["status"] == "saved"

    result = service.search_memory(
        {"query": "savings product B"},
        authenticated_user_id=f"other_{uuid4().hex}",
    )

    assert result["retrieval_status"] == "no_match"
    assert result["retrieved"] == []


def test_access_denied(service, sample_request):
    result = service.save_memory(
        sample_request,
        authenticated_user_id="",
    )

    assert result["status"] == "access_denied"
