"""Tests del correo de respaldo diferido (espera antes de avisar)."""
import asyncio
from unittest.mock import MagicMock

from app.services import email_alert_service as email


def _reset(monkeypatch, configured=True):
    monkeypatch.setattr(email, "_deferred_tasks", {})
    monkeypatch.setattr(email, "is_configured", lambda: configured)
    sent = MagicMock()
    monkeypatch.setattr(email, "send_fallback_email", sent)
    return sent


async def test_deferred_email_sent_after_delay(monkeypatch):
    sent = _reset(monkeypatch)
    assert email.schedule_fallback_email("subject", "body", 0.05) is True
    assert not sent.called
    await asyncio.sleep(0.2)
    assert sent.call_count == 1
    assert sent.call_args.args[0] == "subject"


async def test_deferred_email_cancelled_when_recovered(monkeypatch):
    sent = _reset(monkeypatch)
    email.schedule_fallback_email("s", "b", 0.05, cancel_check=lambda: True)
    await asyncio.sleep(0.2)
    assert not sent.called


async def test_deferred_email_sent_when_still_down(monkeypatch):
    sent = _reset(monkeypatch)
    email.schedule_fallback_email("s", "b", 0.05, cancel_check=lambda: False)
    await asyncio.sleep(0.2)
    assert sent.call_count == 1


async def test_deferred_email_sent_if_cancel_check_fails(monkeypatch):
    """Si el chequeo de cancelacion revienta, el correo sale igual (no se
    pierde el aviso por un error del chequeo)."""
    sent = _reset(monkeypatch)

    def boom():
        raise RuntimeError("sin estado de salud")

    email.schedule_fallback_email("s", "b", 0.05, cancel_check=boom)
    await asyncio.sleep(0.2)
    assert sent.call_count == 1


async def test_deferred_email_replaces_same_key(monkeypatch):
    sent = _reset(monkeypatch)
    email.schedule_fallback_email("primero", "b", 0.05, key="k")
    email.schedule_fallback_email("segundo", "b", 0.05, key="k")
    await asyncio.sleep(0.25)
    assert sent.call_count == 1
    assert sent.call_args.args[0] == "segundo"


async def test_deferred_not_scheduled_without_smtp(monkeypatch):
    sent = _reset(monkeypatch, configured=False)
    assert email.schedule_fallback_email("s", "b", 0.05) is False
    await asyncio.sleep(0.1)
    assert not sent.called


def test_deferred_without_loop_sends_immediately(monkeypatch):
    """Sin event loop (llamada sincrona) cae al envio inmediato."""
    sent = _reset(monkeypatch)
    email.schedule_fallback_email("s", "b", 180)
    assert sent.call_count == 1
