import json
import os

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError

from app.config import get_settings
from app.db import get_session_factory
from app.llm import LLMClient, LLMError, LLMSchemaError, extract_json
from app.llm_usage import usage_summary
from app.models import LLMUsage
from app.schemas import A2Result


class FakeResponse:
    def __init__(self, payload, status_code: int = 200, headers: dict | None = None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def completion(content: str, prompt: int = 100, output: int = 20, cached: int = 0) -> FakeResponse:
    return FakeResponse({
        "choices": [{"message": {"content": content}}],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": output,
            "total_tokens": prompt + output,
            "prompt_tokens_details": {"cached_tokens": cached},
        },
    })


def use_provider(monkeypatch, **overrides) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("LLM_API_KEY", "key")
    monkeypatch.setenv("LLM_MODEL", "base-model")
    for name, value in overrides.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def reset_settings():
    yield
    get_settings.cache_clear()


VALID_A2 = {
    "customer": {"label": "customer", "confidence": 0.9},
    "problem": {"label": "problem", "confidence": 0.9},
    "object": {"label": "object", "confidence": 0.9},
    "promise": {"label": "promise", "confidence": 0.9},
}


def test_stage_routing_splits_fast_and_smart_models(monkeypatch):
    use_provider(
        monkeypatch,
        LLM_MODEL_FAST="cheap-model",
        LLM_MODEL_SMART="smart-model",
    )
    settings = get_settings()
    assert settings.model_for_agent("A2") == ("cheap-model", "fast")
    assert settings.model_for_agent("A4") == ("cheap-model", "fast")
    assert settings.model_for_agent("NARRATIVE") == ("smart-model", "smart")
    assert settings.model_for_agent("CRITIC") == ("smart-model", "smart")


def test_stage_routing_falls_back_to_single_model(monkeypatch):
    use_provider(monkeypatch)
    settings = get_settings()
    assert settings.model_for_agent("A2") == ("base-model", "fast")
    assert settings.model_for_agent("NARRATIVE") == ("base-model", "smart")


def test_usage_is_recorded_per_request(monkeypatch):
    use_provider(monkeypatch)
    recorded: list[dict] = []
    calls: list[dict] = []

    class Recorder:
        def record(self, entry):
            recorded.append(entry)

    def transport(url, **kwargs):
        calls.append(kwargs["json"])
        return completion(json.dumps(VALID_A2), prompt=120, output=30, cached=64)

    client = LLMClient(recorder=Recorder(), transport=transport)
    result = client.generate(agent="A2", schema=A2Result, context={"proposition": "text"}, mock={})

    assert result["customer"]["label"] == "customer"
    assert len(recorded) == 1
    entry = recorded[0]
    assert entry["prompt_tokens"] == 120
    assert entry["completion_tokens"] == 30
    assert entry["cached_tokens"] == 64
    assert entry["total_tokens"] == 150
    assert entry["is_estimated"] is False
    assert entry["purpose"] == "initial"
    assert calls[0]["model"] == "base-model"


def test_repair_round_recovers_from_invalid_json(monkeypatch):
    use_provider(monkeypatch, LLM_SCHEMA_REPAIR_ATTEMPTS="2")
    recorded: list[dict] = []
    bodies: list[list[dict]] = []
    answers = iter(["not json at all", json.dumps(VALID_A2)])

    class Recorder:
        def record(self, entry):
            recorded.append(entry)

    def transport(url, **kwargs):
        bodies.append(kwargs["json"]["messages"])
        return completion(next(answers))

    result = LLMClient(recorder=Recorder(), transport=transport).generate(
        agent="A2", schema=A2Result, context={"proposition": "text"}, mock={}
    )

    assert result["customer"]["confidence"] == 0.9
    assert len(bodies) == 2
    assert recorded[1]["purpose"] == "repair"
    assert recorded[1]["attempt"] == 1
    # The repair round must carry the rejection reason back to the provider.
    assert any("rejected" in message["content"].lower() for message in bodies[1])


def test_repair_round_recovers_from_schema_violation(monkeypatch):
    use_provider(monkeypatch, LLM_SCHEMA_REPAIR_ATTEMPTS="1")
    answers = iter([json.dumps({"customer": {"label": "x", "confidence": 5}}), json.dumps(VALID_A2)])

    def transport(url, **kwargs):
        return completion(next(answers))

    result = LLMClient(transport=transport).generate(
        agent="A2", schema=A2Result, context={"proposition": "text"}, mock={}
    )
    assert result["customer"]["label"] == "customer"


def test_schema_failure_after_repairs_is_retryable(monkeypatch):
    use_provider(monkeypatch, LLM_SCHEMA_REPAIR_ATTEMPTS="1")

    def transport(url, **kwargs):
        return completion("still broken")

    with pytest.raises(LLMSchemaError) as excinfo:
        LLMClient(transport=transport).generate(
            agent="A2", schema=A2Result, context={"proposition": "text"}, mock={}
        )
    assert excinfo.value.retryable is True


def test_transport_retries_on_429(monkeypatch):
    use_provider(monkeypatch, LLM_TRANSPORT_RETRIES="2")
    attempts = {"count": 0}
    delays: list[float] = []

    class RateLimited(FakeResponse):
        def json(self):
            return {"error": "slow down"}

    def transport(url, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            return RateLimited({"error": "slow down"}, status_code=429, headers={"Retry-After": "2"})
        return completion(json.dumps(VALID_A2))

    result = LLMClient(transport=transport, sleep=delays.append).generate(
        agent="A2", schema=A2Result, context={"proposition": "text"}, mock={}
    )
    assert result["customer"]["label"] == "customer"
    assert attempts["count"] == 2
    assert delays == [2.0]


def test_non_retryable_provider_error(monkeypatch):
    use_provider(monkeypatch, LLM_TRANSPORT_RETRIES="2")

    def transport(url, **kwargs):
        return FakeResponse({"error": "bad request"}, status_code=400)

    with pytest.raises(LLMError) as excinfo:
        LLMClient(transport=transport).generate(
            agent="A2", schema=A2Result, context={"proposition": "text"}, mock={}
        )
    assert excinfo.value.retryable is False


def test_usage_is_estimated_when_provider_omits_it(monkeypatch):
    use_provider(monkeypatch)
    recorded: list[dict] = []

    class Recorder:
        def record(self, entry):
            recorded.append(entry)

    def transport(url, **kwargs):
        return FakeResponse({"choices": [{"message": {"content": json.dumps(VALID_A2)}}]})

    LLMClient(recorder=Recorder(), transport=transport).generate(
        agent="A2", schema=A2Result, context={"proposition": "text"}, mock={}
    )
    assert recorded[0]["is_estimated"] is True
    assert recorded[0]["prompt_tokens"] > 0
    assert recorded[0]["completion_tokens"] > 0


def test_mock_provider_records_zero_cost_row(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    get_settings.cache_clear()
    recorded: list[dict] = []

    class Recorder:
        def record(self, entry):
            recorded.append(entry)

    LLMClient(recorder=Recorder()).generate(
        agent="A2", schema=A2Result, context={}, mock=VALID_A2
    )
    assert recorded[0]["tier"] == "mock"
    assert recorded[0]["total_tokens"] == 0


def test_extract_json_handles_fences_and_prose():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure: {"a": {"b": "}"}} done') == {"a": {"b": "}"}}


def test_validate_hook_triggers_repair_round(monkeypatch):
    use_provider(monkeypatch, LLM_SCHEMA_REPAIR_ATTEMPTS="1")
    answers = iter([json.dumps(VALID_A2), json.dumps(VALID_A2)])
    rejections: list[dict] = []
    bodies: list[list[dict]] = []

    def transport(url, **kwargs):
        bodies.append(kwargs["json"]["messages"])
        return completion(next(answers))

    def validate(payload):
        rejections.append(payload)
        if len(rejections) == 1:
            raise ValueError("batch answer covers the wrong propositions")

    result = LLMClient(transport=transport).generate(
        agent="A2", schema=A2Result, context={"proposition": "text"},
        mock=VALID_A2, validate=validate,
    )
    assert result["customer"]["label"] == "customer"
    assert len(bodies) == 2
    assert any("wrong propositions" in message["content"] for message in bodies[1])


def test_usage_summary_and_endpoint(monkeypatch):
    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests")
    from fastapi.testclient import TestClient

    from app.main import app

    factory = get_session_factory()
    with factory() as session:
        try:
            session.execute(text("SELECT 1"))
        except OperationalError:
            pytest.skip("PostgreSQL is not available")
        from app.schemas import ScanCreate
        from app.scans import create_scan

        scan = create_scan(session, ScanCreate(title="Usage accounting"))
        session.add(LLMUsage(
            scan_id=scan.id, agent="A2", model="cheap-model", tier="fast",
            prompt_tokens=1000, completion_tokens=200, total_tokens=1200,
        ))
        session.add(LLMUsage(
            scan_id=scan.id, agent="NARRATIVE", model="smart-model", tier="smart",
            prompt_tokens=500, completion_tokens=100, total_tokens=600, purpose="repair",
            is_estimated=True,
        ))
        session.commit()
        summary = usage_summary(session, scan.id)

    assert summary["totals"]["prompt_tokens"] == 1500
    assert summary["totals"]["completion_tokens"] == 300
    assert summary["totals"]["calls"] == 2
    assert summary["totals"]["estimated_calls"] == 1
    assert summary["by_agent"]["A2"]["cost_usd"] is None  # prices not configured
    assert summary["by_model"]["cheap-model"]["tier"] == "fast"
    assert summary["repair_calls"] == 1

    monkeypatch.setenv("LLM_PRICE_INPUT_PER_MTOK", "0.1")
    monkeypatch.setenv("LLM_PRICE_OUTPUT_PER_MTOK", "0.4")
    get_settings.cache_clear()
    with factory() as session:
        summary = usage_summary(session, scan.id)
        assert summary["totals"]["cost_usd"] == pytest.approx(0.00027, rel=1e-6)
        assert session.scalar(select(func.count(LLMUsage.id)).where(LLMUsage.scan_id == scan.id)) == 2

    client = TestClient(app)
    token = get_settings().app_access_token
    response = client.get(f"/scans/{scan.id}/usage", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    body = response.json()
    assert body["totals"]["prompt_tokens"] == 1500
    assert body["prices"]["input_per_mtok"] == 0.1


def test_production_refuses_mock_provider(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    get_settings.cache_clear()
    settings = get_settings()
    with pytest.raises(RuntimeError, match="mock is forbidden"):
        settings.validate_runtime()
    with pytest.raises(RuntimeError, match="mock is forbidden"):
        settings.validate_worker()


def test_production_api_needs_provider_but_not_api_key(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("LLM_MODEL", "base-model")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    get_settings.cache_clear()
    settings = get_settings()
    # The web service only records model metadata, so it must start without a key.
    settings.validate_runtime()
    # The worker actually calls the provider and must fail fast without a key.
    with pytest.raises(RuntimeError, match="LLM_API_KEY"):
        settings.validate_worker()
