import hashlib
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from app.utils.logger import logger


def _normalize_for_fingerprint(text: str) -> str:
    """
    Normalize text before fingerprinting to group near-identical entries.
    - Lowercase
    - Collapse whitespace
    - Remove trailing noise words like 'page', 'error', 'on'
    - Strip punctuation at boundaries
    """
    text = text.lower().strip()
    text = re.sub(r"\s+", " ", text)  # collapse whitespace
    # Remove common suffixes that don't change meaning
    text = re.sub(r"\s+(page|screen|view)$", "", text)
    return text


def _normalize_url_for_fingerprint(url: str) -> str:
    """Normalize URL to just pathname for consistent fingerprinting."""
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        # Use only the path, strip trailing slash
        return parsed.path.rstrip("/") or "/"
    except Exception:
        return url.rstrip("/")


def _fingerprint(value: str) -> str:
    """Generate SHA-256 fingerprint from a string."""
    return hashlib.sha256(value.encode()).hexdigest()


async def fetch_posthog_events(
    api_key: str,
    project_id: str,
    since: datetime,
) -> list[dict[str, Any]]:
    """
    Fetch relevant events from PostHog since the given timestamp.
    Returns normalized event dicts ready for DB insertion.
    """
    base_url = f"https://eu.posthog.com/api/projects/{project_id}/events"
    headers = {"Authorization": f"Bearer {api_key}"}
    event_names = ["$exception", "$autocapture", "$rageclick"]

    all_events: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=30) as client:
        # ── Fetch exception, autocapture (clicks), and rageclick events ──
        for event_name in event_names:
            try:
                params = {
                    "event": event_name,
                    "after": since.isoformat(),
                    "limit": 200,
                    # "orderBy": ["-timestamp"],
                }
                response = await client.get(base_url, headers=headers, params=params)
                response.raise_for_status()
                data = response.json()

                for raw_event in data.get("results", []):
                    parsed = _parse_event(raw_event, event_name)
                    if parsed:
                        all_events.append(parsed)

            except httpx.HTTPError as e:
                logger.error(f"PostHog fetch error for {event_name}: {e}")
                continue

        # ── Fetch network/API failure events (status >= 500) ─────────────
        try:
            params = {
                "after": since.isoformat(),
                "limit": 200,
                "properties": '[{"key": "$status_code", "value": 500, "operator": "gte", "type": "event"}]',
            }
            response = await client.get(base_url, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()

            for raw_event in data.get("results", []):
                parsed = _parse_network_failure(raw_event)
                if parsed:
                    all_events.append(parsed)

        except httpx.HTTPError as e:
            logger.error(f"PostHog network failure fetch error: {e}")

        # ── Fetch pageview events (needed for dead click + dead end + flow analysis) ──
        try:
            params = {
                "event": "$pageview",
                "after": since.isoformat(),
                "limit": 500,
                # "orderBy": ["-timestamp"],
            }
            response = await client.get(base_url, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()

            for raw_event in data.get("results", []):
                parsed = _parse_pageview(raw_event)
                if parsed:
                    all_events.append(parsed)

        except httpx.HTTPError as e:
            logger.error(f"PostHog pageview fetch error: {e}")

        # ── Fetch pageleave events (needed for dead end duration analysis) ──
        try:
            params = {
                "event": "$pageleave",
                "after": since.isoformat(),
                "limit": 500,
                # "orderBy": ["-timestamp"],
            }
            response = await client.get(base_url, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()

            for raw_event in data.get("results", []):
                parsed = _parse_pageleave(raw_event)
                if parsed:
                    all_events.append(parsed)

        except httpx.HTTPError as e:
            logger.error(f"PostHog pageleave fetch error: {e}")

    logger.info(f"Fetched {len(all_events)} events from PostHog project {project_id}")
    return all_events


def _parse_event(raw: dict, event_name: str) -> dict[str, Any] | None:
    """Parse a raw PostHog event into our normalized format."""
    props = raw.get("properties", {})
    timestamp = raw.get("timestamp")
    if not timestamp:
        return None

    if event_name == "$exception":
        error_msg = props.get("$exception_message", "") or props.get("$exception_type", "Unknown")
        page_url = props.get("$current_url", "")
        normalized_msg = _normalize_for_fingerprint(error_msg)
        normalized_path = _normalize_url_for_fingerprint(page_url)
        return {
            "event_type": "exception",
            "fingerprint": _fingerprint(f"exception:{normalized_msg}:{normalized_path}"),
            "error_message": error_msg[:4096],
            "endpoint": None,
            "page_url": props.get("$current_url"),
            "css_selector": None,
            "session_id": props.get("$session_id"),
            "user_id": raw.get("distinct_id"),
            "status_code": None,
            "timestamp": timestamp,
            "raw_properties": {
                "stack_trace": (props.get("$exception_stack_trace_raw") or "")[:2000],
                "exception_type": props.get("$exception_type"),
            },
        }

    if event_name in ("$rageclick", "$autocapture"):
        elements = props.get("$elements", [])
        selector = _extract_selector(props, elements)
        page_url = props.get("$current_url", "")
        el_text = ""
        tag_name = ""
        if elements and isinstance(elements, list):
            first_el = elements[0]
            el_text = first_el.get("$el_text", "")[:200]
            tag_name = first_el.get("tag_name", "")

        # Rage click: PostHog-flagged or explicit $rageclick event
        is_rage = event_name == "$rageclick" or props.get("$rage_click")

        if is_rage:
            normalized_page = _normalize_url_for_fingerprint(page_url)
            return {
                "event_type": "rage_click",
                "fingerprint": _fingerprint(f"rage:{selector}:{normalized_page}"),
                "error_message": None,
                "endpoint": None,
                "page_url": page_url,
                "css_selector": selector[:1024],
                "session_id": props.get("$session_id"),
                "user_id": raw.get("distinct_id"),
                "status_code": None,
                "timestamp": timestamp,
                "raw_properties": {
                    "element_info": str(elements[:3]) if elements else "",
                    "el_text": el_text,
                    "tag_name": tag_name,
                },
            }

        # All other autocapture click events → potential dead clicks
        # Only consider interactive elements (buttons, links, inputs)
        interactive_tags = {"button", "a", "input", "select", "textarea", "summary"}
        is_interactive = (
            tag_name in interactive_tags
            or any(
                el.get("tag_name", "") in interactive_tags
                for el in (elements[:3] if elements else [])
            )
            or "btn" in selector.lower()
            or "button" in selector.lower()
            or props.get("$event_type") == "click"
        )

        if is_interactive:
            normalized_page = _normalize_url_for_fingerprint(page_url)
            return {
                "event_type": "dead_click",
                "fingerprint": _fingerprint(f"deadclick:{selector}:{normalized_page}"),
                "error_message": f"Click on '{el_text[:80]}' ({tag_name})" if el_text else f"Click on <{tag_name}>",
                "endpoint": None,
                "page_url": page_url,
                "css_selector": selector[:1024],
                "session_id": props.get("$session_id"),
                "user_id": raw.get("distinct_id"),
                "status_code": None,
                "timestamp": timestamp,
                "raw_properties": {
                    "element_info": str(elements[:3]) if elements else "",
                    "el_text": el_text,
                    "tag_name": tag_name,
                    "event_subtype": "click_candidate",
                },
            }

    return None


def _parse_network_failure(raw: dict) -> dict[str, Any] | None:
    """Parse a network failure event."""
    props = raw.get("properties", {})
    timestamp = raw.get("timestamp")
    if not timestamp:
        return None

    status_code = props.get("$status_code")
    if not status_code or int(status_code) < 500:
        return None

    endpoint = props.get("$url", "") or props.get("$current_url", "")
    normalized_endpoint = _normalize_url_for_fingerprint(endpoint)

    return {
        "event_type": "api_failure",
        "fingerprint": _fingerprint(f"api:{normalized_endpoint}:{status_code}"),
        "error_message": f"HTTP {status_code}",
        "endpoint": endpoint[:2048],
        "page_url": props.get("$current_url"),
        "css_selector": None,
        "session_id": props.get("$session_id"),
        "user_id": raw.get("distinct_id"),
        "status_code": int(status_code),
        "timestamp": timestamp,
        "raw_properties": {
            "method": props.get("$method"),
            "response_time": props.get("$response_time"),
        },
    }


def _parse_pageview(raw: dict) -> dict[str, Any] | None:
    """Parse a pageview event (used for dead click, dead end, and flow analysis)."""
    props = raw.get("properties", {})
    timestamp = raw.get("timestamp")
    if not timestamp:
        return None

    return {
        "event_type": "_pageview",
        "fingerprint": "",  # Not clustered directly
        "error_message": None,
        "endpoint": None,
        "page_url": props.get("$current_url", ""),
        "css_selector": None,
        "session_id": props.get("$session_id"),
        "user_id": raw.get("distinct_id"),
        "status_code": None,
        "timestamp": timestamp,
        "raw_properties": {
            "referrer": props.get("$referrer", ""),
            "prev_url": props.get("$prev_pageview_pathname", ""),
            "pathname": props.get("$pathname", ""),
            "screen_height": props.get("$screen_height"),
            "viewport_height": props.get("$viewport_height"),
        },
    }


def _parse_pageleave(raw: dict) -> dict[str, Any] | None:
    """Parse a pageleave event (used for dead end duration analysis)."""
    props = raw.get("properties", {})
    timestamp = raw.get("timestamp")
    if not timestamp:
        return None

    return {
        "event_type": "_pageleave",
        "fingerprint": "",  # Not clustered directly
        "error_message": None,
        "endpoint": None,
        "page_url": props.get("$current_url", ""),
        "css_selector": None,
        "session_id": props.get("$session_id"),
        "user_id": raw.get("distinct_id"),
        "status_code": None,
        "timestamp": timestamp,
        "raw_properties": {
            "prev_url": props.get("$prev_pageview_pathname", ""),
            "pathname": props.get("$pathname", ""),
        },
    }


def _extract_selector(props: dict, elements: list) -> str:
    """Extract the best CSS selector from event properties."""
    if elements and isinstance(elements, list):
        first_el = elements[0]
        # Prefer class, then id, then tag
        cls = first_el.get("attr__class", "")
        el_id = first_el.get("attr__id", "")
        tag = first_el.get("tag_name", "unknown")

        if el_id:
            return f"#{el_id}"
        if cls:
            return f"{tag}.{cls.split()[0]}"
        return tag

    return props.get("$el_text", "") or props.get("tag_name", "unknown")
