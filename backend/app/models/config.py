"""
Config models - all settings editable from UI and persisted to JSON.
"""
import os
from pydantic import BaseModel
from typing import List, Optional


# Host IP used in default service URLs (can be overridden by env var)
_SOFIA_HOST_IP = os.getenv("SOFIA_HOST_IP", "192.168.0.123")


class ServiceConfig(BaseModel):
    id: str
    name: str
    url: str                  # health-check URL  e.g. http://192.168.0.123:8075/health
    enabled: bool = True
    log_path: Optional[str] = None   # absolute path to log file (passive monitoring)
    expected_status: int = 200
    timeout_seconds: int = 5
    failure_threshold: int = 3       # consecutive failures before alerting (grace for --reload)
    restore_enabled: bool = False    # enable WhatsApp-triggered restore for this service
    auto_restore: bool = False       # if True, auto-restore without asking user via WhatsApp


class AlertConfig(BaseModel):
    whatsapp_enabled: bool = True
    whatsapp_number: str = "50766662916"   # default: your number
    wppconnect_url: str = "http://localhost:21465"
    wppconnect_token: str = "THISISMYSECURETOKEN"
    wppconnect_session: str = "default"
    cooldown_minutes: int = 10           # don't spam same alert twice
    max_messages_per_hour: int = 8

    # Multi-channel escalation: if a pending restore expires with no response,
    # forward the alert to additional numbers in order.
    escalation_enabled: bool = False
    escalation_minutes: int = 15
    escalation_numbers: List[str] = []


class AlertRule(BaseModel):
    """
    A dynamic alerting rule evaluated by the rules engine.

    condition_type values:
      - "error_count"      → threshold = max errors in window_minutes
      - "response_ms"      → threshold = max response time in ms
      - "downtime_minutes" → threshold = max downtime minutes
      - "spike"            → threshold = multiplier vs 24h average error rate
    """
    id: str
    name: str
    enabled: bool = True
    condition_type: str
    threshold: float
    window_minutes: int = 60
    service_id: Optional[str] = None
    cooldown_minutes: int = 30


class AutonomyConfig(BaseModel):
    enabled: bool = False
    kill_switch: bool = False
    default_level: int = 1
    sandbox_root: str = "D:/sofia_sandboxes"
    auto_create_jobs_from_issues: bool = True
    auto_fix_issue_min_count: int = 10
    auto_fix_loop_minutes: int = 15
    max_actions_per_hour: int = 5
    max_devin_sessions_per_day: int = 10
    max_autofix_jobs_per_day: int = 3
    max_failed_jobs_before_pause: int = 3
    require_verifier: bool = True
    require_tests_for_code_fixes: bool = True
    require_human_for_apply: bool = True
    commit_in_sandbox: bool = True
    run_smoke_checks: bool = True
    max_files_changed: int = 5
    max_lines_changed: int = 300
    # Number of changed lines counted only on non-test source files. Test files
    # are excluded from this count so adding thorough tests never blocks a fix.
    count_test_files_in_limit: bool = False
    # Promotion: how to push a verified sandbox fix to the real repo.
    #   "pr"     → push the work branch to origin and open a GitHub PR (gh CLI)
    #   "branch" → push the work branch to origin, no PR
    #   "manual" → do nothing automatically; promote only via the UI button
    promotion_mode: str = "pr"
    # Auto-promote verified fixes when the verifier marks them as low risk.
    # medium/high-risk fixes always wait for manual promotion from the UI.
    auto_promote_low_risk: bool = True
    # A job stuck in 'running' longer than this is force-failed by the watchdog.
    job_timeout_minutes: int = 30
    allowed_paths: List[str] = ["backend/app/", "frontend/src/", "sdk/"]
    blocked_paths: List[str] = [
        ".env",
        "backend/data/",
        "backend/logs/",
        "__pycache__/",
        ".pyc",
        ".pytest_cache/",
        ".coverage",
        "node_modules/",
        "frontend/dist/",
        ".git/",
    ]
    forbidden_actions: List[str] = [
        "drop_table",
        "truncate_table",
        "delete_database",
        "force_push",
        "modify_secrets",
        "disable_auth",
    ]


class AppRepoConfig(BaseModel):
    id: str
    name: str
    path: str
    enabled: bool = True
    branch: str = "main"
    autonomy_level: int = 1
    autofix_enabled: bool = False
    test_commands: List[str] = []
    build_commands: List[str] = []
    smoke_urls: List[str] = []
    allowed_paths: List[str] = []
    blocked_paths: List[str] = []


class GithubSyncRepo(BaseModel):
    id: str
    path: str
    enabled: bool = False
    branch: str = "main"


class GithubSyncConfig(BaseModel):
    enabled: bool = False
    auto_push_at_midnight: bool = False
    commit_message_prefix: str = "chore(sync): nightly local sync"
    require_clean_secret_scan: bool = True
    max_files_per_repo: int = 50
    blocked_paths: List[str] = [".env", "data/", "logs/", "__pycache__/", ".pyc", ".pytest_cache/", ".coverage", "node_modules/", "dist/", ".git/"]
    repos: List[GithubSyncRepo] = [
        GithubSyncRepo(id="sofia", path="D:/sofia", enabled=False),
        GithubSyncRepo(id="mayor", path="D:/mayor", enabled=False),
        GithubSyncRepo(id="packing", path="D:/packing", enabled=False),
    ]


DEFAULT_ALERT_RULES: List[AlertRule] = [
    AlertRule(
        id="high_error_rate",
        name="Más de 10 errores en 1h",
        condition_type="error_count",
        threshold=10,
        window_minutes=60,
    ),
    AlertRule(
        id="slow_response",
        name="Response > 5000ms",
        condition_type="response_ms",
        threshold=5000,
        window_minutes=15,
    ),
    AlertRule(
        id="spike",
        name="Spike de errores (3x normal)",
        condition_type="spike",
        threshold=3,
        window_minutes=60,
    ),
]


class MonitorConfig(BaseModel):
    poll_interval_seconds: int = 30
    log_tail_lines: int = 200
    error_retention_days: int = 7
    services: List[ServiceConfig] = []
    alerts: AlertConfig = AlertConfig()
    alert_rules: List[AlertRule] = []
    autonomy: AutonomyConfig = AutonomyConfig()
    app_repos: List[AppRepoConfig] = []
    github_sync: GithubSyncConfig = GithubSyncConfig()


DEFAULT_SERVICES: List[ServiceConfig] = [
    ServiceConfig(
        id="mayor",
        name="Mayor",
        url=f"http://{_SOFIA_HOST_IP}:8075/health",
        log_path="D:/mayor/backend/logs/app.log",
        enabled=True,
        restore_enabled=True,
        auto_restore=False,
    ),
    ServiceConfig(
        id="packing",
        name="Packing",
        url=f"http://{_SOFIA_HOST_IP}:8100/health",
        log_path="D:/packing/backend/logs/app.log",
        enabled=True,
        restore_enabled=True,
        auto_restore=False,
    ),
    ServiceConfig(
        id="wppconnect",
        name="WppConnect",
        url=f"http://{_SOFIA_HOST_IP}:21465/api/default/status-session",
        enabled=True,
        restore_enabled=True,
        auto_restore=False,
    ),
    ServiceConfig(
        id="mayor_frontend",
        name="Mayor Frontend",
        url="https://127.0.0.1:5175",
        enabled=True,
        restore_enabled=True,
        auto_restore=False,
    ),
    ServiceConfig(
        id="packing_frontend",
        name="Packing Frontend",
        url="https://127.0.0.1:3000",
        enabled=True,
        restore_enabled=True,
        auto_restore=False,
    ),
    ServiceConfig(
        id="sofia_frontend",
        name="Sofia Frontend",
        url="http://127.0.0.1:5179",
        enabled=True,
        restore_enabled=True,
        auto_restore=False,
    ),
    ServiceConfig(
        id="diapi",
        name="SAP DIAPI Middleware",
        url="http://localhost:9000/api/Health/Ping",
        enabled=True,
        restore_enabled=True,
        auto_restore=False,
    ),
]


DEFAULT_APP_REPOS: List[AppRepoConfig] = [
    AppRepoConfig(
        id="sofia",
        name="Sofia Monitor",
        path="D:/sofia",
        autonomy_level=3,
        test_commands=["python -m compileall -q backend/app", "npm --prefix frontend run build"],
        smoke_urls=["http://192.168.0.123:5180/api/ping"],
        allowed_paths=["backend/app/", "frontend/src/", "sdk/"],
    ),
    AppRepoConfig(
        id="mayor",
        name="Mayor",
        path="D:/mayor",
        autonomy_level=3,
        autofix_enabled=True,
        smoke_urls=["http://192.168.0.123:8075/health", "http://localhost:9000/api/Health/Ping"],
        allowed_paths=["backend/", "frontend/src/", "sdk/"],
    ),
    AppRepoConfig(
        id="packing",
        name="Packing",
        path="D:/packing",
        autonomy_level=3,
        autofix_enabled=True,
        smoke_urls=["http://192.168.0.123:8100/health", "http://localhost:9000/api/Health/Ping"],
        allowed_paths=["backend/", "frontend/src/", "sdk/"],
    ),
]
