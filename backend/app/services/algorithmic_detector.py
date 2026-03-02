"""
Per-session algorithmic bug detection — zero AI cost, instant results.

Detects issues that don't require contextual reasoning:
  1. Instant bounce (user leaves within 2-3s)
  2. Flash error (error visible < 1s then removed)
  3. Network errors (4xx/5xx)
  4. Console errors/exceptions
  5. Form submit with no response
  6. Silent failure (network error + no DOM error shown)

Each detector produces DetectedIssue objects (same schema as rule engine)
so they merge seamlessly into the main pipeline.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from app.connectors.base import NormalizedEvent, NormalizedSession
from app.services.rule_engine import DetectedIssue, _extract_steps_before
from app.utils.logger import logger


# ── Shared constants ─────────────────────────────────────────────────────────

_AUTH_PAGES = frozenset((
    "/login", "/signin", "/sign-in", "/register", "/signup", "/sign-up",
    "/verify", "/confirm", "/callback", "/auth", "/logout", "/sign_out",
    "/forgot-password", "/reset-password", "/onboarding", "/welcome",
    "/sso", "/oauth",
))

_INTERACTIVE_TYPES = frozenset((
    "click", "tap", "input", "submit", "focus", "dead_click", "rage_click",
))

_ERROR_KEYWORDS_RE = re.compile(
    r"(error|fail|invalid|denied|expired|refused|unavailable|forbidden"
    r"|not found|timed?\s*out|unauthorized|exception|crash|broke"
    r"|something went wrong|try again|oops|unable to|cannot|couldn.t"
    r"|unexpected|sorry|problem|warning|alert|critical"
    r"|could not|failed to|rejected|blocked|disabled"
    r"|no access|no permission|not allowed|bad request|server error"
    r"|500|404|403|401|network|offline|connection|reset)",
    re.IGNORECASE,
)

_SUCCESS_KEYWORDS_RE = re.compile(
    r"(success|saved|submitted|confirmed|complete|done|created|updated"
    r"|sent|accepted|approved|verified|thank|welcome|congratulations"
    r"|logged in|signed in|redirecting)",
    re.IGNORECASE,
)

_LOADING_KEYWORDS_RE = re.compile(
    r"(loading|spinner|skeleton|please wait|fetching|processing"
    r"|initializing|connecting|preparing)",
    re.IGNORECASE,
)

# Transitional / expected UI messages that are NOT errors even if they disappear.
# These are normal app behavior: redirect messages, loading states, auth flows.
_TRANSITIONAL_RE = re.compile(
    r"(redirecting|redirect(?:ed)?\s+to"
    r"|please wait|one moment|hang tight|just a moment|almost there"
    r"|signing (?:you )?(?:in|out|up)|logging (?:you )?(?:in|out)"
    r"|authenticat|verifying|checking (?:your|credentials|session|access)"
    r"|loading your|setting up|establishing|initializing"
    r"|taking you to|navigating to|forwarding to)",
    re.IGNORECASE,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _fingerprint(rule_id: str, key: str) -> str:
    return hashlib.sha256(f"algo:{rule_id}:{key}".encode()).hexdigest()


def _normalize_url(url: str) -> str:
    if not url:
        return ""
    url = url.strip()
    if url.startswith("http://") or url.startswith("https://"):
        parsed = urlparse(url)
        return parsed.scheme + "://" + parsed.netloc + parsed.path.rstrip("/")
    return url.split("#")[0].split("?")[0].rstrip("/").lower()


def _is_auth_page(url: str) -> bool:
    path = urlparse(url).path if url.startswith("http") else url
    path = path.rstrip("/").lower()
    return any(ap in path for ap in _AUTH_PAGES)


def _parse_ts(ts_str: str) -> float | None:
    if not ts_str:
        return None
    try:
        ts_str = ts_str.strip()
        if ts_str.endswith("Z"):
            ts_str = ts_str[:-1] + "+00:00"
        return datetime.fromisoformat(ts_str).timestamp()
    except (ValueError, TypeError):
        return None


def _time_diff_s(ts_a: str, ts_b: str) -> float | None:
    """Return seconds between two timestamps, or None if unparseable."""
    a = _parse_ts(ts_a)
    b = _parse_ts(ts_b)
    if a is None or b is None:
        return None
    return abs(b - a)


def _find_dom_near_ts(
    dom_texts: list[dict],
    target_ts: str,
    window_s: float = 5.0,
    page_filter: str | None = None,
) -> list[dict]:
    """
    Return DOM text snapshots within ±window_s of a target timestamp.
    Optionally filter by page URL. Returns matching snapshots sorted by
    time proximity (closest first).
    """
    target_epoch = _parse_ts(target_ts)
    if target_epoch is None:
        return []

    matches: list[tuple[float, dict]] = []
    page_norm = _normalize_url(page_filter) if page_filter else None

    for dt in dom_texts:
        dt_epoch = _parse_ts(dt.get("timestamp", ""))
        if dt_epoch is None:
            continue
        distance = abs(dt_epoch - target_epoch)
        if distance > window_s:
            continue
        if page_norm:
            dt_page = _normalize_url(dt.get("page", ""))
            if dt_page and dt_page != page_norm:
                continue
        matches.append((distance, dt))

    matches.sort(key=lambda x: x[0])
    return [m[1] for m in matches]


def _dom_text_contains(dom_snapshot: dict, pattern: re.Pattern) -> list[str]:
    """Extract lines from a DOM markdown snapshot that match a regex pattern."""
    text = dom_snapshot.get("text", "")
    if not text:
        return []
    return [
        line.strip()
        for line in text.split("\n")
        if line.strip() and pattern.search(line)
    ]


# ── Algorithmic Detector ─────────────────────────────────────────────────────

class AlgorithmicDetector:
    """
    Per-session rule-based bug detection — no AI, instant, zero cost.

    Usage:
        detector = AlgorithmicDetector()
        issues = detector.detect(session, dom_diffs)
    """

    def __init__(self, skip_page_patterns: list[str] | None = None):
        self._skip_patterns = skip_page_patterns or []

    def _should_skip_page(self, url: str) -> bool:
        if not url:
            return False
        url_lower = url.lower()
        return any(p.lower() in url_lower for p in self._skip_patterns)

    def detect(
        self,
        session: NormalizedSession,
        dom_diffs: list[dict] | None = None,
        dom_texts: list[dict] | None = None,
    ) -> list[DetectedIssue]:
        """
        Run all detectors on a single session, return combined issues.

        Args:
            session: Normalized session data (events, metadata).
            dom_diffs: Pre-computed DOM diffs (line-level changes with visibility duration).
            dom_texts: Raw DOM markdown snapshots (full page text at each timestamp).
                       Used to check what was actually visible on screen when events occurred.
        """
        # Pre-compute auth redirect pages so detectors can skip them
        auth_redirect_pages = self._detect_auth_redirect_pages(session, dom_texts)

        issues: list[DetectedIssue] = []

        issues.extend(self._detect_instant_bounce(session, dom_texts=dom_texts, skip_pages=auth_redirect_pages))
        issues.extend(self._detect_network_error(session, dom_texts=dom_texts))
        issues.extend(self._detect_console_error(session, dom_texts=dom_texts))
        issues.extend(self._detect_form_submit_no_response(session, dom_texts=dom_texts))

        if dom_diffs:
            # NOTE: flash_error detector removed — it produced too many false
            # positives (e.g. UI component re-renders, tab switches, code
            # examples loading). May revisit with better heuristics later.
            issues.extend(self._detect_silent_failure(session, dom_diffs, dom_texts=dom_texts))

        # Filter out any issues on auth redirect pages (transitional pages
        # that auto-redirect to login are not bugs)
        if auth_redirect_pages:
            issues = [
                iss for iss in issues
                if _normalize_url(iss.page_url) not in auth_redirect_pages
            ]

        # Filter out console errors on auth pages (/login, /auth/callback, etc.)
        # when the session shows a successful login flow (user ends up on a
        # non-auth page). These are typically benign OAuth/SSO artifacts.
        auth_page_issues = [
            iss for iss in issues
            if iss.rule_id == "console_error"
            and _is_auth_page(iss.page_url)
        ]
        if auth_page_issues and self._is_successful_auth_flow(session):
            issues = [
                iss for iss in issues
                if not (iss.rule_id == "console_error" and _is_auth_page(iss.page_url))
            ]

        return issues

    def _detect_auth_redirect_pages(
        self,
        session: NormalizedSession,
        dom_texts: list[dict] | None = None,
    ) -> set[str]:
        """
        Detect the auth redirect pattern: user visits a protected page, app
        shows a transitional message (e.g. "Redirecting to login..."), then
        navigates to an auth page. Returns set of normalized page URLs that
        are part of this normal auth redirect flow.

        Pattern:
          1. PAGEVIEW on non-auth page (the protected page)
          2. Optional: DOM shows transitional text ("Redirecting...", "Please wait...")
          3. PAGEVIEW on auth page (/login, /signin, etc.) within 120s
          4. No meaningful user interactions between the two pageviews

        Pages matching this pattern should NOT be flagged for:
          - "Instant bounce" (user left quickly — because they were redirected)
          - "Flash error" (transitional message removed — because redirect completed)
          - Other UX issues (this is normal auth flow)
        """
        redirect_pages: set[str] = set()
        events = session.events
        pageviews = [
            (i, ev) for i, ev in enumerate(events) if ev.event_type == "pageview"
        ]

        for pv_idx in range(len(pageviews) - 1):
            idx, pv = pageviews[pv_idx]
            next_idx, next_pv = pageviews[pv_idx + 1]
            page_url = pv.pathname or pv.url or ""
            next_page_url = next_pv.pathname or next_pv.url or ""

            # Step 1: Current page is NOT an auth page
            if _is_auth_page(page_url):
                continue

            # Step 2: Next page IS an auth page
            if not _is_auth_page(next_page_url):
                continue

            # Step 3: Time between pageviews is < 120s
            gap = _time_diff_s(pv.timestamp, next_pv.timestamp)
            if gap is None or gap > 120.0:
                continue

            # Step 4: No meaningful user interactions between the two pageviews
            between = events[idx + 1: next_idx]
            had_interaction = any(e.event_type in _INTERACTIVE_TYPES for e in between)
            if had_interaction:
                continue

            # Step 5 (optional): Check DOM for transitional message
            has_transitional_dom = False
            if dom_texts:
                nearby = _find_dom_near_ts(
                    dom_texts, pv.timestamp, window_s=gap + 2.0, page_filter=page_url
                )
                for snap in nearby:
                    transitional_lines = _dom_text_contains(snap, _TRANSITIONAL_RE)
                    if transitional_lines:
                        has_transitional_dom = True
                        break

            # Even without DOM confirmation, the pattern is strong enough:
            # non-auth page → auth page with no interactions = redirect flow
            redirect_pages.add(_normalize_url(page_url))
            if has_transitional_dom:
                logger.debug(
                    f"Auth redirect detected: {page_url} → {next_page_url} "
                    f"(transitional DOM confirmed, {gap:.1f}s)"
                )
            else:
                logger.debug(
                    f"Auth redirect detected: {page_url} → {next_page_url} "
                    f"(no interactions, {gap:.1f}s)"
                )

        return redirect_pages

    @staticmethod
    def _is_successful_auth_flow(session: NormalizedSession) -> bool:
        """
        Check if this session represents a successful login flow:
        user starts on auth pages (login, callback, etc.) and eventually
        reaches a non-auth page (dashboard, home, settings, etc.).

        If yes, console errors on auth pages are likely benign OAuth/SSO
        artifacts and should be filtered.
        """
        pageviews = [
            ev for ev in session.events if ev.event_type == "pageview"
        ]
        if len(pageviews) < 2:
            return False

        # Check if any later pageview lands on a non-auth page
        has_auth_page = False
        has_non_auth_after = False

        for pv in pageviews:
            page = pv.pathname or pv.url or ""
            if _is_auth_page(page):
                has_auth_page = True
            elif has_auth_page:
                # We've seen auth page(s) and now landed on a non-auth page
                has_non_auth_after = True
                break

        return has_non_auth_after

    # ── 1. Instant Bounce ────────────────────────────────────────────────

    def _detect_instant_bounce(
        self,
        session: NormalizedSession,
        dom_texts: list[dict] | None = None,
        skip_pages: set[str] | None = None,
    ) -> list[DetectedIssue]:
        """
        User lands on a page and leaves within 2-3 seconds with zero
        interactions (no clicks, inputs, submits, scrolls).

        With DOM markdown: checks what was visible when user bounced
        (loading state, error, empty page) and includes it in evidence.
        """
        issues: list[DetectedIssue] = []
        events = session.events
        pageviews = [
            (i, ev) for i, ev in enumerate(events) if ev.event_type == "pageview"
        ]

        for pv_idx, (idx, pv) in enumerate(pageviews):
            page_url = pv.pathname or pv.url or ""
            if not page_url or _is_auth_page(page_url) or self._should_skip_page(page_url):
                continue
            # Skip pages identified as part of auth redirect flow
            if skip_pages and _normalize_url(page_url) in skip_pages:
                continue

            # Find the end boundary: next pageview or end of session
            next_pv_idx = pageviews[pv_idx + 1][0] if pv_idx + 1 < len(pageviews) else len(events)
            between = events[idx + 1: next_pv_idx]

            # Check time gap
            if pv_idx + 1 < len(pageviews):
                gap = _time_diff_s(pv.timestamp, pageviews[pv_idx + 1][1].timestamp)
            elif between:
                gap = _time_diff_s(pv.timestamp, between[-1].timestamp)
            else:
                continue  # Can't determine duration

            if gap is None or gap > 3.0:
                continue

            # Check for zero interactions
            had_interaction = any(e.event_type in _INTERACTIVE_TYPES for e in between)
            if had_interaction:
                continue

            fp = _fingerprint("instant_bounce", _normalize_url(page_url))
            steps = _extract_steps_before(session, pv.timestamp)

            # ── DOM enrichment: what was visible when user bounced? ──
            dom_state = ""
            bounce_reason = ""
            if dom_texts:
                nearby_dom = _find_dom_near_ts(
                    dom_texts, pv.timestamp, window_s=4.0, page_filter=page_url
                )
                if nearby_dom:
                    snapshot = nearby_dom[0]
                    full_text = snapshot.get("text", "")
                    error_lines = _dom_text_contains(snapshot, _ERROR_KEYWORDS_RE)
                    loading_lines = _dom_text_contains(snapshot, _LOADING_KEYWORDS_RE)

                    if error_lines:
                        bounce_reason = "error_on_page"
                        dom_state = f"Error visible: {error_lines[0][:120]}"
                    elif loading_lines:
                        bounce_reason = "stuck_loading"
                        dom_state = f"Loading state: {loading_lines[0][:120]}"
                    elif len(full_text.strip()) < 50:
                        bounce_reason = "empty_page"
                        dom_state = "Page appeared empty or had minimal content"

            severity = "medium" if bounce_reason in ("error_on_page", "stuck_loading") else "low"
            description = (
                f"User landed on {page_url} and left within {gap:.1f}s "
                f"with zero interactions (no clicks, inputs, or scrolls)."
            )
            if dom_state:
                description += f" DOM state at bounce: {dom_state}"

            why = "Users are immediately leaving this page without engaging, suggesting the content or loading state is driving them away."
            if bounce_reason == "error_on_page":
                why = "User bounced because an error was visible on the page — the page may be broken or showing a confusing error."
            elif bounce_reason == "stuck_loading":
                why = "User bounced while the page was still in a loading state — the page may be too slow to load."
            elif bounce_reason == "empty_page":
                why = "User bounced from a page with very little content — the page may have failed to render."

            evidence = {
                "session": session.id,
                "timestamp": pv.timestamp,
                "page": page_url,
                "time_on_page": f"{gap:.1f}s",
                "interactions": 0,
            }
            if dom_state:
                evidence["dom_state_at_bounce"] = dom_state
            if bounce_reason:
                evidence["bounce_reason"] = bounce_reason

            issues.append(DetectedIssue(
                rule_id="instant_bounce",
                title=f"Instant bounce on {page_url}" + (f" ({bounce_reason.replace('_', ' ')})" if bounce_reason else ""),
                description=description,
                why_issue=why,
                severity=severity,
                category="ux_friction",
                page_url=page_url,
                selector="",
                affected_users=[session.distinct_id],
                total_occurrences=1,
                sample_sessions=[session.id],
                evidence=[evidence],
                confidence=0.90 if bounce_reason else 0.85,
                fingerprint=fp,
                reproduction_steps=steps,
            ))

        return issues

    # ── 2. Flash Error ───────────────────────────────────────────────────

    def _detect_flash_error(
        self,
        session: NormalizedSession,
        dom_diffs: list[dict],
        skip_pages: set[str] | None = None,
    ) -> list[DetectedIssue]:
        """
        Error/alert text appeared in DOM then was removed in < 1 second.
        User couldn't read it.
        """
        issues: list[DetectedIssue] = []
        _DURATION_RE = re.compile(r"\(was visible for (\d+)ms\)")

        for diff in dom_diffs:
            if not diff.get("is_diff"):
                continue

            text = diff.get("text", "")
            page = diff.get("page", "")
            ts = diff.get("timestamp", "")

            # Skip pages identified as part of auth redirect flow
            if skip_pages and _normalize_url(page) in skip_pages:
                continue

            if "REMOVED:" not in text:
                continue

            for line in text.split("\n"):
                line = line.strip()
                if not line.startswith("- "):
                    continue

                # Check if it has a visibility duration < 1000ms
                match = _DURATION_RE.search(line)
                if not match:
                    continue

                duration_ms = int(match.group(1))
                if duration_ms >= 1000:
                    continue  # Visible long enough, not a flash error

                # Check if the removed text is error-like
                # Strip the duration annotation for matching
                clean_line = _DURATION_RE.sub("", line[2:]).strip()
                if not _ERROR_KEYWORDS_RE.search(clean_line):
                    continue

                # Skip transitional/expected messages (redirecting, please wait, etc.)
                if _TRANSITIONAL_RE.search(clean_line):
                    continue

                fp = _fingerprint("flash_error", f"{_normalize_url(page)}||{clean_line[:60]}")

                issues.append(DetectedIssue(
                    rule_id="flash_error",
                    title=f"Flash error: message visible for only {duration_ms}ms",
                    description=(
                        f'Error text "{clean_line[:80]}" appeared on {page} '
                        f"but was removed after only {duration_ms}ms. "
                        f"The user could not read the error message."
                    ),
                    why_issue=(
                        "An error message appeared and disappeared too fast for the user to read. "
                        "This may be caused by a React re-render, auto-dismiss timer, or state reset clearing the error."
                    ),
                    severity="high",
                    category="broken_ui",
                    page_url=page,
                    selector="",
                    affected_users=[session.distinct_id],
                    total_occurrences=1,
                    sample_sessions=[session.id],
                    evidence=[{
                        "session": session.id,
                        "timestamp": ts,
                        "page": page,
                        "error_text": clean_line[:200],
                        "visible_duration_ms": duration_ms,
                    }],
                    confidence=0.90,
                    fingerprint=fp,
                ))

        return issues

    # ── 3. Network Error ─────────────────────────────────────────────────

    def _detect_network_error(
        self,
        session: NormalizedSession,
        dom_texts: list[dict] | None = None,
    ) -> list[DetectedIssue]:
        """
        Detect HTTP 4xx/5xx network errors. Group by endpoint + status code.

        With DOM markdown: checks what the user saw on screen when the error
        occurred (error message shown? page stuck loading? no visible feedback?).
        """
        issues: list[DetectedIssue] = []
        # Group: (endpoint, status_code) → list of events
        groups: dict[str, list[NormalizedEvent]] = {}

        for ev in session.events:
            if ev.event_type != "network_error" or not ev.status_code:
                continue
            if ev.status_code < 400:
                continue

            # Skip 401 on auth pages (normal redirect flow)
            page = ev.url or ev.pathname or ""
            if ev.status_code == 401 and _is_auth_page(page):
                continue

            if self._should_skip_page(page):
                continue

            endpoint = (ev.endpoint or "")[:100]
            key = f"{ev.method or 'GET'}:{endpoint}:{ev.status_code}"
            groups.setdefault(key, []).append(ev)

        for key, evts in groups.items():
            first = evts[0]
            endpoint = (first.endpoint or "")[:100]
            page = first.url or first.pathname or ""
            severity = "high" if (first.status_code or 0) >= 500 else "medium"

            fp = _fingerprint("network_error", f"{_normalize_url(page)}||{key}")
            steps = _extract_steps_before(session, first.timestamp)

            # ── DOM enrichment: what did user see when error happened? ──
            user_visible_state = ""
            if dom_texts:
                nearby = _find_dom_near_ts(
                    dom_texts, first.timestamp, window_s=5.0, page_filter=page
                )
                if nearby:
                    snapshot = nearby[0]
                    error_lines = _dom_text_contains(snapshot, _ERROR_KEYWORDS_RE)
                    if error_lines:
                        user_visible_state = f"Error shown to user: {error_lines[0][:120]}"
                    else:
                        user_visible_state = "No error message visible to user"

            evidence_list = []
            for e in evts[:5]:
                ev_evidence: dict[str, Any] = {
                    "session": session.id,
                    "timestamp": e.timestamp,
                    "method": e.method,
                    "endpoint": (e.endpoint or "")[:150],
                    "status_code": e.status_code,
                    "page": e.url or e.pathname or "",
                }
                evidence_list.append(ev_evidence)

            if user_visible_state and evidence_list:
                evidence_list[0]["dom_visible_state"] = user_visible_state

            description = (
                f"{first.method} {endpoint} returned HTTP {first.status_code} "
                f"{len(evts)} time(s) on {page}."
            )
            if user_visible_state:
                description += f" {user_visible_state}"

            issues.append(DetectedIssue(
                rule_id="network_error",
                title=f"HTTP {first.status_code} on {first.method} {endpoint[:50]}",
                description=description,
                why_issue=(
                    f"API request failed with status {first.status_code}. "
                    f"{'Server error — the backend is broken or overloaded.' if (first.status_code or 0) >= 500 else 'Client error — the request was rejected.'}"
                ),
                severity=severity,
                category="error",
                page_url=page,
                selector=endpoint,
                affected_users=[session.distinct_id],
                total_occurrences=len(evts),
                sample_sessions=[session.id],
                evidence=evidence_list,
                confidence=0.85,
                fingerprint=fp,
                reproduction_steps=steps,
            ))

        return issues

    # ── 4. Console Error ─────────────────────────────────────────────────

    def _detect_console_error(
        self,
        session: NormalizedSession,
        dom_texts: list[dict] | None = None,
    ) -> list[DetectedIssue]:
        """
        Detect JavaScript errors/exceptions. Group by error type + message prefix.

        With DOM markdown: checks if the JS error visibly affected the UI (error
        text in DOM, broken/empty content). If the UI shows an error → severity
        bumped to high. If DOM looks normal → stays medium (background error).
        """
        issues: list[DetectedIssue] = []
        groups: dict[str, list[NormalizedEvent]] = {}

        for ev in session.events:
            if ev.event_type != "error" or not ev.error_message:
                continue

            page = ev.url or ev.pathname or ""
            if self._should_skip_page(page):
                continue

            # Group by page + type + first 100 chars of message
            # (same error on different pages = different issues)
            err_type = ev.error_type or "Error"
            msg_prefix = ev.error_message[:100].strip()
            page_key = _normalize_url(page)
            key = f"{page_key}||{err_type}:{msg_prefix}"
            groups.setdefault(key, []).append(ev)

        for key, evts in groups.items():
            first = evts[0]
            err_type = first.error_type or "Error"
            page = first.url or first.pathname or ""

            fp = _fingerprint("console_error", f"{_normalize_url(page)}||{key[:80]}")
            steps = _extract_steps_before(session, first.timestamp)

            # ── DOM enrichment: did the JS error break visible UI? ──
            ui_impact = ""
            severity = "medium"
            if dom_texts:
                nearby = _find_dom_near_ts(
                    dom_texts, first.timestamp, window_s=5.0, page_filter=page
                )
                if nearby:
                    snapshot = nearby[0]
                    full_text = snapshot.get("text", "")
                    error_lines = _dom_text_contains(snapshot, _ERROR_KEYWORDS_RE)

                    if error_lines:
                        ui_impact = f"Error visible in UI: {error_lines[0][:120]}"
                        severity = "high"
                    elif len(full_text.strip()) < 50:
                        ui_impact = "Page content appears empty/broken after error"
                        severity = "high"
                    else:
                        ui_impact = "UI appears normal despite JS error (background error)"

            evidence_list = []
            for e in evts[:5]:
                ev_evidence: dict[str, Any] = {
                    "session": session.id,
                    "timestamp": e.timestamp,
                    "error_type": e.error_type,
                    "error_message": e.error_message[:200],
                    "page": e.url or e.pathname or "",
                }
                evidence_list.append(ev_evidence)

            if ui_impact and evidence_list:
                evidence_list[0]["ui_impact"] = ui_impact

            description = (
                f"JavaScript {err_type} occurred {len(evts)} time(s) on {page}: "
                f'"{first.error_message[:150]}"'
            )
            if ui_impact:
                description += f" — {ui_impact}"

            issues.append(DetectedIssue(
                rule_id="console_error",
                title=f"{err_type}: {first.error_message[:60]}",
                description=description,
                why_issue=(
                    "A JavaScript error occurred which may affect page functionality. "
                    "This could cause broken buttons, failed form submissions, or missing content."
                ),
                severity=severity,
                category="error",
                page_url=page,
                selector="",
                affected_users=[session.distinct_id],
                total_occurrences=len(evts),
                sample_sessions=[session.id],
                evidence=evidence_list,
                confidence=0.85 if ui_impact else 0.75,
                fingerprint=fp,
                reproduction_steps=steps,
            ))

        return issues

    # ── 5. Form Submit No Response ───────────────────────────────────────

    def _detect_form_submit_no_response(
        self,
        session: NormalizedSession,
        dom_texts: list[dict] | None = None,
    ) -> list[DetectedIssue]:
        """
        User submits a form but nothing happens — no navigation, no network
        request, no error, no DOM change within 3 seconds.

        With DOM markdown: also checks if success/error text appeared in the
        DOM after submit. If success text appeared → not a bug. If error text
        appeared → it IS feedback (not "no response"). If DOM is unchanged →
        confirms the "no response" diagnosis.
        """
        issues: list[DetectedIssue] = []
        events = session.events

        for i, ev in enumerate(events):
            if ev.event_type != "submit":
                continue

            page = ev.url or ev.pathname or ""
            if self._should_skip_page(page):
                continue

            # Look at next 12 events within 10 seconds.
            # We use a generous window because form responses (redirects,
            # error messages, network calls) can take several seconds,
            # and the user may wait before clicking elsewhere.
            _FORM_RESPONSE_WINDOW_S = 10.0
            following = events[i + 1: i + 13]
            following_in_window = []
            for f_ev in following:
                gap = _time_diff_s(ev.timestamp, f_ev.timestamp)
                if gap is not None and gap <= _FORM_RESPONSE_WINDOW_S:
                    following_in_window.append(f_ev)

            if not following_in_window:
                # No events at all within window — definitely no response
                pass
            else:
                has_nav = any(e.event_type == "pageview" for e in following_in_window)
                has_network = any(e.event_type == "network_error" for e in following_in_window)
                has_error = any(e.event_type == "error" for e in following_in_window)
                # Any custom/API-like event
                has_response = any(
                    e.event_type in ("pageview", "network_error", "custom")
                    for e in following_in_window
                )
                # User navigated to a DIFFERENT page → the form triggered
                # a response (redirect, or the user saw feedback and moved on).
                has_page_change = any(
                    e.event_type == "pageview"
                    and _normalize_url(e.url or e.pathname or "") != _normalize_url(page)
                    for e in following_in_window
                )

                if has_nav or has_network or has_error or has_response or has_page_change:
                    continue  # Something happened in events

            # ── DOM enrichment: check if DOM changed after submit ──
            dom_feedback = ""
            if dom_texts:
                # Get DOM snapshots BEFORE submit
                before_dom = _find_dom_near_ts(
                    dom_texts, ev.timestamp, window_s=2.0, page_filter=page
                )
                before_text = before_dom[0].get("text", "") if before_dom else ""

                # Get DOM snapshots AFTER submit (0.5-15s window).
                # Wide window because rrweb snapshots may not be captured
                # immediately after the form submit.
                submit_epoch = _parse_ts(ev.timestamp)
                if submit_epoch is not None:
                    after_snapshots = []
                    for dt in dom_texts:
                        dt_epoch = _parse_ts(dt.get("timestamp", ""))
                        if dt_epoch is not None and 0.5 < (dt_epoch - submit_epoch) <= 15.0:
                            dt_page = _normalize_url(dt.get("page", ""))
                            page_norm = _normalize_url(page)
                            if not dt_page or dt_page == page_norm:
                                after_snapshots.append(dt)

                    # Fallback: if no after-snapshots found, find the
                    # nearest snapshot on the same page within 60s.
                    if not after_snapshots:
                        page_norm = _normalize_url(page)
                        best_snap = None
                        best_dist = float("inf")
                        for dt in dom_texts:
                            dt_epoch = _parse_ts(dt.get("timestamp", ""))
                            if dt_epoch is None:
                                continue
                            dist = dt_epoch - submit_epoch
                            if 0.5 < dist <= 60.0:
                                dt_page = _normalize_url(dt.get("page", ""))
                                if not dt_page or dt_page == page_norm:
                                    if dist < best_dist:
                                        best_dist = dist
                                        best_snap = dt
                        if best_snap:
                            after_snapshots = [best_snap]

                    for after_snap in after_snapshots:
                        after_text = after_snap.get("text", "")
                        # Check if success message appeared
                        success_lines = _dom_text_contains(after_snap, _SUCCESS_KEYWORDS_RE)
                        # Only count as success if this text wasn't already there
                        new_success = [
                            ln for ln in success_lines
                            if ln not in before_text
                        ]
                        if new_success:
                            dom_feedback = "success"
                            break

                        # Check if error message appeared
                        error_lines = _dom_text_contains(after_snap, _ERROR_KEYWORDS_RE)
                        new_errors = [
                            ln for ln in error_lines
                            if ln not in before_text
                        ]
                        if new_errors:
                            dom_feedback = "error_shown"
                            break

                        # Even without matching keywords, if the DOM changed
                        # significantly after submit, the form DID respond.
                        if before_text and after_text:
                            # Simple change ratio: how different are the texts?
                            shorter = min(len(before_text), len(after_text))
                            if shorter > 50:
                                common = sum(
                                    1 for a, b in zip(before_text[:500], after_text[:500])
                                    if a == b
                                )
                                similarity = common / min(500, shorter)
                                if similarity < 0.85:
                                    # >15% of content changed → form gave visual feedback
                                    dom_feedback = "dom_changed"
                                    break

            # If DOM shows success, error, or any significant change → skip
            if dom_feedback in ("success", "error_shown", "dom_changed"):
                continue

            fp = _fingerprint("form_no_response", f"{_normalize_url(page)}||submit")
            steps = _extract_steps_before(session, ev.timestamp)

            evidence = {
                "session": session.id,
                "timestamp": ev.timestamp,
                "page": page,
                "form_action": ev.form_action or "",
                "events_after_submit": [
                    f"{e.event_type} at {e.timestamp}" for e in following_in_window[:5]
                ],
            }
            if dom_texts:
                evidence["dom_changed_after_submit"] = False

            issues.append(DetectedIssue(
                rule_id="form_no_response",
                title=f"Form submit with no response on {page}",
                description=(
                    f"User submitted a form on {page} but nothing happened within 3 seconds — "
                    f"no page navigation, no network request, no error message"
                    + (", and no DOM content change." if dom_texts else ".")
                ),
                why_issue=(
                    "The form appeared to do nothing when submitted. The user has no feedback "
                    "whether the action succeeded or failed."
                ),
                severity="high",
                category="form_validation",
                page_url=page,
                selector=ev.css_selector or "",
                affected_users=[session.distinct_id],
                total_occurrences=1,
                sample_sessions=[session.id],
                evidence=[evidence],
                confidence=0.92 if dom_texts else 0.85,
                fingerprint=fp,
                reproduction_steps=steps,
            ))

        return issues

    # ── 6. Silent Failure ────────────────────────────────────────────────

    def _detect_silent_failure(
        self,
        session: NormalizedSession,
        dom_diffs: list[dict],
        dom_texts: list[dict] | None = None,
    ) -> list[DetectedIssue]:
        """
        Network returned 4xx/5xx but no error message appeared in the DOM
        within ±30 seconds. The user doesn't know something broke.

        With DOM markdown: double-checks raw snapshots as fallback — diffs
        might miss errors that were already on the page. Also captures what
        the user actually saw on screen for richer evidence.
        """
        issues: list[DetectedIssue] = []

        # Collect all network errors
        net_errors = [
            ev for ev in session.events
            if ev.event_type == "network_error"
            and ev.status_code
            and ev.status_code >= 400
        ]

        if not net_errors or not dom_diffs:
            return []

        # Pre-parse DOM diff timestamps and error content (ADDED lines)
        dom_error_windows: list[tuple[float, str]] = []
        for diff in dom_diffs:
            diff_text = diff.get("text", "")
            diff_ts = _parse_ts(diff.get("timestamp", ""))
            if diff_ts is None:
                continue
            for line in diff_text.split("\n"):
                if line.strip().startswith("+ ") and _ERROR_KEYWORDS_RE.search(line):
                    dom_error_windows.append((diff_ts, line.strip()))

        for ev in net_errors:
            page = ev.url or ev.pathname or ""
            if _is_auth_page(page) or self._should_skip_page(page):
                continue

            ev_epoch = _parse_ts(ev.timestamp)
            if ev_epoch is None:
                continue

            # Check 1: Did any error-like text appear in DOM diffs within ±30s?
            error_in_diffs = any(
                abs(dom_ts - ev_epoch) <= 30
                for dom_ts, _ in dom_error_windows
            )

            if error_in_diffs:
                continue  # Error was shown to user — not silent

            # Check 2 (fallback): Raw DOM snapshots — is error text or
            # transitional feedback already visible on the page?
            # Diffs might miss errors that were already present.
            feedback_in_raw_dom = False
            visible_dom_context = ""
            if dom_texts:
                nearby = _find_dom_near_ts(
                    dom_texts, ev.timestamp, window_s=10.0, page_filter=page
                )
                for snapshot in nearby:
                    error_lines = _dom_text_contains(snapshot, _ERROR_KEYWORDS_RE)
                    transitional_lines = _dom_text_contains(snapshot, _TRANSITIONAL_RE)
                    if error_lines:
                        feedback_in_raw_dom = True
                        visible_dom_context = f"Error already on page: {error_lines[0][:120]}"
                        break
                    if transitional_lines:
                        # "Redirecting to login..." etc. counts as user feedback
                        feedback_in_raw_dom = True
                        visible_dom_context = f"Transitional message on page: {transitional_lines[0][:120]}"
                        break

                if not feedback_in_raw_dom and nearby:
                    # Capture what user actually saw for evidence
                    snap_text = nearby[0].get("text", "")
                    if snap_text:
                        # Truncate to first 200 chars for evidence
                        visible_dom_context = f"DOM at time of failure: {snap_text[:200].strip()}"

            if feedback_in_raw_dom:
                continue  # Error or transitional feedback visible — not silent

            endpoint = (ev.endpoint or "")[:100]
            fp = _fingerprint(
                "silent_failure",
                f"{_normalize_url(page)}||{ev.method}:{endpoint}:{ev.status_code}",
            )
            steps = _extract_steps_before(session, ev.timestamp)

            evidence: dict[str, Any] = {
                "session": session.id,
                "timestamp": ev.timestamp,
                "method": ev.method,
                "endpoint": endpoint,
                "status_code": ev.status_code,
                "page": page,
                "error_shown_in_dom": False,
            }
            if visible_dom_context:
                evidence["user_visible_dom"] = visible_dom_context

            issues.append(DetectedIssue(
                rule_id="silent_failure",
                title=f"Silent failure: {ev.method} {endpoint[:40]} → {ev.status_code} (no error shown)",
                description=(
                    f"{ev.method} {endpoint} returned HTTP {ev.status_code} on {page} "
                    f"but no error message appeared in the UI within 30 seconds. "
                    f"The user was not informed of the failure."
                ),
                why_issue=(
                    "A backend request failed but the user received no feedback. "
                    "They may believe the action succeeded when it actually failed."
                ),
                severity="critical",
                category="error",
                page_url=page,
                selector=endpoint,
                affected_users=[session.distinct_id],
                total_occurrences=1,
                sample_sessions=[session.id],
                evidence=[evidence],
                confidence=0.88 if dom_texts else 0.80,
                fingerprint=fp,
                reproduction_steps=steps,
            ))

        return issues
