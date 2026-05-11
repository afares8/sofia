"""
WPPConnect webhook router - receives incoming WhatsApp messages.
WPPConnect sends a POST to this endpoint for every event on the session.
"""
import logging
from fastapi import APIRouter, Request, HTTPException
from app.services import restore_service

logger = logging.getLogger("sofia.webhook")
router = APIRouter(prefix="/webhook", tags=["webhook"])


@router.post("/wppconnect")
async def wppconnect_webhook(request: Request):
    """
    Receives all WPPConnect session events.
    We only care about incoming text messages (type=chat or onMessage).
    """
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "error": "invalid json"}

    event_type = (body.get("event") or body.get("type") or "").lower()
    logger.debug(f"[WEBHOOK] WPP event: {event_type}")

    # Only process incoming message events
    incoming_events = {"onmessage", "message", "message.new", "onanymessage", "chat"}
    if event_type not in incoming_events:
        return {"ok": True, "ignored": True}

    # Extract message data — WPPConnect wraps in different shapes depending on version
    data = body.get("data") or body
    msg_type = (data.get("type") or "").lower()

    # Only process text messages
    if msg_type not in ("chat", "text", ""):
        return {"ok": True, "ignored": True, "reason": f"non-text type: {msg_type}"}

    # fromMe = True means WE sent it, skip
    if data.get("fromMe") or data.get("from_me"):
        return {"ok": True, "ignored": True, "reason": "fromMe"}

    # Extract sender and body
    sender = (
        data.get("from") or
        data.get("sender", {}).get("id") or
        data.get("chatId") or
        ""
    )
    text = (
        data.get("body") or
        data.get("text") or
        data.get("content") or
        ""
    )

    if not sender or not text:
        return {"ok": True, "ignored": True, "reason": "no sender or body"}

    logger.info(f"[WEBHOOK] Incoming message from {sender}: {text[:100]}")
    await restore_service.handle_incoming_message(sender, text)
    return {"ok": True}
