import hashlib
from collections import defaultdict, Counter
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from app.config import get_settings
from app.utils.logger import logger


def _fingerprint(value: str) -> str:
    """Generate SHA-256 fingerprint from a string."""
    return hashlib.sha256(value.encode()).hexdigest()


def detect_anomalies(
    events: list[dict[str, Any]],
    threshold: int | None = None,
) -> list[dict[str, Any]]:
    """
    Run all detection rules on a list of events.
    Returns anomaly cluster dicts for clusters that exceed thresholds.
    """
    settings = get_settings()
    threshold = threshold or settings.default_detection_threshold
    window_minutes = settings.anomaly_window_minutes

    # Separate auxiliary events (used for correlation, not clustered by fingerprint)
    pageviews = [e for e in events if e["event_type"] == "_pageview"]
    pageleaves = [e for e in events if e["event_type"] == "_pageleave"]
    trackable_events = [e for e in events if not e["event_type"].startswith("_")]

    # Group trackable events by fingerprint
    grouped: dict[str, list[dict]] = defaultdict(list)
    for event in trackable_events:
        grouped[event["fingerprint"]].append(event)

    anomalies: list[dict[str, Any]] = []

    # ── Rules A-D: Fingerprint-based detection ───────────────────────
    for fingerprint, group in grouped.items():
        event_type = group[0]["event_type"]

        if event_type in ("console_error", "exception"):
            cluster = _detect_error_anomaly(fingerprint, group, threshold, window_minutes)
        elif event_type == "api_failure":
            cluster = _detect_api_anomaly(fingerprint, group, threshold, window_minutes)
        elif event_type == "rage_click":
            cluster = _detect_rage_click_anomaly(fingerprint, group, settings)
        elif event_type == "dead_click":
            cluster = _detect_dead_click_anomaly(
                fingerprint, group, pageviews, trackable_events, settings
            )
        else:
            continue

        if cluster:
            anomalies.append(cluster)

    # ── Rule E: Dead End Detection (page-level, not fingerprint-based) ──
    dead_end_clusters = _detect_dead_end_pages(
        pageviews, pageleaves, trackable_events, settings
    )
    anomalies.extend(dead_end_clusters)

    # ── Rule F: Confusing Flow Detection (flow-level) ────────────────
    confusing_flow_clusters = _detect_confusing_flows(pageviews, settings)
    anomalies.extend(confusing_flow_clusters)

    # ── Deduplication: merge clusters with identical fingerprints ─────
    merged = _merge_duplicate_clusters(anomalies)

    logger.info(f"Detected {len(merged)} anomaly clusters (from {len(anomalies)} pre-merge) from {len(trackable_events)} events")
    return merged


# ═══════════════════════════════════════════════════════════════════════
# Rule A: Console Error / Exception
# ═══════════════════════════════════════════════════════════════════════

def _detect_error_anomaly(
    fingerprint: str,
    events: list[dict],
    threshold: int,
    window_minutes: int,
) -> dict[str, Any] | None:
    """Same error message >= threshold times within window → anomaly."""
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(minutes=window_minutes)
    recent = [e for e in events if _parse_ts(e["timestamp"]) >= window_start]

    if len(recent) < threshold:
        return None
    return _build_cluster(fingerprint, events[0]["event_type"], recent)


# ═══════════════════════════════════════════════════════════════════════
# Rule B: API Failure
# ═══════════════════════════════════════════════════════════════════════

def _detect_api_anomaly(
    fingerprint: str,
    events: list[dict],
    threshold: int,
    window_minutes: int,
) -> dict[str, Any] | None:
    """Same endpoint returns status >= 500 >= threshold times → anomaly."""
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(minutes=window_minutes)
    recent = [e for e in events if _parse_ts(e["timestamp"]) >= window_start]

    if len(recent) < threshold:
        return None
    return _build_cluster(fingerprint, "api_failure", recent)


# ═══════════════════════════════════════════════════════════════════════
# Rule C: Rage Click
# ═══════════════════════════════════════════════════════════════════════

def _detect_rage_click_anomaly(
    fingerprint: str,
    events: list[dict],
    settings: Any,
) -> dict[str, Any] | None:
    """Same CSS selector clicked rapidly in multiple sessions → anomaly."""
    distinct_sessions = {e.get("session_id") for e in events if e.get("session_id")}

    if len(distinct_sessions) < 2:
        return None
    if len(events) < settings.rage_click_threshold:
        return None
    return _build_cluster(fingerprint, "rage_click", events)


# ═══════════════════════════════════════════════════════════════════════
# Rule D: Dead Click
# ═══════════════════════════════════════════════════════════════════════

def _detect_dead_click_anomaly(
    fingerprint: str,
    click_events: list[dict],
    pageviews: list[dict],
    all_events: list[dict],
    settings: Any,
) -> dict[str, Any] | None:
    """
    Click on interactive element with no subsequent page navigation,
    network request, or JS error within timeout window.
    """
    timeout_ms = settings.dead_click_timeout_ms
    min_sessions = settings.dead_click_min_sessions
    timeout_delta = timedelta(milliseconds=timeout_ms)

    # Build session activity timeline
    session_activity: dict[str, list[tuple[datetime, str]]] = defaultdict(list)

    for pv in pageviews:
        sid = pv.get("session_id")
        if sid:
            session_activity[sid].append((_parse_ts(pv["timestamp"]), "pageview"))

    for ev in all_events:
        sid = ev.get("session_id")
        if not sid:
            continue
        if ev["event_type"] in ("api_failure", "exception", "console_error"):
            session_activity[sid].append((_parse_ts(ev["timestamp"]), ev["event_type"]))

    for sid in session_activity:
        session_activity[sid].sort(key=lambda x: x[0])

    dead_clicks: list[dict] = []
    for click in click_events:
        click_ts = _parse_ts(click["timestamp"])
        sid = click.get("session_id")
        if not sid:
            continue

        activities = session_activity.get(sid, [])
        has_followup = False
        for activity_ts, _ in activities:
            if activity_ts <= click_ts:
                continue
            if activity_ts <= click_ts + timeout_delta:
                has_followup = True
                break
            break

        if not has_followup:
            dead_clicks.append(click)

    if not dead_clicks:
        return None

    dead_sessions = {e.get("session_id") for e in dead_clicks if e.get("session_id")}
    if len(dead_sessions) < min_sessions:
        return None

    cluster = _build_cluster(fingerprint, "dead_click", dead_clicks)

    sample_click = dead_clicks[0]
    el_text = sample_click.get("raw_properties", {}).get("el_text", "")
    tag_name = sample_click.get("raw_properties", {}).get("tag_name", "")
    cluster["error_message"] = (
        f"Dead click: '{el_text[:60]}' ({tag_name}) on {sample_click.get('page_url', 'unknown page')} "
        f"— no response within {timeout_ms}ms across {len(dead_sessions)} sessions"
    )
    return cluster


# ═══════════════════════════════════════════════════════════════════════
# Rule E: Dead End Page
# ═══════════════════════════════════════════════════════════════════════

def _detect_dead_end_pages(
    pageviews: list[dict],
    pageleaves: list[dict],
    trackable_events: list[dict],
    settings: Any,
) -> list[dict[str, Any]]:
    """
    Detect pages where users land and bounce with no meaningful interaction.

    A "dead end" is when a user:
    1. Visits a page
    2. Does NOT click, type, or interact with anything
    3. Leaves within a short duration (or ends the session)

    When this pattern repeats across many sessions on the same URL,
    the page is likely broken, empty, or confusing.
    """
    max_duration = settings.dead_end_max_duration_seconds
    min_sessions = settings.dead_end_min_sessions
    min_bounce_rate = settings.dead_end_min_bounce_rate

    # Build per-session timelines: session_id → sorted [(timestamp, page_url, event_type)]
    session_timelines: dict[str, list[tuple[datetime, str, str]]] = defaultdict(list)

    for pv in pageviews:
        sid = pv.get("session_id")
        if sid:
            session_timelines[sid].append((
                _parse_ts(pv["timestamp"]),
                pv.get("page_url", ""),
                "_pageview",
            ))

    for pl in pageleaves:
        sid = pl.get("session_id")
        if sid:
            session_timelines[sid].append((
                _parse_ts(pl["timestamp"]),
                pl.get("page_url", ""),
                "_pageleave",
            ))

    for ev in trackable_events:
        sid = ev.get("session_id")
        if sid:
            session_timelines[sid].append((
                _parse_ts(ev["timestamp"]),
                ev.get("page_url", ""),
                ev["event_type"],
            ))

    for sid in session_timelines:
        session_timelines[sid].sort(key=lambda x: x[0])

    # For each page URL, track: total visits vs bounces
    # page_url → { "total_sessions": set, "bounce_sessions": set, "bounce_events": list }
    page_stats: dict[str, dict] = defaultdict(lambda: {
        "total_sessions": set(),
        "bounce_sessions": set(),
        "bounce_events": [],
    })

    for sid, timeline in session_timelines.items():
        # Walk through this session's timeline page by page
        i = 0
        while i < len(timeline):
            ts, page_url, etype = timeline[i]
            if etype != "_pageview" or not page_url:
                i += 1
                continue

            normalized_url = _normalize_url(page_url)
            page_stats[normalized_url]["total_sessions"].add(sid)

            # Look ahead: find what happens between this pageview and the next one
            has_interaction = False
            leave_ts = None
            j = i + 1
            while j < len(timeline):
                next_ts, next_url, next_etype = timeline[j]

                # Hit the next pageview — this page visit is done
                if next_etype == "_pageview":
                    leave_ts = next_ts
                    break

                # Hit a pageleave for this page
                if next_etype == "_pageleave":
                    leave_ts = next_ts
                    j += 1
                    continue

                # Any trackable event on this page = interaction
                if next_etype not in ("_pageview", "_pageleave"):
                    has_interaction = True
                    break

                j += 1

            # If no interaction and short duration, it's a bounce
            if not has_interaction:
                if leave_ts:
                    duration = (leave_ts - ts).total_seconds()
                else:
                    # Session ended on this page (last event) — treat as bounce
                    duration = 0

                if duration <= max_duration:
                    page_stats[normalized_url]["bounce_sessions"].add(sid)
                    page_stats[normalized_url]["bounce_events"].append({
                        "event_type": "dead_end",
                        "page_url": page_url,
                        "session_id": sid,
                        "user_id": None,  # We'll pull from pageview
                        "timestamp": ts.isoformat(),
                        "duration_seconds": duration,
                    })

            i += 1

    # Now find pages with high bounce rates across enough sessions
    anomalies: list[dict[str, Any]] = []

    for url, stats in page_stats.items():
        total = len(stats["total_sessions"])
        bounces = len(stats["bounce_sessions"])

        if total < min_sessions:
            continue

        bounce_rate = bounces / total if total > 0 else 0
        if bounce_rate < min_bounce_rate:
            continue

        # Build the anomaly cluster
        bounce_events = stats["bounce_events"]
        fp = _fingerprint(f"deadend:{url}")

        user_ids = set()
        session_ids = []
        timestamps = []
        for be in bounce_events:
            if be.get("session_id"):
                session_ids.append(be["session_id"])
            timestamps.append(_parse_ts(be["timestamp"]))

        # Also pull user_ids from the pageview data
        for pv in pageviews:
            if _normalize_url(pv.get("page_url", "")) == url and pv.get("user_id"):
                user_ids.add(pv["user_id"])

        sample_sessions = list(set(session_ids))[:5]

        # Build session → event timestamp mapping
        session_event_times: dict[str, str] = {}
        for be in bounce_events:
            sid = be.get("session_id")
            ts_str = be.get("timestamp", "")
            if sid and ts_str:
                if sid not in session_event_times or ts_str < session_event_times[sid]:
                    session_event_times[sid] = ts_str

        cluster = {
            "fingerprint": fp,
            "event_type": "dead_end",
            "error_message": (
                f"Dead end page: {url} — {bounce_rate:.0%} of users ({bounces}/{total} sessions) "
                f"leave within {max_duration}s with no interaction"
            ),
            "endpoint": None,
            "css_selector": None,
            "page_url": url,
            "count": bounces,
            "affected_users": len(user_ids),
            "first_seen": min(timestamps).isoformat() if timestamps else datetime.now(timezone.utc).isoformat(),
            "last_seen": max(timestamps).isoformat() if timestamps else datetime.now(timezone.utc).isoformat(),
            "sample_session_ids": sample_sessions,
            "session_event_times": session_event_times,
        }
        anomalies.append(cluster)

    if anomalies:
        logger.info(f"Detected {len(anomalies)} dead end pages")

    return anomalies


# ═══════════════════════════════════════════════════════════════════════
# Rule F: Confusing Flow (Funnel Drop-off)
# ═══════════════════════════════════════════════════════════════════════

def _detect_confusing_flows(
    pageviews: list[dict],
    settings: Any,
) -> list[dict[str, Any]]:
    """
    Detect multi-step flows where users consistently drop off at a specific step.

    How it works:
    1. Build per-session page sequences (ordered list of pages visited)
    2. Find common page transitions (A → B) across sessions
    3. For each page A with enough traffic, check: what % of sessions that visited A
       also continued to ANY next page?
    4. If a page has high entry traffic but most sessions end there (drop-off),
       and it's not a natural terminal page (like /thank-you, /success, /dashboard),
       flag it as a confusing flow step.

    This catches:
    - Multi-step forms where users abandon at a specific step
    - Checkout flows where users drop off
    - Onboarding sequences where users get stuck
    """
    min_sessions = settings.confusing_flow_min_sessions
    drop_threshold = settings.confusing_flow_drop_threshold

    # Terminal pages that are expected endpoints (not confusing if users stop here)
    terminal_keywords = {
        "thank", "success", "confirm", "complete", "done", "welcome",
        "dashboard", "home", "receipt", "order-confirm",
    }

    # Build per-session page sequences
    session_pages: dict[str, list[tuple[datetime, str]]] = defaultdict(list)

    for pv in pageviews:
        sid = pv.get("session_id")
        url = pv.get("page_url", "")
        if sid and url:
            session_pages[sid].append((_parse_ts(pv["timestamp"]), _normalize_url(url)))

    # Sort each session's pages by time
    for sid in session_pages:
        session_pages[sid].sort(key=lambda x: x[0])

    # Count transitions: for each page, how many sessions continued vs stopped
    # page_url → { "entered": set(session_ids), "continued": set(session_ids) }
    flow_stats: dict[str, dict[str, set]] = defaultdict(lambda: {
        "entered": set(),
        "continued": set(),
        "next_pages": Counter(),
    })

    for sid, pages in session_pages.items():
        for i, (ts, url) in enumerate(pages):
            flow_stats[url]["entered"].add(sid)

            if i + 1 < len(pages):
                next_url = pages[i + 1][1]
                if next_url != url:  # Ignore same-page reloads
                    flow_stats[url]["continued"].add(sid)
                    flow_stats[url]["next_pages"][next_url] += 1

    # Find pages with significant drop-off
    anomalies: list[dict[str, Any]] = []

    for url, stats in flow_stats.items():
        entered = len(stats["entered"])
        continued = len(stats["continued"])

        if entered < min_sessions:
            continue

        drop_rate = 1 - (continued / entered) if entered > 0 else 0
        if drop_rate < drop_threshold:
            continue

        # Skip terminal pages (expected endpoints)
        url_lower = url.lower()
        is_terminal = any(kw in url_lower for kw in terminal_keywords)
        if is_terminal:
            continue

        # Skip if this is the root/home page (natural session start/end)
        parsed_url = urlparse(url)
        if parsed_url.path in ("", "/", "/index.html"):
            continue

        fp = _fingerprint(f"confusingflow:{url}")
        dropped_sessions = stats["entered"] - stats["continued"]
        sample_sessions = list(dropped_sessions)[:5]

        # Collect user IDs from pageviews
        user_ids = set()
        timestamps = []
        for pv in pageviews:
            if _normalize_url(pv.get("page_url", "")) == url:
                if pv.get("user_id"):
                    user_ids.add(pv["user_id"])
                timestamps.append(_parse_ts(pv["timestamp"]))

        # Determine what came before (referrer pages)
        incoming_pages = Counter()
        for sid, pages in session_pages.items():
            for i, (ts, page_url) in enumerate(pages):
                if page_url == url and i > 0:
                    incoming_pages[pages[i - 1][1]] += 1

        top_incoming = incoming_pages.most_common(3)
        incoming_str = ", ".join(f"{p} ({c}x)" for p, c in top_incoming) if top_incoming else "direct/unknown"

        # Build session → event timestamp mapping for dropped sessions
        session_event_times: dict[str, str] = {}
        for pv in pageviews:
            if _normalize_url(pv.get("page_url", "")) == url:
                sid = pv.get("session_id")
                ts_str = pv.get("timestamp", "")
                if isinstance(ts_str, datetime):
                    ts_str = ts_str.isoformat()
                if sid and ts_str:
                    if sid not in session_event_times or ts_str < session_event_times[sid]:
                        session_event_times[sid] = ts_str

        cluster = {
            "fingerprint": fp,
            "event_type": "confusing_flow",
            "error_message": (
                f"Flow drop-off: {drop_rate:.0%} of users ({entered - continued}/{entered}) "
                f"abandon at {url} — incoming from: {incoming_str}"
            ),
            "endpoint": None,
            "css_selector": None,
            "page_url": url,
            "count": entered - continued,
            "affected_users": len(user_ids),
            "first_seen": min(timestamps).isoformat() if timestamps else datetime.now(timezone.utc).isoformat(),
            "last_seen": max(timestamps).isoformat() if timestamps else datetime.now(timezone.utc).isoformat(),
            "sample_session_ids": sample_sessions,
            "session_event_times": session_event_times,
        }
        anomalies.append(cluster)

    if anomalies:
        logger.info(f"Detected {len(anomalies)} confusing flow drop-off points")

    return anomalies


# ═══════════════════════════════════════════════════════════════════════
# Deduplication
# ═══════════════════════════════════════════════════════════════════════

def _merge_duplicate_clusters(clusters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Merge anomaly clusters that share the same fingerprint.
    This handles cases where slightly different events produce the same
    normalized fingerprint.
    """
    by_fp: dict[str, dict[str, Any]] = {}

    for cluster in clusters:
        fp = cluster["fingerprint"]
        if fp not in by_fp:
            by_fp[fp] = cluster
        else:
            # Merge into existing
            existing = by_fp[fp]
            existing["count"] = existing.get("count", 0) + cluster.get("count", 0)

            # Merge affected users (take max since we can't dedupe user sets here)
            existing["affected_users"] = max(
                existing.get("affected_users", 0),
                cluster.get("affected_users", 0),
            )

            # Expand time range
            if cluster.get("first_seen") and existing.get("first_seen"):
                existing["first_seen"] = min(existing["first_seen"], cluster["first_seen"])
            if cluster.get("last_seen") and existing.get("last_seen"):
                existing["last_seen"] = max(existing["last_seen"], cluster["last_seen"])

            # Merge session IDs (keep up to 5 unique)
            existing_sessions = set(existing.get("sample_session_ids", []))
            new_sessions = set(cluster.get("sample_session_ids", []))
            existing["sample_session_ids"] = list(existing_sessions | new_sessions)[:5]

            # Merge session_event_times
            existing_times = existing.get("session_event_times", {})
            new_times = cluster.get("session_event_times", {})
            for sid, ts in new_times.items():
                if sid not in existing_times or ts < existing_times[sid]:
                    existing_times[sid] = ts
            existing["session_event_times"] = existing_times

            # Keep the longer/better error message
            new_msg = cluster.get("error_message") or ""
            old_msg = existing.get("error_message") or ""
            if len(new_msg) > len(old_msg):
                existing["error_message"] = new_msg

    return list(by_fp.values())


# ═══════════════════════════════════════════════════════════════════════
# Utilities
# ═══════════════════════════════════════════════════════════════════════

def _normalize_url(url: str) -> str:
    """Normalize a URL by removing query params and fragments for grouping."""
    try:
        parsed = urlparse(url)
        # Keep scheme + netloc + path, strip query and fragment
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
    except Exception:
        return url


def _build_cluster(
    fingerprint: str,
    event_type: str,
    events: list[dict],
) -> dict[str, Any]:
    """Build an anomaly cluster dict from a group of events."""
    timestamps = [_parse_ts(e["timestamp"]) for e in events]
    user_ids = {e.get("user_id") for e in events if e.get("user_id")}
    session_ids = [e.get("session_id") for e in events if e.get("session_id")]

    sample_sessions = list(set(session_ids))[:5]

    # Build session → earliest event timestamp mapping
    session_event_times: dict[str, str] = {}
    for e in events:
        sid = e.get("session_id")
        if not sid:
            continue
        event_ts = e.get("timestamp", "")
        if isinstance(event_ts, datetime):
            event_ts = event_ts.isoformat()
        # Keep the earliest event timestamp per session
        if sid not in session_event_times or event_ts < session_event_times[sid]:
            session_event_times[sid] = event_ts

    return {
        "fingerprint": fingerprint,
        "event_type": event_type,
        "error_message": events[0].get("error_message"),
        "endpoint": events[0].get("endpoint"),
        "css_selector": events[0].get("css_selector"),
        "page_url": events[0].get("page_url"),
        "count": len(events),
        "affected_users": len(user_ids),
        "first_seen": min(timestamps).isoformat(),
        "last_seen": max(timestamps).isoformat(),
        "sample_session_ids": sample_sessions,
        "session_event_times": session_event_times,
    }


def _parse_ts(ts: str | datetime) -> datetime:
    """Parse a timestamp string or return datetime as-is."""
    if isinstance(ts, datetime):
        return ts
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.now(timezone.utc)
