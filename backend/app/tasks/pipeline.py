from datetime import datetime, timezone

from app.database import get_supabase
from app.services.crypto_service import decrypt_token
from app.services.posthog_service import fetch_posthog_events
from app.services.clustering_service import detect_anomalies
from app.services.openai_service import generate_bug_report
from app.services.github_service import create_github_issue, update_github_issue_comment
from app.utils.cost_tracker import CostTracker
from app.utils.logger import logger


async def run_pipeline_for_project(project_id: str) -> None:
    """
    Full detection pipeline for a single project:
    1. Fetch events from PostHog
    2. Store events in database
    3. Detect anomalies
    4. Generate bug reports via OpenAI
    5. Create/update GitHub issues
    """
    db = get_supabase()

    # Load project
    project_result = (
        db.table("projects")
        .select("*")
        .eq("id", project_id)
        .eq("is_active", True)
        .single()
        .execute()
    )
    if not project_result.data:
        logger.error(f"Project {project_id} not found or inactive")
        return

    project = project_result.data
    posthog_api_key = decrypt_token(project["posthog_api_key"])
    github_token = decrypt_token(project["github_token"])

    # Determine last fetch time
    last_run = (
        db.table("job_runs")
        .select("last_fetched_at")
        .eq("project_id", project_id)
        .eq("status", "completed")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    if last_run.data:
        since = datetime.fromisoformat(last_run.data[0]["last_fetched_at"].replace("Z", "+00:00"))
    else:
        # First run: look back 1 hour
        since = datetime.now(timezone.utc).replace(hour=datetime.now(timezone.utc).hour - 1)

    now = datetime.now(timezone.utc)

    # Create job run record
    job_run = (
        db.table("job_runs")
        .insert({
            "project_id": project_id,
            "last_fetched_at": now.isoformat(),
            "status": "running",
            "events_fetched": 0,
            "anomalies_detected": 0,
            "issues_created": 0,
        })
        .execute()
    )
    job_run_id = job_run.data[0]["id"]

    try:
        # ── Step 1: Fetch events ─────────────────────────────────────────
        events = await fetch_posthog_events(
            api_key=posthog_api_key,
            project_id=project["posthog_project_id"],
            since=since,
        )

        if not events:
            logger.info(f"No new events for project {project_id}")
            _update_job_run(db, job_run_id, "completed", 0, 0, 0)
            return

        # ── Step 2: Store events (skip auxiliary _pageview/_pageleave) ───
        event_rows = [
            {
                "project_id": project_id,
                "event_type": e["event_type"],
                "fingerprint": e["fingerprint"],
                "error_message": e.get("error_message"),
                "endpoint": e.get("endpoint"),
                "page_url": e.get("page_url"),
                "css_selector": e.get("css_selector"),
                "session_id": e.get("session_id"),
                "user_id": e.get("user_id"),
                "status_code": e.get("status_code"),
                "raw_properties": e.get("raw_properties"),
                "timestamp": e["timestamp"],
            }
            for e in events
            if not e["event_type"].startswith("_")  # Skip _pageview, _pageleave
        ]

        # Insert in batches of 50
        for i in range(0, len(event_rows), 50):
            batch = event_rows[i : i + 50]
            db.table("events").insert(batch).execute()

        logger.info(f"Stored {len(event_rows)} events for project {project_id}")

        # ── Step 3: Detect anomalies ─────────────────────────────────────
        anomaly_clusters = detect_anomalies(
            events=events,
            threshold=project["detection_threshold"],
        )

        if not anomaly_clusters:
            logger.info(f"No anomalies detected for project {project_id}")
            _update_job_run(db, job_run_id, "completed", len(events), 0, 0)
            return

        # ── Step 4 & 5: Generate reports and create issues ───────────────
        cost_tracker = CostTracker()
        issues_created = 0

        for cluster in anomaly_clusters:
            fp = cluster["fingerprint"]

            # Upsert anomaly cluster
            existing_cluster = (
                db.table("anomaly_clusters")
                .select("id, status")
                .eq("project_id", project_id)
                .eq("fingerprint", fp)
                .execute()
            )

            if existing_cluster.data:
                # Update existing cluster
                update_data = {
                    "count": cluster["count"],
                    "affected_users": cluster["affected_users"],
                    "last_seen": cluster["last_seen"],
                    "sample_session_ids": cluster["sample_session_ids"],
                }
                if cluster.get("session_event_times"):
                    update_data["session_event_times"] = cluster["session_event_times"]
                db.table("anomaly_clusters").update(update_data).eq("project_id", project_id).eq("fingerprint", fp).execute()

                # Check if already has a GitHub issue
                existing_issue = (
                    db.table("github_issues")
                    .select("github_issue_id")
                    .eq("project_id", project_id)
                    .eq("cluster_fingerprint", fp)
                    .execute()
                )

                if existing_issue.data:
                    # Update existing issue with comment
                    update_github_issue_comment(
                        repo_name=project["github_repo"],
                        token=github_token,
                        issue_number=existing_issue.data[0]["github_issue_id"],
                        cluster=cluster,
                    )
                    continue

            else:
                # Insert new cluster
                insert_data = {
                    "project_id": project_id,
                    "fingerprint": fp,
                    "event_type": cluster["event_type"],
                    "error_message": cluster.get("error_message"),
                    "endpoint": cluster.get("endpoint"),
                    "css_selector": cluster.get("css_selector"),
                    "page_url": cluster.get("page_url"),
                    "count": cluster["count"],
                    "affected_users": cluster["affected_users"],
                    "first_seen": cluster["first_seen"],
                    "last_seen": cluster["last_seen"],
                    "sample_session_ids": cluster["sample_session_ids"],
                    "status": "new",
                }
                if cluster.get("session_event_times"):
                    insert_data["session_event_times"] = cluster["session_event_times"]
                db.table("anomaly_clusters").insert(insert_data).execute()

            # Generate bug report via OpenAI
            report = await generate_bug_report(cluster, cost_tracker=cost_tracker)
            if not report:
                continue

            # Create GitHub issue
            issue_result = create_github_issue(
                repo_name=project["github_repo"],
                token=github_token,
                report=report,
                cluster=cluster,
                posthog_project_id=project["posthog_project_id"],
            )

            if issue_result:
                # Store issue mapping
                db.table("github_issues").insert({
                    "project_id": project_id,
                    "cluster_fingerprint": fp,
                    "github_issue_id": issue_result["github_issue_id"],
                    "github_issue_url": issue_result["github_issue_url"],
                    "status": "open",
                }).execute()

                # Update cluster status
                db.table("anomaly_clusters").update({
                    "status": "github_issued",
                }).eq("project_id", project_id).eq("fingerprint", fp).execute()

                issues_created += 1

        _update_job_run(
            db, job_run_id, "completed",
            len(events), len(anomaly_clusters), issues_created,
        )

        # Log AI cost summary for this pipeline run
        cost_tracker.log_summary(analysis_id=project_id)

        logger.info(
            f"Pipeline complete for project {project_id}: "
            f"{len(events)} events, {len(anomaly_clusters)} anomalies, "
            f"{issues_created} issues created"
        )

    except Exception as e:
        logger.error(f"Pipeline failed for project {project_id}: {e}")
        _update_job_run(db, job_run_id, "failed", 0, 0, 0, str(e))
        raise


def _update_job_run(
    db,
    job_run_id: str,
    status: str,
    events_fetched: int,
    anomalies_detected: int,
    issues_created: int,
    error_message: str | None = None,
) -> None:
    """Update a job run record with results."""
    update_data = {
        "status": status,
        "events_fetched": events_fetched,
        "anomalies_detected": anomalies_detected,
        "issues_created": issues_created,
    }
    if error_message:
        update_data["error_message"] = error_message

    db.table("job_runs").update(update_data).eq("id", job_run_id).execute()
