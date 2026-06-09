"""
Config service - load/save MonitorConfig from config.json on disk.
All settings are editable via the UI and persisted here.
"""
import json
import os
from pathlib import Path
from app.models.config import MonitorConfig, DEFAULT_SERVICES, DEFAULT_ALERT_RULES, DEFAULT_APP_REPOS, AlertConfig, AutonomyConfig, GithubSyncConfig

CONFIG_PATH = Path(os.getenv("SOFIA_CONFIG_PATH", str(Path(__file__).resolve().parents[2] / "data" / "config.json")))


def _ensure_dir():
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)


def load_config() -> MonitorConfig:
    _ensure_dir()
    if CONFIG_PATH.exists():
        try:
            raw = CONFIG_PATH.read_text(encoding="utf-8")
            data = json.loads(raw)
            cfg = MonitorConfig(**data)
            changed = False
            defaults_by_id = {s.id: s for s in DEFAULT_SERVICES}
            raw_services = data.get("services", [])
            for i, svc in enumerate(cfg.services):
                default = defaults_by_id.get(svc.id)
                raw_svc = raw_services[i] if i < len(raw_services) and isinstance(raw_services[i], dict) else {}
                if default and "restore_enabled" not in raw_svc:
                    svc.restore_enabled = default.restore_enabled
                    changed = True
                if default and "auto_restore" not in raw_svc:
                    svc.auto_restore = default.auto_restore
                    changed = True
            existing_service_ids = {svc.id for svc in cfg.services}
            for default in DEFAULT_SERVICES:
                if default.id not in existing_service_ids:
                    cfg.services.append(default)
                    changed = True
            if not cfg.alert_rules:
                cfg.alert_rules = DEFAULT_ALERT_RULES
                changed = True
            if not cfg.app_repos:
                cfg.app_repos = DEFAULT_APP_REPOS
                changed = True
            else:
                # Merge missing fields into existing repos
                default_repo_by_id = {r.id: r for r in DEFAULT_APP_REPOS}
                raw_repos = data.get("app_repos", [])
                for i, repo in enumerate(cfg.app_repos):
                    default = default_repo_by_id.get(repo.id)
                    raw_repo = raw_repos[i] if i < len(raw_repos) and isinstance(raw_repos[i], dict) else {}
                    if default and "autofix_enabled" not in raw_repo:
                        repo.autofix_enabled = default.autofix_enabled
                        changed = True
                    if default and "autonomy_level" not in raw_repo:
                        repo.autonomy_level = default.autonomy_level
                        changed = True
                existing_repo_ids = {r.id for r in cfg.app_repos}
                for default in DEFAULT_APP_REPOS:
                    if default.id not in existing_repo_ids:
                        cfg.app_repos.append(default)
                        changed = True

            # Migrate autonomy defaults if fields are missing in old configs
            raw_autonomy = data.get("autonomy", {})
            default_autonomy = AutonomyConfig()
            if "auto_create_jobs_from_issues" not in raw_autonomy:
                cfg.autonomy.auto_create_jobs_from_issues = default_autonomy.auto_create_jobs_from_issues
                changed = True
            if "max_autofix_jobs_per_day" not in raw_autonomy:
                cfg.autonomy.max_autofix_jobs_per_day = default_autonomy.max_autofix_jobs_per_day
                changed = True
            if "max_failed_jobs_before_pause" not in raw_autonomy:
                cfg.autonomy.max_failed_jobs_before_pause = default_autonomy.max_failed_jobs_before_pause
                changed = True

            if not cfg.github_sync.repos:
                cfg.github_sync = GithubSyncConfig()
                changed = True

            if changed:
                save_config(cfg)
            return cfg
        except Exception:
            pass
    # First run: return defaults
    cfg = MonitorConfig(
        services=DEFAULT_SERVICES,
        alerts=AlertConfig(),
        alert_rules=DEFAULT_ALERT_RULES,
        app_repos=DEFAULT_APP_REPOS,
    )
    save_config(cfg)
    return cfg


def save_config(cfg: MonitorConfig) -> None:
    _ensure_dir()
    CONFIG_PATH.write_text(cfg.model_dump_json(indent=2), encoding="utf-8")
