from typing import Any

from github import Github, GithubException

from app.models.schemas import BugReport
from app.utils.logger import logger


def _build_replay_link(provider: str, project_id: str, host: str, session_id: str) -> str:
    """Build the session replay URL based on the provider."""
    if provider == "fullstory":
        return f"https://app.fullstory.com/ui/session/{session_id}"
    elif provider == "logrocket":
        return f"https://app.logrocket.com/{project_id}/sessions/{session_id}"
    elif provider == "clarity":
        return f"https://clarity.microsoft.com/projects/{project_id}/session/{session_id}"
    else:  # posthog
        h = host or "eu.posthog.com"
        return f"https://{h}/project/{project_id}/replay/{session_id}"


def _format_issue_body(
    report: BugReport,
    cluster: dict[str, Any],
    provider_project_id: str,
    session_provider: str = "posthog",
    provider_host: str = "",
) -> str:
    """Format the GitHub issue body with bug report and cluster data."""
    session_links = ""
    for sid in cluster.get("sample_session_ids", []):
        link = _build_replay_link(session_provider, provider_project_id, provider_host, sid)
        session_links += f"- [{sid}]({link})\n"

    body = f"""## Bug Report (Auto-Generated)

### Summary
{report.summary}

### Details
| Field | Value |
|-------|-------|
| **Severity** | {report.severity} |
| **Confidence** | {report.confidence_score:.0%} |
| **Event Type** | {cluster.get('event_type', 'N/A')} |
| **Occurrences** | {cluster.get('count', 0)} |
| **Affected Users** | {cluster.get('affected_users', 0)} |
| **First Seen** | {cluster.get('first_seen', 'N/A')} |
| **Last Seen** | {cluster.get('last_seen', 'N/A')} |

### Error Info
"""

    if cluster.get("error_message"):
        body += f"```\n{cluster['error_message']}\n```\n\n"
    if cluster.get("endpoint"):
        body += f"**Endpoint:** `{cluster['endpoint']}`\n\n"
    if cluster.get("page_url"):
        body += f"**Page URL:** {cluster['page_url']}\n\n"
    if cluster.get("css_selector"):
        body += f"**CSS Selector:** `{cluster['css_selector']}`\n\n"

    body += f"""### Reproduction Steps
"""
    for i, step in enumerate(report.reproduction_steps, 1):
        body += f"{i}. {step}\n"

    if session_links:
        body += f"""
### Session Replays
{session_links}
"""

    body += """
---
*This issue was automatically created by [Buglyft](https://buglyft.com).*
"""
    return body


def _severity_labels(severity: str) -> list[str]:
    """Get GitHub labels for the severity."""
    labels = ["bug", "auto-detected"]
    severity_map = {
        "critical": "priority: critical",
        "high": "priority: high",
        "medium": "priority: medium",
        "low": "priority: low",
    }
    if severity in severity_map:
        labels.append(severity_map[severity])
    return labels


def create_github_issue(
    repo_name: str,
    token: str,
    report: BugReport,
    cluster: dict[str, Any],
    provider_project_id: str = "",
    session_provider: str = "posthog",
    provider_host: str = "",
) -> dict[str, Any] | None:
    """
    Create a new GitHub issue for an anomaly cluster.
    Returns issue metadata or None on failure.
    """
    try:
        g = Github(token)
        repo = g.get_repo(repo_name)

        body = _format_issue_body(report, cluster, provider_project_id, session_provider, provider_host)
        labels = _severity_labels(report.severity)

        # Create labels if they don't exist
        existing_labels = {label.name for label in repo.get_labels()}
        for label_name in labels:
            if label_name not in existing_labels:
                try:
                    color = "d73a4a" if "critical" in label_name else "e4e669"
                    repo.create_label(name=label_name, color=color)
                except GithubException:
                    pass  # Label may already exist (race condition)

        issue = repo.create_issue(
            title=report.title,
            body=body,
            labels=labels,
        )

        logger.info(f"Created GitHub issue #{issue.number}: {report.title}")

        return {
            "github_issue_id": issue.number,
            "github_issue_url": issue.html_url,
        }

    except GithubException as e:
        logger.error(f"GitHub issue creation failed: {e}")
        return None


def update_github_issue_comment(
    repo_name: str,
    token: str,
    issue_number: int,
    cluster: dict[str, Any],
) -> bool:
    """
    Add a comment to an existing GitHub issue with updated occurrence data.
    Returns True on success.
    """
    try:
        g = Github(token)
        repo = g.get_repo(repo_name)
        issue = repo.get_issue(issue_number)

        comment_body = f"""### Updated Occurrence Data

| Field | Value |
|-------|-------|
| **Total Occurrences** | {cluster.get('count', 0)} |
| **Affected Users** | {cluster.get('affected_users', 0)} |
| **Last Seen** | {cluster.get('last_seen', 'N/A')} |

This issue is still occurring. The anomaly cluster has been updated with new data.

---
*Auto-updated by [Buglyft](https://buglyft.com)*
"""
        issue.create_comment(comment_body)
        logger.info(f"Updated GitHub issue #{issue_number} with new occurrence data")
        return True

    except GithubException as e:
        logger.error(f"GitHub comment update failed: {e}")
        return False
