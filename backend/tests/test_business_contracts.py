"""Pure fixture probes, then the same Sofia occurrence/incident path. No commerce I/O."""
import importlib.util
from pathlib import Path
import sys
import json

import pytest


@pytest.fixture
def contracts():
    # Scope module installation so collection cannot replace another SDK module.
    name = "isolated_sofia_contracts"
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[2]/"sdk/sofia_contracts.py")
    module = importlib.util.module_from_spec(spec)
    prior = sys.modules.get(name)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        if prior is None:
            sys.modules.pop(name)
        else:
            sys.modules[name] = prior


def test_sales_returns_fx_and_cart_fixture_contracts(contracts):
    c = contracts
    assert c.sale_total(expected_total="11.50", observed_total="11.50").passed
    assert not c.sale_total(expected_total="11.50", observed_total="10.50").passed
    assert c.return_limit(sold_quantity=2, returned_quantity=2).passed
    assert not c.return_limit(sold_quantity=2, returned_quantity=3).passed
    assert c.purchase_fx(source_total="12.34", applied_rate="1.25", observed_total="15.43").passed
    assert not c.purchase_fx(source_total="12.34", applied_rate="1.25", observed_total="12.34").passed
    expected = {"fixture-product": 2}
    assert c.recovered_cart(expected_fixture=expected, observed_fixture=expected).passed
    assert not c.recovered_cart(expected_fixture=expected, observed_fixture={}).passed


def test_probe_payload_has_scope_and_no_business_data(contracts):
    probe = contracts.recovered_cart(expected_fixture={"fixture-product": 2}, observed_fixture={})
    payload = probe.incident_payload(service_id="svc", tenant_id="fixture-a", environment="test", release="test-r1")
    assert payload["source"] == "synthetic"
    assert payload["tags"]["tenant_id"] == "fixture-a"
    assert "fixture-product" not in json.dumps(payload)
    assert contracts.sale_total(expected_total="1", observed_total="1").incident_payload(
        service_id="svc", tenant_id="a", environment="test", release="r1") is None
    with pytest.raises(ValueError):
        probe.incident_payload(service_id="svc", tenant_id="person@example.com", environment="test", release="r1")
    with pytest.raises(ValueError):
        contracts.purchase_fx(source_total="10", applied_rate="NaN", observed_total="10")


async def test_failed_business_probe_enters_existing_ingestion_then_incident(contracts, initialized_db, monkeypatch):
    import httpx
    from fastapi import FastAPI
    from app.models.config import AppRepoConfig, MonitorConfig
    from app.routers import ingest
    from app.services import incident_team_service as team
    from unittest.mock import AsyncMock

    cfg = MonitorConfig(app_repos=[AppRepoConfig(id="svc", name="Fixture", path="unused",
        incident_team_enabled=True, incident_github_repo="fixture/monitor",
        incident_tenant_ids=["fixture-a"], incident_environments=["test"])])
    monkeypatch.setattr(ingest, "load_config", lambda: cfg)
    monkeypatch.setattr(team, "load_config", lambda: cfg)
    alert = AsyncMock(return_value=False)
    monkeypatch.setattr(ingest.whatsapp_service, "send_alert", alert)
    payload = contracts.sale_total(expected_total="10", observed_total="9").incident_payload(
        service_id="svc", tenant_id="fixture-a", environment="test", release="r1")
    app = FastAPI()
    app.include_router(ingest.router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        response = await client.post("/api/ingest/event", json=payload)
    assert response.status_code == 200
    occurrences = await initialized_db.get_occurrences(response.json()["issue_id"])
    run_id = team.enqueue(occurrences[0]["id"])
    assert team.get_run(run_id)["source"] == "business_invariant"
    assert team.get_run(run_id)["status"] == "pending"
