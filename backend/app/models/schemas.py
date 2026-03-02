from pydantic import BaseModel, Field
from datetime import datetime
from typing import Any, Optional


# ── Project ──────────────────────────────────────────────────────────────────

class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    session_provider: str = Field(default="posthog", description="posthog | fullstory | logrocket | clarity")
    provider_api_key: str = Field(..., description="API key for the session replay provider")
    provider_project_id: str = Field(..., description="Project/org ID inside the provider")
    provider_host: str = Field(default="", description="Optional custom host (e.g. eu.posthog.com for PostHog)")
    github_repo: str = Field(default="", description="owner/repo format (optional)")
    github_token: str = Field(default="", description="GitHub PAT (optional)")
    detection_threshold: int = Field(default=5, ge=1, le=100)
    min_sessions_threshold: int = Field(default=2, ge=1, le=50, description="Minimum unique sessions before creating an issue (default 2)")
    skip_page_patterns: list[str] = Field(default_factory=list, description="URL patterns to skip in flow analysis (e.g. /auth/callback, /oauth)")


class ProjectResponse(BaseModel):
    id: str
    name: str
    session_provider: str = "posthog"
    provider_project_id: str = ""
    provider_host: str = ""
    github_repo: str
    detection_threshold: int
    min_sessions_threshold: int = 2
    skip_page_patterns: list[str] = []
    created_at: str
    updated_at: str
    user_id: str = ""


class ProjectDetail(ProjectResponse):
    recent_anomalies: list["AnomalyClusterResponse"] = []
    last_job_run: Optional["JobRunResponse"] = None


# ── Events ───────────────────────────────────────────────────────────────────

class EventResponse(BaseModel):
    id: str
    project_id: str
    event_type: str
    fingerprint: str
    error_message: Optional[str] = None
    endpoint: Optional[str] = None
    page_url: Optional[str] = None
    css_selector: Optional[str] = None
    session_id: Optional[str] = None
    user_id: Optional[str] = None
    status_code: Optional[int] = None
    timestamp: str
    created_at: str


# ── Anomaly Clusters ─────────────────────────────────────────────────────────

class AnomalyClusterResponse(BaseModel):
    id: str
    project_id: str
    fingerprint: str
    event_type: str
    error_message: Optional[str] = None
    endpoint: Optional[str] = None
    css_selector: Optional[str] = None
    page_url: Optional[str] = None
    count: int
    affected_users: int
    first_seen: str
    last_seen: str
    sample_session_ids: list[str] = []
    status: str
    created_at: str
    updated_at: str


# ── GitHub Issues ────────────────────────────────────────────────────────────

class GitHubIssueResponse(BaseModel):
    id: str
    project_id: str
    cluster_fingerprint: str
    github_issue_id: int
    github_issue_url: str
    status: str
    created_at: str
    updated_at: str


# ── Job Runs ─────────────────────────────────────────────────────────────────

class JobRunResponse(BaseModel):
    id: str
    project_id: str
    last_fetched_at: str
    status: str
    events_fetched: int
    anomalies_detected: int
    issues_created: int
    created_at: str


# ── AI Bug Report ────────────────────────────────────────────────────────────

class BugReport(BaseModel):
    title: str
    summary: str
    reproduction_steps: list[str]
    severity: str = Field(..., description="critical, high, medium, low")
    confidence_score: float = Field(..., ge=0.0, le=1.0)


# ── Session Analysis ────────────────────────────────────────────────────────

class SessionIssue(BaseModel):
    title: str
    description: str
    why_issue: Optional[str] = None
    reproduction_steps: Optional[list[str]] = []
    severity: str
    category: str
    evidence: Optional[list[Any]] = []
    page_url: Optional[str] = None
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    session_id: Optional[str] = None
    fingerprint: Optional[str] = None


class AICostSummary(BaseModel):
    total_calls: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    by_function: dict = {}


class AnalysisProgressResponse(BaseModel):
    project_id: str
    status: str  # pending, running, completed, failed
    sessions_total: int = 0
    sessions_analyzed: int = 0
    issues_found: int = 0
    issues: list[SessionIssue] = []
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    ai_cost: Optional[AICostSummary] = None


class AnalysisTriggerResponse(BaseModel):
    message: str
    analysis_id: str


# ── Notification Settings ───────────────────────────────────────────────────

class NotificationSettings(BaseModel):
    email_enabled: bool = True
    email_address: Optional[str] = None
    slack_enabled: bool = False
    slack_webhook_url: Optional[str] = None


# ── API Responses ────────────────────────────────────────────────────────────

class ProjectProviderUpdate(BaseModel):
    session_provider: str = Field(..., description="posthog | fullstory | logrocket | clarity")
    provider_api_key: str = Field(..., description="API key for the session replay provider")
    provider_project_id: str = Field(..., description="Project/org ID inside the provider")
    provider_host: str = Field(default="", description="Optional custom host")


class RunTriggerResponse(BaseModel):
    message: str
    job_run_id: Optional[str] = None


class IssueStatusUpdate(BaseModel):
    status: str = Field(..., description="new | in_progress | resolved | closed | not_an_issue")


# Rebuild forward refs
ProjectDetail.model_rebuild()
