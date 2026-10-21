"""Isolated DB/config, local disposable Git repos, mocked model and GitHub I/O."""
import asyncio
import json
import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from app.models.config import AppRepoConfig, MonitorConfig
from app.models.event import ErrorEvent
from app.services import autonomy_service, codex_cli_service, incident_team_service as team


@pytest.fixture
async def setup(initialized_db, tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    def git(*args):
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=source,
                              check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-b", "main")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Isolated test")
    (source / "backend/app").mkdir(parents=True)
    (source / "backend/app/sample.py").write_text("value = 1\n")
    git("add", "backend/app/sample.py")
    git("commit", "-m", "fixture")
    repo = AppRepoConfig(id="svc", name="Service", path=str(source), incident_team_enabled=True,
                         incident_github_repo="fixture/monitor", incident_tenant_ids=["a", "b"],
                         incident_environments=["test", "staging"], incident_required_checks=["test"])
    cfg = MonitorConfig(app_repos=[repo])
    cfg.autonomy.sandbox_root = str(tmp_path / "sandboxes")
    cfg.autonomy.kill_switch = False
    limits = cfg.autonomy.incident_team
    limits.enabled = limits.execution_enabled = limits.isolated_worker_confirmed = True
    limits.upstream_spend_cap_confirmed = limits.legacy_dispatch_excluded_confirmed = True
    limits.max_reserved_cost_cents_per_day = 900
    limits.session_cost_ceiling_cents = 100
    monkeypatch.setattr(team, "load_config", lambda: cfg)
    monkeypatch.setenv("SOFIA_API_KEY", "test-only-operator")
    monkeypatch.setattr(autonomy_service, "load_config", lambda: cfg)
    model = Mock()
    def answer(prompt, **kw):
        assert kw["safe_workspace"] is True and kw["log_output"] is False
        if not kw["read_only"]:
            target = Path(kw["cwd"])
            (target/"backend/app/sample.py").write_text("value = 2\n")
            # A newly created file must be included in the audited diff.
            (target/"backend/app/new.py").write_text("fixed = True\n")
            return 0, "prepared"
        if "DIFF:" in prompt:
            assert "new.py" in prompt and "+fixed = True" in prompt
            return 0, json.dumps({"approved": True, "risk": "low", "reasons": ["reviewed"], "required_actions": []})
        return 0, "cause hypothesis"
    model.side_effect = answer
    monkeypatch.setattr(codex_cli_service, "run_codex", model)
    # Clone-local config isn't inherited. Use per-process test-only Git identity.
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Isolated test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Isolated test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.invalid")
    return initialized_db, cfg, model


async def occurrence(db, *, tenant="a", environment="test", release="r1", source="active", message="failure", detail=None):
    await db.upsert_event(ErrorEvent(service_id="svc", service_name="Service", level="ERROR", message=message,
                                    tags=json.dumps({"tenant_id": tenant}), environment=environment,
                                    release=release, source=source, detail=detail))
    with sqlite3.connect(db.DB_PATH) as conn:
        return conn.execute("SELECT MAX(id) FROM occurrences").fetchone()[0]


def ci_stub(monkeypatch, sha, *, conclusion="success", app="github-actions", workflow_sha=None):
    def read(endpoint):
        if "check-runs" in endpoint:
            return {"total_count": 1, "check_runs": [{"id": 12, "name": "test", "head_sha": sha,
                    "status": "completed", "conclusion": conclusion, "app": {"slug": app},
                    "details_url": "https://github.com/fixture/monitor/actions/runs/44/job/55"}]}
        return {"head_sha": workflow_sha or sha, "path": ".github/workflows/ci.yml", "event": "push",
                "status": "completed", "conclusion": "success"}
    mock = Mock(side_effect=read)
    monkeypatch.setattr(team, "github_json", mock)
    return mock


async def test_vertical_detect_prepare_review_ci_human_plan(setup, monkeypatch):
    db, cfg, model = setup
    run_id = team.enqueue(await occurrence(db))
    team.execute(run_id)
    run = team.get_run(run_id)
    assert run["status"] == "awaiting_ci"
    assert model.call_count == 3
    assert Path(cfg.app_repos[0].path, "backend/app/sample.py").read_text() == "value = 1\n"
    assert team.git(run["sandbox_path"], "remote") == ""
    assert len({e["actor"] for e in run["evidence"] if e["stage"] in {"analyst", "engineer", "reviewer"}}) == 3
    ci = ci_stub(monkeypatch, run["candidate_sha"])
    team.check_ci(run_id)
    plan = team.approve_deployment(run_id, run["candidate_sha"], run["base_sha"], "sha256:"+"a"*64)
    assert ci.call_count == 4  # re-fetch at the approval boundary
    assert plan["execution"] == "disconnected" and not plan["issue_resolved"]
    assert team.get_run(run_id)["status"] == "deployment_requested"
    assert (await db.get_issues())[0]["resolved"] == 0
    with pytest.raises(ValueError):
        team.approve_deployment(run_id, run["candidate_sha"], run["base_sha"], "sha256:"+"a"*64)
    team.execute(run_id)
    assert model.call_count == 3


async def test_scope_is_immutable_per_occurrence_and_deduplicates(setup):
    db, _, _ = setup
    first = await occurrence(db, tenant="a")
    second = await occurrence(db, tenant="b")
    a = team.enqueue(first)
    b = team.enqueue(second)
    assert a != b
    assert team.get_run(a)["tenant_id"] == "a"  # issue tags now belong to b
    assert team.enqueue(first) == a  # duplicate delivery never increments
    assert team.enqueue(await occurrence(db, tenant="a")) == a
    assert team.get_run(a)["occurrences"] == 2
    for context in ({"environment": "staging"}, {"release": "r2"}, {"source": "synthetic"}):
        assert team.enqueue(await occurrence(db, **context)) not in {a, b}


async def test_missing_and_unauthorized_scope_never_borrows_issue_context(setup):
    db, cfg, model = setup
    for kwargs in ({"tenant": ""}, {"tenant": "stranger"}, {"environment": "prod"}, {"release": ""}):
        assert team.enqueue(await occurrence(db, **kwargs)) is None
    oid = await occurrence(db)
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("UPDATE occurrences SET context=NULL WHERE id=?", (oid,))
    assert team.enqueue(oid) is None
    assert model.call_count == 0


async def test_from_now_cursor_and_purge_zero_are_safe(setup):
    db, _, _ = setup
    await occurrence(db)
    assert team.poll() == []  # no historic backlog on activation
    oid = await occurrence(db)
    assert team.poll() == [team.enqueue(oid)]
    assert team.poll() == []
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("DELETE FROM occurrences")
    assert team.poll() == []  # retention, not a rewind


async def test_atomic_claim_and_daily_budget_reserve_before_work(setup):
    db, cfg, model = setup
    run_id = team.enqueue(await occurrence(db))
    with ThreadPoolExecutor(8) as pool:
        claimed = list(pool.map(team.claim, [run_id]*8))
    assert claimed.count(True) == 1
    cfg.autonomy.incident_team.max_reserved_cost_cents_per_day = 300
    other = team.enqueue(await occurrence(db, message="other"))
    with pytest.raises(ValueError, match="Presupuesto"):
        team.claim(other)
    assert model.call_count == 0
    with sqlite3.connect(db.DB_PATH) as conn:
        assert conn.execute("SELECT SUM(sessions),SUM(cost_cents) FROM incident_reservations").fetchone() == (3, 300)


@pytest.mark.parametrize("setting", ["execution_enabled", "isolated_worker_confirmed", "upstream_spend_cap_confirmed", "legacy_dispatch_excluded_confirmed"])
async def test_disabled_controls_fail_before_model(setup, setting):
    db, cfg, model = setup
    setattr(cfg.autonomy.incident_team, setting, False)
    run_id = team.enqueue(await occurrence(db))
    with pytest.raises(ValueError):
        team.execute(run_id)
    assert not model.called


async def test_crash_recovery_does_not_retry_or_refund(setup):
    db, _, model = setup
    run_id = team.enqueue(await occurrence(db))
    assert team.claim(run_id)
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("UPDATE incident_runs SET lease_until=0 WHERE id=?", (run_id,))
    team.recover_expired()
    assert team.get_run(run_id)["status"] == "uncertain"
    team.execute(run_id)
    assert not model.called
    with sqlite3.connect(db.DB_PATH) as conn:
        assert conn.execute("SELECT COUNT(*) FROM incident_reservations").fetchone()[0] == 1


@pytest.mark.parametrize("raw", ['{"approved":"false","risk":"low","reasons":[],"required_actions":[]}',
                                  '{"approved":false,"approved":true,"risk":"low","reasons":[],"required_actions":[]}',
                                  'true', '```json\n{"approved":true}\n```'])
def test_no_truthy_or_ambiguous_reviewer_approval(raw):
    with pytest.raises(ValueError):
        team.strict_decision(raw)


@pytest.mark.parametrize("options", [{"conclusion": "skipped"}, {"conclusion": "failure"}, {"app": "agent"}, {"workflow_sha": "a"*40}])
async def test_independent_ci_fails_closed(setup, monkeypatch, options):
    db, _, _ = setup
    run_id = team.enqueue(await occurrence(db))
    team.execute(run_id)
    run = team.get_run(run_id)
    ci_stub(monkeypatch, run["candidate_sha"], **options)
    with pytest.raises(ValueError):
        team.check_ci(run_id)
    assert team.get_run(run_id)["status"] == "awaiting_ci"


async def test_stale_approval_rejected_and_ci_rechecked(setup, monkeypatch):
    db, _, _ = setup
    run_id = team.enqueue(await occurrence(db))
    team.execute(run_id)
    run = team.get_run(run_id)
    ci_stub(monkeypatch, run["candidate_sha"])
    team.check_ci(run_id)
    with pytest.raises(ValueError):
        team.approve_deployment(run_id, "b"*40, run["base_sha"], "sha256:"+"a"*64)
    ci_stub(monkeypatch, run["candidate_sha"], conclusion="failure")
    with pytest.raises(ValueError):
        team.approve_deployment(run_id, run["candidate_sha"], run["base_sha"], "sha256:"+"a"*64)
    assert team.get_run(run_id)["deployment_plan"] is None


async def test_candidate_changed_by_reviewer_blocks(setup):
    db, cfg, model = setup
    original = model.side_effect
    def mutate(prompt, **kwargs):
        result = original(prompt, **kwargs)
        if "DIFF:" in prompt:
            Path(kwargs["cwd"], "backend/app/sample.py").write_text("value = 999\n")
        return result
    model.side_effect = mutate
    run_id = team.enqueue(await occurrence(db))
    team.execute(run_id)
    assert team.get_run(run_id)["status"] == "blocked"
    assert team.get_run(run_id)["candidate_sha"] is None


async def test_redacted_evidence_never_contains_sensitive_event_values(setup):
    db, _, _ = setup
    secret = "ghp_"+"a"*36
    run_id = team.enqueue(await occurrence(db, detail=f"{secret} user@example.com +507 6000 1234 https://host/path?token=abc"))
    run = team.get_run(run_id)
    for forbidden in (secret, "user@example.com", "6000 1234", "token=abc"):
        assert forbidden not in run["snapshot"]
    assert team.digest(run["snapshot"]) == run["snapshot_sha"]


def test_redaction_covers_common_credential_and_stream_formats():
    values = ["AKIA"+"A"*16, "AIza"+"a"*35, "eyJabc.def.ghi", "Basic dXNlcjpwYXNz", "rtsp://viewer:fixture-password@camera/live?secret=x"]
    redacted = team.redact(" ".join(values))
    assert all(value not in redacted for value in values)
    assert "fixture-password" not in redacted and "secret=x" not in redacted


async def test_operator_router_has_no_optional_auth_or_agent_evidence_endpoint(setup, monkeypatch):
    from app.routers.incident_team import router
    app = FastAPI()  # no main/lifespan/workers
    app.include_router(router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        monkeypatch.delenv("SOFIA_API_KEY", raising=False)
        assert (await client.get("/api/autonomy/incidents")).status_code == 403
        monkeypatch.setenv("SOFIA_API_KEY", "test-only-operator")
        assert (await client.get("/api/autonomy/incidents")).status_code == 403
        assert (await client.get("/api/autonomy/incidents", headers={"Authorization": "Bearer test-only-operator"})).status_code == 200


async def test_managed_repo_blocks_legacy_writers_before_any_model_or_git(setup, monkeypatch):
    db, cfg, model = setup
    job = await db.create_ai_job("fixture", service_id="svc", repo_id="svc", mode="fix")
    await autonomy_service.run_job(job)
    assert (await db.get_ai_job(job))["status"] == "blocked"
    await db.update_ai_job(job, status="verified", commit_sha="a"*40)
    io = Mock(side_effect=AssertionError("No external operation allowed"))
    monkeypatch.setattr(autonomy_service, "_run", io)
    with pytest.raises(RuntimeError, match="legacy"):
        await autonomy_service.promote_job(job)
    from app.services import nightly_review_service as nightly
    monkeypatch.setattr(nightly, "load_config", lambda: cfg)
    monkeypatch.setattr(nightly, "_codex_available", lambda: True)
    monkeypatch.setattr(nightly.db_service, "get_nightly_report", AsyncMock(return_value={
        "status": "approved", "proposals": json.dumps([{"service_id": "svc"}])}))
    ok, _ = await nightly.apply_proposal(1, 0)
    assert not ok and not io.called and not model.called


async def test_legacy_publication_keeps_issue_open_and_prevents_respawn(setup, monkeypatch):
    db, cfg, _ = setup
    cfg.autonomy.incident_team.enabled = False
    cfg.autonomy.promotion_mode = "branch"
    oid = await occurrence(db)
    with sqlite3.connect(db.DB_PATH) as conn:
        issue_id = conn.execute("SELECT issue_id FROM occurrences WHERE id=?", (oid,)).fetchone()[0]
    job = await db.create_ai_job("fixture", service_id="svc", repo_id="svc", issue_id=issue_id, mode="fix")
    await db.update_ai_job(job, status="verified", commit_sha="a"*40, sandbox_path="fixture", work_branch="fixture")
    io = Mock(return_value=(0, ""))
    monkeypatch.setattr(autonomy_service, "_run", io)
    await autonomy_service.promote_job(job)
    assert (await db.get_issues())[0]["resolved"] == 0
    assert (await db.get_open_ai_job_for_issue(issue_id))["status"] == "promoted"


@pytest.mark.parametrize("kind", ["credential", "governance", "deleted", "empty"])
async def test_diff_policy_includes_deleted_files_and_rejects_unsafe_changes(setup, kind):
    db, cfg, model = setup
    original = model.side_effect
    def prepare(prompt, **kwargs):
        result = original(prompt, **kwargs)
        root = Path(kwargs["cwd"])
        if not kwargs["read_only"]:
            if kind == "credential":
                (root/"backend/app/sample.py").write_text('password = "fixture-password-value"\n')
            elif kind == "governance":
                (root/"AGENTS.md").write_text("unsafe new rules\n")
            elif kind == "deleted":
                (root/"backend/app/sample.py").unlink()
            elif kind == "empty":
                (root/"backend/app/sample.py").write_text("value = 1\n")
                (root/"backend/app/new.py").unlink()
        if "DIFF:" in prompt and kind == "deleted":
            assert "deleted file mode" in prompt
        return result
    model.side_effect = prepare
    run_id = team.enqueue(await occurrence(db))
    team.execute(run_id)
    assert team.get_run(run_id)["status"] == ("awaiting_ci" if kind == "deleted" else "blocked")


async def test_revoked_scope_and_kill_switch_stop_at_boundaries(setup):
    db, cfg, model = setup
    original = model.side_effect
    def answer(prompt, **kwargs):
        result = original(prompt, **kwargs)
        cfg.autonomy.kill_switch = True
        return result
    model.side_effect = answer
    run_id = team.enqueue(await occurrence(db))
    team.execute(run_id)
    assert model.call_count == 1
    assert team.get_run(run_id)["status"] == "blocked"
