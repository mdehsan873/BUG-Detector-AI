"""
Notification settings API routes.
GET/PUT notification settings per project, POST to test notifications.
"""

from fastapi import APIRouter, HTTPException

from app.database import get_supabase
from app.models.schemas import NotificationSettings
from app.services.notification_service import send_slack_notification, send_email_notification
from app.utils.logger import logger

router = APIRouter()


@router.get("/{project_id}/notifications", response_model=NotificationSettings)
async def get_notification_settings(project_id: str):
    """Get notification settings for a project."""
    db = get_supabase()

    result = (
        db.table("projects")
        .select("notification_email_enabled, notification_email_address, notification_slack_enabled, notification_slack_webhook_url")
        .eq("id", project_id)
        .eq("is_active", True)
        .single()
        .execute()
    )

    if not result.data:
        raise HTTPException(status_code=404, detail="Project not found")

    p = result.data
    return NotificationSettings(
        email_enabled=p.get("notification_email_enabled", True),
        email_address=p.get("notification_email_address"),
        slack_enabled=p.get("notification_slack_enabled", False),
        slack_webhook_url=p.get("notification_slack_webhook_url"),
    )


@router.put("/{project_id}/notifications", response_model=NotificationSettings)
async def update_notification_settings(project_id: str, settings: NotificationSettings):
    """Update notification settings for a project."""
    db = get_supabase()

    # Verify project exists
    project = (
        db.table("projects")
        .select("id")
        .eq("id", project_id)
        .eq("is_active", True)
        .single()
        .execute()
    )

    if not project.data:
        raise HTTPException(status_code=404, detail="Project not found")

    # Update notification fields
    update_data = {
        "notification_email_enabled": settings.email_enabled,
        "notification_email_address": settings.email_address or "",
        "notification_slack_enabled": settings.slack_enabled,
        "notification_slack_webhook_url": settings.slack_webhook_url or "",
    }

    db.table("projects").update(update_data).eq("id", project_id).execute()

    logger.info(f"Updated notification settings for project {project_id}")
    return settings


@router.post("/{project_id}/notifications/test")
async def test_notification(project_id: str, channel: str = "all"):
    """Send a test notification for a project."""
    db = get_supabase()

    result = (
        db.table("projects")
        .select("*")
        .eq("id", project_id)
        .eq("is_active", True)
        .single()
        .execute()
    )

    if not result.data:
        raise HTTPException(status_code=404, detail="Project not found")

    p = result.data
    test_issue = {
        "title": "Test Notification",
        "description": "This is a test notification from Buglyft to verify your notification settings are working correctly.",
        "severity": "medium",
        "category": "test",
        "page_url": "https://example.com/test",
    }

    results = {}

    if channel in ("all", "email"):
        email = p.get("notification_email_address", "")
        if email:
            success = send_email_notification(email, test_issue, p["name"])
            results["email"] = "sent" if success else "failed"
        else:
            results["email"] = "no_email_configured"

    if channel in ("all", "slack"):
        webhook = p.get("notification_slack_webhook_url", "")
        if webhook:
            success = await send_slack_notification(webhook, test_issue, p["name"])
            results["slack"] = "sent" if success else "failed"
        else:
            results["slack"] = "no_webhook_configured"

    return {"message": "Test notifications sent", "results": results}
