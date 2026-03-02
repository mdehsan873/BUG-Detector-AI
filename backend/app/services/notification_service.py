"""
Notification service for sending issue alerts via email and Slack.
"""

import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Any

import httpx

from app.config import get_settings
from app.utils.logger import logger


async def send_slack_notification(
    webhook_url: str,
    issue: dict[str, Any],
    project_name: str,
) -> bool:
    """Send an issue notification to a Slack channel via webhook."""
    severity_emoji = {
        "critical": ":red_circle:",
        "high": ":large_orange_circle:",
        "medium": ":large_yellow_circle:",
        "low": ":white_circle:",
    }

    emoji = severity_emoji.get(issue.get("severity", "medium"), ":warning:")
    title = issue.get("title", "Unknown Issue")
    description = issue.get("description", issue.get("error_message", ""))
    page_url = issue.get("page_url", "N/A")
    category = issue.get("category", issue.get("event_type", "unknown"))

    payload = {
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{emoji} New Issue Detected",
                },
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Project:*\n{project_name}"},
                    {"type": "mrkdwn", "text": f"*Severity:*\n{issue.get('severity', 'medium').upper()}"},
                    {"type": "mrkdwn", "text": f"*Category:*\n{category.replace('_', ' ').title()}"},
                    {"type": "mrkdwn", "text": f"*Page:*\n{page_url}"},
                ],
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{title}*\n{description[:500]}",
                },
            },
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(webhook_url, json=payload)
            if response.status_code == 200:
                logger.info(f"Slack notification sent: {title}")
                return True
            else:
                logger.error(f"Slack notification failed: HTTP {response.status_code}")
                return False
    except Exception as e:
        logger.error(f"Slack notification error: {e}")
        return False


def send_email_notification(
    to_email: str,
    issue: dict[str, Any],
    project_name: str,
) -> bool:
    """Send an issue notification via email using SMTP."""
    settings = get_settings()

    if not settings.smtp_host:
        logger.warning("SMTP not configured, skipping email notification")
        return False

    severity = issue.get("severity", "medium")
    title = issue.get("title", "Unknown Issue")
    description = issue.get("description", issue.get("error_message", ""))
    page_url = issue.get("page_url", "N/A")

    subject = f"[Buglyft] [{severity.upper()}] {title}"

    html_body = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 600px; margin: 0 auto; padding: 24px;">
      <div style="background: #4f46e5; color: white; padding: 20px 24px; border-radius: 12px 12px 0 0;">
        <h2 style="margin: 0; font-size: 18px;">New Issue Detected</h2>
        <p style="margin: 4px 0 0; opacity: 0.8; font-size: 14px;">{project_name}</p>
      </div>
      <div style="background: white; border: 1px solid #e2e5f0; border-top: none; padding: 24px; border-radius: 0 0 12px 12px;">
        <div style="display: inline-block; background: {'#fee2e2' if severity in ('critical', 'high') else '#fef3c7'}; color: {'#991b1b' if severity in ('critical', 'high') else '#92400e'}; padding: 4px 12px; border-radius: 6px; font-size: 12px; font-weight: 600; margin-bottom: 16px;">
          {severity.upper()}
        </div>
        <h3 style="margin: 0 0 8px; color: #1a1d23; font-size: 16px;">{title}</h3>
        <p style="margin: 0 0 16px; color: #4b5063; font-size: 14px; line-height: 1.6;">{description[:800]}</p>
        <div style="background: #f8f9fb; padding: 12px 16px; border-radius: 8px; font-size: 13px; color: #4b5063;">
          <strong>Page:</strong> {page_url}
        </div>
      </div>
      <p style="text-align: center; color: #8b90a0; font-size: 12px; margin-top: 16px;">
        Sent by Buglyft
      </p>
    </div>
    """

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = settings.smtp_from_email or settings.smtp_user
        msg["To"] = to_email
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
            if settings.smtp_port == 587:
                server.starttls()
            if settings.smtp_user and settings.smtp_password:
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(msg)

        logger.info(f"Email notification sent to {to_email}: {title}")
        return True

    except Exception as e:
        logger.error(f"Email notification error: {e}")
        return False
