
import pytest

from agent.memory.memory_service import MemoryService


@pytest.fixture
def service():
    return MemoryService()


def test_mem01_save_memory(service):
    result = service.save_memory(
        {
            "memory_type": "user_decision",
            "content": "적금 B를 제외함",
            "source_ref": "advice_001",
        },
        authenticated_user_id="user_001",
    )

    assert result["status"] == "saved"
    assert result["memory_id"]


def test_mem02_duplicate_memory(service):
    request = {
        "memory_type": "user_decision",
        "content": "적금 B를 제외함",
        "source_ref": "advice_001",
    }

    first = service.save_memory(
        request, authenticated_user_id="user_001"
    )
    second = service.save_memory(
        request, authenticated_user_id="user_001"
    )

    assert first["status"] == "saved"
    assert second["status"] == "duplicate"
    assert first["memory_id"] == second["memory_id"]


def test_mem03_search_memory(service):
    service.save_memory(
        {
            "memory_type": "recommendation",
            "content": "적금 A와 적금 B를 추천함",
            "source_ref": "advice_002",
        },
        authenticated_user_id="user_001",
    )

    result = service.search_memory(
        {"query": "적금 B"},
        authenticated_user_id="user_001",
    )

    assert result["retrieval_status"] == "found"
    assert len(result["retrieved"]) == 1
    assert result["retrieved"][0]["source_ref"] == "advice_002"


def test_mem04_no_match(service):
    result = service.search_memory(
        {"query": "결혼자금"},
        authenticated_user_id="user_001",
    )

    assert result["retrieval_status"] == "no_match"
    assert result["retrieved"] == []


def test_mem05_user_isolation(service):
    service.save_memory(
        {
            "memory_type": "user_decision",
            "content": "적금 B를 제외함",
            "source_ref": "advice_003",
        },
        authenticated_user_id="user_001",
    )

    other_user_result = service.search_memory(
        {"query": "적금 B"},
        authenticated_user_id="user_002",
    )

    assert other_user_result["retrieval_status"] == "no_match"
    assert other_user_result["retrieved"] == []


def test_mem06_access_denied(service):
    result = service.search_memory(
        {"query": "적금"},
        authenticated_user_id="",
    )

    assert result["retrieval_status"] == "access_denied"
    assert result["retrieved"] == []

from agent.memory.supervisor_adapter import retrieve_memory_for_supervisor


def test_mem07_supervisor_adapter():
    service = MemoryService()

    service.save_memory(
        {
            "memory_type": "user_decision",
            "content": "적금 B를 중도해지 조건 때문에 제외함",
            "source_ref": "advice_001",
        },
        authenticated_user_id="user_001",
    )

    result = retrieve_memory_for_supervisor(
        service,
        {"query": "적금 B"},
        authenticated_user_id="user_001",
    )

    assert result["retrieval_status"] == "found"
    assert len(result["retrieved"]) == 1
    assert result["retrieved"][0]["source_ref"] == "advice_001"