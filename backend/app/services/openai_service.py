import json
import time
from typing import Any

from openai import AsyncOpenAI

from app.config import get_settings
from app.models.schemas import BugReport
from app.utils.cost_tracker import CostTracker
from app.utils.logger import logger

SYSTEM_PROMPT = """You are an expert software engineer analyzing production bug data.
Given event cluster data, generate a structured bug report.

You must return valid JSON with exactly these fields:
- title: A concise, descriptive bug title (max 100 chars)
- summary: A clear description of the bug and its impact (2-3 sentences)
- reproduction_steps: An ordered list of steps to reproduce the issue
- severity: One of "critical", "high", "medium", "low"
- confidence_score: A float between 0.0 and 1.0 indicating how confident you are this is a real bug

Event types you may encounter:
- exception: JavaScript exception thrown in production
- console_error: Repeated console.error messages
- api_failure: Backend endpoint returning HTTP 500+ errors
- rage_click: Users rapidly clicking the same element out of frustration
- dead_click: Users clicking an interactive element (button, link) that produces NO response — no page navigation, no network request, no visible change within a timeout window. Indicates broken buttons, disabled handlers, or misconfigured links.
- dead_end: A page where a high percentage of users land and immediately leave with NO interaction — no clicks, no scrolls, no form fills. They visit and bounce within seconds. Indicates the page is broken, empty, confusing, or fails to load. The error_message includes bounce rate and session counts.
- confusing_flow: A step in a multi-page flow (checkout, onboarding, signup) where users consistently drop off and abandon the process. The error_message includes the drop-off percentage and which pages users came from. Indicates the step is confusing, has a UX problem, or has a hidden error preventing completion.

Severity guidelines:
- critical: Data loss, security vulnerability, or complete feature failure affecting many users
- high: Major feature broken, frequent errors affecting multiple users. Dead clicks on primary CTAs that block core flows are HIGH. Dead end pages on critical paths (checkout, signup, pricing) are HIGH. Confusing flow steps with >70% drop-off on payment/registration are HIGH.
- medium: Feature partially broken, intermittent errors, or poor UX. Dead clicks on secondary actions are MEDIUM. Dead end pages on non-critical paths are MEDIUM. Confusing flow steps with 50-70% drop-off are MEDIUM.
- low: Minor cosmetic issues, edge cases, or rare occurrences

Confidence guidelines:
- 0.9+: Clear, repeated error with high user impact
- 0.75-0.9: Likely a real bug with meaningful pattern. Dead clicks across 5+ sessions are at least 0.8. Dead ends with 70%+ bounce rate across 10+ sessions are 0.85+. Confusing flows with 60%+ drop-off across 15+ sessions are 0.8+.
- 0.5-0.75: Possible bug but could be user error or transient
- <0.5: Uncertain, may be noise
"""


async def generate_bug_report(
    cluster: dict[str, Any],
    cost_tracker: CostTracker | None = None,
) -> BugReport | None:
    """
    Send anomaly cluster data to OpenAI and get a structured bug report.
    Returns None if confidence is below threshold.
    """
    settings = get_settings()
    client = AsyncOpenAI(api_key=settings.openai_api_key)

    user_prompt = f"""Analyze this production anomaly cluster and generate a bug report:

Event Type: {cluster.get('event_type', 'unknown')}
Error Message: {cluster.get('error_message', 'N/A')}
API Endpoint: {cluster.get('endpoint', 'N/A')}
Page URL: {cluster.get('page_url', 'N/A')}
CSS Selector: {cluster.get('css_selector', 'N/A')}
Occurrence Count: {cluster.get('count', 0)}
Affected Users: {cluster.get('affected_users', 0)}
First Seen: {cluster.get('first_seen', 'N/A')}
Last Seen: {cluster.get('last_seen', 'N/A')}
Sample Session IDs: {json.dumps(cluster.get('sample_session_ids', []))}
"""

    try:
        t0 = time.perf_counter()
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
            max_tokens=1000,
        )
        duration_ms = (time.perf_counter() - t0) * 1000

        # Track cost
        if cost_tracker:
            rec = cost_tracker.record(
                function="generate_bug_report",
                model="gpt-4o-mini",
                response=response,
                duration_ms=duration_ms,
            )
            logger.debug(
                f"generate_bug_report: {rec.total_tokens} tokens, ${rec.cost_usd:.4f}, {duration_ms:.0f}ms"
            )

        content = response.choices[0].message.content
        if not content:
            logger.error("OpenAI returned empty response")
            return None

        parsed = json.loads(content)
        report = BugReport(**parsed)

        if report.confidence_score < settings.confidence_threshold:
            logger.info(
                f"Bug report confidence {report.confidence_score} below threshold "
                f"{settings.confidence_threshold}, skipping: {report.title}"
            )
            return None

        logger.info(f"Generated bug report: {report.title} (confidence: {report.confidence_score})")
        return report

    except Exception as e:
        logger.error(f"OpenAI bug report generation failed: {e}")
        return None
