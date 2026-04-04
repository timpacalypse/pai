"""Home maintenance alert scheduler — checks for due tasks and sends email alerts."""

import asyncio
import logging
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from app.core.config import settings
from app.services.home_knowledge_service import get_alerts

logger = logging.getLogger("pai.services.home_alerts")


async def check_and_send_alerts() -> dict:
    """Check for overdue/upcoming tasks and send an email if any exist."""
    alerts = await get_alerts()

    if alerts["overdue_count"] == 0 and alerts["upcoming_count"] == 0:
        logger.info("home_alerts_check: nothing due")
        return {"checked": True, "email_sent": False, "reason": "nothing due"}

    email_sent = False
    if settings.gmail_address and settings.gmail_app_password:
        email_sent = _send_alert_email(alerts)

    summary = {
        "checked": True,
        "overdue": alerts["overdue_count"],
        "upcoming": alerts["upcoming_count"],
        "email_sent": email_sent,
    }
    logger.info("home_alerts_check_completed", extra=summary)
    return summary


def _send_alert_email(alerts: dict) -> bool:
    """Send an alert email for overdue and upcoming home tasks."""
    overdue = alerts["overdue"]
    upcoming = alerts["upcoming"]

    subject_parts = []
    if overdue:
        subject_parts.append(f"{len(overdue)} overdue")
    if upcoming:
        subject_parts.append(f"{len(upcoming)} upcoming")
    subject = f"PAI Home Alerts: {', '.join(subject_parts)}"

    html_body = _build_alert_html(overdue, upcoming)
    text_body = _build_alert_text(overdue, upcoming)

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"PAI Home Manager <{settings.gmail_address}>"
        msg["To"] = settings.gmail_recipient or settings.gmail_address

        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(settings.gmail_address, settings.gmail_app_password)
            server.send_message(msg)

        logger.info("home_alert_email_sent", extra={"to": msg["To"]})
        return True

    except Exception as e:
        logger.error("home_alert_email_failed: %s", e)
        return False


def _build_alert_html(overdue: list[dict], upcoming: list[dict]) -> str:
    """Build HTML email for home maintenance alerts."""
    now_str = datetime.now(timezone.utc).strftime("%B %d, %Y")

    def _task_rows(tasks: list[dict], color: str) -> str:
        rows = ""
        for t in tasks:
            due = ""
            if t.get("next_due_at"):
                due_dt = t["next_due_at"]
                if isinstance(due_dt, str):
                    due = due_dt[:10]
                else:
                    due = due_dt.strftime("%Y-%m-%d")
            recur = f"every {t['recurrence_days']}d" if t.get("recurrence_days") else "one-time"
            rows += f"""
            <tr style="border-bottom:1px solid #2a2d42;">
                <td style="padding:10px;color:{color};font-weight:600;">{t.get('item_name','—')}</td>
                <td style="padding:10px;color:#e4e6f0;">{t.get('description','')}</td>
                <td style="padding:10px;color:#8b8fa8;">{due}</td>
                <td style="padding:10px;color:#8b8fa8;">{recur}</td>
                <td style="padding:10px;color:#8b8fa8;">{t.get('priority','normal')}</td>
            </tr>"""
        return rows

    overdue_section = ""
    if overdue:
        overdue_section = f"""
            <h2 style="color:#f44336;font-size:18px;margin:20px 0 10px 0;">
                ⚠️ Overdue ({len(overdue)})
            </h2>
            <table style="width:100%;border-collapse:collapse;">
                <thead><tr style="border-bottom:2px solid #f44336;">
                    <th style="text-align:left;padding:8px;color:#f44336;">Item</th>
                    <th style="text-align:left;padding:8px;color:#f44336;">Task</th>
                    <th style="text-align:left;padding:8px;color:#f44336;">Due</th>
                    <th style="text-align:left;padding:8px;color:#f44336;">Recurrence</th>
                    <th style="text-align:left;padding:8px;color:#f44336;">Priority</th>
                </tr></thead>
                <tbody>{_task_rows(overdue, '#f44336')}</tbody>
            </table>"""

    upcoming_section = ""
    if upcoming:
        upcoming_section = f"""
            <h2 style="color:#ff9800;font-size:18px;margin:20px 0 10px 0;">
                📋 Upcoming ({len(upcoming)})
            </h2>
            <table style="width:100%;border-collapse:collapse;">
                <thead><tr style="border-bottom:2px solid #ff9800;">
                    <th style="text-align:left;padding:8px;color:#ff9800;">Item</th>
                    <th style="text-align:left;padding:8px;color:#ff9800;">Task</th>
                    <th style="text-align:left;padding:8px;color:#ff9800;">Due</th>
                    <th style="text-align:left;padding:8px;color:#ff9800;">Recurrence</th>
                    <th style="text-align:left;padding:8px;color:#ff9800;">Priority</th>
                </tr></thead>
                <tbody>{_task_rows(upcoming, '#ff9800')}</tbody>
            </table>"""

    return f"""
    <html>
    <body style="background:#0f1117;font-family:'Segoe UI',system-ui,sans-serif;margin:0;padding:20px;">
        <div style="max-width:700px;margin:0 auto;background:#161822;border-radius:12px;overflow:hidden;border:1px solid #2a2d42;">
            <div style="background:#1a1d2e;padding:24px 30px;border-bottom:1px solid #2a2d42;">
                <h1 style="color:#4f8ef7;margin:0;font-size:22px;">🏠 PAI Home Maintenance Alerts</h1>
                <p style="color:#8b8fa8;margin:6px 0 0 0;font-size:13px;">{now_str}</p>
            </div>
            <div style="padding:24px 30px;">
                {overdue_section}
                {upcoming_section}
            </div>
            <div style="padding:16px 30px;background:#1a1d2e;border-top:1px solid #2a2d42;text-align:center;">
                <p style="color:#5c6078;font-size:12px;margin:0;">
                    PAI Home Knowledge Base &bull;
                    <a href="http://localhost:3000" style="color:#4f8ef7;text-decoration:none;">Open PAI</a>
                </p>
            </div>
        </div>
    </body>
    </html>"""


def _build_alert_text(overdue: list[dict], upcoming: list[dict]) -> str:
    """Build plain-text email for home maintenance alerts."""
    lines = ["PAI Home Maintenance Alerts", "=" * 40, ""]

    if overdue:
        lines.append(f"OVERDUE ({len(overdue)}):")
        for t in overdue:
            due = ""
            if t.get("next_due_at"):
                due_dt = t["next_due_at"]
                due = due_dt[:10] if isinstance(due_dt, str) else due_dt.strftime("%Y-%m-%d")
            lines.append(f"  ! {t.get('item_name','—')}: {t.get('description','')} — due {due}")
        lines.append("")

    if upcoming:
        lines.append(f"UPCOMING ({len(upcoming)}):")
        for t in upcoming:
            due = ""
            if t.get("next_due_at"):
                due_dt = t["next_due_at"]
                due = due_dt[:10] if isinstance(due_dt, str) else due_dt.strftime("%Y-%m-%d")
            lines.append(f"  - {t.get('item_name','—')}: {t.get('description','')} — due {due}")

    return "\n".join(lines)


async def home_alert_scheduler_loop():
    """
    Background loop: checks for due home tasks daily and sends alerts.
    """
    interval_hours = settings.home_alert_hours
    if interval_hours <= 0:
        logger.info("home_alert_scheduler_disabled")
        return

    logger.info("home_alert_scheduler_started", extra={"interval_hours": interval_hours})

    while True:
        try:
            await check_and_send_alerts()
        except Exception as e:
            logger.error("home_alert_scheduler_failed: %s", e)

        await asyncio.sleep(interval_hours * 3600)
