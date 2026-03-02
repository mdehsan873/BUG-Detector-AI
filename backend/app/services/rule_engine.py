"""
Rule-based cross-session bug detection engine.

Instead of asking AI to guess per-session, this engine applies deterministic
rules across ALL sessions and only flags patterns that repeat across multiple
users.  This eliminates noise and builds trust.

Critical implementation rule:
  Never create an issue if only 1 session or 1 user.
  Only escalate if unique_users >= min_users AND total_occurrences >= threshold.
  Default threshold: 2 users (configurable per project).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.connectors.base import NormalizedSession, NormalizedEvent
from app.utils.logger import logger


# ─── Error text patterns to detect in UI element text / error messages ────────

_ERROR_PATTERNS = re.compile(
    r"("
    r"failed\s+to\s+fetch"
    r"|something\s+went\s+wrong"
    r"|unexpected\s+error"
    r"|server\s+error"
    r"|internal\s+server\s+error"
    r"|an?\s+error\s+(has\s+)?occur(?:r)?ed"
    r"|network\s+error"
    r"|request\s+failed"
    r"|connection\s+(refused|timed?\s*out|reset|failed)"
    r"|could\s+not\s+connect"
    r"|unable\s+to\s+(load|connect|process|complete|fetch)"
    r"|not\s+found"
    r"|access\s+denied"
    r"|unauthorized"
    r"|forbidden"
    r"|timeout"
    r"|too\s+many\s+requests"
    r"|rate\s+limit"
    r"|bad\s+request"
    r"|service\s+unavailable"
    r"|gateway\s+timeout"
    r"|try\s+again\s+later"
    r"|failed\s+to\s+(load|submit|save|create|update|delete|register|sign|log)"
    r"|oops"
    r"|error\s*[:!]"
    r"|err_"
    r"|ECONNREFUSED"
    r"|ETIMEDOUT"
    r"|CORS\s+error"
    r")",
    re.IGNORECASE,
)

# Patterns to EXCLUDE (not real errors — normal UI text)
_ERROR_FALSE_POSITIVES = re.compile(
    r"("
    r"error\s+reporting"
    r"|error\s+log"
    r"|error\s+handling"
    r"|no\s+errors?\s+found"
    r"|password\s+not\s+found"  # might be normal "not found" label
    r"|page\s+not\s+found"  # 404 pages handled elsewhere
    r"|sign\s+in\s+to"
    r"|log\s+in\s+to"
    r"|forgot\s+password"
    r")",
    re.IGNORECASE,
)


# ─── Output types ─────────────────────────────────────────────────────────────

@dataclass
class DetectedIssue:
    """A confirmed cross-session issue detected by the rule engine."""

    rule_id: str                  # e.g. "rage_click", "dead_click"
    title: str                    # Human-readable title
    description: str              # What happened
    why_issue: str                # Real-world user impact
    severity: str                 # critical | high | medium | low
    category: str                 # Maps to frontend badge
    page_url: str                 # Where it happened
    selector: str                 # CSS selector or element description
    affected_users: list[str]     # distinct_ids
    total_occurrences: int        # Total events across all sessions
    sample_sessions: list[str]    # Up to 5 session IDs for evidence
    evidence: list[dict]          # Timestamped evidence items
    confidence: float             # 0.0–1.0
    fingerprint: str = ""         # Dedup key
    reproduction_steps: list[str] = field(default_factory=list)  # Real user steps from session

    def to_dict(self) -> dict:
        d = {
            "rule_id": self.rule_id,
            "title": self.title,
            "description": self.description,
            "why_issue": self.why_issue,
            "severity": self.severity,
            "category": self.category,
            "page_url": self.page_url,
            "affected_element": self.selector,
            "affected_users": len(self.affected_users),
            "affected_user_ids": self.affected_users[:10],
            "total_occurrences": self.total_occurrences,
            "sample_sessions": self.sample_sessions[:5],
            "evidence": self.evidence[:10],
            "confidence": self.confidence,
            "fingerprint": self.fingerprint,
            "reproduction_steps": self.reproduction_steps,
        }
        return d


# ─── Helper types ─────────────────────────────────────────────────────────────

@dataclass
class _Match:
    """A single rule match within one session."""
    session_id: str
    user_id: str
    page_url: str
    selector: str
    timestamp: str
    count: int = 1
    extra: dict = field(default_factory=dict)


def _fingerprint(rule_id: str, key: str) -> str:
    return hashlib.sha256(f"rule:{rule_id}:{key}".encode()).hexdigest()


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _page_key(ev: NormalizedEvent) -> str:
    url = (ev.pathname or ev.url or "")
    # Strip fragment (#) and query (?) for consistent dedup
    url = url.split("#")[0].split("?")[0]
    return url.rstrip("/").lower()


def _selector_key(ev: NormalizedEvent) -> str:
    """Build a stable key for an element."""
    if ev.css_selector:
        return ev.css_selector
    parts = []
    if ev.tag_name:
        parts.append(ev.tag_name)
    if ev.element_type:
        parts.append(f"[type={ev.element_type}]")
    if ev.element_name:
        parts.append(f"[name={ev.element_name}]")
    if ev.element_text:
        parts.append(f"'{ev.element_text[:30]}'")
    return " ".join(parts) if parts else "(unknown)"


# ─── Step extraction (real user actions from session events) ──────────────────

def _event_to_step(ev: NormalizedEvent) -> str | None:
    """Convert a single event into a human-readable step. Returns None for skip."""
    page = ev.pathname or ev.url or ""

    if ev.event_type == "pageview":
        return f"User navigated to {page}"

    if ev.event_type == "pageleave":
        return f"User left {page}"

    if ev.event_type in ("click", "tap"):
        target = ev.element_text or ev.css_selector or ev.tag_name or "element"
        if len(target) > 50:
            target = target[:47] + "..."
        tag_hint = f" {ev.tag_name}" if ev.tag_name and ev.tag_name not in target.lower() else ""
        return f"User clicked '{target}'{tag_hint} on {page}"

    if ev.event_type == "dead_click":
        target = ev.element_text or ev.css_selector or ev.tag_name or "element"
        if len(target) > 50:
            target = target[:47] + "..."
        return f"User clicked '{target}' (no response) on {page}"

    if ev.event_type == "rage_click":
        target = ev.element_text or ev.css_selector or ev.tag_name or "element"
        if len(target) > 50:
            target = target[:47] + "..."
        return f"User rage-clicked '{target}' on {page}"

    if ev.event_type == "input":
        field_name = ev.element_name or ev.css_selector or "field"
        field_type = f" ({ev.element_type})" if ev.element_type and ev.element_type not in ("text",) else ""
        return f"User typed in '{field_name}'{field_type} field on {page}"

    if ev.event_type == "focus":
        field_name = ev.element_name or ev.css_selector or "field"
        return f"User focused on '{field_name}' field on {page}"

    if ev.event_type == "submit":
        action = f" to {ev.form_action}" if ev.form_action and ev.form_action not in ("", "#") else ""
        return f"User submitted form{action} on {page}"

    # Errors and network errors are NOT user actions — skip them in repro steps.
    # Repro steps should only contain user interactions (navigation, clicks, input, scrolls).
    if ev.event_type in ("error", "network_error", "console_error"):
        return None

    if ev.event_type == "scroll" or ev.scroll_y is not None:
        return f"User scrolled on {page}"

    # Skip unknown/noise events
    return None


def _extract_steps_before(
    session: NormalizedSession,
    trigger_timestamp: str,
    max_steps: int = 15,
) -> list[str]:
    """
    Extract user actions from a session leading up to (and including) the trigger event.
    Returns human-readable steps like:
      ["User navigated to /dashboard", "User clicked on 'Save'", ...]
    """
    trigger_ts = _parse_ts(trigger_timestamp)
    steps: list[str] = []
    prev_step: str | None = None  # Deduplicate consecutive identical steps

    for ev in session.events:
        ev_ts = _parse_ts(ev.timestamp)
        # Include events up to and slightly past the trigger
        if trigger_ts and ev_ts and ev_ts > trigger_ts:
            break

        step = _event_to_step(ev)
        if step and step != prev_step:
            steps.append(step)
            prev_step = step

    # Keep only the last N steps (the ones closest to the issue)
    if len(steps) > max_steps:
        steps = steps[-max_steps:]

    return steps


def _extract_steps_for_match(
    sessions: list[NormalizedSession],
    match: _Match,
    max_steps: int = 15,
) -> list[str]:
    """Find the session for a match and extract steps."""
    for session in sessions:
        if session.id == match.session_id:
            return _extract_steps_before(session, match.timestamp, max_steps)
    return []


# ─── Cluster helper ───────────────────────────────────────────────────────────

def _cluster_matches(
    matches: list[_Match],
    min_users: int,
    min_occurrences: int,
) -> dict[str, list[_Match]]:
    """
    Group matches by (selector, page_url) key and filter by thresholds.
    Returns only clusters that meet both min_users and min_occurrences.
    """
    clusters: dict[str, list[_Match]] = {}
    for m in matches:
        key = f"{m.selector}||{m.page_url}"
        clusters.setdefault(key, []).append(m)

    filtered: dict[str, list[_Match]] = {}
    for key, group in clusters.items():
        users = {m.user_id for m in group}
        total = sum(m.count for m in group)
        if len(users) >= min_users and total >= min_occurrences:
            filtered[key] = group

    return filtered


def _cluster_by_page(
    matches: list[_Match],
    min_users: int,
    min_occurrences: int,
) -> dict[str, list[_Match]]:
    """Group matches by page_url only."""
    clusters: dict[str, list[_Match]] = {}
    for m in matches:
        clusters.setdefault(m.page_url, []).append(m)

    filtered: dict[str, list[_Match]] = {}
    for key, group in clusters.items():
        users = {m.user_id for m in group}
        total = sum(m.count for m in group)
        if len(users) >= min_users and total >= min_occurrences:
            filtered[key] = group

    return filtered


# ═══════════════════════════════════════════════════════════════════════════════
# RULE ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

class RuleEngine:
    """
    Cross-session rule-based detection engine.

    Processes a batch of sessions and returns issues that repeat across
    multiple users — eliminating single-session noise entirely.
    """

    def __init__(
        self,
        min_users: int = 2,
        min_occurrences: int = 2,
        skip_page_patterns: list[str] | None = None,
    ):
        self.min_users = min_users
        self.min_occurrences = min_occurrences
        # Build combined skip pattern from user config + defaults
        default_patterns = [
            r"auth/callback", r"/callback", r"/redirect",
            r"/oauth", r"/sso", r"/logout", r"/verify",
        ]
        user_patterns = skip_page_patterns or []
        all_patterns = default_patterns + [re.escape(p) for p in user_patterns if p]
        self._skip_page_re = re.compile(
            r"(" + "|".join(all_patterns) + r")",
            re.IGNORECASE,
        )

    def analyze(self, sessions: list[NormalizedSession]) -> list[DetectedIssue]:
        """Run all rules across sessions and return confirmed issues."""

        # ── Pre-process: sort events by timestamp within each session ─────
        # PostHog events can arrive out of order which causes false positives
        for session in sessions:
            if session.events:
                session.events.sort(key=lambda ev: ev.timestamp or "")

        all_issues: list[DetectedIssue] = []

        rules = [
            self._rule_rage_click,
            self._rule_dead_click,
            self._rule_navigation_loop,
            self._rule_rapid_back_nav,
            self._rule_stuck_interaction,
            self._rule_form_abandonment,
            self._rule_button_spam,
            self._rule_broken_flow,
            self._rule_scroll_frustration,
            self._rule_rapid_refresh,
            # self._rule_hover_without_action,  # Phase 2
            self._rule_unexpected_exit,
            self._rule_error_text_on_page,
        ]

        for rule_fn in rules:
            try:
                issues = rule_fn(sessions)
                all_issues.extend(issues)
            except Exception as exc:
                logger.error(f"Rule {rule_fn.__name__} failed: {exc}")

        # Deduplicate by fingerprint
        seen_fp: set[str] = set()
        deduped: list[DetectedIssue] = []
        for issue in all_issues:
            if issue.fingerprint and issue.fingerprint not in seen_fp:
                seen_fp.add(issue.fingerprint)
                deduped.append(issue)
            elif not issue.fingerprint:
                deduped.append(issue)

        # Second pass: deduplicate overlapping rules on same element+page.
        # E.g. rage_click and button_spam can fire for same selector+page.
        # Keep the higher-severity / higher-confidence one.
        severity_order = {"critical": 4, "high": 3, "medium": 2, "low": 1}
        seen_element_page: dict[str, int] = {}  # key → index in final list
        final: list[DetectedIssue] = []
        for issue in deduped:
            page = (issue.page_url or "").split("#")[0].split("?")[0].rstrip("/").lower()
            selector_norm = (issue.selector or "").strip().lower()
            element_page_key = f"{selector_norm}||{page}"

            if element_page_key in seen_element_page:
                existing_idx = seen_element_page[element_page_key]
                existing = final[existing_idx]
                # Keep the one with more users, or higher severity, or higher confidence
                new_score = (
                    len(issue.affected_users) * 10
                    + severity_order.get(issue.severity, 0)
                    + issue.confidence * 5
                )
                old_score = (
                    len(existing.affected_users) * 10
                    + severity_order.get(existing.severity, 0)
                    + existing.confidence * 5
                )
                if new_score > old_score:
                    final[existing_idx] = issue
                    logger.debug(
                        f"Dedup: replaced '{existing.title}' with '{issue.title}' "
                        f"(score {old_score:.1f} → {new_score:.1f})"
                    )
            else:
                seen_element_page[element_page_key] = len(final)
                final.append(issue)

        logger.info(
            f"Rule engine: {len(sessions)} sessions → "
            f"{len(final)} issues from {len(rules)} rules "
            f"(deduped from {len(all_issues)} raw)"
        )
        return final

    # ── Rule 1: Rage Click ────────────────────────────────────────────────

    def _rule_rage_click(self, sessions: list[NormalizedSession]) -> list[DetectedIssue]:
        """
        ≥5 clicks on same selector within 3 seconds, across ≥3 sessions.
        """
        matches: list[_Match] = []

        for session in sessions:
            click_groups: dict[str, list[NormalizedEvent]] = {}
            for ev in session.events:
                if ev.event_type in ("click", "tap", "rage_click"):
                    sel = _selector_key(ev)
                    page = _page_key(ev)
                    key = f"{sel}||{page}"
                    click_groups.setdefault(key, []).append(ev)

            for key, clicks in click_groups.items():
                if len(clicks) < 5:
                    continue
                # Check for 5 clicks within 3-second window
                for i in range(len(clicks) - 4):
                    window = clicks[i:i + 5]
                    ts_start = _parse_ts(window[0].timestamp)
                    ts_end = _parse_ts(window[-1].timestamp)
                    if ts_start and ts_end and (ts_end - ts_start).total_seconds() <= 3:
                        sel, page = key.split("||", 1)
                        matches.append(_Match(
                            session_id=session.id,
                            user_id=session.distinct_id,
                            page_url=page,
                            selector=sel,
                            timestamp=window[0].timestamp,
                            count=len(window),
                        ))
                        break  # One match per session per element

        clusters = _cluster_matches(matches, self.min_users, self.min_occurrences)
        issues: list[DetectedIssue] = []

        for key, group in clusters.items():
            sel, page = key.split("||", 1)
            users = list({m.user_id for m in group})
            total = sum(m.count for m in group)
            sessions_list = list({m.session_id for m in group})

            issues.append(DetectedIssue(
                rule_id="rage_click",
                title=f"Rage Click on {sel[:50]} at {page}",
                description=(
                    f"Users rapidly click '{sel}' on {page} because it doesn't respond. "
                    f"Detected {total} rage clicks across {len(users)} users."
                ),
                why_issue="Element appears interactive but fails to respond, causing user frustration.",
                severity="high" if len(users) >= 5 else "medium",
                category="rage_click",
                page_url=page,
                selector=sel,
                affected_users=users,
                total_occurrences=total,
                sample_sessions=sessions_list[:5],
                evidence=[{"session": m.session_id, "timestamp": m.timestamp, "clicks": m.count} for m in group[:5]],
                confidence=min(0.95, 0.7 + len(users) * 0.05),
                fingerprint=_fingerprint("rage_click", key),
                reproduction_steps=_extract_steps_for_match(sessions, group[0]),
            ))

        return issues

    # ── Rule 2: Dead Click ────────────────────────────────────────────────

    def _rule_dead_click(self, sessions: list[NormalizedSession]) -> list[DetectedIssue]:
        """
        Click with no navigation, no DOM mutation, no network request within 2s.
        Repeated across ≥3 sessions.
        """
        matches: list[_Match] = []

        for session in sessions:
            for i, ev in enumerate(session.events):
                if ev.event_type not in ("click", "tap", "dead_click"):
                    continue

                sel = _selector_key(ev)
                page = _page_key(ev)
                ev_ts = _parse_ts(ev.timestamp)
                if not ev_ts:
                    continue

                # Already flagged as dead_click by connector
                if ev.event_type == "dead_click":
                    matches.append(_Match(
                        session_id=session.id, user_id=session.distinct_id,
                        page_url=page, selector=sel, timestamp=ev.timestamp,
                    ))
                    continue

                # Check next events within 2 seconds
                has_response = False
                for j in range(i + 1, min(i + 10, len(session.events))):
                    nev = session.events[j]
                    nev_ts = _parse_ts(nev.timestamp)
                    if nev_ts and (nev_ts - ev_ts).total_seconds() > 2:
                        break
                    if nev.event_type in ("pageview", "network_error", "error", "submit"):
                        has_response = True
                        break
                    # Input/change on a related element counts as response
                    if nev.event_type in ("input", "focus"):
                        has_response = True
                        break

                if not has_response:
                    matches.append(_Match(
                        session_id=session.id, user_id=session.distinct_id,
                        page_url=page, selector=sel, timestamp=ev.timestamp,
                    ))

        clusters = _cluster_matches(matches, self.min_users, self.min_occurrences)
        issues: list[DetectedIssue] = []

        for key, group in clusters.items():
            sel, page = key.split("||", 1)
            users = list({m.user_id for m in group})
            sessions_list = list({m.session_id for m in group})

            issues.append(DetectedIssue(
                rule_id="dead_click",
                title=f"Dead Click on {sel[:50]} at {page}",
                description=(
                    f"Clicking '{sel}' on {page} produces no response — no navigation, "
                    f"no network request, no UI change. {len(users)} users affected."
                ),
                why_issue="Element looks clickable but does nothing, confusing users.",
                severity="high" if len(users) >= 5 else "medium",
                category="dead_click",
                page_url=page,
                selector=sel,
                affected_users=users,
                total_occurrences=len(group),
                sample_sessions=sessions_list[:5],
                evidence=[{"session": m.session_id, "timestamp": m.timestamp} for m in group[:5]],
                confidence=min(0.95, 0.7 + len(users) * 0.05),
                fingerprint=_fingerprint("dead_click", key),
                reproduction_steps=_extract_steps_for_match(sessions, group[0]),
            ))

        return issues

    # ── Rule 3: Navigation Loop ───────────────────────────────────────────

    def _rule_navigation_loop(self, sessions: list[NormalizedSession]) -> list[DetectedIssue]:
        """
        User bounces A→B→A→B at least 3 cycles, across ≥3 users.
        """
        matches: list[_Match] = []

        for session in sessions:
            pages = [
                (_page_key(ev), ev.timestamp)
                for ev in session.events
                if ev.event_type == "pageview" and _page_key(ev)
            ]
            if len(pages) < 6:
                continue

            # Detect A→B→A→B patterns
            for i in range(len(pages) - 5):
                a = pages[i][0]
                b = pages[i + 1][0]
                if a == b:
                    continue
                # Check for 3 cycles: A B A B A B
                is_loop = True
                for j in range(2, 6):
                    expected = a if j % 2 == 0 else b
                    if i + j >= len(pages) or pages[i + j][0] != expected:
                        is_loop = False
                        break

                if is_loop:
                    pair_key = f"{min(a, b)}↔{max(a, b)}"
                    matches.append(_Match(
                        session_id=session.id, user_id=session.distinct_id,
                        page_url=pair_key, selector=pair_key,
                        timestamp=pages[i][1], count=3,
                    ))
                    break  # One match per session

        clusters = _cluster_by_page(matches, self.min_users, self.min_occurrences)
        issues: list[DetectedIssue] = []

        for pair_key, group in clusters.items():
            users = list({m.user_id for m in group})
            sessions_list = list({m.session_id for m in group})

            issues.append(DetectedIssue(
                rule_id="navigation_loop",
                title=f"Navigation Loop: {pair_key}",
                description=(
                    f"Users keep bouncing between pages in a loop ({pair_key}). "
                    f"{len(users)} users stuck in this pattern."
                ),
                why_issue="Users can't find what they need and keep going back and forth — broken flow or confusing navigation.",
                severity="high",
                category="navigation_loop",
                page_url=pair_key,
                selector=pair_key,
                affected_users=users,
                total_occurrences=sum(m.count for m in group),
                sample_sessions=sessions_list[:5],
                evidence=[{"session": m.session_id, "timestamp": m.timestamp, "pages": m.page_url} for m in group[:5]],
                confidence=min(0.95, 0.75 + len(users) * 0.05),
                fingerprint=_fingerprint("navigation_loop", pair_key),
                reproduction_steps=_extract_steps_for_match(sessions, group[0]),
            ))

        return issues

    # ── Rule 4: Rapid Back Navigation ─────────────────────────────────────

    def _rule_rapid_back_nav(self, sessions: list[NormalizedSession]) -> list[DetectedIssue]:
        """
        User enters a page and leaves within <3 seconds. Across ≥3 users.
        """
        matches: list[_Match] = []

        for session in sessions:
            pageviews = [
                (ev, _page_key(ev), _parse_ts(ev.timestamp))
                for ev in session.events
                if ev.event_type == "pageview" and _parse_ts(ev.timestamp)
            ]

            for i in range(len(pageviews) - 1):
                _, page, ts = pageviews[i]
                _, next_page, next_ts = pageviews[i + 1]
                if ts and next_ts:
                    duration = (next_ts - ts).total_seconds()
                    if 0 < duration < 3 and page != next_page:
                        matches.append(_Match(
                            session_id=session.id, user_id=session.distinct_id,
                            page_url=page, selector=page,
                            timestamp=pageviews[i][0].timestamp,
                            extra={"duration": duration, "exited_to": next_page},
                        ))

        clusters = _cluster_by_page(matches, self.min_users, self.min_occurrences)
        issues: list[DetectedIssue] = []

        for page, group in clusters.items():
            users = list({m.user_id for m in group})
            sessions_list = list({m.session_id for m in group})
            avg_duration = sum(m.extra.get("duration", 0) for m in group) / len(group)

            issues.append(DetectedIssue(
                rule_id="rapid_back_nav",
                title=f"Instant Bounce on {page}",
                description=(
                    f"Users land on {page} and leave within {avg_duration:.1f}s on average. "
                    f"{len(users)} users bounced immediately."
                ),
                why_issue="Page is confusing, broken, or not what users expected — they leave instantly.",
                severity="medium",
                category="rapid_back_nav",
                page_url=page,
                selector=page,
                affected_users=users,
                total_occurrences=len(group),
                sample_sessions=sessions_list[:5],
                evidence=[{"session": m.session_id, "timestamp": m.timestamp, "duration_s": m.extra.get("duration")} for m in group[:5]],
                confidence=min(0.9, 0.65 + len(users) * 0.05),
                fingerprint=_fingerprint("rapid_back_nav", page),
                reproduction_steps=_extract_steps_for_match(sessions, group[0]),
            ))

        return issues

    # ── Rule 5: Stuck Interaction ─────────────────────────────────────────

    def _rule_stuck_interaction(self, sessions: list[NormalizedSession]) -> list[DetectedIssue]:
        """
        User clicks but nothing changes on the page within 30 seconds.
        Only triggers on click/tap (not submit — submit has its own feedback).
        "Stuck" means: click → no pageview, no input, no text change, no
        navigation for 30+ seconds.
        """
        STUCK_WINDOW = 30  # seconds

        matches: list[_Match] = []

        for session in sessions:
            for i, ev in enumerate(session.events):
                if ev.event_type not in ("click", "tap"):
                    continue

                ev_ts = _parse_ts(ev.timestamp)
                if not ev_ts:
                    continue

                sel = _selector_key(ev)
                page = _page_key(ev)

                # Look at all events within the 30-second window after click
                has_response = False
                idle_seconds = 0.0
                for j in range(i + 1, min(i + 30, len(session.events))):
                    nev = session.events[j]
                    nev_ts = _parse_ts(nev.timestamp)
                    if not nev_ts:
                        continue

                    gap = (nev_ts - ev_ts).total_seconds()

                    if gap > STUCK_WINDOW:
                        # Past the window — user was idle the whole time
                        idle_seconds = gap
                        break

                    # Any meaningful response within the window = not stuck
                    if nev.event_type in (
                        "pageview", "pageleave", "input", "submit",
                        "focus", "error", "network_error",
                    ):
                        has_response = True
                        break

                    # Another click on a DIFFERENT element = user moved on
                    if nev.event_type in ("click", "tap"):
                        nev_sel = _selector_key(nev)
                        if nev_sel != sel:
                            has_response = True
                            break

                if not has_response and idle_seconds >= STUCK_WINDOW:
                    matches.append(_Match(
                        session_id=session.id, user_id=session.distinct_id,
                        page_url=page, selector=sel,
                        timestamp=ev.timestamp,
                        extra={"idle_seconds": idle_seconds},
                    ))

        clusters = _cluster_matches(matches, self.min_users, self.min_occurrences)
        issues: list[DetectedIssue] = []

        for key, group in clusters.items():
            sel, page = key.split("||", 1)
            users = list({m.user_id for m in group})
            sessions_list = list({m.session_id for m in group})
            avg_idle = sum(m.extra.get("idle_seconds", 0) for m in group) / len(group)

            issues.append(DetectedIssue(
                rule_id="stuck_interaction",
                title=f"Stuck After Clicking {sel[:40]} on {page}",
                description=(
                    f"Users click '{sel}' on {page} but nothing happens — they wait "
                    f"{avg_idle:.0f}s on average with no response. {len(users)} users affected."
                ),
                why_issue="Click produces no visible result, leaving users stuck and confused.",
                severity="high" if avg_idle >= 15 else "medium",
                category="stuck_interaction",
                page_url=page,
                selector=sel,
                affected_users=users,
                total_occurrences=len(group),
                sample_sessions=sessions_list[:5],
                evidence=[{"session": m.session_id, "timestamp": m.timestamp, "idle_s": m.extra.get("idle_seconds")} for m in group[:5]],
                confidence=min(0.9, 0.7 + len(users) * 0.04),
                fingerprint=_fingerprint("stuck_interaction", key),
                reproduction_steps=_extract_steps_for_match(sessions, group[0]),
            ))

        return issues

    # ── Rule 6: Form Abandonment ──────────────────────────────────────────

    def _rule_form_abandonment(self, sessions: list[NormalizedSession]) -> list[DetectedIssue]:
        """
        Users start filling a form (input events) but exit without submitting.
        Occurs in ≥X% of sessions on that page.
        """
        # Track per-page: sessions with input, sessions with submit
        page_input_sessions: dict[str, set[str]] = {}
        page_submit_sessions: dict[str, set[str]] = {}
        page_input_matches: dict[str, list[_Match]] = {}

        for session in sessions:
            pages_with_input: set[str] = set()
            pages_with_submit: set[str] = set()
            input_timestamps: dict[str, str] = {}

            for ev in session.events:
                page = _page_key(ev)
                if not page:
                    continue
                if ev.event_type in ("input", "focus") and ev.element_name:
                    pages_with_input.add(page)
                    if page not in input_timestamps:
                        input_timestamps[page] = ev.timestamp
                if ev.event_type == "submit":
                    pages_with_submit.add(page)

            for page in pages_with_input:
                page_input_sessions.setdefault(page, set()).add(session.id)
                if page not in pages_with_submit:
                    page_input_matches.setdefault(page, []).append(_Match(
                        session_id=session.id, user_id=session.distinct_id,
                        page_url=page, selector=page,
                        timestamp=input_timestamps.get(page, ""),
                    ))
            for page in pages_with_submit:
                page_submit_sessions.setdefault(page, set()).add(session.id)

        issues: list[DetectedIssue] = []

        for page, abandoned_matches in page_input_matches.items():
            users = list({m.user_id for m in abandoned_matches})
            if len(users) < self.min_users:
                continue

            total_with_input = len(page_input_sessions.get(page, set()))
            total_with_submit = len(page_submit_sessions.get(page, set()))
            abandon_rate = len(abandoned_matches) / total_with_input if total_with_input > 0 else 0

            # Only flag if abandonment rate is significant (≥40%)
            if abandon_rate < 0.4:
                continue

            sessions_list = list({m.session_id for m in abandoned_matches})

            issues.append(DetectedIssue(
                rule_id="form_abandonment",
                title=f"Form Abandoned on {page} ({abandon_rate:.0%} drop-off)",
                description=(
                    f"{len(abandoned_matches)} of {total_with_input} sessions started filling the form "
                    f"on {page} but never submitted. {total_with_submit} sessions completed it."
                ),
                why_issue="Users begin filling the form but give up — possible UX friction, confusing fields, or error blocking submission.",
                severity="high" if abandon_rate >= 0.6 else "medium",
                category="form_abandonment",
                page_url=page,
                selector=page,
                affected_users=users,
                total_occurrences=len(abandoned_matches),
                sample_sessions=sessions_list[:5],
                evidence=[{"session": m.session_id, "timestamp": m.timestamp, "abandon_rate": f"{abandon_rate:.0%}"} for m in abandoned_matches[:5]],
                confidence=min(0.95, 0.6 + abandon_rate * 0.3),
                fingerprint=_fingerprint("form_abandonment", page),
                reproduction_steps=_extract_steps_for_match(sessions, abandoned_matches[0]),
            ))

        return issues

    # ── Rule 7: Button Spam ───────────────────────────────────────────────

    def _rule_button_spam(self, sessions: list[NormalizedSession]) -> list[DetectedIssue]:
        """
        ≥4 clicks on same CTA within 5 seconds. Similar to rage click but
        slightly slower — user keeps trying because they expect a response.
        """
        matches: list[_Match] = []

        for session in sessions:
            click_groups: dict[str, list[NormalizedEvent]] = {}
            for ev in session.events:
                if ev.event_type in ("click", "tap"):
                    # Only track interactive elements (buttons, links, CTAs)
                    if ev.tag_name not in ("button", "a", "input", "select", ""):
                        continue
                    sel = _selector_key(ev)
                    page = _page_key(ev)
                    key = f"{sel}||{page}"
                    click_groups.setdefault(key, []).append(ev)

            for key, clicks in click_groups.items():
                if len(clicks) < 4:
                    continue
                for i in range(len(clicks) - 3):
                    window = clicks[i:i + 4]
                    ts_start = _parse_ts(window[0].timestamp)
                    ts_end = _parse_ts(window[-1].timestamp)
                    if ts_start and ts_end:
                        gap = (ts_end - ts_start).total_seconds()
                        # Between 3-5 seconds (rage click catches <3s)
                        if 3 < gap <= 5:
                            sel, page = key.split("||", 1)
                            matches.append(_Match(
                                session_id=session.id, user_id=session.distinct_id,
                                page_url=page, selector=sel,
                                timestamp=window[0].timestamp,
                                count=len(window),
                            ))
                            break

        clusters = _cluster_matches(matches, self.min_users, self.min_occurrences)
        issues: list[DetectedIssue] = []

        for key, group in clusters.items():
            sel, page = key.split("||", 1)
            users = list({m.user_id for m in group})
            sessions_list = list({m.session_id for m in group})

            issues.append(DetectedIssue(
                rule_id="button_spam",
                title=f"Button Spam on {sel[:50]} at {page}",
                description=(
                    f"Users repeatedly press '{sel}' on {page} expecting a response. "
                    f"{len(users)} users hit this."
                ),
                why_issue="CTA doesn't give feedback on click — users keep pressing thinking it didn't register.",
                severity="medium",
                category="button_spam",
                page_url=page,
                selector=sel,
                affected_users=users,
                total_occurrences=sum(m.count for m in group),
                sample_sessions=sessions_list[:5],
                evidence=[{"session": m.session_id, "timestamp": m.timestamp, "clicks": m.count} for m in group[:5]],
                confidence=min(0.85, 0.65 + len(users) * 0.04),
                fingerprint=_fingerprint("button_spam", key),
                reproduction_steps=_extract_steps_for_match(sessions, group[0]),
            ))

        return issues

    # ── Rule 8: Broken Flow ───────────────────────────────────────────────

    def _rule_broken_flow(self, sessions: list[NormalizedSession]) -> list[DetectedIssue]:
        """
        Users start a multi-step flow but fail to reach the next step.
        Detects sudden drop-offs in page sequences seen across sessions.

        Key improvements:
        - Excludes auth/callback/redirect/onboarding pages (transient routes)
        - Only counts drop-offs at MID-FLOW pages (not the last page in a session)
        - Requires the page to NOT be a common final destination
        """
        # Build page transition counts (only count mid-flow pages, not last page)
        transition_counts: dict[str, int] = {}  # "A→B" : count
        page_mid_flow_visits: dict[str, int] = {}  # pages visited NOT as last page
        page_last_visits: dict[str, int] = {}  # pages visited as last page

        for session in sessions:
            pages = []
            seen_pages: set[str] = set()
            for ev in session.events:
                if ev.event_type == "pageview":
                    p = _page_key(ev)
                    if p and p not in seen_pages:
                        pages.append(p)
                        seen_pages.add(p)

            if len(pages) < 2:
                continue

            # Count transitions
            for i in range(len(pages) - 1):
                key = f"{pages[i]}→{pages[i+1]}"
                transition_counts[key] = transition_counts.get(key, 0) + 1
                # This page is mid-flow (has a next page)
                page_mid_flow_visits[pages[i]] = page_mid_flow_visits.get(pages[i], 0) + 1

            # Last page in session — natural endpoint, not a drop-off
            page_last_visits[pages[-1]] = page_last_visits.get(pages[-1], 0) + 1

        # Find mid-flow pages where many users arrive but few continue
        issues: list[DetectedIssue] = []

        for page, mid_flow_count in page_mid_flow_visits.items():
            # Skip transient/auth pages
            if self._skip_page_re.search(page):
                continue

            total_visits = mid_flow_count + page_last_visits.get(page, 0)
            if total_visits < self.min_users * 2:
                continue

            # Find outgoing transitions from this page
            outgoing = {
                k: v for k, v in transition_counts.items()
                if k.startswith(f"{page}→")
            }
            total_continuing = sum(outgoing.values())

            # Drop-offs = users who visited this page but it was their LAST page
            # (they never went to any next page in the same session)
            drop_off_count = page_last_visits.get(page, 0)

            if drop_off_count < self.min_users:
                continue

            drop_off_rate = drop_off_count / total_visits if total_visits > 0 else 0

            # Only flag if >60% of ALL visitors drop off here AND at least some
            # users DO continue (proving it's a mid-flow page, not a natural endpoint)
            if drop_off_rate >= 0.6 and total_continuing >= 1:
                sample_steps: list[str] = []
                sample_session_ids: list[str] = []
                sample_users: list[str] = []
                for s in sessions:
                    for ev in s.events:
                        if ev.event_type == "pageview" and _page_key(ev) == page:
                            if not sample_steps:
                                sample_steps = _extract_steps_before(s, ev.timestamp)
                            sample_session_ids.append(s.id)
                            sample_users.append(s.distinct_id)
                            break
                    if len(sample_session_ids) >= 5:
                        break

                issues.append(DetectedIssue(
                    rule_id="broken_flow",
                    title=f"Drop-off at {page} ({drop_off_rate:.0%} abandon)",
                    description=(
                        f"{total_visits} users reached {page} but {drop_off_count} "
                        f"({drop_off_rate:.0%}) did not continue to any next step. "
                        f"{total_continuing} users did continue, suggesting this is a mid-flow page."
                    ),
                    why_issue="Users get stuck at this step in the flow and abandon — possible blocker, confusing UI, or broken functionality.",
                    severity="critical" if drop_off_rate >= 0.8 else "high",
                    category="broken_flow",
                    page_url=page,
                    selector=page,
                    affected_users=list(set(sample_users)),
                    total_occurrences=drop_off_count,
                    sample_sessions=sample_session_ids[:5],
                    evidence=[
                        {"page": page, "visitors": total_visits, "drop_offs": drop_off_count,
                         "continued": total_continuing, "rate": f"{drop_off_rate:.0%}"},
                        {"outgoing_transitions": {k.split("→")[1]: v for k, v in outgoing.items()}},
                    ],
                    confidence=min(0.9, 0.6 + drop_off_rate * 0.3),
                    fingerprint=_fingerprint("broken_flow", page),
                    reproduction_steps=sample_steps,
                ))

        return issues

    # ── Rule 9: Scroll Frustration ────────────────────────────────────────

    def _rule_scroll_frustration(self, sessions: list[NormalizedSession]) -> list[DetectedIssue]:
        """
        Scroll direction changes ≥6 times in a short window. Across ≥3 users.
        """
        matches: list[_Match] = []

        for session in sessions:
            # Track scroll_y changes on same page
            current_page = ""
            scroll_positions: list[tuple[int, str]] = []

            for ev in session.events:
                if ev.event_type == "pageview":
                    # Reset for new page
                    if scroll_positions:
                        direction_changes = self._count_direction_changes(scroll_positions)
                        if direction_changes >= 6:
                            matches.append(_Match(
                                session_id=session.id, user_id=session.distinct_id,
                                page_url=current_page, selector=current_page,
                                timestamp=scroll_positions[0][1],
                                extra={"direction_changes": direction_changes},
                            ))
                    current_page = _page_key(ev)
                    scroll_positions = []
                elif ev.scroll_y is not None and current_page:
                    scroll_positions.append((ev.scroll_y, ev.timestamp))

            # Check last page
            if scroll_positions:
                direction_changes = self._count_direction_changes(scroll_positions)
                if direction_changes >= 6:
                    matches.append(_Match(
                        session_id=session.id, user_id=session.distinct_id,
                        page_url=current_page, selector=current_page,
                        timestamp=scroll_positions[0][1],
                        extra={"direction_changes": direction_changes},
                    ))

        clusters = _cluster_by_page(matches, self.min_users, self.min_occurrences)
        issues: list[DetectedIssue] = []

        for page, group in clusters.items():
            users = list({m.user_id for m in group})
            sessions_list = list({m.session_id for m in group})

            issues.append(DetectedIssue(
                rule_id="scroll_frustration",
                title=f"Scroll Frustration on {page}",
                description=(
                    f"Users scroll up and down repeatedly on {page} — "
                    f"likely searching for something they can't find. {len(users)} users affected."
                ),
                why_issue="Missing CTA, hidden element, or confusing layout causing users to scroll back and forth.",
                severity="medium",
                category="scroll_frustration",
                page_url=page,
                selector=page,
                affected_users=users,
                total_occurrences=len(group),
                sample_sessions=sessions_list[:5],
                evidence=[{"session": m.session_id, "timestamp": m.timestamp, "changes": m.extra.get("direction_changes")} for m in group[:5]],
                confidence=min(0.85, 0.6 + len(users) * 0.05),
                fingerprint=_fingerprint("scroll_frustration", page),
                reproduction_steps=_extract_steps_for_match(sessions, group[0]),
            ))

        return issues

    @staticmethod
    def _count_direction_changes(positions: list[tuple[int, str]]) -> int:
        if len(positions) < 3:
            return 0
        changes = 0
        prev_direction = None
        for i in range(1, len(positions)):
            diff = positions[i][0] - positions[i - 1][0]
            if diff == 0:
                continue
            direction = "down" if diff > 0 else "up"
            if prev_direction and direction != prev_direction:
                changes += 1
            prev_direction = direction
        return changes

    # ── Rule 10: Rapid Refresh ────────────────────────────────────────────

    def _rule_rapid_refresh(self, sessions: list[NormalizedSession]) -> list[DetectedIssue]:
        """
        Same page loaded ≥3 times within 30 seconds. Across ≥3 users.
        """
        matches: list[_Match] = []

        for session in sessions:
            pageviews: list[tuple[str, datetime]] = []
            for ev in session.events:
                if ev.event_type == "pageview":
                    ts = _parse_ts(ev.timestamp)
                    page = _page_key(ev)
                    if ts and page:
                        pageviews.append((page, ts))

            # Sliding window: same page ≥3 times in 30s
            for i in range(len(pageviews)):
                page_i, ts_i = pageviews[i]
                same_page_count = 1
                for j in range(i + 1, len(pageviews)):
                    page_j, ts_j = pageviews[j]
                    if (ts_j - ts_i).total_seconds() > 30:
                        break
                    if page_j == page_i:
                        same_page_count += 1

                if same_page_count >= 3:
                    matches.append(_Match(
                        session_id=session.id, user_id=session.distinct_id,
                        page_url=page_i, selector=page_i,
                        timestamp=pageviews[i][1].isoformat(),
                        count=same_page_count,
                    ))
                    break  # One match per session

        clusters = _cluster_by_page(matches, self.min_users, self.min_occurrences)
        issues: list[DetectedIssue] = []

        for page, group in clusters.items():
            users = list({m.user_id for m in group})
            sessions_list = list({m.session_id for m in group})

            issues.append(DetectedIssue(
                rule_id="rapid_refresh",
                title=f"Rapid Refresh on {page}",
                description=(
                    f"Users refresh {page} multiple times within 30 seconds. "
                    f"{len(users)} users hit this — likely broken loading state."
                ),
                why_issue="Page fails to load properly, forcing users to refresh repeatedly.",
                severity="high",
                category="rapid_refresh",
                page_url=page,
                selector=page,
                affected_users=users,
                total_occurrences=sum(m.count for m in group),
                sample_sessions=sessions_list[:5],
                evidence=[{"session": m.session_id, "timestamp": m.timestamp, "refreshes": m.count} for m in group[:5]],
                confidence=min(0.95, 0.75 + len(users) * 0.05),
                fingerprint=_fingerprint("rapid_refresh", page),
                reproduction_steps=_extract_steps_for_match(sessions, group[0]),
            ))

        return issues

    # ── Rule 11: Hover Without Action (Phase 2 — skipped) ────────────────

    # def _rule_hover_without_action(self, sessions):
    #     """Hover events without click. Optional for later phase."""
    #     pass

    # ── Rule 12: Unexpected Exit Spike ────────────────────────────────────

    def _rule_unexpected_exit(self, sessions: list[NormalizedSession]) -> list[DetectedIssue]:
        """
        High exit rate after clicking a specific element.
        Click selector → exit within 5 seconds, across high % of users.
        """
        # Track: (selector, page) → sessions where click was followed by exit
        click_exit_matches: list[_Match] = []

        for session in sessions:
            events = session.events
            for i, ev in enumerate(events):
                if ev.event_type not in ("click", "tap"):
                    continue

                sel = _selector_key(ev)
                page = _page_key(ev)
                ev_ts = _parse_ts(ev.timestamp)
                if not ev_ts or not sel:
                    continue

                # Check if user exits within 5 seconds
                exited = True
                for j in range(i + 1, min(i + 10, len(events))):
                    nev = events[j]
                    nev_ts = _parse_ts(nev.timestamp)
                    if nev_ts and (nev_ts - ev_ts).total_seconds() > 5:
                        break
                    if nev.event_type in ("click", "tap", "input", "submit", "pageview"):
                        exited = False
                        break

                # Only count if this was near the end of the session
                remaining_events = len(events) - i - 1
                if exited and remaining_events <= 2:
                    click_exit_matches.append(_Match(
                        session_id=session.id, user_id=session.distinct_id,
                        page_url=page, selector=sel,
                        timestamp=ev.timestamp,
                    ))

        clusters = _cluster_matches(click_exit_matches, self.min_users, self.min_occurrences)
        issues: list[DetectedIssue] = []

        for key, group in clusters.items():
            sel, page = key.split("||", 1)
            users = list({m.user_id for m in group})
            sessions_list = list({m.session_id for m in group})

            issues.append(DetectedIssue(
                rule_id="unexpected_exit",
                title=f"Exit After Clicking {sel[:40]} on {page}",
                description=(
                    f"Users click '{sel}' on {page} and immediately leave the site. "
                    f"{len(users)} users exited after this interaction."
                ),
                why_issue="This interaction triggers users to abandon — likely confusing result, error, or broken CTA.",
                severity="high" if len(users) >= 5 else "medium",
                category="unexpected_exit",
                page_url=page,
                selector=sel,
                affected_users=users,
                total_occurrences=len(group),
                sample_sessions=sessions_list[:5],
                evidence=[{"session": m.session_id, "timestamp": m.timestamp} for m in group[:5]],
                confidence=min(0.9, 0.65 + len(users) * 0.05),
                fingerprint=_fingerprint("unexpected_exit", key),
                reproduction_steps=_extract_steps_for_match(sessions, group[0]),
            ))

        return issues

    # ── Rule 12: Error Text on Page ──────────────────────────────────────

    def _rule_error_text_on_page(self, sessions: list[NormalizedSession]) -> list[DetectedIssue]:
        """
        Detect error messages visible in page content / element text.

        Scans element_text, error_message, and validation_message for known
        error patterns like "Failed to fetch", "something went wrong", etc.
        Also catches JS exceptions ($exception events).

        Groups by (error_text_normalized, page) across sessions.
        """
        matches: list[_Match] = []

        for session in sessions:
            seen_errors: set[str] = set()  # Deduplicate per-session

            for ev in session.events:
                page = _page_key(ev)
                if not page:
                    continue

                # Collect all text to scan from this event
                texts_to_check: list[str] = []
                if ev.element_text:
                    texts_to_check.append(ev.element_text)
                if ev.error_message:
                    texts_to_check.append(ev.error_message)
                if ev.validation_message:
                    texts_to_check.append(ev.validation_message)

                # Also check raw properties for $el_text, exception messages
                raw_props = ev.raw.get("properties", {}) if ev.raw else {}
                el_text_raw = raw_props.get("$el_text", "")
                if el_text_raw and el_text_raw not in texts_to_check:
                    texts_to_check.append(str(el_text_raw)[:200])
                exc_msg = raw_props.get("$exception_message", "")
                if exc_msg and exc_msg not in texts_to_check:
                    texts_to_check.append(str(exc_msg)[:200])

                for text in texts_to_check:
                    if not text or len(text) < 4:
                        continue

                    match = _ERROR_PATTERNS.search(text)
                    if not match:
                        continue

                    # Skip false positives
                    if _ERROR_FALSE_POSITIVES.search(text):
                        continue

                    # Normalize the error text for grouping
                    error_text = match.group(0).strip().lower()
                    # Use first 60 chars of the full text as context
                    full_context = text[:60].strip()

                    dedup_key = f"{error_text}||{page}"
                    if dedup_key in seen_errors:
                        continue
                    seen_errors.add(dedup_key)

                    matches.append(_Match(
                        session_id=session.id,
                        user_id=session.distinct_id,
                        page_url=page,
                        selector=error_text,
                        timestamp=ev.timestamp,
                        extra={"error_text": full_context, "event_type": ev.event_type},
                    ))

        # Group by (error_text_normalized, page)
        clusters = _cluster_matches(matches, self.min_users, self.min_occurrences)
        issues: list[DetectedIssue] = []

        for key, group in clusters.items():
            error_text, page = key.split("||", 1)
            users = list({m.user_id for m in group})
            sessions_list = list({m.session_id for m in group})
            sample_context = group[0].extra.get("error_text", error_text)

            issues.append(DetectedIssue(
                rule_id="error_text",
                title=f'"{sample_context}" error on {page}',
                description=(
                    f'Error message "{sample_context}" appears on {page} — '
                    f"detected across {len(users)} users in {len(group)} sessions."
                ),
                why_issue="Users see an error message on the page, indicating a failed operation or broken feature.",
                severity="critical" if len(users) >= 5 else "high",
                category="error",
                page_url=page,
                selector=error_text,
                affected_users=users,
                total_occurrences=len(group),
                sample_sessions=sessions_list[:5],
                evidence=[
                    {
                        "session": m.session_id,
                        "timestamp": m.timestamp,
                        "error_text": m.extra.get("error_text", ""),
                        "event_type": m.extra.get("event_type", ""),
                    }
                    for m in group[:5]
                ],
                confidence=min(0.95, 0.80 + len(users) * 0.03),
                fingerprint=_fingerprint("error_text", key),
                reproduction_steps=_extract_steps_for_match(sessions, group[0]),
            ))

        return issues
