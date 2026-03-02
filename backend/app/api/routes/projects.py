import asyncio
import json
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.api.deps import get_current_user
from app.connectors import PROVIDER_LABELS, PROVIDERS
from app.database import get_supabase
from app.models.schemas import (
    AnomalyClusterResponse,
    BugReport,
    IssueStatusUpdate,
    JobRunResponse,
    ProjectCreate,
    ProjectDetail,
    ProjectProviderUpdate,
    ProjectResponse,
    RunTriggerResponse,
)
from app.services.crypto_service import decrypt_token, encrypt_token
from app.services.github_service import create_github_issue
from app.utils.logger import logger

router = APIRouter()


# ── Credential validation helpers ─────────────────────────────────────────────

class ValidatePosthogRequest(BaseModel):
    api_key: str
    project_id: str
    host: str = "eu.posthog.com"


class ValidateGithubRequest(BaseModel):
    repo: str
    token: str


async def _validate_posthog_credentials(api_key: str, project_id: str, host: str) -> dict:
    """Test PostHog credentials by fetching 1 session. Returns {valid, error?}."""
    url = f"https://{host}/api/projects/{project_id}/events"
    headers = {"Authorization": f"Bearer {api_key}"}
    params = {"limit": "1"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers, params=params)
        if resp.status_code == 200:
            return {"valid": True}
        if resp.status_code in (401, 403):
            return {"valid": False, "error": "Invalid API key — check that your Personal API key is correct"}
        if resp.status_code == 404:
            return {"valid": False, "error": "Project not found — check your Project ID"}
        return {"valid": False, "error": f"Unexpected response ({resp.status_code}) from PostHog"}
    except httpx.TimeoutException:
        return {"valid": False, "error": f"Could not reach PostHog at {host} — request timed out"}
    except httpx.ConnectError:
        return {"valid": False, "error": f"Could not connect to {host} — check the host/region"}
    except Exception as exc:
        return {"valid": False, "error": f"Connection failed: {str(exc)[:120]}"}


def _validate_github_credentials(token: str, repo: str) -> dict:
    """Test GitHub token + repo access. Returns {valid, error?, repo_name?}."""
    try:
        from github import Github, GithubException
        g = Github(token, timeout=10)
        repo_obj = g.get_repo(repo)
        return {"valid": True, "repo_name": repo_obj.full_name}
    except GithubException as e:
        status = e.status if hasattr(e, "status") else 0
        if status == 401:
            return {"valid": False, "error": "Invalid token — check that your Personal Access Token is correct"}
        if status == 403:
            return {"valid": False, "error": "Token lacks permissions — ensure the 'repo' scope is enabled"}
        if status == 404:
            return {"valid": False, "error": f"Repository '{repo}' not found — check the owner/repo format"}
        return {"valid": False, "error": f"GitHub error: {str(e)[:120]}"}
    except Exception as exc:
        return {"valid": False, "error": f"Connection failed: {str(exc)[:120]}"}


@router.post("/validate/posthog")
async def validate_posthog(body: ValidatePosthogRequest):
    """Test PostHog credentials before saving."""
    return await _validate_posthog_credentials(body.api_key, body.project_id, body.host)


@router.post("/validate/github")
async def validate_github(body: ValidateGithubRequest):
    """Test GitHub credentials before saving."""
    return _validate_github_credentials(body.token, body.repo)

# Column list used by select queries
_PROJECT_COLS = "id, user_id, name, session_provider, provider_project_id, provider_host, github_repo, detection_threshold, min_sessions_threshold, skip_page_patterns, created_at, updated_at"


@router.get("/providers")
async def list_providers():
    """Return list of supported session replay providers."""
    return [
        {"id": pid, "name": PROVIDER_LABELS[pid]}
        for pid in PROVIDERS
    ]


@router.post("", response_model=ProjectResponse, status_code=201)
async def create_project(project: ProjectCreate, current_user: dict = Depends(get_current_user)):
    """Create a new project with encrypted credentials. user_id is taken from the JWT token."""
    db = get_supabase()

    if project.session_provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {project.session_provider}")

    # ── Validate credentials before saving ────────────────────────────────────
    ph_result = await _validate_posthog_credentials(
        project.provider_api_key, project.provider_project_id, project.provider_host or "eu.posthog.com",
    )
    if not ph_result["valid"]:
        raise HTTPException(status_code=400, detail=ph_result.get("error", "PostHog credentials are invalid"))

    if project.github_repo and project.github_token:
        gh_result = _validate_github_credentials(project.github_token, project.github_repo)
        if not gh_result["valid"]:
            raise HTTPException(status_code=400, detail=gh_result.get("error", "GitHub credentials are invalid"))

    encrypted_provider_key = encrypt_token(project.provider_api_key)
    encrypted_github_token = encrypt_token(project.github_token) if project.github_token else ""

    result = (
        db.table("projects")
        .insert(
            {
                "name": project.name,
                "session_provider": project.session_provider,
                "provider_api_key": encrypted_provider_key,
                "provider_project_id": project.provider_project_id,
                "provider_host": project.provider_host,
                "github_repo": project.github_repo,
                "github_token": encrypted_github_token,
                "detection_threshold": project.detection_threshold,
                "min_sessions_threshold": project.min_sessions_threshold,
                "skip_page_patterns": project.skip_page_patterns,
                "user_id": current_user["id"],
            }
        )
        .execute()
    )

    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create project")

    row = result.data[0]
    return ProjectResponse(
        id=row["id"],
        name=row["name"],
        session_provider=row.get("session_provider", "posthog"),
        provider_project_id=row.get("provider_project_id", ""),
        provider_host=row.get("provider_host", ""),
        github_repo=row["github_repo"],
        detection_threshold=row["detection_threshold"],
        min_sessions_threshold=row.get("min_sessions_threshold", 2),
        skip_page_patterns=row.get("skip_page_patterns") or [],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        user_id=row.get("user_id", ""),
    )


@router.get("", response_model=list[ProjectResponse])
async def list_projects(current_user: dict = Depends(get_current_user)):
    """List all active projects for the authenticated user."""
    db = get_supabase()
    result = (
        db.table("projects")
        .select(_PROJECT_COLS)
        .eq("is_active", True)
        .eq("user_id", current_user["id"])
        .order("created_at", desc=True)
        .execute()
    )
    return [ProjectResponse(**row) for row in result.data]


@router.get("/{project_id}", response_model=ProjectDetail)
async def get_project(project_id: str):
    """Get project details with recent anomalies and last job run."""
    db = get_supabase()

    project_result = (
        db.table("projects")
        .select(_PROJECT_COLS)
        .eq("id", project_id)
        .eq("is_active", True)
        .single()
        .execute()
    )

    if not project_result.data:
        raise HTTPException(status_code=404, detail="Project not found")

    row = project_result.data

    # Fetch recent anomaly clusters (only active issues: new, in_progress)
    clusters_result = (
        db.table("anomaly_clusters")
        .select("*")
        .eq("project_id", project_id)
        .neq("status", "not_an_issue")
        .neq("status", "resolved")
        .neq("status", "closed")
        .order("last_seen", desc=True)
        .limit(20)
        .execute()
    )

    anomalies = [AnomalyClusterResponse(**c) for c in clusters_result.data]

    # Fetch last job run
    job_result = (
        db.table("job_runs")
        .select("*")
        .eq("project_id", project_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    last_job = JobRunResponse(**job_result.data[0]) if job_result.data else None

    return ProjectDetail(
        **row,
        recent_anomalies=anomalies,
        last_job_run=last_job,
    )


@router.get("/{project_id}/issues")
async def list_project_issues(
    project_id: str,
    status: Optional[str] = Query(None, description="Filter by status: new, in_progress, resolved, closed, not_an_issue"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(10, ge=1, le=100, description="Items per page"),
):
    """List issues/anomaly clusters for a project with pagination and optional status filter."""
    db = get_supabase()

    query = (
        db.table("anomaly_clusters")
        .select("*", count="exact")
        .eq("project_id", project_id)
        .order("last_seen", desc=True)
    )

    # Exclude not_an_issue by default unless specifically requested
    if status:
        query = query.eq("status", status)
    else:
        query = query.neq("status", "not_an_issue")

    # Pagination
    offset = (page - 1) * page_size
    query = query.range(offset, offset + page_size - 1)

    result = query.execute()

    return {
        "items": [AnomalyClusterResponse(**c) for c in result.data],
        "total": result.count or 0,
        "page": page,
        "page_size": page_size,
        "total_pages": ((result.count or 0) + page_size - 1) // page_size if result.count else 0,
    }


@router.get("/{project_id}/issues/{fingerprint}")
async def get_issue_by_fingerprint(project_id: str, fingerprint: str):
    """Get a single issue/anomaly cluster by its fingerprint."""
    db = get_supabase()

    result = (
        db.table("anomaly_clusters")
        .select("*")
        .eq("project_id", project_id)
        .eq("fingerprint", fingerprint)
        .execute()
    )

    if not result.data:
        raise HTTPException(status_code=404, detail="Issue not found")

    cluster = result.data[0]

    # Build a richer response
    category = cluster.get("event_type", "unknown").replace("ai_", "")
    is_ai = cluster.get("event_type", "").startswith("ai_")

    # Parse ai_details JSON if available
    ai_details = {}
    raw_ai = cluster.get("ai_details")
    if raw_ai:
        if isinstance(raw_ai, str):
            try:
                ai_details = json.loads(raw_ai)
            except json.JSONDecodeError:
                ai_details = {}
        elif isinstance(raw_ai, dict):
            ai_details = raw_ai

    # Use ai_details for richer info when available
    description = ai_details.get("description", "")
    if not description:
        description = f"{cluster.get('count', 1)} occurrences affecting {cluster.get('affected_users', 1)} users on {cluster.get('page_url', 'unknown page')}"

    severity = ai_details.get("severity", "high" if cluster.get("count", 0) > 10 else "medium")

    return {
        "fingerprint": cluster["fingerprint"],
        "title": cluster.get("error_message", "Unknown Issue"),
        "description": description,
        "severity": severity,
        "category": ai_details.get("category", category),
        "event_type": cluster.get("event_type", "unknown"),
        "is_ai_detected": is_ai,
        "page_url": cluster.get("page_url"),
        "count": cluster.get("count", 1),
        "affected_users": cluster.get("affected_users", 1),
        "first_seen": cluster.get("first_seen"),
        "last_seen": cluster.get("last_seen"),
        "status": cluster.get("status", "new"),
        "session_ids": cluster.get("sample_session_ids", []),
        "session_event_times": cluster.get("session_event_times", {}),
        "session_start_times": ai_details.get("session_start_times", {}),
        "why_issue": ai_details.get("why_issue"),
        "reproduction_steps": ai_details.get("reproduction_steps", []),
        "evidence": ai_details.get("evidence", []),
        "confidence": ai_details.get("confidence"),
        "element": ai_details.get("element"),
        "github_issue_id": ai_details.get("github_issue_id"),
        "github_issue_url": ai_details.get("github_issue_url"),
    }


@router.put("/{project_id}/provider", response_model=ProjectResponse)
async def update_project_provider(project_id: str, data: ProjectProviderUpdate):
    """Update the session replay provider settings for a project."""
    db = get_supabase()

    if data.session_provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {data.session_provider}")

    # Verify project exists
    project_result = (
        db.table("projects")
        .select("id")
        .eq("id", project_id)
        .eq("is_active", True)
        .single()
        .execute()
    )
    if not project_result.data:
        raise HTTPException(status_code=404, detail="Project not found")

    # ── Validate credentials before saving ────────────────────────────────────
    ph_result = await _validate_posthog_credentials(
        data.provider_api_key, data.provider_project_id, data.provider_host or "eu.posthog.com",
    )
    if not ph_result["valid"]:
        raise HTTPException(status_code=400, detail=ph_result.get("error", "PostHog credentials are invalid"))

    encrypted_provider_key = encrypt_token(data.provider_api_key)

    result = (
        db.table("projects")
        .update({
            "session_provider": data.session_provider,
            "provider_api_key": encrypted_provider_key,
            "provider_project_id": data.provider_project_id,
            "provider_host": data.provider_host,
        })
        .eq("id", project_id)
        .execute()
    )

    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to update provider settings")

    row = result.data[0]
    return ProjectResponse(
        id=row["id"],
        name=row["name"],
        session_provider=row.get("session_provider", "posthog"),
        provider_project_id=row.get("provider_project_id", ""),
        provider_host=row.get("provider_host", ""),
        github_repo=row["github_repo"],
        detection_threshold=row["detection_threshold"],
        min_sessions_threshold=row.get("min_sessions_threshold", 2),
        skip_page_patterns=row.get("skip_page_patterns") or [],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        user_id=row.get("user_id", ""),
    )


@router.post("/{project_id}/run", response_model=RunTriggerResponse)
async def trigger_run(project_id: str):
    """Manually trigger the detection pipeline for a project (legacy endpoint)."""
    db = get_supabase()

    project_result = (
        db.table("projects")
        .select("id")
        .eq("id", project_id)
        .eq("is_active", True)
        .single()
        .execute()
    )

    if not project_result.data:
        raise HTTPException(status_code=404, detail="Project not found")

    return RunTriggerResponse(
        message="Use the AI Analyze button instead — rule detection has been replaced."
    )


@router.post("/{project_id}/issues/{fingerprint}/github")
async def create_github_issue_manual(
    project_id: str,
    fingerprint: str,
    current_user: dict = Depends(get_current_user),
):
    """Manually create a GitHub issue for an anomaly cluster."""
    db = get_supabase()

    # ── Fetch project (need GitHub settings) ──────────────────────────────────
    project_result = (
        db.table("projects")
        .select("id, github_repo, github_token, provider_project_id, session_provider, provider_host")
        .eq("id", project_id)
        .eq("is_active", True)
        .eq("user_id", current_user["id"])
        .single()
        .execute()
    )
    if not project_result.data:
        raise HTTPException(status_code=404, detail="Project not found")

    project = project_result.data
    github_repo = project.get("github_repo", "")
    encrypted_gh_token = project.get("github_token", "")

    if not github_repo or not encrypted_gh_token:
        raise HTTPException(status_code=400, detail="GitHub integration is not configured for this project")

    try:
        github_token = decrypt_token(encrypted_gh_token)
    except Exception:
        raise HTTPException(status_code=400, detail="Failed to decrypt GitHub token — please reconfigure")

    # ── Fetch the anomaly cluster ─────────────────────────────────────────────
    cluster_result = (
        db.table("anomaly_clusters")
        .select("*")
        .eq("project_id", project_id)
        .eq("fingerprint", fingerprint)
        .execute()
    )
    if not cluster_result.data:
        raise HTTPException(status_code=404, detail="Issue not found")

    cluster = cluster_result.data[0]

    # Parse ai_details
    ai_details = {}
    raw_ai = cluster.get("ai_details")
    if raw_ai:
        if isinstance(raw_ai, str):
            try:
                ai_details = json.loads(raw_ai)
            except json.JSONDecodeError:
                ai_details = {}
        elif isinstance(raw_ai, dict):
            ai_details = raw_ai

    # Check if GitHub issue already exists
    if ai_details.get("github_issue_id"):
        raise HTTPException(
            status_code=409,
            detail="A GitHub issue already exists for this bug",
        )

    # ── Build BugReport + cluster dict for github_service ─────────────────────
    report = BugReport(
        title=cluster.get("error_message", "Untitled Bug"),
        summary=ai_details.get("description", cluster.get("error_message", "")),
        reproduction_steps=ai_details.get("reproduction_steps", []),
        severity=ai_details.get("severity", "medium"),
        confidence_score=ai_details.get("confidence", 0.5),
    )

    cluster_data = {
        "event_type": cluster.get("event_type", "unknown"),
        "count": cluster.get("count", 1),
        "affected_users": cluster.get("affected_users", 1),
        "first_seen": cluster.get("first_seen", ""),
        "last_seen": cluster.get("last_seen", ""),
        "error_message": cluster.get("error_message", ""),
        "page_url": cluster.get("page_url", ""),
        "endpoint": cluster.get("endpoint", ""),
        "css_selector": cluster.get("css_selector", ""),
        "sample_session_ids": (cluster.get("sample_session_ids") or [])[:5],
    }

    gh_result = create_github_issue(
        repo_name=github_repo,
        token=github_token,
        report=report,
        cluster=cluster_data,
        provider_project_id=project.get("provider_project_id", ""),
        session_provider=project.get("session_provider", "posthog"),
        provider_host=project.get("provider_host", ""),
    )

    if not gh_result:
        raise HTTPException(status_code=502, detail="Failed to create GitHub issue — check repo name and token permissions")

    # ── Update the cluster with GitHub info ───────────────────────────────────
    ai_details["github_issue_id"] = gh_result["github_issue_id"]
    ai_details["github_issue_url"] = gh_result["github_issue_url"]

    db.table("anomaly_clusters").update({
        "ai_details": json.dumps(ai_details) if isinstance(ai_details, dict) else ai_details,
        "status": "github_issued",
    }).eq("id", cluster["id"]).execute()

    return {
        "github_issue_id": gh_result["github_issue_id"],
        "github_issue_url": gh_result["github_issue_url"],
        "status": "github_issued",
    }


@router.patch("/{project_id}/issues/{fingerprint}/status")
async def update_issue_status(project_id: str, fingerprint: str, data: IssueStatusUpdate):
    """Update the status of an anomaly cluster (issue)."""
    db = get_supabase()

    valid_statuses = ["new", "in_progress", "resolved", "closed", "not_an_issue"]
    if data.status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {', '.join(valid_statuses)}")

    # Verify issue exists
    existing = (
        db.table("anomaly_clusters")
        .select("id, fingerprint")
        .eq("project_id", project_id)
        .eq("fingerprint", fingerprint)
        .execute()
    )
    if not existing.data:
        raise HTTPException(status_code=404, detail="Issue not found")

    # Update the status
    db.table("anomaly_clusters").update({"status": data.status}).eq("id", existing.data[0]["id"]).execute()

    # If marked as "not_an_issue", add to dismissed fingerprints
    if data.status == "not_an_issue":
        try:
            db.table("dismissed_fingerprints").upsert({
                "project_id": project_id,
                "fingerprint": fingerprint,
            }, on_conflict="project_id,fingerprint").execute()
        except Exception as e:
            logger.warning(f"Failed to dismiss fingerprint: {e}")

    # If un-dismissing (changing FROM not_an_issue to something else), remove from dismissed
    elif data.status != "not_an_issue":
        try:
            db.table("dismissed_fingerprints").delete().eq("project_id", project_id).eq("fingerprint", fingerprint).execute()
        except Exception:
            pass

    return {"status": data.status, "fingerprint": fingerprint}
