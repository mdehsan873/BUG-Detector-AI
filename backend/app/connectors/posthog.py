"""
PostHog session replay connector.

Fetches sessions via the PostHog API (/api/projects/{id}/sessions or
falls back to /events grouping by $session_id) and normalises them
into the common NormalizedSession / NormalizedEvent format.

Also fetches session recording snapshots (rrweb DOM data) to extract
visible page text for AI-based error text detection.
"""

from __future__ import annotations

import gzip
import json
import re
from datetime import datetime, timezone

import httpx

from app.connectors.base import NormalizedEvent, NormalizedSession, SessionConnector
from app.utils.logger import logger

# ── rrweb snapshot text extraction ──────────────────────────────────────────


def _rrweb_ts_to_iso(ts) -> str:
    """Convert rrweb millisecond-epoch timestamp to ISO 8601 string."""
    if not ts:
        return ""
    try:
        ms = int(ts)
        # rrweb timestamps are milliseconds since Unix epoch
        dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
        return dt.isoformat()
    except (ValueError, TypeError, OSError):
        # Already a string date or unparseable
        return str(ts) if ts else ""

# Tags that should be skipped entirely (invisible / non-content)
_SKIP_TAGS = frozenset(
    ("script", "style", "noscript", "meta", "link", "head", "svg", "path",
     "defs", "clippath", "lineargradient", "radialgradient", "symbol")
)

# Tags that map to markdown headings
_HEADING_TAGS = {"h1": "#", "h2": "##", "h3": "###", "h4": "####", "h5": "#####", "h6": "######"}

# Semantic landmark/section tags we annotate with [SECTION: ...] markers
_SECTION_TAGS = frozenset(("nav", "header", "footer", "main", "aside", "section", "article", "dialog"))

# Form-related tags that get special annotations
_FORM_TAGS = frozenset(("form", "input", "textarea", "select", "button", "label", "fieldset", "legend"))


def _rrweb_node_to_markdown(node: dict, depth: int = 0, list_type: str = "") -> str:
    """
    Recursively convert an rrweb DOM snapshot node into structured markdown.

    Preserves page structure so the AI can reason about WHERE text appears:
    - Headings → # / ## / ###
    - Forms → [FORM], [INPUT name="..." type="..." placeholder="..."]
    - Buttons/links → [BUTTON: text], [LINK: text → href]
    - Sections → [NAV], [HEADER], [FOOTER], [DIALOG], etc.
    - Lists → bullet / numbered items
    - Error-like elements → [ALERT: ...] or [ERROR: ...]
    """
    if depth > 50:
        return ""

    node_type = node.get("type")

    # ── Text node (rrweb type 3) ──────────────────────────────────────
    if node_type == 3:
        text = (node.get("textContent") or "").strip()
        return text if text else ""

    # ── Element node (rrweb type 2) ───────────────────────────────────
    if node_type == 2:
        tag = (node.get("tagName") or "").lower()

        # Skip invisible / non-content tags
        if tag in _SKIP_TAGS:
            return ""

        attrs = node.get("attributes") or {}
        style = attrs.get("style", "")
        style_compact = style.replace(" ", "")

        # Detect visibility/state markers instead of skipping hidden elements
        visibility_markers: list[str] = []
        if "display:none" in style_compact:
            visibility_markers.append("HIDDEN:display-none")
        if "visibility:hidden" in style_compact:
            visibility_markers.append("HIDDEN:visibility-hidden")
        if "opacity:0" in style_compact:
            visibility_markers.append("HIDDEN:opacity-0")
        if attrs.get("hidden") is not None:
            visibility_markers.append("HIDDEN:attr")
        if attrs.get("aria-hidden") == "true":
            visibility_markers.append("HIDDEN:aria")
        if attrs.get("disabled") is not None:
            visibility_markers.append("DISABLED")
        if attrs.get("aria-busy") == "true":
            visibility_markers.append("LOADING")

        # Recursively get children's markdown
        children = node.get("childNodes") or []
        child_parts: list[str] = []
        for child in children:
            if isinstance(child, dict):
                lt = list_type if tag in ("ul", "ol") else ""
                if tag == "ul":
                    lt = "ul"
                elif tag == "ol":
                    lt = "ol"
                part = _rrweb_node_to_markdown(child, depth + 1, lt)
                if part:
                    child_parts.append(part)
        inner = " ".join(child_parts).strip() if child_parts else ""

        # Prefix with visibility markers if element is hidden/disabled/loading
        marker_prefix = ""
        if visibility_markers and inner:
            marker_prefix = "[" + ",".join(visibility_markers) + "] "

        # ── Headings ──────────────────────────────────────────────────
        if tag in _HEADING_TAGS and inner:
            prefix = _HEADING_TAGS[tag]
            return f"\n{prefix} {marker_prefix}{inner}\n"

        # ── Links ─────────────────────────────────────────────────────
        if tag == "a" and inner:
            href = attrs.get("href", "")
            if href and href != "#":
                return f"{marker_prefix}[LINK: {inner} → {href}]"
            return f"{marker_prefix}[LINK: {inner}]"

        # ── Buttons ───────────────────────────────────────────────────
        if tag == "button" and inner:
            btn_type = attrs.get("type", "")
            disabled = "disabled" if attrs.get("disabled") is not None else ""
            annotation = " ".join(filter(None, [btn_type, disabled])).strip()
            if annotation:
                return f"{marker_prefix}[BUTTON ({annotation}): {inner}]"
            return f"{marker_prefix}[BUTTON: {inner}]"

        # ── Form inputs ──────────────────────────────────────────────
        if tag == "input":
            itype = attrs.get("type", "text")
            name = attrs.get("name", "") or attrs.get("id", "")
            placeholder = attrs.get("placeholder", "")
            value = attrs.get("value", "")
            aria_label = attrs.get("aria-label", "")
            aria_invalid = attrs.get("aria-invalid", "")
            # Mask password values
            if itype == "password" and value:
                value = "***"
            parts = [f'type="{itype}"']
            if name:
                parts.append(f'name="{name}"')
            if placeholder:
                parts.append(f'placeholder="{placeholder}"')
            if value:
                parts.append(f'value="{value}"')
            if aria_label:
                parts.append(f'aria-label="{aria_label}"')
            if aria_invalid:
                parts.append(f'aria-invalid="{aria_invalid}"')
            return f"[INPUT {' '.join(parts)}]"

        if tag == "textarea":
            name = attrs.get("name", "") or attrs.get("id", "")
            placeholder = attrs.get("placeholder", "")
            parts = []
            if name:
                parts.append(f'name="{name}"')
            if placeholder:
                parts.append(f'placeholder="{placeholder}"')
            content = inner or ""
            return f"[TEXTAREA {' '.join(parts)}]{': ' + content if content else ''}"

        if tag == "select":
            name = attrs.get("name", "") or attrs.get("id", "")
            return f"[SELECT name=\"{name}\"] {inner}" if inner else f"[SELECT name=\"{name}\"]"

        if tag == "option":
            selected = " (selected)" if attrs.get("selected") is not None else ""
            return f"[OPTION{selected}: {inner}]" if inner else ""

        if tag == "label":
            for_attr = attrs.get("for", "")
            if for_attr:
                return f"[LABEL for=\"{for_attr}\"]: {inner}" if inner else ""
            return f"[LABEL]: {inner}" if inner else ""

        if tag == "form":
            action = attrs.get("action", "")
            method = attrs.get("method", "")
            header = f"{marker_prefix}[FORM action=\"{action}\" method=\"{method}\"]" if action else f"{marker_prefix}[FORM]"
            return f"\n{header}\n{inner}\n[/FORM]\n" if inner else ""

        # ── Lists ─────────────────────────────────────────────────────
        if tag == "li":
            prefix = "- " if list_type == "ul" else "1. "
            return f"\n{prefix}{inner}" if inner else ""

        if tag in ("ul", "ol"):
            return f"\n{inner}\n" if inner else ""

        # ── Sections / landmarks ──────────────────────────────────────
        if tag in _SECTION_TAGS and inner:
            section_name = tag.upper()
            # Check for role or aria-label for better context
            role = attrs.get("role", "")
            aria_label = attrs.get("aria-label", "")
            label = aria_label or role or ""
            if label:
                section_name = f"{section_name}: {label}"
            return f"\n[{section_name}]\n{inner}\n[/{tag.upper()}]\n"

        # ── Images ────────────────────────────────────────────────────
        if tag == "img":
            alt = attrs.get("alt", "")
            return f"[IMG: {alt}]" if alt else ""

        # ── Error/alert detection via class/role ──────────────────────
        cls = (attrs.get("class", "") or "").lower()
        role = (attrs.get("role", "") or "").lower()
        if role in ("alert", "alertdialog", "status") and inner:
            return f"\n{marker_prefix}[ALERT ({role})]: {inner}\n"
        if inner and any(kw in cls for kw in ("error", "alert", "warning", "danger", "toast", "notification", "snackbar")):
            return f"\n{marker_prefix}[UI-{cls.split()[0].upper() if cls else 'NOTICE'}]: {inner}\n"

        # ── Tables (simplified) ───────────────────────────────────────
        if tag == "th" and inner:
            return f"**{inner}** | "
        if tag == "td" and inner:
            return f"{inner} | "
        if tag == "tr" and inner:
            return f"\n| {inner}"

        # ── Line breaks / separators ──────────────────────────────────
        if tag == "br":
            return "\n"
        if tag == "hr":
            return "\n---\n"

        # ── Paragraphs and divs ───────────────────────────────────────
        if tag == "p" and inner:
            return f"\n{marker_prefix}{inner}\n"

        # Default: return children content with visibility markers
        return f"{marker_prefix}{inner}" if marker_prefix else inner

    # ── Document node (rrweb type 0) or other top-level nodes ─────────
    children = node.get("childNodes") or []
    parts = []
    for child in children:
        if isinstance(child, dict):
            part = _rrweb_node_to_markdown(child, depth + 1, list_type)
            if part:
                parts.append(part)
    return "\n".join(parts)


def _extract_text_from_rrweb_node(node: dict, texts: list[str], depth: int = 0) -> None:
    """
    Legacy flat text extraction — kept as fallback.
    Recursively extract visible text from an rrweb DOM snapshot node.
    """
    if depth > 50:
        return
    node_type = node.get("type")
    if node_type == 3:
        return
    if node_type == 2:
        tag = (node.get("tagName") or "").lower()
        if tag in _SKIP_TAGS:
            return
        attrs = node.get("attributes") or {}
        style = attrs.get("style", "")
        if "display:none" in style.replace(" ", "") or "visibility:hidden" in style.replace(" ", ""):
            return
        if attrs.get("hidden") is not None or attrs.get("aria-hidden") == "true":
            return
    children = node.get("childNodes") or []
    direct_text_parts: list[str] = []
    for child in children:
        if not isinstance(child, dict):
            continue
        if child.get("type") == 3:
            t = (child.get("textContent") or "").strip()
            if t:
                direct_text_parts.append(t)
    combined = " ".join(direct_text_parts).strip()
    if combined and len(combined) >= 2:
        texts.append(combined)
    for child in children:
        if isinstance(child, dict) and child.get("type") != 3:
            _extract_text_from_rrweb_node(child, texts, depth + 1)


def _clean_markdown(md: str) -> str:
    """Collapse excessive whitespace in generated markdown."""
    # Collapse 3+ consecutive newlines into 2
    cleaned = re.sub(r"\n{3,}", "\n\n", md)
    # Remove trailing spaces on lines
    cleaned = "\n".join(line.rstrip() for line in cleaned.split("\n"))
    return cleaned.strip()


def _parse_rrweb_records(snapshot_lines: list[str]) -> list[dict]:
    """Parse JSONL snapshot lines into a flat list of rrweb record dicts."""
    records: list[dict] = []
    parse_errors = 0
    for line in snapshot_lines:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
            if isinstance(parsed, list):
                records.extend(r for r in parsed if isinstance(r, dict))
            elif isinstance(parsed, dict):
                records.append(parsed)
        except (json.JSONDecodeError, ValueError):
            parse_errors += 1
    logger.debug(f"Parsed {len(records)} rrweb records ({parse_errors} errors)")
    return records


def _decompress_record_data(record: dict) -> dict | None:
    """Extract and decompress the 'data' field from an rrweb record.
    Returns the parsed data dict, or None if it can't be parsed."""
    rr_type = record.get("type")
    raw_data = record.get("data", {})

    if isinstance(raw_data, str):
        if raw_data[:2] == "\x1f\x8b":
            try:
                decompressed = gzip.decompress(raw_data.encode("latin-1"))
                return json.loads(decompressed)
            except Exception as exc:
                if rr_type == 2:
                    logger.warning(f"FullSnapshot gzip decompress failed: {exc}")
                return None
        else:
            try:
                return json.loads(raw_data)
            except (json.JSONDecodeError, ValueError):
                return None
    elif isinstance(raw_data, dict):
        return raw_data
    return None


# ── rrweb Incremental Mutation Replay ──────────────────────────────────────


def _build_node_map(node: dict, node_map: dict[int, dict] | None = None) -> dict[int, dict]:
    """
    Recursively traverse a Type 2 full snapshot DOM tree and build a flat
    {node_id: node_dict} lookup map for mutation targeting.

    Each node gets a '_children_ids' list for ordered child tracking.
    """
    if node_map is None:
        node_map = {}

    nid = node.get("id")
    if nid is not None:
        # Store a shallow copy with children IDs
        entry = dict(node)
        children = node.get("childNodes") or []
        entry["_children_ids"] = [c.get("id") for c in children if isinstance(c, dict) and c.get("id") is not None]
        node_map[nid] = entry

        for child in children:
            if isinstance(child, dict):
                _build_node_map(child, node_map)

    return node_map


def _apply_mutations(node_map: dict[int, dict], mutation_data: dict) -> None:
    """
    Apply a single Type 3 (IncrementalSnapshot, source=0) mutation to the node_map.

    Handles: adds, removes, texts, attributes.
    """
    # ── Removes: delete nodes from map ──
    removes = mutation_data.get("removes") or []
    for rm in removes:
        if not isinstance(rm, dict):
            continue
        rm_id = rm.get("id")
        if rm_id is not None and rm_id in node_map:
            # Remove from parent's children list
            parent_id = rm.get("parentId")
            if parent_id and parent_id in node_map:
                parent = node_map[parent_id]
                cids = parent.get("_children_ids", [])
                if rm_id in cids:
                    cids.remove(rm_id)
            del node_map[rm_id]

    # ── Adds: insert new nodes ──
    adds = mutation_data.get("adds") or []
    for add in adds:
        if not isinstance(add, dict):
            continue
        add_node = add.get("node")
        if isinstance(add_node, str):
            try:
                add_node = json.loads(add_node)
            except (json.JSONDecodeError, ValueError):
                continue
        if not isinstance(add_node, dict):
            continue

        nid = add_node.get("id")
        if nid is None:
            continue

        # Add node to map (recursively for children)
        _build_node_map(add_node, node_map)

        # Insert into parent's children list
        parent_id = add.get("parentId")
        if parent_id and parent_id in node_map:
            parent = node_map[parent_id]
            cids = parent.setdefault("_children_ids", [])
            next_id = add.get("nextId")
            if next_id and next_id in cids:
                idx = cids.index(next_id)
                cids.insert(idx, nid)
            elif nid not in cids:
                cids.append(nid)

    # ── Texts: update textContent ──
    texts = mutation_data.get("texts") or []
    for tm in texts:
        if not isinstance(tm, dict):
            continue
        tid = tm.get("id")
        if tid is not None and tid in node_map:
            node_map[tid]["textContent"] = tm.get("value", "")

    # ── Attributes: update element attributes ──
    attributes = mutation_data.get("attributes") or []
    for attr_change in attributes:
        if not isinstance(attr_change, dict):
            continue
        aid = attr_change.get("id")
        if aid is not None and aid in node_map:
            attrs = attr_change.get("attributes") or {}
            if isinstance(attrs, dict):
                existing = node_map[aid].setdefault("attributes", {})
                for k, v in attrs.items():
                    if v is None:
                        existing.pop(k, None)  # null = remove attribute
                    else:
                        existing[k] = v


def _node_map_to_markdown(node_map: dict[int, dict], node_id: int, depth: int = 0) -> str:
    """
    Walk the reconstructed DOM tree (from node_map) and produce markdown.
    Reuses the same rendering logic as _rrweb_node_to_markdown but navigates
    via the flat node_map + _children_ids structure.
    """
    if depth > 50 or node_id not in node_map:
        return ""

    node = node_map[node_id]

    # Rebuild a standard rrweb node structure for _rrweb_node_to_markdown
    # by re-attaching childNodes from the map
    rebuilt = dict(node)
    children_ids = node.get("_children_ids", [])
    child_nodes = []
    for cid in children_ids:
        if cid in node_map:
            child_nodes.append(_rebuild_node_tree(node_map, cid, depth + 1))
    if child_nodes:
        rebuilt["childNodes"] = child_nodes
    else:
        rebuilt["childNodes"] = []

    return _rrweb_node_to_markdown(rebuilt, depth)


def _rebuild_node_tree(node_map: dict[int, dict], node_id: int, depth: int = 0) -> dict:
    """Recursively rebuild a standard rrweb node tree from the flat node_map."""
    if depth > 50 or node_id not in node_map:
        return {}

    node = dict(node_map[node_id])
    children_ids = node.get("_children_ids", [])
    child_nodes = []
    for cid in children_ids:
        if cid in node_map:
            child_nodes.append(_rebuild_node_tree(node_map, cid, depth + 1))
    node["childNodes"] = child_nodes
    return node


def reconstruct_dom_at_timestamp(
    snapshot_lines: list[str],
    target_ts_ms: float,
) -> str:
    """
    Reconstruct the DOM state at a specific timestamp by replaying
    Type 3 incremental mutations on top of the last Type 2 full snapshot.

    Args:
        snapshot_lines: Raw rrweb JSONL lines from PostHog recording
        target_ts_ms: Target timestamp in milliseconds since epoch

    Returns:
        Markdown representation of the DOM at target_ts_ms, or empty string if no snapshot found.
    """
    records = _parse_rrweb_records(snapshot_lines)

    # Find the last Type 2 (FullSnapshot) before target_ts
    best_snapshot = None
    best_snapshot_ts = 0
    mutations_to_apply: list[tuple[float, dict]] = []

    for record in records:
        rr_type = record.get("type")
        ts = record.get("timestamp", 0)

        if not ts:
            continue

        data = _decompress_record_data(record)
        if data is None:
            continue

        if rr_type == 2 and ts <= target_ts_ms:
            if ts >= best_snapshot_ts:
                best_snapshot = data
                best_snapshot_ts = ts
                mutations_to_apply = []  # reset — only need mutations after this snapshot

        elif rr_type == 3 and ts <= target_ts_ms and ts > best_snapshot_ts:
            source = data.get("source")
            if source == 0:  # Mutation source
                mutations_to_apply.append((ts, data))

    if not best_snapshot:
        return ""

    # Build node map from the full snapshot
    root_node = best_snapshot.get("node", {})
    if isinstance(root_node, str):
        try:
            root_node = json.loads(root_node)
        except (json.JSONDecodeError, ValueError):
            return ""
    if not isinstance(root_node, dict):
        return ""

    node_map = _build_node_map(root_node)
    root_id = root_node.get("id")
    if root_id is None:
        return ""

    # Apply mutations in chronological order
    mutations_to_apply.sort(key=lambda x: x[0])
    for _ts, mut_data in mutations_to_apply:
        _apply_mutations(node_map, mut_data)

    # Convert back to markdown
    md = _node_map_to_markdown(node_map, root_id)
    md = _clean_markdown(md)

    return md[:15000]  # cap at 15k chars like Type 2 snapshots


def _extract_recording_signals(snapshot_lines: list[str]) -> list[dict]:
    """
    Extract network requests and console logs from rrweb recording data.

    PostHog stores these as rrweb type 6 (Plugin) events inside the session
    recording blobs. The events API only returns high-level events like
    $pageview and $exception (with generic messages). The REAL detailed data
    — specific HTTP endpoints, status codes, console error messages like
    "Delete account failed" — lives here.

    Returns list of dicts:
        {"type": "network_error", "timestamp": str, "method": str,
         "url": str, "status_code": int, "duration_ms": float}
        {"type": "console_error", "timestamp": str, "level": str,
         "message": str}
    """
    records = _parse_rrweb_records(snapshot_lines)
    signals: list[dict] = []

    for record in records:
        rr_type = record.get("type")
        timestamp = record.get("timestamp", 0)

        # Type 6 = Plugin event (console, network, etc.)
        if rr_type == 6:
            data = record.get("data", {})
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except (json.JSONDecodeError, ValueError):
                    continue
            if not isinstance(data, dict):
                continue

            plugin = data.get("plugin", "")
            payload = data.get("payload", {})

            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (json.JSONDecodeError, ValueError):
                    continue
            if not isinstance(payload, dict):
                continue

            ts_iso = _rrweb_ts_to_iso(timestamp)

            # ── Network requests (rrweb/network@1 plugin) ──────────
            if "network" in plugin.lower() or "rrweb/network" in plugin:
                requests_list = payload.get("requests", [])
                if isinstance(requests_list, list):
                    for req in requests_list:
                        if not isinstance(req, dict):
                            continue
                        status = req.get("status")
                        if status and int(str(status)) >= 400:
                            method = req.get("method", "").upper() or "GET"
                            url = req.get("url", "") or req.get("name", "")
                            duration = req.get("duration", 0)
                            req_ts = req.get("timestamp") or req.get("startTime")
                            req_ts_iso = _rrweb_ts_to_iso(req_ts) if req_ts else ts_iso
                            # Extract response/request bodies if available
                            resp_body = ""
                            req_body = ""
                            response = req.get("response") or {}
                            if isinstance(response, dict):
                                body = response.get("body", "")
                                if body and isinstance(body, str):
                                    resp_body = body[:500]
                                elif body and isinstance(body, dict):
                                    resp_body = json.dumps(body)[:500]
                            request_data = req.get("request") or {}
                            if isinstance(request_data, dict):
                                body = request_data.get("body", "")
                                if body and isinstance(body, str):
                                    req_body = body[:500]
                                elif body and isinstance(body, dict):
                                    req_body = json.dumps(body)[:500]

                            signals.append({
                                "type": "network_error",
                                "timestamp": req_ts_iso,
                                "method": method,
                                "url": url[:500],
                                "status_code": int(str(status)),
                                "duration_ms": float(duration) if duration else 0,
                                "response_body": resp_body,
                                "request_body": req_body,
                            })

            # ── Console logs (rrweb/console@1 plugin) ──────────────
            elif "console" in plugin.lower() or "rrweb/console" in plugin:
                level = payload.get("level", "log")
                traces = payload.get("payload", [])  # console args
                if isinstance(traces, list):
                    parts = []
                    for part in traces:
                        if isinstance(part, str):
                            parts.append(part)
                        elif isinstance(part, dict):
                            # Serialized objects/errors
                            parts.append(str(part.get("message", "") or part.get("text", "") or json.dumps(part)[:200]))
                        else:
                            parts.append(str(part)[:200])
                    message = " ".join(parts)[:500]
                elif isinstance(traces, str):
                    message = traces[:500]
                else:
                    message = str(traces)[:500]

                if level in ("error", "warn", "assert") and message:
                    signals.append({
                        "type": "console_error",
                        "timestamp": ts_iso,
                        "level": level,
                        "message": message,
                    })

        # Also check for custom event type patterns PostHog may use
        # Some PostHog versions embed network data in type 5 (custom events)
        elif rr_type == 5:
            data = record.get("data", {})
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except (json.JSONDecodeError, ValueError):
                    continue
            if not isinstance(data, dict):
                continue

            tag = data.get("tag", "")
            payload = data.get("payload", {})
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (json.JSONDecodeError, ValueError):
                    continue

            ts_iso = _rrweb_ts_to_iso(timestamp)

            # PostHog network capture via custom event
            if isinstance(payload, dict):
                requests_list = payload.get("requests", [])
                if isinstance(requests_list, list):
                    for req in requests_list:
                        if not isinstance(req, dict):
                            continue
                        status = req.get("status") or req.get("statusCode")
                        if status and int(str(status)) >= 400:
                            method = req.get("method", "").upper() or "GET"
                            url = req.get("url", "") or req.get("name", "")
                            duration = req.get("duration", 0)
                            req_ts = req.get("timestamp") or req.get("startTime")
                            req_ts_iso = _rrweb_ts_to_iso(req_ts) if req_ts else ts_iso
                            # Extract response/request bodies if available
                            resp_body = ""
                            req_body = ""
                            response = req.get("response") or {}
                            if isinstance(response, dict):
                                body = response.get("body", "")
                                if body and isinstance(body, str):
                                    resp_body = body[:500]
                                elif body and isinstance(body, dict):
                                    resp_body = json.dumps(body)[:500]
                            request_data = req.get("request") or {}
                            if isinstance(request_data, dict):
                                body = request_data.get("body", "")
                                if body and isinstance(body, str):
                                    req_body = body[:500]
                                elif body and isinstance(body, dict):
                                    req_body = json.dumps(body)[:500]
                            signals.append({
                                "type": "network_error",
                                "timestamp": req_ts_iso,
                                "method": method,
                                "url": url[:500],
                                "status_code": int(str(status)),
                                "duration_ms": float(duration) if duration else 0,
                                "response_body": resp_body,
                                "request_body": req_body,
                            })

    if signals:
        net_count = sum(1 for s in signals if s["type"] == "network_error")
        console_count = sum(1 for s in signals if s["type"] == "console_error")
        logger.info(f"Recording signals: {net_count} network errors, {console_count} console errors")

    return signals


def _extract_texts_from_snapshot_data(snapshot_lines: list[str]) -> list[dict]:
    """
    Parse rrweb JSONL snapshot data and extract structured markdown per page view.

    Returns list of {
        "text": str,          # structured markdown of the page
        "page": str,          # URL
        "timestamp": str,     # ISO 8601
        "is_markdown": True   # flag for downstream consumers
    }

    Each FullSnapshot produces one markdown document representing the full page.
    Incremental mutations between snapshots are collected into a separate
    "changes" document per page so the AI sees what changed dynamically.
    """
    records = _parse_rrweb_records(snapshot_lines)

    current_url = ""
    page_snapshots: list[dict] = []  # final output
    # Track incremental text changes between full snapshots
    incremental_texts: list[str] = []
    last_snapshot_url = ""

    for record in records:
        rr_type = record.get("type")
        timestamp = record.get("timestamp", 0)

        data = _decompress_record_data(record)
        if data is None:
            continue

        # ── Meta record: track current URL ────────────────────────────
        if rr_type == 4:
            new_url = data.get("href", "")
            if new_url and new_url != current_url:
                # URL changed — flush any pending incremental texts
                if incremental_texts and last_snapshot_url:
                    changes_md = "\n".join(incremental_texts)
                    if len(changes_md.strip()) >= 10:
                        page_snapshots.append({
                            "text": f"## Dynamic changes on page\n\n{_clean_markdown(changes_md)}",
                            "page": last_snapshot_url,
                            "timestamp": _rrweb_ts_to_iso(timestamp),
                            "is_markdown": True,
                        })
                    incremental_texts = []
                current_url = new_url
            continue

        # ── FullSnapshot: convert entire DOM to markdown ──────────────
        if rr_type == 2:
            # Flush pending incremental texts from previous snapshot
            if incremental_texts and last_snapshot_url:
                changes_md = "\n".join(incremental_texts)
                if len(changes_md.strip()) >= 10:
                    page_snapshots.append({
                        "text": f"## Dynamic changes on page\n\n{_clean_markdown(changes_md)}",
                        "page": last_snapshot_url,
                        "timestamp": _rrweb_ts_to_iso(timestamp),
                        "is_markdown": True,
                    })
                incremental_texts = []

            node = data.get("node", {})
            if isinstance(node, str):
                try:
                    node = json.loads(node)
                except (json.JSONDecodeError, ValueError):
                    node = {}
            if isinstance(node, dict) and node:
                md = _rrweb_node_to_markdown(node)
                md = _clean_markdown(md)
                if len(md) >= 20:  # minimum meaningful content
                    page_snapshots.append({
                        "text": md[:15000],  # cap at 15k chars per snapshot
                        "page": current_url,
                        "timestamp": _rrweb_ts_to_iso(timestamp),
                        "is_markdown": True,
                    })
                    last_snapshot_url = current_url
            continue

        # ── IncrementalSnapshot: capture mutations ────────────────────
        if rr_type == 3:
            source = data.get("source")
            if source == 0:  # Mutation
                # Newly added nodes
                adds = data.get("adds") or []
                for add in adds:
                    if isinstance(add, str):
                        try:
                            add = json.loads(add)
                        except (json.JSONDecodeError, ValueError):
                            continue
                    if not isinstance(add, dict):
                        continue
                    add_node = add.get("node", {})
                    if isinstance(add_node, str):
                        try:
                            add_node = json.loads(add_node)
                        except (json.JSONDecodeError, ValueError):
                            continue
                    if isinstance(add_node, dict):
                        md = _rrweb_node_to_markdown(add_node)
                        md = md.strip()
                        if md and len(md) >= 4:
                            incremental_texts.append(md[:2000])

                # Text content changes
                text_mutations = data.get("texts") or []
                for tm in text_mutations:
                    if isinstance(tm, dict):
                        val = (tm.get("value") or "").strip()
                        if val and len(val) >= 4:
                            incremental_texts.append(val[:500])

    # Flush any remaining incremental texts
    if incremental_texts and last_snapshot_url:
        changes_md = "\n".join(incremental_texts)
        if len(changes_md.strip()) >= 10:
            page_snapshots.append({
                "text": f"## Dynamic changes on page\n\n{_clean_markdown(changes_md)}",
                "page": last_snapshot_url,
                "timestamp": _rrweb_ts_to_iso(0),
                "is_markdown": True,
            })

    # Deduplicate: same page markdown only needs to appear once
    # (multiple FullSnapshots of the same page with identical content)
    seen: set[tuple[str, str]] = set()
    deduped: list[dict] = []
    for item in page_snapshots:
        # Use first 500 chars of text + page as dedup key
        key = (item["text"][:500], item["page"])
        if key not in seen:
            seen.add(key)
            deduped.append(item)

    pages = {d["page"] for d in deduped}
    logger.info(
        f"DOM markdown extraction: {len(page_snapshots)} snapshots → "
        f"{len(deduped)} unique across {len(pages)} pages"
    )
    return deduped


def _extract_element_info(props: dict) -> tuple[str, str, str]:
    """
    Extract element tag, text, and CSS selector from PostHog event properties.
    Returns (tag_name, el_text, css_selector).
    """
    tag_name = ""
    el_text = ""
    css_selector = ""

    # Method 1: $elements array
    elements = props.get("$elements") or props.get("elements") or []
    if elements and isinstance(elements, list) and len(elements) > 0:
        first_el = elements[0] if isinstance(elements[0], dict) else {}
        tag_name = first_el.get("tag_name", "")
        el_text = (first_el.get("$el_text", "") or first_el.get("text", ""))[:100]
        attrs = first_el.get("attributes", {}) or first_el.get("attr", {})
        el_id = attrs.get("attr__id", "") or first_el.get("attr_id", "")
        el_class = attrs.get("attr__class", "") or first_el.get("attr_class", "")
        if el_id:
            css_selector = f"#{el_id}"
        elif el_class:
            css_selector = f".{el_class.split()[0]}" if el_class else ""
        if not el_text and len(elements) > 1:
            for el in elements[1:]:
                if isinstance(el, dict):
                    t = (el.get("$el_text", "") or el.get("text", ""))[:100]
                    if t:
                        el_text = t
                        break

    # Method 2: Top-level properties
    if not tag_name:
        tag_name = props.get("$element_tag", "") or props.get("tag_name", "")
    if not el_text:
        el_text = (props.get("$el_text", "") or props.get("element_text", ""))[:100]
    if not css_selector:
        css_selector = props.get("$element_selector", "") or props.get("$css_selector", "")

    # Method 3: $elements_chain string
    if not tag_name and not el_text:
        chain = props.get("$elements_chain", "")
        if chain and isinstance(chain, str):
            first_part = chain.split(";")[0] if ";" in chain else chain
            if "." in first_part:
                tag_name = first_part.split(".")[0]
            elif ":" in first_part:
                tag_name = first_part.split(":")[0]
            else:
                tag_name = first_part
            if "text=" in chain:
                text_start = chain.index("text=") + 5
                text_end = chain.find(";", text_start)
                el_text = chain[text_start:text_end if text_end > 0 else None].strip("'\"")[:100]

    return tag_name.strip(), el_text.strip(), css_selector.strip()


def _extract_form_info(props: dict, elements: list) -> tuple[str, str, str, str]:
    """
    Extract form-related element info from PostHog event properties.
    Returns (element_type, element_name, element_value, validation_message).
    """
    element_type = ""
    element_name = ""
    element_value = ""
    validation_message = ""

    # From $elements array
    if elements and isinstance(elements, list) and len(elements) > 0:
        first_el = elements[0] if isinstance(elements[0], dict) else {}
        attrs = first_el.get("attributes", {}) or first_el.get("attr", {})
        element_type = attrs.get("attr__type", "") or first_el.get("type", "")
        element_name = attrs.get("attr__name", "") or attrs.get("attr__id", "") or first_el.get("name", "")
        element_value = attrs.get("attr__value", "")
        # Mask passwords
        if element_type == "password" and element_value:
            element_value = "***"
        # Placeholder can hint at expected input
        placeholder = attrs.get("attr__placeholder", "")
        if placeholder and not element_name:
            element_name = f"[placeholder={placeholder}]"
        # aria-invalid indicates validation error
        if attrs.get("attr__aria-invalid") == "true":
            validation_message = attrs.get("attr__aria-errormessage", "") or "Field marked invalid"

    # From top-level properties
    if not element_type:
        element_type = props.get("$element_type", "") or props.get("element_type", "")
    if not element_name:
        element_name = props.get("$element_name", "") or props.get("element_name", "")
    if not element_value:
        element_value = props.get("$element_value", "")
        if element_type == "password" and element_value:
            element_value = "***"

    return element_type.strip(), element_name.strip(), element_value[:50].strip(), validation_message.strip()


def _strip_sensitive_url(url: str) -> str:
    """Strip access tokens, auth fragments, and sensitive query params from URLs.

    PostHog captures the full URL including OAuth callback fragments like:
      /auth/callback/#access_token=eyJhbG...&refresh_token=...
    These contain JWTs and must not appear in issue descriptions or repro steps.
    """
    if not url:
        return url
    # Strip fragment (everything after #) if it contains token-like params
    if "#" in url:
        base, fragment = url.split("#", 1)
        sensitive_keys = ("access_token", "token", "refresh_token", "id_token",
                          "provider_token", "code", "secret", "key")
        if any(k in fragment.lower() for k in sensitive_keys):
            return base
    # Strip sensitive query params
    if "?" in url:
        from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
        parsed = urlparse(url)
        params = parse_qs(parsed.query, keep_blank_values=True)
        sensitive_keys = {"access_token", "token", "refresh_token", "id_token",
                          "provider_token", "code", "secret", "key", "api_key",
                          "password", "session_id"}
        filtered = {k: v for k, v in params.items() if k.lower() not in sensitive_keys}
        clean_query = urlencode(filtered, doseq=True)
        return urlunparse(parsed._replace(query=clean_query))
    return url


def _normalise_event(raw: dict) -> NormalizedEvent:
    """Convert a single PostHog event dict into a NormalizedEvent."""
    props = raw.get("properties", {})
    event_name = raw.get("event", "unknown")
    timestamp = raw.get("timestamp", "")
    url = _strip_sensitive_url(props.get("$current_url", ""))
    pathname = props.get("$pathname", "")

    elements = props.get("$elements") or props.get("elements") or []
    tag, text, sel = _extract_element_info(props)
    el_type, el_name, el_value, validation_msg = _extract_form_info(props, elements)

    # Viewport / scroll info for layout-related UX bugs
    viewport_w = props.get("$viewport_width") or props.get("$screen_width")
    viewport_h = props.get("$viewport_height") or props.get("$screen_height")
    scroll_y = props.get("$scroll_y") or props.get("$scrollY")

    # Map PostHog event names → canonical types
    etype = "custom"
    error_msg = ""
    error_type = ""
    status_code = None
    method = ""
    endpoint = ""
    form_action = ""

    if event_name == "$pageview":
        etype = "pageview"
    elif event_name == "$pageleave":
        etype = "pageleave"
    elif event_name == "$autocapture":
        raw_event_type = props.get("$event_type", "click")
        # Distinguish form-related interactions
        if raw_event_type == "submit":
            etype = "submit"
            form_action = props.get("$form_action", "")
        elif raw_event_type == "change":
            etype = "input"
        elif raw_event_type == "focus":
            etype = "focus"
        elif raw_event_type == "blur":
            etype = "blur"
        elif tag in ("input", "textarea", "select") and raw_event_type == "click":
            etype = "focus"  # clicking on an input is effectively focusing
        else:
            etype = raw_event_type  # click, etc.
        # Detect dead clicks: click on non-interactive elements
        if etype == "click" and tag and tag not in ("a", "button", "input", "select", "textarea", "label", "summary"):
            if not sel or ("btn" not in sel.lower() and "button" not in sel.lower() and "link" not in sel.lower()):
                etype = "dead_click"
    elif event_name == "$rageclick":
        etype = "rage_click"
    elif event_name == "$exception":
        etype = "error"
        error_msg = props.get("$exception_message", "Unknown error")[:500]
        error_type = props.get("$exception_type", "")
    elif event_name == "$web_vitals":
        etype = "custom"  # could track LCP/CLS/FID later
    else:
        sc = props.get("$status_code", "")
        if sc and int(str(sc)) >= 400:
            etype = "network_error"
            status_code = int(str(sc))
            method = props.get("$method", "")
            endpoint = props.get("$url", url)

    return NormalizedEvent(
        timestamp=timestamp,
        event_type=etype,
        url=url,
        pathname=pathname,
        tag_name=tag,
        element_text=text,
        css_selector=sel,
        element_type=el_type,
        element_name=el_name,
        element_value=el_value,
        form_action=form_action,
        validation_message=validation_msg,
        error_message=error_msg,
        error_type=error_type,
        status_code=status_code,
        method=method,
        endpoint=endpoint,
        viewport_width=int(viewport_w) if viewport_w else None,
        viewport_height=int(viewport_h) if viewport_h else None,
        scroll_y=int(scroll_y) if scroll_y else None,
        raw=raw,
    )


class PostHogConnector(SessionConnector):
    provider = "posthog"

    def __init__(self, api_key: str, project_id: str, host: str = "eu.posthog.com", **_kw):
        self.api_key = api_key
        self.project_id = project_id
        self.host = host

    # ── public ────────────────────────────────────────────────────────────

    async def fetch_sessions(
        self,
        since: datetime,
        limit: int = 50,
    ) -> list[NormalizedSession]:
        from app.utils.retry import with_retries

        base_url = f"https://{self.host}/api/projects/{self.project_id}/sessions"
        headers = {"Authorization": f"Bearer {self.api_key}"}

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                params = {
                    "date_from": since.strftime("%Y-%m-%d"),
                    "limit": limit,
                    "order_by": "-start_time",
                }
                resp = await with_retries(
                    lambda: client.get(base_url, headers=headers, params=params),
                    max_retries=3,
                    base_delay=1.0,
                    retryable_exceptions=(httpx.HTTPError, ConnectionError, TimeoutError),
                    operation="PostHog fetch sessions",
                )
                if resp.status_code == 404:
                    return await self._sessions_from_events(client, since, limit)
                resp.raise_for_status()
                raw_sessions = resp.json().get("results", [])
            except httpx.HTTPError as exc:
                logger.warning(f"PostHog sessions API error, falling back: {exc}")
                return await self._sessions_from_events(client, since, limit)

        return [self._normalise_session(s) for s in raw_sessions]

    def build_replay_url(self, session_id: str) -> str:
        return f"https://{self.host}/replay/{session_id}"

    async def _fetch_recording_lines(self, session_id: str) -> list[str]:
        """
        Fetch raw rrweb JSONL lines from PostHog recording blobs.
        Shared by fetch_session_dom_texts() and fetch_session_recording_signals().
        """
        from app.utils.retry import with_retries

        headers = {"Authorization": f"Bearer {self.api_key}"}

        async with httpx.AsyncClient(timeout=60) as client:
            snapshot_url = (
                f"https://{self.host}/api/environments/{self.project_id}"
                f"/session_recordings/{session_id}/snapshots"
            )
            try:
                resp = await with_retries(
                    lambda: client.get(snapshot_url, headers=headers, params={"blob_v2": "true"}),
                    max_retries=2,
                    base_delay=1.5,
                    retryable_exceptions=(httpx.HTTPError, ConnectionError, TimeoutError),
                    operation=f"PostHog fetch recording {session_id[:12]}",
                )
                if resp.status_code in (404, 405):
                    snapshot_url_fallback = (
                        f"https://{self.host}/api/projects/{self.project_id}"
                        f"/session_recordings/{session_id}/snapshots"
                    )
                    resp = await with_retries(
                        lambda: client.get(snapshot_url_fallback, headers=headers, params={"blob_v2": "true"}),
                        max_retries=2,
                        base_delay=1.5,
                        retryable_exceptions=(httpx.HTTPError, ConnectionError, TimeoutError),
                        operation=f"PostHog fetch recording fallback {session_id[:12]}",
                    )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning(f"PostHog snapshot list error for {session_id}: {exc}")
                return []

            blob_data = resp.json()
            logger.debug(f"PostHog snapshot response type={type(blob_data).__name__}, "
                         f"keys={list(blob_data.keys()) if isinstance(blob_data, dict) else 'N/A'}")

            blob_keys: list[str] = []

            if isinstance(blob_data, dict):
                sources = blob_data.get("sources", [])
                for s in sources:
                    if isinstance(s, dict):
                        bk = s.get("blob_key", "")
                        if bk and s.get("source") == "blob_v2":
                            blob_keys.append(bk)
                    elif isinstance(s, str) and s:
                        blob_keys.append(s)

                if not blob_keys and "snapshot_data_by_window_id" in blob_data:
                    lines: list[str] = []
                    for window_id, records in blob_data["snapshot_data_by_window_id"].items():
                        if isinstance(records, list):
                            for rec in records:
                                lines.append(json.dumps(rec) if isinstance(rec, dict) else str(rec))
                    return lines

            elif isinstance(blob_data, list):
                return [json.dumps(item) if isinstance(item, dict) else str(item) for item in blob_data]

            if not blob_keys:
                logger.debug(f"No blob keys found for session {session_id}. "
                             f"Response preview: {str(blob_data)[:300]}")
                return []

            logger.info(f"Found {len(blob_keys)} blob keys for session {session_id}")

            # Fetch ALL blobs in parallel — network errors / console logs from
            # the recording can appear late in the session (e.g. account deletion
            # at minute 27/30). PostHog blob_v2 API allows max 20 keys per
            # request, so we chunk in groups of 10 and fetch in parallel.
            import asyncio as _asyncio

            chunk_size = 10
            chunks: list[tuple[str, str]] = []
            for chunk_start in range(0, len(blob_keys), chunk_size):
                chunk_end = min(chunk_start + chunk_size, len(blob_keys)) - 1
                chunks.append((blob_keys[chunk_start], blob_keys[chunk_end]))

            async def _fetch_chunk(start_key: str, end_key: str) -> list[str]:
                try:
                    blob_resp = await client.get(
                        snapshot_url,
                        headers=headers,
                        params={
                            "source": "blob_v2",
                            "start_blob_key": start_key,
                            "end_blob_key": end_key,
                        },
                    )
                    blob_resp.raise_for_status()
                    content = blob_resp.text
                    logger.debug(
                        f"Fetched blob range {start_key}-{end_key} for {session_id}: "
                        f"{len(content)} bytes"
                    )
                    return content.strip().split("\n") if content.strip() else []
                except httpx.HTTPError as exc:
                    logger.warning(
                        f"PostHog blob fetch error for range {start_key}-{end_key} "
                        f"session {session_id}: {exc}"
                    )
                    return []

            # Fetch all chunks in parallel
            chunk_results = await _asyncio.gather(*[
                _fetch_chunk(sk, ek) for sk, ek in chunks
            ])

            all_lines: list[str] = []
            for lines in chunk_results:
                all_lines.extend(lines)

            logger.info(
                f"Fetched {len(all_lines)} lines from {len(chunks)} chunks "
                f"({len(blob_keys)} blobs) for session {session_id}"
            )

            return all_lines

    async def fetch_session_dom_texts(self, session_id: str) -> list[dict]:
        """
        Fetch rrweb recording snapshots for a session and extract visible DOM text.
        Returns list of {"text": str, "page": str, "timestamp": str}.
        """
        all_lines = await self._fetch_recording_lines(session_id)
        if not all_lines:
            return []

        texts = _extract_texts_from_snapshot_data(all_lines)
        logger.info(
            f"Extracted {len(texts)} DOM texts from session {session_id}"
        )
        return texts

    async def fetch_session_recording_signals(self, session_id: str) -> list[dict]:
        """
        Extract network errors and console logs from rrweb recording data.

        PostHog's events API only returns high-level events ($pageview, $exception)
        with generic messages like "Unknown error". The REAL detailed data — specific
        HTTP endpoints + status codes, console logs like "Delete account failed" —
        lives in the rrweb recording blobs (type 6 plugin events).

        Returns list of:
            {"type": "network_error", "timestamp": str, "method": str,
             "url": str, "status_code": int, "duration_ms": float}
            {"type": "console_error", "timestamp": str, "level": str,
             "message": str}
        """
        all_lines = await self._fetch_recording_lines(session_id)
        if not all_lines:
            return []
        return _extract_recording_signals(all_lines)

    async def fetch_session_dom_and_signals(
        self, session_id: str
    ) -> tuple[list[dict], list[dict]]:
        """
        Fetch BOTH DOM texts and recording signals in a single API call
        (avoids fetching blobs twice).

        Returns (dom_texts, recording_signals).
        """
        all_lines = await self._fetch_recording_lines(session_id)
        if not all_lines:
            return [], []

        dom_texts = _extract_texts_from_snapshot_data(all_lines)
        signals = _extract_recording_signals(all_lines)

        logger.info(
            f"Extracted {len(dom_texts)} DOM texts + "
            f"{len(signals)} recording signals from session {session_id}"
        )
        return dom_texts, signals

    @staticmethod
    def enrich_session_events(
        session: NormalizedSession,
        recording_signals: list[dict],
    ) -> NormalizedSession:
        """
        Enrich a session's events with network errors and console logs
        extracted from the recording data.

        The PostHog events API often only returns generic $exception events
        with "Unknown error". The recording data contains the REAL details:
        specific HTTP endpoints, status codes, and console error messages.

        This method:
        1. Adds network_error events (HTTP 4xx/5xx) not already in events
        2. Replaces "Unknown error" console errors with detailed messages
        3. Adds new console_error events from recording that were missed

        Returns a new NormalizedSession with enriched events.
        """
        if not recording_signals:
            return session

        # Index existing events by type+timestamp for dedup
        existing_net_keys: set[str] = set()
        existing_error_ts: dict[str, int] = {}  # timestamp → index in events list

        events = list(session.events)  # copy

        for i, ev in enumerate(events):
            if ev.event_type == "network_error":
                key = f"{ev.method}:{ev.endpoint}:{ev.status_code}"
                existing_net_keys.add(key)
            elif ev.event_type == "error":
                existing_error_ts[ev.timestamp[:19]] = i  # truncate to seconds

        new_events: list[NormalizedEvent] = []

        for sig in recording_signals:
            if sig["type"] == "network_error":
                key = f"{sig['method']}:{sig['url']}:{sig['status_code']}"
                if key not in existing_net_keys:
                    # Determine page URL from nearest pageview
                    page_url = ""
                    sig_ts = sig["timestamp"]
                    for ev in reversed(events):
                        if ev.event_type == "pageview" and ev.timestamp <= sig_ts:
                            page_url = ev.url
                            break

                    new_events.append(NormalizedEvent(
                        timestamp=sig["timestamp"],
                        event_type="network_error",
                        url=page_url,
                        pathname="",
                        method=sig["method"],
                        endpoint=sig["url"],
                        status_code=sig["status_code"],
                        error_message=f"HTTP {sig['status_code']}",
                    ))
                    existing_net_keys.add(key)
                    logger.debug(
                        f"Enriched: +network_error {sig['method']} {sig['url'][:60]} → {sig['status_code']}"
                    )

            elif sig["type"] == "console_error":
                sig_ts_key = sig["timestamp"][:19]
                msg = sig.get("message", "")

                if not msg or len(msg) < 5:
                    continue

                # Check if this replaces an "Unknown error" at same timestamp
                matched_idx = None
                for ts_key, idx in existing_error_ts.items():
                    # Match within same second
                    if ts_key == sig_ts_key:
                        matched_idx = idx
                        break

                if matched_idx is not None:
                    existing_ev = events[matched_idx]
                    # Replace generic message with detailed one
                    if existing_ev.error_message in ("Unknown error", "", "Error"):
                        events[matched_idx] = NormalizedEvent(
                            timestamp=existing_ev.timestamp,
                            event_type="error",
                            url=existing_ev.url,
                            pathname=existing_ev.pathname,
                            error_message=msg,
                            error_type=existing_ev.error_type or "Error",
                            tag_name=existing_ev.tag_name,
                            element_text=existing_ev.element_text,
                            css_selector=existing_ev.css_selector,
                            viewport_width=existing_ev.viewport_width,
                            viewport_height=existing_ev.viewport_height,
                            raw=existing_ev.raw,
                        )
                        logger.debug(
                            f"Enriched: replaced 'Unknown error' with '{msg[:80]}'"
                        )
                else:
                    # New console error not in events — add it
                    # Skip purely informational messages
                    msg_lower = msg.lower()
                    if any(skip in msg_lower for skip in (
                        "[posthog]", "tracking event", "debug", "[info]",
                    )):
                        continue

                    page_url = ""
                    for ev in reversed(events):
                        if ev.event_type == "pageview" and ev.timestamp <= sig["timestamp"]:
                            page_url = ev.url
                            break

                    new_events.append(NormalizedEvent(
                        timestamp=sig["timestamp"],
                        event_type="error",
                        url=page_url,
                        pathname="",
                        error_message=msg,
                        error_type="ConsoleError",
                    ))
                    logger.debug(f"Enriched: +console_error '{msg[:80]}'")

        if new_events:
            events.extend(new_events)
            events.sort(key=lambda ev: ev.timestamp or "")
            logger.info(
                f"Session {session.id}: enriched with {len(new_events)} events from recording "
                f"(total: {len(events)} events)"
            )

        return NormalizedSession(
            id=session.id,
            distinct_id=session.distinct_id,
            start_time=session.start_time,
            end_time=session.end_time,
            events=events,
            replay_url=session.replay_url,
            metadata=session.metadata,
        )

    # ── private ───────────────────────────────────────────────────────────

    def _normalise_session(self, raw: dict) -> NormalizedSession:
        events = [_normalise_event(e) for e in raw.get("events", [])]
        # Sort events by timestamp — PostHog can return them out of order
        events.sort(key=lambda ev: ev.timestamp or "")
        return NormalizedSession(
            id=raw.get("id", ""),
            distinct_id=raw.get("distinct_id", ""),
            start_time=raw.get("start_time", ""),
            end_time=raw.get("end_time", ""),
            events=events,
            replay_url=self.build_replay_url(raw.get("id", "")),
            metadata={"provider": "posthog"},
        )

    async def _sessions_from_events(
        self,
        client: httpx.AsyncClient,
        since: datetime,
        limit: int,
    ) -> list[NormalizedSession]:
        """Build sessions by grouping /events by $session_id."""
        base_url = f"https://{self.host}/api/projects/{self.project_id}/events"
        headers = {"Authorization": f"Bearer {self.api_key}"}

        all_events: list[dict] = []
        for event_type in ["$pageview", "$pageleave", "$autocapture", "$rageclick", "$exception"]:
            try:
                params = {"event": event_type, "after": since.isoformat(), "limit": 500}
                resp = await client.get(base_url, headers=headers, params=params)
                resp.raise_for_status()
                all_events.extend(resp.json().get("results", []))
            except httpx.HTTPError as exc:
                logger.warning(f"PostHog event fetch error for {event_type}: {exc}")

        # Group by session
        seen_uuids: set[str] = set()
        session_map: dict[str, list[dict]] = {}
        for ev in all_events:
            uid = ev.get("uuid") or ev.get("id", "")
            if uid and uid in seen_uuids:
                continue
            if uid:
                seen_uuids.add(uid)
            sid = ev.get("properties", {}).get("$session_id")
            if sid:
                session_map.setdefault(sid, []).append(ev)

        sessions: list[NormalizedSession] = []
        for sid, events in session_map.items():
            events.sort(key=lambda e: e.get("timestamp", ""))
            if len(events) < 2:
                continue
            norm_events = [_normalise_event(e) for e in events]
            sessions.append(NormalizedSession(
                id=sid,
                distinct_id=events[0].get("distinct_id", ""),
                start_time=events[0].get("timestamp", ""),
                end_time=events[-1].get("timestamp", ""),
                events=norm_events,
                replay_url=self.build_replay_url(sid),
                metadata={"provider": "posthog"},
            ))

        sessions.sort(key=lambda s: len(s.events), reverse=True)
        return sessions[:limit]
