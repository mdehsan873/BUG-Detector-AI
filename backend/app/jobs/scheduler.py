import asyncio
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_settings
from app.database import get_supabase
from app.connectors import get_connector
from app.services.crypto_service import decrypt_token
from app.services.session_analysis_service import run_session_analysis
from app.utils.logger import logger

from app.api.routes.analysis import _store_ai_issues, _send_issue_notifications


def _run_all_analyses():
    """Synchronous wrapper that runs the async AI analysis for all active projects."""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_run_all_analyses_async())
        loop.close()
    except Exception as e:
        logger.error(f"Scheduler job failed: {e}")


async def _run_all_analyses_async():
    """Run AI session analysis for all active projects."""
    db = get_supabase()

    projects = (
        db.table("projects")
        .select("*")
        .eq("is_active", True)
        .execute()
    )

    if not projects.data:
        logger.info("No active projects to analyze")
        return

    for project in projects.data:
        try:
            await _run_analysis_for_project(project)
        except Exception as e:
            logger.error(f"AI analysis failed for project {project['id']}: {e}")
            continue


async def _run_analysis_for_project(project: dict) -> None:
    """Run AI session analysis for a single project (called by scheduler)."""
    project_id = project["id"]
    provider = project.get("session_provider", "posthog")

    try:
        api_key = decrypt_token(project["provider_api_key"])
    except Exception as e:
        logger.error(f"Cannot decrypt API key for project {project_id}: {e}")
        return

    # Debug: log key prefix to verify decryption works correctly in production
    key_preview = api_key[:8] if len(api_key) > 8 else "???"
    host = project.get("provider_host", "")
    proj_ext_id = project.get("provider_project_id", "")
    logger.info(
        f"Scheduler: project {project_id} — key starts with '{key_preview}...', "
        f"host={host}, ext_project={proj_ext_id}"
    )

    connector = get_connector(
        provider=provider,
        api_key=api_key,
        project_id=proj_ext_id,
        host=host,
    )

    since = datetime.now(timezone.utc) - timedelta(hours=24)

    logger.info(f"Scheduler: starting AI analysis for project {project_id} ({provider})")

    result = await run_session_analysis(
        connector=connector,
        since=since,
        max_sessions=30,
        db_project_id=project_id,
    )

    if result["issues"]:
        await _store_ai_issues(project_id, result["issues"])
        await _send_issue_notifications(project, result["issues"])

    logger.info(
        f"Scheduler: project {project_id} done — "
        f"{result['sessions_analyzed']} sessions, {result['issues_found']} issues"
    )


def start_scheduler() -> BackgroundScheduler:
    """Start the APScheduler with the AI analysis job running every 5 minutes."""
    settings = get_settings()
    scheduler = BackgroundScheduler()

    scheduler.add_job(
        func=_run_all_analyses,
        trigger=IntervalTrigger(minutes=settings.detection_interval_minutes),
        id="ai_session_analysis",
        name="Run AI session analysis for all projects",
        replace_existing=True,
        max_instances=1,
    )

    scheduler.start()
    logger.info(
        f"Scheduler started: AI analysis running every {settings.detection_interval_minutes} minutes"
    )
    return scheduler


def stop_scheduler(scheduler: BackgroundScheduler) -> None:
    """Gracefully stop the scheduler."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
