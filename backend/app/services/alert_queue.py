"""
Background worker that flushes the WhatsApp alert queue when WPPConnect is
unreachable. Runs every 30s; each attempt tries to drain the queue, but stops
at the first failure to avoid hammering a downed gateway.
"""
import asyncio
import logging

from app.services import whatsapp_service
from app.services.config_service import load_config

logger = logging.getLogger("sofia.alert_queue")

FLUSH_INTERVAL_SECONDS = 30
MAX_ATTEMPTS_PER_MESSAGE = 10


async def alert_queue_loop():
    logger.info("[QUEUE] Alert queue loop started.")
    while True:
        try:
            cfg = load_config()
            await whatsapp_service.flush_queue(cfg.alerts, MAX_ATTEMPTS_PER_MESSAGE)
        except Exception as exc:
            logger.error(f"[QUEUE] flush failed: {exc}")
        await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
