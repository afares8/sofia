"""Tests de la supresion de ruido (familias conocido-esperadas)."""
import json

from app.models.event import ErrorEvent
from app.services import noise_service


def _write_cfg(path, patterns):
    path.write_text(json.dumps({"log_ignore_patterns": patterns}),
                    encoding="utf-8")
    return path


def _point(monkeypatch, path):
    monkeypatch.setattr(noise_service, "CONFIG_PATH", path)
    noise_service._cache["mtime"] = None


def test_matches_by_service_and_pattern(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path / "config.json", [
        {"id": "m1", "service_id": "mayor", "pattern": "IP privada sin proxy"},
        {"id": "p1", "service_id": "packing", "pattern": "Invalid AP timezone"},
    ])
    _point(monkeypatch, cfg)
    assert noise_service.ignored_reason(
        "mayor", "PROBLEMA DETECTADO: IP privada sin proxy") == "m1"
    assert noise_service.ignored_reason(
        "packing", "Invalid AP timezone 'America/Panama'") == "p1"
    # scoping: el patron de mayor NO aplica a packing
    assert noise_service.ignored_reason(
        "packing", "PROBLEMA DETECTADO: IP privada sin proxy") is None


def test_does_not_hide_real_errors(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path / "config.json", [
        {"id": "m1", "service_id": "mayor", "pattern": "IP privada sin proxy"},
    ])
    _point(monkeypatch, cfg)
    assert noise_service.ignored_reason(
        "mayor", "Traceback: ValueError: no such column") is None
    assert noise_service.ignored_reason("mayor", "") is None


def test_optional_service_id_applies_to_all(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path / "config.json",
                     [{"id": "g1", "pattern": "known expected"}])
    _point(monkeypatch, cfg)
    assert noise_service.ignored_reason("mayor", "this is known expected") == "g1"
    assert noise_service.ignored_reason("packing", "known expected too") == "g1"


def test_bad_regex_and_missing_config_fail_open(tmp_path, monkeypatch):
    cfg = _write_cfg(tmp_path / "config.json", [{"id": "bad", "pattern": "("}])
    _point(monkeypatch, cfg)
    assert noise_service.ignored_reason("mayor", "anything") is None
    _point(monkeypatch, tmp_path / "nope.json")
    assert noise_service.ignored_reason("mayor", "anything") is None


async def test_upsert_event_skips_expected_and_keeps_real(
        initialized_db, tmp_sofia_paths, monkeypatch):
    from app.services import db_service

    cfg = _write_cfg(tmp_sofia_paths["cfg_path"], [
        {"id": "m1", "service_id": "mayor", "pattern": "IP privada sin proxy"}])
    _point(monkeypatch, cfg)
    noise_service.reset_stats()

    noise = ErrorEvent(service_id="mayor", service_name="Mayor", level="WARNING",
                       message="PROBLEMA DETECTADO: IP privada sin proxy",
                       source="passive")
    assert await db_service.upsert_event(noise) == (0, False, False)

    real = ErrorEvent(service_id="mayor", service_name="Mayor", level="ERROR",
                      message="Traceback: ValueError: boom", source="passive")
    issue_id, is_new, _ = await db_service.upsert_event(real)
    assert issue_id > 0 and is_new is True

    assert noise_service.get_stats()["counts"].get("mayor:m1") == 1
