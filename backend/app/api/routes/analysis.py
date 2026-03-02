"""
Session analysis API routes.
Provides endpoints to trigger AI session analysis and poll progress.
"""

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from urllib.parse import urlparse, urlunparse

from fastapi import APIRouter, HTTPException

from app.utils.cost_tracker import CostTracker


def _normalize_url(url: str) -> str:
    """Normalize a URL: strip fragment, query string, trailing slash, lowercase."""
    if not url:
        return ""
    url = url.strip()
    if url.startswith("http://") or url.startswith("https://"):
        parsed = urlparse(url)
        cleaned = urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
        return cleaned.rstrip("/").lower()
    url = url.split("#")[0].split("?")[0]
    return url.rstrip("/").lower()


def _text_similarity(a: str, b: str) -> float:
    """
    Return similarity ratio (0.0–1.0) between two strings.
    Uses SequenceMatcher which handles insertions, deletions, and substitutions.
    """
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()

from app.connectors import get_connector
from app.database import get_supabase
from app.models.schemas import (
    AnalysisProgressResponse,
    AnalysisTriggerResponse,
    SessionIssue,
)
from app.services.crypto_service import decrypt_token
from app.services.notification_service import (
    send_email_notification,
    send_slack_notification,
)
from app.services.session_analysis_service import run_session_analysis
from app.utils.logger import logger

router = APIRouter()


# ── Persistent analysis state (Supabase-backed) ─────────────────────────────
# Replaces the old in-memory _analysis_store dict. Each analysis run is stored
# in the `analysis_runs` table so state survives server restarts and works
# across multiple instances.

def _save_analysis_state(analysis_id: str, state: dict) -> None:
    """Persist analysis state to Supabase."""
    db = get_supabase()
    try:
        # Serialize issues to JSON string for DB storage
        db_state = dict(state)
        if "issues" in db_state:
            db_state["issues_json"] = json.dumps(db_state.pop("issues"), default=str)
        if "ai_cost" in db_state:
            db_state["ai_cost_json"] = json.dumps(db_state.pop("ai_cost"), default=str)

        existing = (
            db.table("analysis_runs")
            .select("id")
            .eq("id", analysis_id)
            .execute()
        )

        if existing.data:
            db.table("analysis_runs").update(db_state).eq("id", analysis_id).execute()
        else:
            db_state["id"] = analysis_id
            db.table("analysis_runs").insert(db_state).execute()
    except Exception as e:
        logger.warning(f"Failed to persist analysis state: {e}")


def _load_analysis_state(analysis_id: str) -> dict | None:
    """Load analysis state from Supabase."""
    db = get_supabase()
    try:
        result = (
            db.table("analysis_runs")
            .select("*")
            .eq("id", analysis_id)
            .single()
            .execute()
        )
        if not result.data:
            return None

        state = dict(result.data)
        # Deserialize JSON fields
        if "issues_json" in state:
            try:
                state["issues"] = json.loads(state.pop("issues_json"))
            except (json.JSONDecodeError, TypeError):
                state["issues"] = []
        if "ai_cost_json" in state:
            try:
                state["ai_cost"] = json.loads(state.pop("ai_cost_json"))
            except (json.JSONDecodeError, TypeError):
                state["ai_cost"] = {}

        return state
    except Exception as e:
        logger.warning(f"Failed to load analysis state: {e}")
        return None


def _load_latest_analysis(project_id: str) -> dict | None:
    """Load the most recent analysis for a project from Supabase."""
    db = get_supabase()
    try:
        result = (
            db.table("analysis_runs")
            .select("*")
            .eq("project_id", project_id)
            .order("started_at", desc=True)
            .limit(1)
            .execute()
        )
        if not result.data:
            return None

        state = dict(result.data[0])
        if "issues_json" in state:
            try:
                state["issues"] = json.loads(state.pop("issues_json"))
            except (json.JSONDecodeError, TypeError):
                state["issues"] = []
        if "ai_cost_json" in state:
            try:
                state["ai_cost"] = json.loads(state.pop("ai_cost_json"))
            except (json.JSONDecodeError, TypeError):
                state["ai_cost"] = {}

        return state
    except Exception as e:
        logger.warning(f"Failed to load latest analysis: {e}")
        return None


def _find_running_analysis(project_id: str) -> dict | None:
    """Check if there's already a running analysis for this project."""
    db = get_supabase()
    try:
        result = (
            db.table("analysis_runs")
            .select("id, project_id, status")
            .eq("project_id", project_id)
            .eq("status", "running")
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None
    except Exception:
        return None


@router.get("/{project_id}/analyze/latest", response_model=AnalysisProgressResponse)
async def get_latest_analysis(project_id: str):
    """Get the most recent analysis for a project. Falls back to DB anomalies."""
    # Check persistent store
    latest = _load_latest_analysis(project_id)

    if latest:
        return AnalysisProgressResponse(
            project_id=project_id,
            status=latest["status"],
            sessions_total=latest.get("sessions_total", 0),
            sessions_analyzed=latest.get("sessions_analyzed", 0),
            issues_found=latest.get("issues_found", 0),
            issues=[SessionIssue(**i) for i in latest.get("issues", [])],
            started_at=latest.get("started_at"),
            completed_at=latest.get("completed_at"),
        )

    # Fall back to DB: load AI-detected anomaly clusters as issues
    db = get_supabase()
    ai_clusters = (
        db.table("anomaly_clusters")
        .select("*")
        .eq("project_id", project_id)
        .like("event_type", "ai_%")
        .order("last_seen", desc=True)
        .limit(50)
        .execute()
    )

    if ai_clusters.data:
        issues = []
        for c in ai_clusters.data:
            category = c.get("event_type", "ai_detected").replace("ai_", "")

            # Parse ai_details if available
            ai_details = {}
            raw_ai = c.get("ai_details")
            if raw_ai:
                if isinstance(raw_ai, str):
                    try:
                        ai_details = json.loads(raw_ai)
                    except json.JSONDecodeError:
                        ai_details = {}
                elif isinstance(raw_ai, dict):
                    ai_details = raw_ai

            description = ai_details.get("description", "")
            if not description:
                description = f"{c.get('count', 1)} occurrences affecting {c.get('affected_users', 1)} users"

            issues.append(SessionIssue(
                title=c.get("error_message", "Unknown Issue"),
                description=description,
                why_issue=ai_details.get("why_issue"),
                reproduction_steps=ai_details.get("reproduction_steps", []),
                severity=ai_details.get("severity", "medium"),
                category=ai_details.get("category", category),
                evidence=ai_details.get("evidence", []),
                page_url=c.get("page_url"),
                confidence=ai_details.get("confidence", 0.85),
                session_id=c["sample_session_ids"][0] if c.get("sample_session_ids") else None,
                fingerprint=c.get("fingerprint"),
            ))
        return AnalysisProgressResponse(
            project_id=project_id,
            status="completed",
            sessions_total=len(ai_clusters.data),
            sessions_analyzed=len(ai_clusters.data),
            issues_found=len(issues),
            issues=issues,
            completed_at=ai_clusters.data[0].get("last_seen") if ai_clusters.data else None,
        )

    return AnalysisProgressResponse(
        project_id=project_id,
        status="none",
    )


@router.post("/{project_id}/analyze", response_model=AnalysisTriggerResponse)
async def trigger_analysis(project_id: str):
    """Start AI session analysis for a project."""
    db = get_supabase()

    project_result = (
        db.table("projects")
        .select("*")
        .eq("id", project_id)
        .eq("is_active", True)
        .single()
        .execute()
    )

    if not project_result.data:
        raise HTTPException(status_code=404, detail="Project not found")

    # Check if analysis already running for this project (persistent check)
    running = _find_running_analysis(project_id)
    if running:
        return AnalysisTriggerResponse(
            message="Analysis already in progress",
            analysis_id=running["id"],
        )

    analysis_id = str(uuid.uuid4())
    initial_state = {
        "project_id": project_id,
        "status": "running",
        "sessions_total": 0,
        "sessions_analyzed": 0,
        "issues_found": 0,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
    }
    _save_analysis_state(analysis_id, initial_state)

    # Run analysis in background
    asyncio.create_task(
        _run_analysis_background(analysis_id, project_result.data)
    )

    return AnalysisTriggerResponse(
        message="Session analysis started",
        analysis_id=analysis_id,
    )


@router.get("/{project_id}/analyze/{analysis_id}", response_model=AnalysisProgressResponse)
async def get_analysis_progress(project_id: str, analysis_id: str):
    """Get progress of an ongoing or completed analysis."""
    state = _load_analysis_state(analysis_id)
    if not state or state.get("project_id") != project_id:
        raise HTTPException(status_code=404, detail="Analysis not found")
    return AnalysisProgressResponse(
        project_id=project_id,
        status=state["status"],
        sessions_total=state.get("sessions_total", 0),
        sessions_analyzed=state.get("sessions_analyzed", 0),
        issues_found=state.get("issues_found", 0),
        issues=[SessionIssue(**i) for i in state.get("issues", [])],
        started_at=state.get("started_at"),
        completed_at=state.get("completed_at"),
        ai_cost=state.get("ai_cost") or None,
    )


async def _run_analysis_background(analysis_id: str, project: dict):
    """Background task: run full AI session analysis."""
    # Keep a local copy for fast progress updates; persist periodically
    state = {
        "project_id": project["id"],
        "status": "running",
        "sessions_total": 0,
        "sessions_analyzed": 0,
        "issues_found": 0,
        "issues": [],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
    }

    _last_persist_at = [0.0]  # mutable ref for closure

    try:
        provider = project.get("session_provider", "posthog")
        api_key = decrypt_token(project["provider_api_key"])
        connector = get_connector(
            provider=provider,
            api_key=api_key,
            project_id=project.get("provider_project_id", ""),
            host=project.get("provider_host", ""),
        )
        since = datetime.now(timezone.utc) - timedelta(hours=24)

        import time as _time

        async def progress_cb(analyzed: int, total: int, issues: int):
            state["sessions_analyzed"] = analyzed
            state["sessions_total"] = total
            state["issues_found"] = issues
            # Persist progress every 5 seconds to avoid hammering DB
            now = _time.time()
            if now - _last_persist_at[0] >= 5.0:
                _save_analysis_state(analysis_id, state)
                _last_persist_at[0] = now

        min_users = project.get("min_sessions_threshold", 2)
        skip_pages = project.get("skip_page_patterns") or []

        # ── Load dismissed issues as AI memory ──────────────────────────
        dismissed_patterns = _load_dismissed_patterns(project["id"])

        # ── Create cost tracker for this analysis run ───────────────────
        cost_tracker = CostTracker()

        result = await run_session_analysis(
            connector=connector,
            since=since,
            max_sessions=1000000,
            progress_callback=progress_cb,
            db_project_id=project["id"],
            min_users=min_users,
            skip_page_patterns=skip_pages,
            dismissed_patterns=dismissed_patterns,
            cost_tracker=cost_tracker,
        )

        state["status"] = "completed"
        state["sessions_analyzed"] = result["sessions_analyzed"]
        state["sessions_total"] = result["sessions_analyzed"]
        state["issues_found"] = result["issues_found"]
        state["issues"] = result["issues"]
        state["completed_at"] = datetime.now(timezone.utc).isoformat()
        state["ai_cost"] = result.get("ai_cost", {})

        # Persist final state
        _save_analysis_state(analysis_id, state)

        # Store AI-found issues as anomaly clusters in the DB
        if result["issues"]:
            await _store_ai_issues(project["id"], result["issues"], project)
            # Send notifications for found issues
            await _send_issue_notifications(project, result["issues"])

        ai_cost = result.get("ai_cost", {})
        logger.info(
            f"Analysis {analysis_id} completed: "
            f"{result['sessions_analyzed']} sessions, {result['issues_found']} issues, "
            f"AI cost: ${ai_cost.get('total_cost_usd', 0):.4f} "
            f"({ai_cost.get('total_tokens', 0):,} tokens, {ai_cost.get('total_calls', 0)} calls)"
        )

    except Exception as e:
        state["status"] = "failed"
        state["completed_at"] = datetime.now(timezone.utc).isoformat()
        _save_analysis_state(analysis_id, state)
        logger.error(f"Analysis {analysis_id} failed: {e}")


def _load_dismissed_patterns(project_id: str) -> list[dict]:
    """
    Load issues marked as 'not_an_issue' for a project.
    These become AI memory — the AI will avoid detecting similar patterns.
    Returns list of {"title": str, "category": str, "page_url": str, "description": str}
    """
    db = get_supabase()
    try:
        result = (
            db.table("anomaly_clusters")
            .select("error_message, event_type, page_url, ai_details")
            .eq("project_id", project_id)
            .eq("status", "not_an_issue")
            .limit(50)
            .execute()
        )
        if not result.data:
            return []

        patterns = []
        for row in result.data:
            ai_details = {}
            raw_ai = row.get("ai_details")
            if raw_ai:
                if isinstance(raw_ai, str):
                    try:
                        ai_details = json.loads(raw_ai)
                    except json.JSONDecodeError:
                        ai_details = {}
                elif isinstance(raw_ai, dict):
                    ai_details = raw_ai

            patterns.append({
                "title": row.get("error_message", ""),
                "category": ai_details.get("category", row.get("event_type", "")),
                "page_url": row.get("page_url", ""),
                "description": ai_details.get("description", ""),
                "why_dismissed": ai_details.get("why_issue", ""),
            })

        logger.info(f"Loaded {len(patterns)} dismissed patterns as AI memory for project {project_id}")
        return patterns

    except Exception as e:
        logger.warning(f"Failed to load dismissed patterns: {e}")
        return []


async def _store_ai_issues(project_id: str, issues: list[dict], project: dict | None = None):
    """
    Store AI-detected issues as anomaly clusters in the database.
    Properly merges duplicates: increments count, appends sessions,
    tracks distinct affected users, and merges timestamps.
    Optionally creates GitHub issues if project has GitHub integration configured.
    """
    db = get_supabase()
    now = datetime.now(timezone.utc).isoformat()

    # Load dismissed fingerprints for this project
    dismissed_result = db.table("dismissed_fingerprints").select("fingerprint").eq("project_id", project_id).execute()
    dismissed_fps = {r["fingerprint"] for r in dismissed_result.data} if dismissed_result.data else set()

    # GitHub integration settings
    github_repo = ""
    github_token = ""
    provider_project_id = ""
    session_provider = "posthog"
    provider_host = ""
    if project:
        github_repo = project.get("github_repo", "")
        encrypted_gh_token = project.get("github_token", "")
        if github_repo and encrypted_gh_token:
            try:
                github_token = decrypt_token(encrypted_gh_token)
            except Exception:
                github_token = ""
        provider_project_id = project.get("provider_project_id", "")
        session_provider = project.get("session_provider", "posthog")
        provider_host = project.get("provider_host", "")

    for issue in issues:
        fp = issue.get("fingerprint", "")
        if not fp:
            continue

        # Skip dismissed fingerprints
        if fp in dismissed_fps:
            logger.info(f"Skipping dismissed fingerprint: {fp[:16]}...")
            continue

        session_id = issue.get("session_id", "")
        distinct_id = issue.get("distinct_id", "")
        category = issue.get("category", "ai_detected")
        # Rule-based issues use category directly; legacy AI issues get ai_ prefix
        rule_id = issue.get("rule_id", "")
        event_type = category if rule_id else f"ai_{category}"

        # Build session_event_times using real PostHog event timestamps
        session_event_times: dict[str, str] = {}
        session_start_times: dict[str, str] = {}
        if session_id:
            event_ts = issue.get("_event_timestamp")
            session_start = issue.get("_session_start")
            if event_ts:
                session_event_times[session_id] = event_ts
            else:
                # Fallback: try extracting from evidence
                import re as _re
                evidence = issue.get("evidence", [])
                for ev in evidence:
                    ts = None
                    if isinstance(ev, dict) and ev.get("timestamp"):
                        ts = ev["timestamp"]
                    elif isinstance(ev, str):
                        match = _re.search(r"(\d{4}-\d{2}-\d{2}T[\d:.+Z-]+)", ev)
                        if match:
                            ts = match.group(1)
                    if ts:
                        session_event_times[session_id] = ts
                        break
                if session_id not in session_event_times:
                    session_event_times[session_id] = now
            if session_start:
                session_start_times[session_id] = session_start

        # Check for existing cluster with same fingerprint
        existing = (
            db.table("anomaly_clusters")
            .select("*")
            .eq("project_id", project_id)
            .eq("fingerprint", fp)
            .execute()
        )

        # Also check for similar open issues on the same page with same category
        if not existing.data:
            page_url = issue.get("page_url", "")
            if page_url:
                event_type_variants = [event_type]
                if event_type.startswith("ai_"):
                    event_type_variants.append(event_type[3:])
                else:
                    event_type_variants.append(f"ai_{event_type}")

                page_url_normalized = _normalize_url(page_url)
                similar = (
                    db.table("anomaly_clusters")
                    .select("*")
                    .eq("project_id", project_id)
                    .in_("event_type", event_type_variants)
                    .in_("status", ["new", "github_issued", "resolved", "closed", "in_progress", "not_an_issue"])
                    .execute()
                )
                if similar.data:
                    page_matches = [
                        row for row in similar.data
                        if _normalize_url(row.get("page_url") or "") == page_url_normalized
                    ]
                    if page_matches:
                        if page_matches[0].get("status") == "not_an_issue":
                            logger.info(f"Skipping issue similar to dismissed: {event_type} on {page_url_normalized}")
                            continue
                        similar.data = page_matches
                        existing = similar

        # Layer 3: Fuzzy title + description similarity
        if not existing.data:
            issue_title = issue.get("title", "")
            issue_desc = issue.get("description", "")
            if issue_title:
                all_clusters = (
                    db.table("anomaly_clusters")
                    .select("id, error_message, page_url, ai_details, status")
                    .eq("project_id", project_id)
                    .in_("status", ["new", "github_issued", "resolved", "closed", "in_progress", "not_an_issue"])
                    .limit(100)
                    .execute()
                )
                if all_clusters.data:
                    best_match = None
                    best_score = 0.0
                    for row in all_clusters.data:
                        existing_title = row.get("error_message", "")
                        title_sim = _text_similarity(issue_title, existing_title)

                        existing_ai = row.get("ai_details")
                        existing_desc = ""
                        if existing_ai:
                            if isinstance(existing_ai, str):
                                try:
                                    existing_desc = json.loads(existing_ai).get("description", "")
                                except (json.JSONDecodeError, ValueError):
                                    pass
                            elif isinstance(existing_ai, dict):
                                existing_desc = existing_ai.get("description", "")

                        desc_sim = _text_similarity(issue_desc, existing_desc) if issue_desc and existing_desc else 0.0

                        score = title_sim
                        if title_sim >= 0.90 or (title_sim >= 0.80 and desc_sim >= 0.80):
                            if score > best_score:
                                best_score = score
                                best_match = row

                    if best_match:
                        if best_match.get("status") == "not_an_issue":
                            logger.info(
                                f"Skipping issue similar (title {best_score:.0%}) to dismissed: "
                                f"'{issue_title}' ≈ '{best_match.get('error_message', '')}'"
                            )
                            continue
                        full_row = (
                            db.table("anomaly_clusters")
                            .select("*")
                            .eq("id", best_match["id"])
                            .execute()
                        )
                        if full_row.data:
                            existing = full_row
                            logger.info(
                                f"Fuzzy matched issue '{issue_title}' → "
                                f"'{best_match.get('error_message', '')}' ({best_score:.0%} similar)"
                            )

        if existing.data:
            row = existing.data[0]

            # ── Re-open resolved/closed issues instead of creating new ──
            old_status = row.get("status", "new")
            if old_status in ("resolved", "closed"):
                db.table("anomaly_clusters").update({"status": "new"}).eq("id", row["id"]).execute()
                logger.info(f"Re-opened {old_status} issue '{row.get('error_message', '')}' (fingerprint: {fp[:16]}...)")

            # ── Merge into existing cluster ──────────────────────────────
            old_count = row.get("count", 0)
            new_count = old_count + 1

            old_sessions = row.get("sample_session_ids") or []
            merged_sessions = list(dict.fromkeys(old_sessions + ([session_id] if session_id else [])))[:10]

            old_ai_details = {}
            raw_ai = row.get("ai_details")
            if raw_ai:
                if isinstance(raw_ai, str):
                    try:
                        old_ai_details = json.loads(raw_ai)
                    except json.JSONDecodeError:
                        old_ai_details = {}
                elif isinstance(raw_ai, dict):
                    old_ai_details = raw_ai

            tracked_users = set(old_ai_details.get("distinct_user_ids", []))
            if distinct_id:
                tracked_users.add(distinct_id)
            affected_users = max(len(tracked_users), row.get("affected_users", 1))

            old_event_times = row.get("session_event_times") or {}
            if isinstance(old_event_times, str):
                try:
                    old_event_times = json.loads(old_event_times)
                except json.JSONDecodeError:
                    old_event_times = {}
            for sid, ts in session_event_times.items():
                if sid not in old_event_times:
                    old_event_times[sid] = ts
            merged_event_times = old_event_times

            old_start_times = old_ai_details.get("session_start_times", {})
            for sid, ts in session_start_times.items():
                if sid not in old_start_times:
                    old_start_times[sid] = ts
            merged_start_times = old_start_times

            old_evidence = old_ai_details.get("evidence", [])
            new_evidence = issue.get("evidence", [])
            merged_evidence = old_evidence + new_evidence
            seen_ev = set()
            deduped_evidence = []
            for ev in merged_evidence:
                ev_key = json.dumps(ev, sort_keys=True, default=str) if isinstance(ev, dict) else str(ev)
                if ev_key not in seen_ev:
                    seen_ev.add(ev_key)
                    deduped_evidence.append(ev)
            merged_evidence = deduped_evidence[-20:]

            ai_details = {
                "description": issue.get("description") or old_ai_details.get("description", ""),
                "why_issue": issue.get("why_issue") or old_ai_details.get("why_issue", ""),
                "reproduction_steps": issue.get("reproduction_steps") or old_ai_details.get("reproduction_steps", []),
                "evidence": merged_evidence,
                "severity": _higher_severity(
                    issue.get("severity", "medium"),
                    old_ai_details.get("severity", "medium"),
                ),
                "confidence": max(
                    issue.get("confidence", 0.5),
                    old_ai_details.get("confidence", 0.5),
                ),
                "category": category,
                "session_start_times": merged_start_times,
                "distinct_user_ids": list(tracked_users),
            }

            new_element = None
            if issue.get("_element_tag") or issue.get("_element_text") or issue.get("_element_selector"):
                new_element = {
                    "tag": issue.get("_element_tag", ""),
                    "text": issue.get("_element_text", ""),
                    "selector": issue.get("_element_selector", ""),
                }
            ai_details["element"] = new_element or old_ai_details.get("element")

            update_data = {
                "count": new_count,
                "affected_users": affected_users,
                "last_seen": now,
                "sample_session_ids": merged_sessions,
                "ai_details": json.dumps(ai_details),
                "session_event_times": merged_event_times,
            }

            db.table("anomaly_clusters").update(update_data).eq(
                "id", row["id"]
            ).execute()

            # Update GitHub issue if one exists
            if github_repo and github_token:
                gh_issue_id = old_ai_details.get("github_issue_id")
                if gh_issue_id:
                    try:
                        from app.services.github_service import update_github_issue_comment
                        update_github_issue_comment(
                            github_repo, github_token, gh_issue_id,
                            {**row, "count": new_count, "affected_users": affected_users, "last_seen": now},
                        )
                    except Exception as e:
                        logger.warning(f"Failed to update GitHub issue #{gh_issue_id}: {e}")

            logger.info(
                f"Merged AI issue '{issue.get('title')}' into existing cluster "
                f"(count: {old_count} → {new_count}, sessions: {len(merged_sessions)}, users: {affected_users})"
            )

        else:
            # ── Insert new cluster ───────────────────────────────────────
            distinct_users = issue.get("affected_user_ids", [])
            if not distinct_users and distinct_id:
                distinct_users = [distinct_id]

            ai_details = {
                "description": issue.get("description", ""),
                "why_issue": issue.get("why_issue", ""),
                "reproduction_steps": issue.get("reproduction_steps", []),
                "evidence": issue.get("evidence", []),
                "severity": issue.get("severity", "medium"),
                "confidence": issue.get("confidence", 0.5),
                "category": category,
                "session_start_times": session_start_times,
                "distinct_user_ids": distinct_users,
            }

            if issue.get("_element_tag") or issue.get("_element_text") or issue.get("_element_selector"):
                ai_details["element"] = {
                    "tag": issue.get("_element_tag", ""),
                    "text": issue.get("_element_text", ""),
                    "selector": issue.get("_element_selector", ""),
                }

            issue_count = issue.get("total_occurrences", 1)
            issue_affected_users = issue.get("affected_users", 1)
            if isinstance(issue_affected_users, list):
                issue_affected_users = len(issue_affected_users)

            sample_sessions = issue.get("sample_sessions", [])
            if not sample_sessions and session_id:
                sample_sessions = [session_id]

            insert_data = {
                "project_id": project_id,
                "fingerprint": fp,
                "event_type": event_type,
                "error_message": issue.get("title", ""),
                "page_url": issue.get("page_url", ""),
                "count": issue_count,
                "affected_users": issue_affected_users,
                "first_seen": now,
                "last_seen": now,
                "sample_session_ids": sample_sessions[:10],
                "status": "new",
                "ai_details": json.dumps(ai_details),
            }
            if session_event_times:
                insert_data["session_event_times"] = session_event_times

            db.table("anomaly_clusters").insert(insert_data).execute()

            # Create GitHub issue for new bugs
            if github_repo and github_token:
                try:
                    from app.services.github_service import create_github_issue
                    from app.models.schemas import BugReport

                    report = BugReport(
                        title=issue.get("title", "Untitled Bug"),
                        summary=issue.get("description", ""),
                        reproduction_steps=issue.get("reproduction_steps", []),
                        severity=issue.get("severity", "medium"),
                        confidence_score=issue.get("confidence", 0.5),
                    )

                    gh_result = create_github_issue(
                        repo_name=github_repo,
                        token=github_token,
                        report=report,
                        cluster={
                            "event_type": event_type,
                            "count": issue_count,
                            "affected_users": issue_affected_users,
                            "first_seen": now,
                            "last_seen": now,
                            "error_message": issue.get("title", ""),
                            "page_url": issue.get("page_url", ""),
                            "sample_session_ids": sample_sessions[:5],
                        },
                        provider_project_id=provider_project_id,
                        session_provider=session_provider,
                        provider_host=provider_host,
                    )

                    if gh_result:
                        # Store GitHub issue reference in ai_details
                        ai_details["github_issue_id"] = gh_result["github_issue_id"]
                        ai_details["github_issue_url"] = gh_result["github_issue_url"]
                        db.table("anomaly_clusters").update({
                            "ai_details": json.dumps(ai_details),
                            "status": "github_issued",
                        }).eq("project_id", project_id).eq("fingerprint", fp).execute()

                        logger.info(f"Created GitHub issue #{gh_result['github_issue_id']} for '{issue.get('title')}'")
                except Exception as e:
                    logger.warning(f"Failed to create GitHub issue for '{issue.get('title')}': {e}")

            logger.info(
                f"Created new AI issue cluster '{issue.get('title')}' "
                f"(session: {session_id[:12] if session_id else 'none'})"
            )


def _higher_severity(a: str, b: str) -> str:
    """Return the more severe of two severity levels."""
    order = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    return a if order.get(a, 0) >= order.get(b, 0) else b


@router.post("/{project_id}/cleanup-duplicates")
async def cleanup_duplicate_issues(project_id: str):
    """
    One-time cleanup: merge duplicate anomaly clusters that have the same
    event_type + page_url combination within the same project.
    Keeps the oldest cluster and merges counts, sessions, and users into it.
    """
    db = get_supabase()

    all_clusters = (
        db.table("anomaly_clusters")
        .select("*")
        .eq("project_id", project_id)
        .order("first_seen", desc=False)
        .execute()
    )

    if not all_clusters.data:
        return {"message": "No clusters to clean up", "merged": 0, "deleted": 0}

    groups: dict[str, list[dict]] = {}
    for c in all_clusters.data:
        key = f"{c.get('event_type', '')}|{_normalize_url(c.get('page_url') or '')}"
        groups.setdefault(key, []).append(c)

    ungrouped = [c for key, cs in groups.items() if len(cs) == 1 for c in cs]
    if len(ungrouped) >= 2:
        used_ids: set[str] = set()
        for i, c1 in enumerate(ungrouped):
            if c1["id"] in used_ids:
                continue
            title1 = c1.get("error_message", "")
            if not title1:
                continue
            fuzzy_group = [c1]
            for c2 in ungrouped[i + 1:]:
                if c2["id"] in used_ids:
                    continue
                title2 = c2.get("error_message", "")
                if title2 and _text_similarity(title1, title2) >= 0.90:
                    fuzzy_group.append(c2)
                    used_ids.add(c2["id"])
            if len(fuzzy_group) > 1:
                used_ids.add(c1["id"])
                fkey = f"fuzzy|{c1['id']}"
                groups[fkey] = fuzzy_group

    merged_count = 0
    deleted_ids: list[str] = []

    for key, clusters in groups.items():
        if len(clusters) <= 1:
            continue

        primary = clusters[0]
        primary_id = primary["id"]

        total_count = primary.get("count", 0)
        all_session_ids = list(primary.get("sample_session_ids") or [])
        all_event_times = dict(primary.get("session_event_times") or {})

        primary_ai = {}
        raw_ai = primary.get("ai_details")
        if raw_ai:
            if isinstance(raw_ai, str):
                try:
                    primary_ai = json.loads(raw_ai)
                except json.JSONDecodeError:
                    primary_ai = {}
            elif isinstance(raw_ai, dict):
                primary_ai = raw_ai

        tracked_users = set(primary_ai.get("distinct_user_ids", []))
        all_evidence = list(primary_ai.get("evidence", []))
        merged_start_times = dict(primary_ai.get("session_start_times", {}))
        best_severity = primary_ai.get("severity", "medium")
        best_confidence = primary_ai.get("confidence", 0.5)

        for dup in clusters[1:]:
            total_count += dup.get("count", 0)

            dup_sessions = dup.get("sample_session_ids") or []
            all_session_ids.extend(dup_sessions)

            dup_times = dup.get("session_event_times") or {}
            if isinstance(dup_times, str):
                try:
                    dup_times = json.loads(dup_times)
                except json.JSONDecodeError:
                    dup_times = {}
            for sid, ts in dup_times.items():
                if sid not in all_event_times:
                    all_event_times[sid] = ts

            dup_ai = {}
            raw_dup_ai = dup.get("ai_details")
            if raw_dup_ai:
                if isinstance(raw_dup_ai, str):
                    try:
                        dup_ai = json.loads(raw_dup_ai)
                    except json.JSONDecodeError:
                        dup_ai = {}
                elif isinstance(raw_dup_ai, dict):
                    dup_ai = raw_dup_ai

            dup_users = dup_ai.get("distinct_user_ids", [])
            tracked_users.update(dup_users)

            dup_evidence = dup_ai.get("evidence", [])
            all_evidence.extend(dup_evidence)

            dup_starts = dup_ai.get("session_start_times", {})
            for sid, ts in dup_starts.items():
                if sid not in merged_start_times:
                    merged_start_times[sid] = ts

            best_severity = _higher_severity(best_severity, dup_ai.get("severity", "medium"))
            best_confidence = max(best_confidence, dup_ai.get("confidence", 0.5))

            if not primary_ai.get("why_issue") and dup_ai.get("why_issue"):
                primary_ai["why_issue"] = dup_ai["why_issue"]
            if not primary_ai.get("element") and dup_ai.get("element"):
                primary_ai["element"] = dup_ai["element"]
            if not primary_ai.get("reproduction_steps") and dup_ai.get("reproduction_steps"):
                primary_ai["reproduction_steps"] = dup_ai["reproduction_steps"]

            deleted_ids.append(dup["id"])

        unique_sessions = list(dict.fromkeys(all_session_ids))[:10]
        seen_ev = set()
        deduped_evidence = []
        for ev in all_evidence:
            ev_key = json.dumps(ev, sort_keys=True, default=str) if isinstance(ev, dict) else str(ev)
            if ev_key not in seen_ev:
                seen_ev.add(ev_key)
                deduped_evidence.append(ev)

        primary_ai["evidence"] = deduped_evidence[-20:]
        primary_ai["severity"] = best_severity
        primary_ai["confidence"] = best_confidence
        primary_ai["session_start_times"] = merged_start_times
        primary_ai["distinct_user_ids"] = list(tracked_users)

        earliest = primary.get("first_seen", "")
        latest = primary.get("last_seen", "")
        for dup in clusters[1:]:
            if dup.get("first_seen") and dup["first_seen"] < earliest:
                earliest = dup["first_seen"]
            if dup.get("last_seen") and dup["last_seen"] > latest:
                latest = dup["last_seen"]

        db.table("anomaly_clusters").update({
            "count": total_count,
            "affected_users": max(len(tracked_users), primary.get("affected_users", 1)),
            "sample_session_ids": unique_sessions,
            "session_event_times": all_event_times,
            "first_seen": earliest,
            "last_seen": latest,
            "ai_details": json.dumps(primary_ai),
        }).eq("id", primary_id).execute()

        merged_count += len(clusters) - 1

    for did in deleted_ids:
        db.table("anomaly_clusters").delete().eq("id", did).execute()

    return {
        "message": f"Merged {merged_count} duplicate clusters",
        "merged": merged_count,
        "deleted": len(deleted_ids),
    }


async def _send_issue_notifications(project: dict, issues: list[dict]):
    """Send notifications for AI-detected issues based on project settings."""
    project_name = project.get("name", "Unknown Project")

    email_enabled = project.get("notification_email_enabled", True)
    email_address = project.get("notification_email_address", "")
    slack_enabled = project.get("notification_slack_enabled", False)
    slack_webhook = project.get("notification_slack_webhook_url", "")

    for issue in issues:
        if email_enabled and email_address:
            try:
                send_email_notification(email_address, issue, project_name)
            except Exception as e:
                logger.error(f"Failed to send email notification: {e}")

        if slack_enabled and slack_webhook:
            try:
                await send_slack_notification(slack_webhook, issue, project_name)
            except Exception as e:
                logger.error(f"Failed to send Slack notification: {e}")
