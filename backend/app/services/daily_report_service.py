import logging
from datetime import datetime

from app.services import db_service, whatsapp_service
from app.services.config_service import load_config

logger = logging.getLogger("sofia.daily_report")


def _line_items(rows: list[dict], label_keys: list[str], count_key: str = "count", limit: int = 6) -> str:
    if not rows:
        return "  - ninguno"
    lines = []
    for row in rows[:limit]:
        label = " / ".join(str(row.get(k, "-")) for k in label_keys)
        count = row.get(count_key, row.get("occurrences", row.get("issues", 0)))
        lines.append(f"  - {label}: {count}")
    return "\n".join(lines)


def format_daily_report(data: dict, since_hours: int = 24) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    issues_by_service = data.get("issues_by_service", [])
    top_issue_lines = []
    for row in issues_by_service[:5]:
        top_issue_lines.append(
            f"  - {row.get('service_id')} {row.get('level')}: "
            f"{row.get('issues')} issues / {row.get('occurrences')} ocurrencias"
        )
    if not top_issue_lines:
        top_issue_lines.append("  - ninguno")

    return (
        f"📊 *Reporte diario Sofia* ({since_hours}h)\n"
        f"_Generado: {now}_\n\n"
        f"*Errores*\n"
        f"- Issues vistos: {data.get('issues_total', 0)}\n"
        f"- Ocurrencias: {data.get('occurrences_total', 0)}\n"
        f"- Abiertos: {data.get('unresolved_total', 0)} "
        f"(CRITICAL {data.get('critical_open', 0)} / ERROR {data.get('error_open', 0)})\n"
        f"- Métricas registradas: {data.get('metrics_total', 0)}\n\n"
        f"*Top servicios/niveles*\n"
        f"{chr(10).join(top_issue_lines)}\n\n"
        f"*AI Engineer*\n"
        f"{_line_items(data.get('jobs_by_status', []), ['status'])}\n\n"
        f"*Acciones / restores*\n"
        f"Restores:\n{_line_items(data.get('restores_by_status', []), ['status'])}\n"
        f"Acciones:\n{_line_items(data.get('actions_by_status', []), ['action_type', 'status'])}\n\n"
        f"*GitHub sync*\n"
        f"{_line_items(data.get('github_sync_by_status', []), ['status'])}\n\n"
        f"*Revisión nocturna*\n"
        f"- Reportes: {data.get('nightly_reports', 0)}\n"
        f"- Proposal runs: {data.get('proposal_runs', 0)}\n\n"
        f"*Alertas*\n"
        f"- Pendientes en cola WhatsApp: {data.get('queued_alerts_pending', 0)}\n\n"
        f"*Auditoría*\n"
        f"{_line_items(data.get('audit_events', []), ['event_type'])}"
    )


async def build_daily_report(since_hours: int = 24) -> str:
    data = await db_service.get_activity_counts(since_hours=since_hours)
    return format_daily_report(data, since_hours=since_hours)


async def send_daily_report(since_hours: int = 24) -> bool:
    cfg = load_config()
    report = await build_daily_report(since_hours=since_hours)
    sent = await whatsapp_service.send_message(cfg.alerts, report)
    logger.info(f"[DAILY_REPORT] sent={sent}")
    return sent
