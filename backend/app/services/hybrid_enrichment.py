"""
Hybrid enrichment: focused micro-AI calls on event clusters.

Instead of one massive AI call per session (which produces vague descriptions),
this module groups related signals into tight time-window clusters and sends
each cluster to AI with a focused ±5 second context. This produces precise,
actionable bug descriptions like:

  "Account deletion fails with HTTP 500 — user sees 'Request failed'"

Instead of generic ones like:

  "Unknown Error on Settings Page"

Usage:
    clusters = build_event_clusters(session, dom_texts, dom_diffs)
    enriched = await analyze_session_clusters(session, clusters, cost_tracker)
    all_issues = enrich_or_replace_algo_issues(algo_issues, enriched, session)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from openai import AsyncOpenAI

from app.config import get_settings
from app.connectors.base import NormalizedEvent, NormalizedSession
from app.services.rule_engine import DetectedIssue, _extract_steps_before
from app.utils.cost_tracker import CostTracker
from app.utils.logger import logger


# ── Constants ────────────────────────────────────────────────────────────────

_CLUSTER_WINDOW_S = 10.0  # ±10 seconds around trigger event
_MERGE_GAP_S = 5.0       # Merge triggers within 5s into one cluster
_MAX_CLUSTERS_PER_SESSION = 8  # Cap to control cost

_AUTH_PAGES = frozenset((
    "/login", "/signin", "/sign-in", "/register", "/signup", "/sign-up",
    "/verify", "/confirm", "/callback", "/auth", "/logout", "/sign_out",
    "/forgot-password", "/reset-password", "/onboarding", "/welcome",
    "/sso", "/oauth",
))

_INTERACTIVE_TYPES = frozenset((
    "click", "tap", "input", "submit", "focus", "dead_click", "rage_click",
))


# ── Helpers ──────────────────────────────────────────────────────────────────

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


def _fingerprint(prefix: str, key: str) -> str:
    return hashlib.sha256(f"{prefix}:{key}".encode()).hexdigest()


def _describe_element(ev: NormalizedEvent) -> str:
    """Build human-readable element description."""
    parts = []
    if ev.tag_name:
        tag_desc = f"<{ev.tag_name}"
        if ev.element_type:
            tag_desc += f" type={ev.element_type}"
        tag_desc += ">"
        parts.append(tag_desc)
    if ev.element_text:
        parts.append(f"'{ev.element_text}'")
    return " ".join(parts) if parts else "<unknown>"


def _event_to_line(ev: NormalizedEvent) -> str:
    """Convert a single event to a readable timeline line."""
    if ev.event_type == "pageview":
        return f"PAGEVIEW: {ev.pathname or ev.url}"
    elif ev.event_type == "pageleave":
        return f"PAGE_LEAVE: {ev.pathname or ev.url}"
    elif ev.event_type == "submit":
        el = _describe_element(ev)
        action = f" → {ev.form_action}" if ev.form_action else ""
        return f"FORM_SUBMIT: {el}{action}"
    elif ev.event_type == "input":
        el = _describe_element(ev)
        val = f" value='{ev.element_value}'" if ev.element_value else ""
        return f"INPUT: {el}{val}"
    elif ev.event_type in ("click", "tap"):
        el = _describe_element(ev)
        return f"CLICK: {el}"
    elif ev.event_type == "dead_click":
        el = _describe_element(ev)
        return f"DEAD_CLICK: {el} — click had no effect"
    elif ev.event_type == "rage_click":
        el = _describe_element(ev)
        return f"RAGE_CLICK: {el} — rapid repeated clicks (user frustrated)"
    elif ev.event_type == "network_error":
        endpoint = (ev.endpoint or "")[:80]
        return f"NETWORK_ERROR: {ev.method} {endpoint} → HTTP {ev.status_code}"
    elif ev.event_type == "error":
        err_type = ev.error_type or "Error"
        msg = (ev.error_message or "")[:120]
        return f"JS_ERROR: {err_type}: {msg}"
    elif ev.event_type == "scroll":
        return f"SCROLL: scrollY={ev.scroll_y}"
    elif ev.event_type == "focus":
        el = _describe_element(ev)
        return f"FOCUS: {el}"
    elif ev.event_type == "blur":
        el = _describe_element(ev)
        val_msg = f" ⚠ {ev.validation_message}" if ev.validation_message else ""
        return f"BLUR: {el}{val_msg}"
    else:
        return f"{ev.event_type.upper()}: {ev.url or ev.pathname or ''}"


# ── EventCluster ─────────────────────────────────────────────────────────────

@dataclass
class EventCluster:
    """A group of related signals within a tight time window."""
    cluster_id: str
    center_ts: str                          # ISO timestamp of primary trigger
    center_epoch: float                     # Epoch for sorting/math
    page_url: str
    cluster_type: str                       # network_error | console_error | form_fail | mixed
    trigger_events: list[NormalizedEvent]   # The core problematic events
    events: list[NormalizedEvent]           # ALL events in window
    dom_snapshots: list[dict]               # Raw DOM markdown in window
    dom_diffs: list[dict]                   # DOM diffs in window


# ── Cluster Building ─────────────────────────────────────────────────────────

def build_event_clusters(
    session: NormalizedSession,
    dom_texts: list[dict] | None = None,
    dom_diffs: list[dict] | None = None,
    skip_page_patterns: list[str] | None = None,
) -> list[EventCluster]:
    """
    Scan a session for incident triggers and group related signals into clusters.

    Triggers:
      - network_error with status >= 400
      - error (JS exception)
      - submit with no response within 3s

    Returns list of EventCluster objects, merged if overlapping.
    """
    skip_patterns = skip_page_patterns or []

    # ── Step 1: Find trigger events ──────────────────────────────────
    triggers: list[tuple[float, NormalizedEvent, str]] = []  # (epoch, event, type)

    for i, ev in enumerate(session.events):
        page = ev.url or ev.pathname or ""

        # Skip auth and filtered pages
        if _is_auth_page(page):
            continue
        if any(p.lower() in page.lower() for p in skip_patterns):
            continue

        if ev.event_type == "network_error" and ev.status_code and ev.status_code >= 400:
            epoch = _parse_ts(ev.timestamp)
            if epoch:
                triggers.append((epoch, ev, "network_error"))

        elif ev.event_type == "error" and ev.error_message:
            epoch = _parse_ts(ev.timestamp)
            if epoch:
                triggers.append((epoch, ev, "console_error"))

        elif ev.event_type == "submit":
            # Check if form had no response within 10s (aligned with algo detector)
            epoch = _parse_ts(ev.timestamp)
            if epoch is None:
                continue
            following = session.events[i + 1: i + 13]
            had_response = False
            for f_ev in following:
                f_epoch = _parse_ts(f_ev.timestamp)
                if f_epoch and (f_epoch - epoch) <= 10.0:
                    if f_ev.event_type in ("pageview", "network_error", "error"):
                        had_response = True
                        break
                elif f_epoch and (f_epoch - epoch) > 10.0:
                    break
            if not had_response:
                triggers.append((epoch, ev, "form_fail"))

    if not triggers:
        return []

    # Sort by time
    triggers.sort(key=lambda x: x[0])

    # ── Step 2: Merge overlapping triggers into clusters ─────────────
    merged_groups: list[list[tuple[float, NormalizedEvent, str]]] = []
    current_group: list[tuple[float, NormalizedEvent, str]] = [triggers[0]]

    for i in range(1, len(triggers)):
        prev_epoch = current_group[-1][0]
        curr_epoch = triggers[i][0]
        if (curr_epoch - prev_epoch) <= _MERGE_GAP_S:
            current_group.append(triggers[i])
        else:
            merged_groups.append(current_group)
            current_group = [triggers[i]]
    merged_groups.append(current_group)

    # Cap clusters
    if len(merged_groups) > _MAX_CLUSTERS_PER_SESSION:
        merged_groups = merged_groups[:_MAX_CLUSTERS_PER_SESSION]

    # ── Step 3: Build EventCluster objects ───────────────────────────
    clusters: list[EventCluster] = []

    for group_idx, group in enumerate(merged_groups):
        # Center = earliest trigger in group
        center_epoch = group[0][0]
        center_ts = group[0][1].timestamp
        trigger_events = [t[1] for t in group]
        trigger_types = set(t[2] for t in group)

        # Determine cluster type
        if len(trigger_types) > 1:
            cluster_type = "mixed"
        else:
            cluster_type = trigger_types.pop()

        # Page URL from primary trigger
        primary = group[0][1]
        page_url = primary.url or primary.pathname or ""

        # Window bounds
        window_start = center_epoch - _CLUSTER_WINDOW_S
        window_end = group[-1][0] + _CLUSTER_WINDOW_S

        # Collect ALL events in window
        window_events: list[NormalizedEvent] = []
        for ev in session.events:
            ev_epoch = _parse_ts(ev.timestamp)
            if ev_epoch is not None and window_start <= ev_epoch <= window_end:
                window_events.append(ev)

        # Collect DOM snapshots in window
        window_dom: list[dict] = []
        if dom_texts:
            for dt in dom_texts:
                dt_epoch = _parse_ts(dt.get("timestamp", ""))
                if dt_epoch is not None and window_start - 2 <= dt_epoch <= window_end + 2:
                    window_dom.append(dt)

            # Fallback: if no DOM snapshots in window, find the NEAREST one
            # on the same page (within 120s max) so the AI still gets DOM context.
            if not window_dom:
                _MAX_DOM_FALLBACK_S = 120.0  # max distance to look
                best_snap = None
                best_dist = float("inf")
                for dt in dom_texts:
                    dt_page = dt.get("page", "")
                    # Match same page (or accept any if page_url is empty)
                    if page_url and dt_page and not dt_page.endswith(page_url.rstrip("/")):
                        # Try a looser match: path portion
                        dt_path = urlparse(dt_page).path.rstrip("/")
                        cluster_path = urlparse(page_url).path.rstrip("/")
                        if dt_path != cluster_path:
                            continue
                    dt_epoch = _parse_ts(dt.get("timestamp", ""))
                    if dt_epoch is not None:
                        dist = abs(dt_epoch - center_epoch)
                        if dist < best_dist and dist <= _MAX_DOM_FALLBACK_S:
                            best_dist = dist
                            best_snap = dt
                if best_snap:
                    # Mark it so context builder knows it's approximate
                    snap_copy = dict(best_snap)
                    snap_copy["_approx_distance_s"] = round(best_dist, 1)
                    window_dom.append(snap_copy)

        # Collect DOM diffs in window
        window_diffs: list[dict] = []
        if dom_diffs:
            for dd in dom_diffs:
                dd_epoch = _parse_ts(dd.get("timestamp", ""))
                if dd_epoch is not None and window_start - 2 <= dd_epoch <= window_end + 2:
                    window_diffs.append(dd)

        cluster = EventCluster(
            cluster_id=f"cluster_{session.id[:8]}_{group_idx}",
            center_ts=center_ts,
            center_epoch=center_epoch,
            page_url=page_url,
            cluster_type=cluster_type,
            trigger_events=trigger_events,
            events=window_events,
            dom_snapshots=window_dom,
            dom_diffs=window_diffs,
        )
        clusters.append(cluster)

    return clusters


# ── Context Building ─────────────────────────────────────────────────────────

def build_cluster_context(
    cluster: EventCluster,
    session: NormalizedSession,
) -> str:
    """
    Build a focused micro-context for AI analysis of a single cluster.

    Includes:
    1. ALL user steps from session start to trigger (full reproduction path)
    2. Timeline of events in the ±5s window
    3. DOM markdown at the time of the incident
    """
    parts: list[str] = []

    # ── 1. Full reproduction steps (from session start to trigger) ───
    steps = _extract_steps_before(session, cluster.center_ts, max_steps=15)
    if steps:
        parts.append("USER STEPS BEFORE INCIDENT:")
        for i, step in enumerate(steps, 1):
            parts.append(f"  {i}. {step}")
        parts.append("")

    # ── 2. Event timeline in the window ──────────────────────────────
    parts.append(f"INCIDENT WINDOW on {cluster.page_url}:")
    parts.append("")

    # Sort events by timestamp
    sorted_events = sorted(
        cluster.events,
        key=lambda e: _parse_ts(e.timestamp) or 0.0,
    )

    for ev in sorted_events:
        ev_epoch = _parse_ts(ev.timestamp) or 0.0
        offset = ev_epoch - cluster.center_epoch
        sign = "+" if offset >= 0 else ""
        line = _event_to_line(ev)
        parts.append(f"  [{sign}{offset:.1f}s] {line}")

    parts.append("")

    # ── 3. DOM state at time of incident ─────────────────────────────
    if cluster.dom_snapshots:
        # Find the snapshot closest to the trigger
        best_snap = None
        best_dist = float("inf")
        for snap in cluster.dom_snapshots:
            snap_epoch = _parse_ts(snap.get("timestamp", ""))
            if snap_epoch is not None:
                dist = abs(snap_epoch - cluster.center_epoch)
                if dist < best_dist:
                    best_dist = dist
                    best_snap = snap

        if best_snap:
            dom_text = best_snap.get("text", "")
            # Send the full rendered DOM so AI sees what the user saw.
            # Cap at 6000 chars — enough for full page content while
            # keeping total prompt under model limits.
            if len(dom_text) > 6000:
                dom_text = dom_text[:6000] + "\n... (truncated)"
            approx = best_snap.get("_approx_distance_s")
            if approx is not None:
                parts.append(f"DOM STATE (nearest snapshot, ~{approx}s from incident):")
            else:
                parts.append("DOM STATE AT INCIDENT:")
            parts.append(dom_text)
            parts.append("")

    # ── 4. DOM changes (diffs) in window ─────────────────────────────
    if cluster.dom_diffs:
        diff_lines: list[str] = []
        for dd in cluster.dom_diffs:
            if dd.get("is_diff"):
                diff_lines.append(dd.get("text", ""))
        if diff_lines:
            combined = "\n".join(diff_lines)
            if len(combined) > 2000:
                combined = combined[:2000] + "\n... (truncated)"
            parts.append("DOM CHANGES IN WINDOW:")
            parts.append(combined)
            parts.append("")

    return "\n".join(parts)


# ── Micro-AI Prompt ──────────────────────────────────────────────────────────

_CLUSTER_ANALYSIS_PROMPT = """You analyze a specific incident from a user session recording.
Given: the user's steps before the incident, events in a 5-second window, and what the DOM showed.

Your job: explain EXACTLY what went wrong. Be specific — use error codes, API endpoints, button names, and what the user saw.

Rules:
- Title must describe the SPECIFIC action that failed, not a generic label
  GOOD: "Account deletion fails with HTTP 500 — 'Request failed' shown"
  BAD: "Unknown Error on Settings Page"
  BAD: "Network Error"
- Description must include: what user did → what broke → what feedback they got
- reproduction_steps: use the EXACT steps from USER STEPS BEFORE INCIDENT (copy them), plus what happened after
- If this is normal app behavior (auth redirect, expected validation, etc.), return {"issues": []}

Return JSON:
{
  "issues": [{
    "title": "precise bug title (max 80 chars)",
    "description": "2-3 sentences with specifics",
    "why_issue": "1 sentence real-world impact on the user",
    "severity": "critical|high|medium|low",
    "category": "error|form_validation|broken_ui|silent_failure|ux_friction",
    "confidence": 0.7-1.0
  }]
}"""


# ── Async AI Analysis ────────────────────────────────────────────────────────

async def _analyze_single_cluster(
    cluster: EventCluster,
    session: NormalizedSession,
    cost_tracker: CostTracker | None = None,
) -> dict | None:
    """
    Analyze a single cluster with a focused micro-AI call.
    Returns enriched issue dict or None.
    """
    settings = get_settings()
    client = AsyncOpenAI(api_key=settings.openai_api_key)

    context = build_cluster_context(cluster, session)

    # Sanity check: skip if context is too tiny (nothing to analyze)
    if len(context.strip()) < 50:
        return None

    start_ms = time.time() * 1000

    try:
        from app.utils.retry import with_retries

        response = await with_retries(
            lambda: client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": _CLUSTER_ANALYSIS_PROMPT},
                    {"role": "user", "content": context},
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
                max_tokens=600,
            ),
            max_retries=2,
            base_delay=2.0,
            retryable_exceptions=(ConnectionError, TimeoutError, Exception),
            operation="OpenAI cluster analysis",
        )

        duration_ms = (time.time() * 1000) - start_ms

        if cost_tracker:
            cost_tracker.record(
                function="hybrid_cluster",
                model="gpt-4o-mini",
                response=response,
                session_id=session.id,
                duration_ms=duration_ms,
            )

        content = response.choices[0].message.content
        if not content:
            return None

        parsed = json.loads(content)
        issues = parsed.get("issues", [])
        if not issues:
            return None

        # Take the first (primary) issue
        issue = issues[0]
        issue["_source"] = "hybrid_cluster"
        issue["_cluster_id"] = cluster.cluster_id
        issue["_cluster_type"] = cluster.cluster_type
        issue["_cluster_center_ts"] = cluster.center_ts
        issue["page_url"] = cluster.page_url

        return issue

    except Exception as e:
        logger.error(f"Cluster analysis failed for {cluster.cluster_id}: {e}")
        return None


async def analyze_session_clusters(
    session: NormalizedSession,
    clusters: list[EventCluster],
    cost_tracker: CostTracker | None = None,
) -> list[dict]:
    """
    Analyze all clusters for a session in parallel.
    Returns list of enriched issue dicts.
    """
    if not clusters:
        return []

    tasks = [
        _analyze_single_cluster(cluster, session, cost_tracker)
        for cluster in clusters
    ]
    results = await asyncio.gather(*tasks)
    return [r for r in results if r is not None]


# ── Merge Logic ──────────────────────────────────────────────────────────────

def _title_similarity(a: str, b: str) -> float:
    """Quick token-overlap similarity between two titles (0..1)."""
    if not a or not b:
        return 0.0
    a_tokens = set(a.lower().split())
    b_tokens = set(b.lower().split())
    if not a_tokens or not b_tokens:
        return 0.0
    overlap = len(a_tokens & b_tokens)
    return overlap / max(len(a_tokens), len(b_tokens))


def _dedup_hybrid_issues(hybrid_issues: list[dict]) -> list[dict]:
    """
    Deduplicate hybrid issues that have the same (or very similar) title + page.

    When multiple clusters produce essentially the same issue (e.g. 4 clusters
    all saying "Account deletion fails with HTTP 500"), keep only the one with
    the highest confidence. Merge evidence from the duplicates into the winner.
    """
    if len(hybrid_issues) <= 1:
        return hybrid_issues

    # Group by normalized page + similar title
    groups: list[list[int]] = []
    assigned: set[int] = set()

    for i in range(len(hybrid_issues)):
        if i in assigned:
            continue
        group = [i]
        assigned.add(i)
        i_page = _normalize_url(hybrid_issues[i].get("page_url", ""))
        i_title = hybrid_issues[i].get("title", "")

        for j in range(i + 1, len(hybrid_issues)):
            if j in assigned:
                continue
            j_page = _normalize_url(hybrid_issues[j].get("page_url", ""))
            j_title = hybrid_issues[j].get("title", "")

            # Same page (or both empty) AND similar title → duplicate
            same_page = (i_page == j_page) or (not i_page and not j_page)
            similar_title = _title_similarity(i_title, j_title) >= 0.70
            # Also catch exact title match regardless of page
            exact_title = i_title.lower().strip() == j_title.lower().strip()

            if (same_page and similar_title) or exact_title:
                group.append(j)
                assigned.add(j)

        groups.append(group)

    # For each group, pick the highest-confidence one and merge evidence
    deduped: list[dict] = []
    for group in groups:
        if len(group) == 1:
            deduped.append(hybrid_issues[group[0]])
            continue

        # Sort by confidence descending, pick winner
        sorted_indices = sorted(
            group,
            key=lambda idx: hybrid_issues[idx].get("confidence", 0.0),
            reverse=True,
        )
        winner = hybrid_issues[sorted_indices[0]].copy()

        # Merge evidence from duplicates: collect all unique cluster IDs
        seen_cluster_ids = {winner.get("_cluster_id", "")}
        extra_evidence = []
        for idx in sorted_indices[1:]:
            dup = hybrid_issues[idx]
            cid = dup.get("_cluster_id", "")
            if cid and cid not in seen_cluster_ids:
                seen_cluster_ids.add(cid)
                extra_evidence.append({
                    "cluster_id": cid,
                    "timestamp": dup.get("_cluster_center_ts", ""),
                    "page": dup.get("page_url", ""),
                    "cluster_type": dup.get("_cluster_type", ""),
                    "source": "hybrid_enrichment",
                })

        if extra_evidence:
            winner["_merged_cluster_count"] = len(group)

        deduped.append(winner)

    logger.info(
        f"Hybrid dedup: {len(hybrid_issues)} → {len(deduped)} issues "
        f"({len(hybrid_issues) - len(deduped)} duplicates removed)"
    )
    return deduped


def enrich_or_replace_algo_issues(
    algo_issues: list[dict],
    hybrid_issues: list[dict],
    session: NormalizedSession,
    seen_fingerprints: set[str],
) -> list[dict]:
    """
    Merge hybrid-enriched issues with algo issues.

    For each hybrid issue:
    1. Find matching algo issue (same page + overlapping category/type)
    2. If found: REPLACE algo issue's title/description/why_issue with enriched version
    3. If not found: add as a new issue

    Deduplicates hybrid issues against each other first (e.g. 4 clusters all
    producing "Account deletion fails with HTTP 500" → keep only 1).

    Returns the updated list of all issues.
    """
    if not hybrid_issues:
        return algo_issues

    # ── Step 0: Deduplicate hybrid issues against each other ─────────
    hybrid_issues = _dedup_hybrid_issues(hybrid_issues)

    # Index algo issues by page URL for fast lookup
    algo_by_page: dict[str, list[int]] = {}
    # Also index by session ID for cross-page matching
    algo_by_session: dict[str, list[int]] = {}
    for i, issue in enumerate(algo_issues):
        page = _normalize_url(issue.get("page_url", ""))
        if page:
            algo_by_page.setdefault(page, []).append(i)
        sid = issue.get("session_id", "")
        if sid:
            algo_by_session.setdefault(sid, []).append(i)

    replaced_indices: set[int] = set()
    new_issues: list[dict] = []

    def _categories_match(algo_cat: str, hybrid_cat: str) -> bool:
        return algo_cat == hybrid_cat or (
            algo_cat == "error" and hybrid_cat in ("error", "form_validation", "silent_failure")
        )

    for h_issue in hybrid_issues:
        h_page = _normalize_url(h_issue.get("page_url", ""))
        h_category = h_issue.get("category", "")
        h_title = h_issue.get("title", "")
        h_confidence = h_issue.get("confidence", 0.0)

        # Skip low-confidence hybrid results
        if h_confidence < 0.70:
            continue

        # Try to find a matching algo issue to enrich
        # Strategy 1: Match by same page + same/related category
        matched_idx = None
        if h_page in algo_by_page:
            for idx in algo_by_page[h_page]:
                if idx in replaced_indices:
                    continue
                algo_issue = algo_issues[idx]
                algo_cat = algo_issue.get("category", "")
                if _categories_match(algo_cat, h_category):
                    matched_idx = idx
                    break

        # Strategy 2: If no page match, try same session + same category
        if matched_idx is None and session.id in algo_by_session:
            for idx in algo_by_session[session.id]:
                if idx in replaced_indices:
                    continue
                algo_issue = algo_issues[idx]
                algo_cat = algo_issue.get("category", "")
                if _categories_match(algo_cat, h_category):
                    matched_idx = idx
                    break

        if matched_idx is not None:
            # Enrich existing algo issue — replace vague parts, keep structure
            replaced_indices.add(matched_idx)
            enriched = algo_issues[matched_idx].copy()
            enriched["title"] = h_issue.get("title", enriched["title"])
            enriched["description"] = h_issue.get("description", enriched["description"])
            enriched["why_issue"] = h_issue.get("why_issue", enriched["why_issue"])
            enriched["severity"] = h_issue.get("severity", enriched["severity"])
            enriched["confidence"] = max(
                enriched.get("confidence", 0.0),
                h_confidence,
            )
            enriched["_enriched_by"] = "hybrid"

            # Add reproduction steps from full session journey
            cluster_ts = h_issue.get("_cluster_center_ts", "")
            if cluster_ts:
                enriched["reproduction_steps"] = _extract_steps_before(
                    session, cluster_ts, max_steps=15
                )

            algo_issues[matched_idx] = enriched
        else:
            # New issue not caught by algo — add it
            fp = _fingerprint(
                "hybrid",
                f"{h_issue.get('_cluster_type', '')}:{h_page}:{h_title[:60]}",
            )
            if fp in seen_fingerprints:
                continue

            new_issue = {
                "rule_id": f"hybrid_{h_issue.get('_cluster_type', 'unknown')}",
                "title": h_title,
                "description": h_issue.get("description", ""),
                "why_issue": h_issue.get("why_issue", ""),
                "severity": h_issue.get("severity", "medium"),
                "category": h_category or "error",
                "page_url": h_issue.get("page_url", ""),
                "selector": "",
                "affected_users": 1,
                "affected_user_ids": [session.distinct_id],
                "total_occurrences": 1,
                "sample_sessions": [session.id],
                "evidence": [{
                    "cluster_id": h_issue.get("_cluster_id", ""),
                    "timestamp": h_issue.get("_cluster_center_ts", ""),
                    "page": h_issue.get("page_url", ""),
                    "cluster_type": h_issue.get("_cluster_type", ""),
                    "source": "hybrid_enrichment",
                }],
                "confidence": h_confidence,
                "fingerprint": fp,
                "session_id": session.id,
                "distinct_id": session.distinct_id or "",
                "_enriched_by": "hybrid",
            }

            # Add reproduction steps
            cluster_ts = h_issue.get("_cluster_center_ts", "")
            if cluster_ts:
                new_issue["reproduction_steps"] = _extract_steps_before(
                    session, cluster_ts, max_steps=15
                )

            new_issues.append(new_issue)
            seen_fingerprints.add(fp)

    return algo_issues + new_issues


def count_session_triggers(session: NormalizedSession) -> int:
    """
    Count how many incident triggers a session has.
    Used to determine if Phase 3 can be skipped (all triggers covered by clusters).
    """
    count = 0
    for i, ev in enumerate(session.events):
        if ev.event_type == "network_error" and ev.status_code and ev.status_code >= 400:
            count += 1
        elif ev.event_type == "error" and ev.error_message:
            count += 1
        elif ev.event_type == "submit":
            count += 1
    return count


# ── AI Issue Merge ──────────────────────────────────────────────────────────
#
# After all phases (rule engine → algo → hybrid → AI) produce issues,
# many are symptoms of the same root cause. E.g. for one failed DELETE:
#   - "HTTP 500 on POST /delete-account" (network_error)
#   - "Error deleting account: Request failed" (console_error)
#   - "Delete account failed: Request failed" (console_error)
#   - "Silent failure: POST /delete-account → 500" (silent_failure)
#   - "Unknown error" (console_error)
#
# This step uses AI to group related issues by root cause and merge them
# into a single, well-described issue per root cause.


_MERGE_PROMPT = """You are a QA engineer merging duplicate bug reports from automated detection.

You will receive a list of issues detected in a user session. Many of these are SYMPTOMS of the same underlying bug. Your job is to GROUP them by root cause and produce ONE merged issue per distinct bug.

RULES:
1. Group issues that are symptoms of the same root cause (e.g. a failed API call AND the console errors it produces AND the "silent failure" for the same endpoint).
2. Console errors like "[UsageStore] Database error", "[ProjectsStore] Error fetching projects", "Uncaught (in promise)" with the same error code (e.g. PGRST116) on the same page are ONE issue, not separate bugs.
3. "Unknown error" on the same page as specific errors is always a duplicate — merge it into the specific error.
4. Network errors (HTTP 500) AND console errors mentioning the same endpoint/action are the same bug.
5. "Silent failure" for the same endpoint as a network_error is the same bug — merge into the network error issue.
6. Keep truly different bugs separate (e.g. accessibility warnings vs API failures).
7. For each merged group, use the BEST title from the group (most specific and descriptive).
8. Merge evidence from all issues in the group.
9. Use the highest severity and confidence from the group.
10. Write a description that covers the full picture (the API failure + all the cascade errors).
11. reproduction_steps should only contain USER ACTIONS (navigate, click, type, scroll) — never include error events or network calls in repro steps.
12. CRITICAL: If any issue in a group has "UI_VISIBLE" data showing what the user saw on screen (error text, DOM state), you MUST include that in the merged description. Never drop UI-visible error information. Mention what the user actually saw (e.g. "the user saw 'Request failed' in the UI").

Return JSON:
{
  "merged_issues": [
    {
      "group_indices": [0, 2, 5],  // indices of issues from the input that belong to this group
      "title": "precise merged bug title (max 80 chars)",
      "description": "2-4 sentences covering the full failure: what the user did, what broke, what they saw ON SCREEN",
      "severity": "critical|high|medium|low",
      "category": "error|form_validation|broken_ui|silent_failure|ux_friction",
      "confidence": 0.7-1.0,
      "page_url": "primary page where this bug manifests"
    }
  ]
}"""


def _summarize_issue_for_merge(idx: int, issue: dict) -> str:
    """Build a concise summary of an issue for the AI merge prompt."""
    parts = [f"Issue #{idx}:"]
    parts.append(f"  Title: {issue.get('title', 'N/A')}")
    parts.append(f"  Page: {issue.get('page_url', 'N/A')}")
    parts.append(f"  Severity: {issue.get('severity', 'N/A')}")
    parts.append(f"  Category: {issue.get('category', 'N/A')}")
    parts.append(f"  Source: {issue.get('_enriched_by') or issue.get('rule_id', 'N/A')}")
    parts.append(f"  Confidence: {issue.get('confidence', 'N/A')}")

    desc = issue.get("description", "")
    if len(desc) > 200:
        desc = desc[:200] + "..."
    parts.append(f"  Description: {desc}")

    # Include evidence summary — with UI visibility info
    evidence = issue.get("evidence", [])
    if evidence:
        ev_lines = []
        ui_visible_texts: list[str] = []
        for ev in evidence[:5]:
            if isinstance(ev, dict):
                ev_summary = []
                for k in ("error_type", "error_message", "method", "endpoint",
                           "status_code", "timestamp", "page"):
                    v = ev.get(k)
                    if v:
                        ev_summary.append(f"{k}={str(v)[:80]}")
                if ev_summary:
                    ev_lines.append("    - " + ", ".join(ev_summary))

                # Collect UI-visible error info — this is critical context
                ui_impact = ev.get("ui_impact", "")
                if ui_impact:
                    ui_visible_texts.append(ui_impact)
                # Also check error_shown_in_dom for silent failure detector
                if ev.get("error_shown_in_dom") is False:
                    ui_visible_texts.append("No error shown in UI (silent failure)")

        if ev_lines:
            parts.append("  Evidence:")
            parts.extend(ev_lines)

        # Add UI visibility section — the AI must not drop this
        if ui_visible_texts:
            parts.append("  UI_VISIBLE (what user saw on screen):")
            for txt in ui_visible_texts[:3]:
                parts.append(f"    - {txt[:200]}")

    return "\n".join(parts)


async def merge_related_issues_with_ai(
    issues: list[dict],
    session: NormalizedSession | None = None,
    cost_tracker: CostTracker | None = None,
) -> list[dict]:
    """
    Use AI to merge related issues into distinct root-cause bugs.

    Takes the full list of issues from all detection phases and groups
    issues that are symptoms of the same underlying problem.

    Returns a new list with merged issues (fewer, better-described).
    Preserves evidence and repro steps from the original issues.
    """
    if len(issues) <= 2:
        return issues  # Nothing to merge

    settings = get_settings()
    client = AsyncOpenAI(api_key=settings.openai_api_key)

    # Build the issue summaries for AI
    issue_summaries = []
    for i, issue in enumerate(issues):
        issue_summaries.append(_summarize_issue_for_merge(i, issue))

    context = "ISSUES TO MERGE:\n\n" + "\n\n".join(issue_summaries)

    start_ms = time.time() * 1000

    try:
        from app.utils.retry import with_retries

        response = await with_retries(
            lambda: client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": _MERGE_PROMPT},
                    {"role": "user", "content": context},
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
                max_tokens=1200,
            ),
            max_retries=2,
            base_delay=2.0,
            retryable_exceptions=(ConnectionError, TimeoutError, Exception),
            operation="OpenAI issue merge",
        )

        duration_ms = (time.time() * 1000) - start_ms

        if cost_tracker:
            cost_tracker.record(
                function="issue_merge",
                model="gpt-4o-mini",
                response=response,
                session_id=session.id if session else "",
                duration_ms=duration_ms,
            )

        content = response.choices[0].message.content
        if not content:
            logger.warning("AI merge returned empty content — keeping original issues")
            return issues

        parsed = json.loads(content)
        merged_groups = parsed.get("merged_issues", [])

        if not merged_groups:
            logger.warning("AI merge returned no groups — keeping original issues")
            return issues

        # Build merged issue list
        merged_issues: list[dict] = []
        used_indices: set[int] = set()

        for group in merged_groups:
            indices = group.get("group_indices", [])
            if not indices:
                continue

            # Validate indices
            valid_indices = [i for i in indices if 0 <= i < len(issues)]
            if not valid_indices:
                continue

            used_indices.update(valid_indices)

            # Pick the best original issue as the base (highest confidence)
            base_idx = max(valid_indices, key=lambda i: issues[i].get("confidence", 0))
            merged = issues[base_idx].copy()

            # Override with AI's merged title/description/severity
            merged["title"] = group.get("title", merged["title"])
            merged["description"] = group.get("description", merged["description"])
            merged["severity"] = group.get("severity", merged["severity"])
            merged["category"] = group.get("category", merged["category"])
            merged["page_url"] = group.get("page_url", merged["page_url"])
            merged["confidence"] = max(
                merged.get("confidence", 0),
                group.get("confidence", 0),
            )
            merged["_enriched_by"] = "ai_merge"
            merged["_merged_from_count"] = len(valid_indices)

            # Merge evidence from all issues in the group
            all_evidence = []
            seen_evidence_keys: set[str] = set()
            all_ui_impacts: list[str] = []
            for idx in valid_indices:
                for ev in issues[idx].get("evidence", []):
                    if isinstance(ev, dict):
                        # Dedup by a key of important fields
                        ev_key = f"{ev.get('timestamp', '')}:{ev.get('error_message', '')}:{ev.get('endpoint', '')}"
                        if ev_key not in seen_evidence_keys:
                            seen_evidence_keys.add(ev_key)
                            all_evidence.append(ev)
                        # Collect UI visibility info (even from deduped evidence)
                        ui_imp = ev.get("ui_impact", "")
                        if ui_imp and ui_imp not in all_ui_impacts:
                            all_ui_impacts.append(ui_imp)
            merged["evidence"] = all_evidence

            # Preserve UI-visible error info on the merged issue
            if all_ui_impacts:
                merged["ui_impacts"] = all_ui_impacts
                # Ensure the AI-written description didn't lose UI context:
                # append UI info if the description doesn't mention what the user saw
                desc = merged.get("description", "")
                desc_lower = desc.lower()
                has_ui_ref = any(
                    kw in desc_lower
                    for kw in ("user saw", "visible", "displayed", "shown", "ui", "screen")
                )
                if not has_ui_ref and all_ui_impacts:
                    # Append what the user actually saw
                    ui_text = all_ui_impacts[0][:150]
                    merged["description"] = desc.rstrip(".") + f". {ui_text}."

            # Use the best reproduction steps (from the issue with highest confidence,
            # or pick the first non-empty one)
            best_steps = merged.get("reproduction_steps", [])
            if not best_steps:
                for idx in valid_indices:
                    steps = issues[idx].get("reproduction_steps", [])
                    if steps:
                        best_steps = steps
                        break
            merged["reproduction_steps"] = best_steps

            # Merge affected users / occurrences
            all_user_ids: set[str] = set()
            total_occ = 0
            for idx in valid_indices:
                for uid in issues[idx].get("affected_user_ids", []):
                    all_user_ids.add(uid)
                total_occ += issues[idx].get("total_occurrences", 1)
            if all_user_ids:
                merged["affected_user_ids"] = list(all_user_ids)
                merged["affected_users"] = len(all_user_ids)
            merged["total_occurrences"] = total_occ

            merged_issues.append(merged)

        # Add any issues that weren't included in any group (safety net)
        for i, issue in enumerate(issues):
            if i not in used_indices:
                logger.debug(f"AI merge missed issue #{i} '{issue.get('title', '')[:50]}' — keeping it")
                merged_issues.append(issue)

        logger.info(
            f"AI merge: {len(issues)} issues → {len(merged_issues)} "
            f"({len(issues) - len(merged_issues)} merged away)"
        )

        return merged_issues

    except Exception as e:
        logger.error(f"AI issue merge failed: {e} — keeping original issues")
        return issues


# ── Phase 5: AI False Positive Validation ─────────────────────────────────

_VALIDATION_PROMPT = """You are a QA expert reviewing potential bugs detected from user session recordings.

For each candidate issue below, determine whether it's a REAL BUG or a FALSE POSITIVE (normal behavior misidentified as a bug).

Think step by step:
1. What was the user trying to do?
2. What happened (according to the evidence)?
3. Is this expected application behavior or a genuine bug?

Common FALSE POSITIVES (NOT bugs):
- User reading a page briefly then navigating away (instant bounce)
- Auth redirects (login → callback → dashboard)
- Loading states before content appears (spinners, skeleton screens)
- User navigating back to a previous page
- Legitimate double-clicks or repeated clicks
- Form submission followed by redirect (normal flow)
- Console warnings that don't affect the user
- Rate limiting or "try again" errors (recoverable)

Return a JSON array with one entry per issue:
[
  {
    "issue_index": 0,
    "is_real_bug": true,
    "reasoning": "The API returned 500 and the user saw 'Request failed' — this is a server error",
    "adjusted_confidence": 0.9
  },
  ...
]

Only mark is_real_bug=true if the issue clearly describes a broken user experience."""


async def validate_issues_with_ai(
    issues: list[dict],
    cost_tracker: CostTracker | None = None,
) -> list[dict]:
    """
    Phase 5: AI False Positive Filter.

    Sends all candidate issues to AI for validation. Filters out issues
    where the AI determines it's not a real bug or confidence is too low.
    """
    if not issues:
        return issues

    # Don't bother validating if only 1 issue — cost/benefit not worth it
    if len(issues) <= 1:
        return issues

    settings = get_settings()
    if not settings.openai_api_key:
        logger.warning("Phase 5 skipped: no OpenAI API key")
        return issues

    client = AsyncOpenAI(api_key=settings.openai_api_key)

    # Build issue summaries for validation
    issue_summaries = []
    for i, issue in enumerate(issues):
        summary = (
            f"Issue #{i}: {issue.get('title', 'Untitled')}\n"
            f"  Description: {issue.get('description', 'N/A')}\n"
            f"  Page: {issue.get('affected_url', 'N/A')}\n"
            f"  Severity: {issue.get('severity', 'N/A')}\n"
            f"  Confidence: {issue.get('confidence', 'N/A')}\n"
            f"  Evidence: {issue.get('why_issue', 'N/A')}\n"
            f"  Steps: {'; '.join(issue.get('reproduction_steps', [])[:5])}"
        )
        issue_summaries.append(summary)

    user_content = "CANDIDATE ISSUES TO VALIDATE:\n\n" + "\n\n".join(issue_summaries)

    try:
        from app.utils.retry import with_retries

        t0 = time.time()
        response = await with_retries(
            lambda: client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": _VALIDATION_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                temperature=0.1,
                max_tokens=1500,
                response_format={"type": "json_object"},
            ),
            max_retries=2,
            base_delay=2.0,
            retryable_exceptions=(ConnectionError, TimeoutError, Exception),
            operation="OpenAI issue validation",
        )
        duration_ms = (time.time() - t0) * 1000

        if cost_tracker:
            cost_tracker.record(
                function_name="validate_issues_with_ai",
                model="gpt-4o-mini",
                response=response,
                duration_ms=duration_ms,
            )

        raw = response.choices[0].message.content or "{}"
        parsed = json.loads(raw)

        # Accept both {"validations": [...]} and direct [...]
        validations = parsed if isinstance(parsed, list) else parsed.get("validations", parsed.get("issues", []))
        if not isinstance(validations, list):
            logger.warning("Phase 5: AI returned unexpected format — keeping all issues")
            return issues

        # Build lookup by index
        validation_map: dict[int, dict] = {}
        for v in validations:
            if isinstance(v, dict) and "issue_index" in v:
                validation_map[v["issue_index"]] = v

        # Filter issues
        validated: list[dict] = []
        filtered_count = 0
        for i, issue in enumerate(issues):
            v = validation_map.get(i)
            if v is None:
                # AI didn't return a verdict — keep the issue
                validated.append(issue)
                continue

            is_real = v.get("is_real_bug", True)
            adjusted_conf = v.get("adjusted_confidence", issue.get("confidence", 0.7))

            if not is_real or adjusted_conf < 0.5:
                filtered_count += 1
                logger.info(
                    f"Phase 5 filtered: '{issue.get('title', '')[:60]}' "
                    f"(is_real={is_real}, conf={adjusted_conf:.2f}, "
                    f"reason={v.get('reasoning', 'N/A')[:80]})"
                )
                continue

            # Update confidence with AI's adjusted value
            issue["confidence"] = round(min(adjusted_conf, 1.0), 2)
            issue.setdefault("metadata", {})["ai_validation"] = {
                "is_real_bug": is_real,
                "reasoning": v.get("reasoning", ""),
                "adjusted_confidence": adjusted_conf,
            }
            validated.append(issue)

        logger.info(
            f"Phase 5 validation: {len(issues)} issues → {len(validated)} "
            f"({filtered_count} false positives removed)"
        )
        return validated

    except Exception as e:
        logger.error(f"Phase 5 validation failed: {e} — keeping all issues")
        return issues
