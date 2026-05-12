"""
Config service - load/save MonitorConfig from config.json on disk.
All settings are editable via the UI and persisted here.
"""
import json
import os
from pathlib import Path
from app.models.config import MonitorConfig, DEFAULT_SERVICES, DEFAULT_ALERT_RULES, AlertConfig

CONFIG_PATH = Path(os.getenv("SOFIA_CONFIG_PATH", "data/config.json"))


def _ensure_dir():
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)


def load_config() -> MonitorConfig:
    _ensure_dir()
    if CONFIG_PATH.exists():
        try:
            raw = CONFIG_PATH.read_text(encoding="utf-8")
            data = json.loads(raw)
            return MonitorConfig(**data)
        except Exception:
            pass
    # First run: return defaults
    cfg = MonitorConfig(
        services=DEFAULT_SERVICES,
        alerts=AlertConfig(),
        alert_rules=DEFAULT_ALERT_RULES,
    )
    save_config(cfg)
    return cfg


def save_config(cfg: MonitorConfig) -> None:
    _ensure_dir()
    CONFIG_PATH.write_text(cfg.model_dump_json(indent=2), encoding="utf-8")
