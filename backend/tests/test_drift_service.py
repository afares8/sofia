"""Tests del detector de drift (codigo mas nuevo que el servicio)."""
import os

from app.services import drift_service


def _code(tmp_path, mtime):
    code = tmp_path / "backend"
    code.mkdir()
    f = code / "app.py"
    f.write_text("x = 1\n", encoding="utf-8")
    os.utime(f, (mtime, mtime))
    return code


def _point(monkeypatch, code, started):
    monkeypatch.setattr(drift_service, "APPS", {"Svc": ("app", str(code))})
    monkeypatch.setattr(drift_service, "_service_starts", lambda: {"Svc": started})
    monkeypatch.setattr(drift_service, "_cache", {"at": 0.0, "report": None})


def test_detects_newer_code(tmp_path, monkeypatch):
    _point(monkeypatch, _code(tmp_path, 1_000_000.0), 999_999.0)
    rep = drift_service.check(force=True)
    assert rep["drifted"] == ["app"]
    assert rep["items"][0]["drift"] is True


def test_no_drift_when_service_newer(tmp_path, monkeypatch):
    _point(monkeypatch, _code(tmp_path, 1_000_000.0), 1_000_001.0)
    rep = drift_service.check(force=True)
    assert rep["drifted"] == []
    assert rep["items"][0]["drift"] is False


def test_unknown_start_is_not_reported_as_drift(tmp_path, monkeypatch):
    _point(monkeypatch, _code(tmp_path, 1_000_000.0), None)
    rep = drift_service.check(force=True)
    assert rep["drifted"] == []
    assert rep["items"][0]["drift"] is None


def test_missing_code_dir_is_not_drift(tmp_path, monkeypatch):
    _point(monkeypatch, tmp_path / "nope", 1_000_000.0)
    rep = drift_service.check(force=True)
    assert rep["drifted"] == []
