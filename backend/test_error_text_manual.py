"""
Manual test script for error text detection pipeline.

Run from the backend/ directory:
    python test_error_text_manual.py

This fetches a specific PostHog session, runs the full text collection
pipeline (events + DOM snapshots), and reports what it finds — without
sending anything to OpenAI.

This helps debug whether we're collecting the right text data before
AI classification.
"""

import asyncio
import json
import re
import sys
import os

# Add parent dir to path so we can import app modules
sys.path.insert(0, os.path.dirname(__file__))

from app.connectors.posthog import PostHogConnector, _extract_texts_from_snapshot_data
from app.connectors.base import NormalizedSession

# ── Configuration ─────────────────────────────────────────────────────────────
SESSION_ID = "019c95b8-6216-7427-9aef-25390c259fdd"
PROJECT_ID = "25118"
API_KEY = "phx_jFKvI5EyZ4K1X0tZjolVfLs1qJdqyZpFH2aBMPqyTa2dJpg"
HOST = "eu.posthog.com"

# Error hint regex (same as in session_analysis_service.py)
_ERROR_HINT_PATTERNS = re.compile(
    r"(error|fail|invalid|denied|expired|refused|unavailable|forbidden"
    r"|not found|timed?\s*out|unauthorized|exception|crash|broke"
    r"|something went wrong|try again|oops|unable to|cannot|couldn.t"
    r"|unexpected|sorry|problem|issue|warning|alert|critical"
    r"|could not|failed to|unable|rejected|blocked|disabled"
    r"|no access|no permission|not allowed|bad request|server error"
    r"|500|404|403|401|network|offline|connection|reset)",
    re.IGNORECASE,
)


def print_section(title: str):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")


async def main():
    connector = PostHogConnector(
        api_key=API_KEY,
        project_id=PROJECT_ID,
        host=HOST,
    )

    # ── Step 1: Fetch session events ──────────────────────────────────────
    print_section("STEP 1: Fetching session events from PostHog")

    from datetime import datetime, timedelta, timezone
    since = datetime.now(timezone.utc) - timedelta(days=7)

    # We need to fetch events for this specific session
    import httpx
    headers = {"Authorization": f"Bearer {API_KEY}"}
    events_url = f"https://{HOST}/api/projects/{PROJECT_ID}/events"

    all_events = []
    async with httpx.AsyncClient(timeout=30) as client:
        for event_type in ["$pageview", "$pageleave", "$autocapture", "$rageclick", "$exception"]:
            try:
                params = {
                    "event": event_type,
                    "limit": 200,
                    "properties": json.dumps([
                        {"key": "$session_id", "value": SESSION_ID, "type": "event"}
                    ])
                }
                resp = await client.get(events_url, headers=headers, params=params)
                resp.raise_for_status()
                results = resp.json().get("results", [])
                session_events = [
                    e for e in results
                    if e.get("properties", {}).get("$session_id") == SESSION_ID
                ]
                print(f"  {event_type}: {len(session_events)} events")
                all_events.extend(session_events)
            except Exception as e:
                print(f"  {event_type}: ERROR - {e}")

    all_events.sort(key=lambda e: e.get("timestamp", ""))
    print(f"\n  Total events: {len(all_events)}")

    if not all_events:
        print("\n  WARNING: No events found! The session may have expired or the API key may not have access.")
        print("  Trying to build session from connector.fetch_sessions() instead...")
        sessions = await connector.fetch_sessions(since=since, limit=100)
        target = [s for s in sessions if s.id == SESSION_ID]
        if target:
            session = target[0]
            print(f"  Found session with {len(session.events)} events")
        else:
            print(f"  Session {SESSION_ID} not found in recent sessions.")
            print(f"  Available sessions: {[s.id[:16] for s in sessions[:10]]}")
            return
    else:
        # Build NormalizedSession from raw events
        from app.connectors.posthog import _normalise_event
        norm_events = [_normalise_event(e) for e in all_events]
        norm_events.sort(key=lambda ev: ev.timestamp or "")
        session = NormalizedSession(
            id=SESSION_ID,
            distinct_id=all_events[0].get("distinct_id", ""),
            start_time=all_events[0].get("timestamp", ""),
            end_time=all_events[-1].get("timestamp", ""),
            events=norm_events,
            replay_url=connector.build_replay_url(SESSION_ID),
            metadata={"provider": "posthog"},
        )

    # ── Print session timeline ────────────────────────────────────────────
    print_section("SESSION TIMELINE")
    for i, ev in enumerate(session.events):
        line = f"  [{i:3d}] {(ev.timestamp or '')[:19]} | {ev.event_type:15s}"
        if ev.url:
            line += f" | {ev.url[:60]}"
        if ev.element_text:
            line += f"\n        text: \"{ev.element_text[:80]}\""
        if ev.error_message:
            line += f"\n        ERROR: {ev.error_message[:100]}"
        if ev.validation_message:
            line += f"\n        VALIDATION: {ev.validation_message}"
        print(line)

    # ── Step 2: Collect text from events ──────────────────────────────────
    print_section("STEP 2: Text collected from EVENTS")
    event_texts = []
    seen_texts = set()
    for idx, ev in enumerate(session.events):
        page = ev.url or ev.pathname or ""
        if not page:
            continue

        texts_to_check = []
        if ev.element_text and len(ev.element_text) >= 4:
            texts_to_check.append(("element_text", ev.element_text))
        if ev.error_message and len(ev.error_message) >= 4:
            texts_to_check.append(("error_message", ev.error_message))
        if ev.validation_message and len(ev.validation_message) >= 4:
            texts_to_check.append(("validation_msg", ev.validation_message))

        raw_props = ev.raw.get("properties", {}) if ev.raw else {}
        el_text_raw = raw_props.get("$el_text", "")
        if el_text_raw and len(str(el_text_raw)) >= 4:
            texts_to_check.append(("$el_text", str(el_text_raw)[:200]))
        exc_msg = raw_props.get("$exception_message", "")
        if exc_msg and len(str(exc_msg)) >= 4:
            texts_to_check.append(("$exception_msg", str(exc_msg)[:200]))

        for source, text in texts_to_check:
            dedup_key = f"{text[:60].lower()}||{page.rstrip('/').lower()}"
            if dedup_key in seen_texts:
                continue
            seen_texts.add(dedup_key)
            is_error_like = bool(_ERROR_HINT_PATTERNS.search(text))
            event_texts.append({
                "text": text[:200],
                "page": page,
                "source": source,
                "event_index": idx,
                "event_type": ev.event_type,
                "is_error_like": is_error_like,
            })
            marker = " *** ERROR-LIKE ***" if is_error_like else ""
            print(f"  [{idx:3d}] [{source:15s}] {text[:100]}{marker}")
            print(f"         Page: {page[:80]}")

    print(f"\n  Total unique texts from events: {len(event_texts)}")
    error_like_from_events = [t for t in event_texts if t["is_error_like"]]
    print(f"  Error-like texts from events: {len(error_like_from_events)}")

    # ── Step 3: Fetch DOM snapshots ───────────────────────────────────────
    print_section("STEP 3: Fetching DOM snapshots from PostHog recording")

    # First, fetch raw blob data and inspect type=2 (FullSnapshot) records directly
    print("  --- Debug: inspecting raw blob records ---")
    try:
        debug_headers = {"Authorization": f"Bearer {API_KEY}"}
        async with httpx.AsyncClient(timeout=60) as debug_client:
            snap_url = (
                f"https://{HOST}/api/environments/{PROJECT_ID}"
                f"/session_recordings/{SESSION_ID}/snapshots"
            )
            # Get blob keys
            debug_resp = await debug_client.get(snap_url, headers=debug_headers, params={"blob_v2": "true"})
            blob_data = debug_resp.json()
            sources = blob_data.get("sources", [])
            blob_keys = []
            for s in sources:
                if isinstance(s, dict) and s.get("source") == "blob_v2":
                    blob_keys.append(s.get("blob_key", ""))
                elif isinstance(s, str) and s:
                    blob_keys.append(s)
            print(f"  Found {len(blob_keys)} blob keys")

            # Fetch ALL blob ranges
            if blob_keys:
                lines = []
                chunk_size = 10
                for ci in range(0, len(blob_keys), chunk_size):
                    start_key = blob_keys[ci]
                    end_key = blob_keys[min(ci + chunk_size - 1, len(blob_keys) - 1)]
                    blob_resp = await debug_client.get(
                        snap_url,
                        headers=debug_headers,
                        params={"source": "blob_v2", "start_blob_key": start_key, "end_blob_key": end_key},
                    )
                    content = blob_resp.text
                    chunk_lines = content.strip().split("\n") if content.strip() else []
                    lines.extend(chunk_lines)
                    print(f"  Chunk {ci//chunk_size}: {len(content)} bytes, {len(chunk_lines)} lines")
                print(f"  Total: {len(lines)} JSONL lines")

                # Parse all records, find type=2
                all_records = []
                type2_records = []
                for line_idx, line in enumerate(lines):
                    try:
                        parsed = json.loads(line.strip())
                        if isinstance(parsed, list):
                            for item in parsed:
                                if isinstance(item, dict):
                                    all_records.append(item)
                                    if item.get("type") == 2:
                                        type2_records.append({"line_idx": line_idx, "record": item})
                        elif isinstance(parsed, dict):
                            all_records.append(parsed)
                            if parsed.get("type") == 2:
                                type2_records.append({"line_idx": line_idx, "record": parsed})
                    except Exception:
                        pass

                print(f"  Total records: {len(all_records)}")
                type_counts = {}
                for r in all_records:
                    rt = r.get("type", -1)
                    type_counts[rt] = type_counts.get(rt, 0) + 1
                print(f"  Type counts: {type_counts}")
                print(f"  Found {len(type2_records)} FullSnapshot (type=2) records")

                # Dump type=2 records to file for deep inspection
                for i, t2 in enumerate(type2_records):
                    rec = t2["record"]
                    data = rec.get("data")
                    data_type = type(data).__name__
                    print(f"\n  --- FullSnapshot #{i} (line {t2['line_idx']}) ---")
                    print(f"    Record keys: {list(rec.keys())}")
                    print(f"    data type: {data_type}")
                    if isinstance(data, dict):
                        print(f"    data keys: {list(data.keys())}")
                        node = data.get("node")
                        if node is None:
                            print(f"    data['node'] is MISSING!")
                            print(f"    data preview: {repr(str(data)[:500])}")
                        elif isinstance(node, dict):
                            print(f"    node keys: {list(node.keys())}")
                            print(f"    node type={node.get('type')}, tagName={node.get('tagName')}")
                            children = node.get("childNodes", [])
                            print(f"    node childNodes count: {len(children)}")
                            if children:
                                for ci, ch in enumerate(children[:3]):
                                    if isinstance(ch, dict):
                                        print(f"      child[{ci}]: type={ch.get('type')}, tag={ch.get('tagName')}, keys={list(ch.keys())[:6]}")
                        elif isinstance(node, str):
                            print(f"    node is STRING, len={len(node)}, first 200: {repr(node[:200])}")
                        else:
                            print(f"    node is {type(node).__name__}: {repr(str(node)[:200])}")
                    elif isinstance(data, str):
                        print(f"    data is STRING, len={len(data)}, first 300: {repr(data[:300])}")
                    elif data is None:
                        print(f"    data is None!")
                    else:
                        print(f"    data is {data_type}: {repr(str(data)[:300])}")

                # Dump the first type=2 record to file
                if type2_records:
                    dump_path = os.path.join(os.path.dirname(__file__), "fullsnapshot_debug.json")
                    # Truncate very large node trees for the dump
                    rec_copy = json.loads(json.dumps(type2_records[0]["record"], default=str))
                    with open(dump_path, "w") as f:
                        json.dump(rec_copy, f, indent=2, default=str)
                    print(f"\n  ✅ First FullSnapshot record dumped to: {dump_path}")

    except Exception as e:
        import traceback
        print(f"  Debug error: {e}")
        traceback.print_exc()

    print("\n  --- Now calling connector.fetch_session_dom_texts() ---")
    try:
        dom_texts = await connector.fetch_session_dom_texts(SESSION_ID)
        print(f"  Structured page snapshots extracted: {len(dom_texts)}")

        # Dump ALL DOM texts as JSON for inspection
        dump_path = os.path.join(os.path.dirname(__file__), "dom_texts_dump.json")
        with open(dump_path, "w") as f:
            json.dump(dom_texts, f, indent=2, default=str)
        print(f"  ✅ JSON dump: {dump_path}")

        # Also dump each page's markdown to a readable file
        md_dump_path = os.path.join(os.path.dirname(__file__), "dom_markdown_dump.md")
        with open(md_dump_path, "w") as f:
            for i, item in enumerate(dom_texts):
                f.write(f"\n{'='*80}\n")
                f.write(f"## Page {i+1}: {item.get('page', 'unknown')}\n")
                f.write(f"**Timestamp:** {item.get('timestamp', 'N/A')}\n")
                f.write(f"**Is Markdown:** {item.get('is_markdown', False)}\n")
                f.write(f"**Length:** {len(item.get('text', ''))} chars\n")
                f.write(f"{'─'*80}\n\n")
                f.write(item.get("text", ""))
                f.write("\n\n")
        print(f"  ✅ Readable markdown dump: {md_dump_path}")

        for i, item in enumerate(dom_texts):
            page = item.get("page", "")
            text_len = len(item.get("text", ""))
            is_md = item.get("is_markdown", False)
            print(f"  [{i+1}] {page[:80]}  ({text_len} chars, markdown={is_md})")

    except Exception as e:
        import traceback
        print(f"  ERROR fetching DOM texts: {e}")
        traceback.print_exc()
        dom_texts = []

    # Filter DOM texts with error hint pattern
    error_dom_texts = []
    for item in dom_texts:
        text = item.get("text", "")
        if _ERROR_HINT_PATTERNS.search(text):
            error_dom_texts.append(item)

    print_section("STEP 3b: Error-like DOM texts (after filtering)")
    for i, item in enumerate(error_dom_texts):
        print(f"  [{i:3d}] \"{item['text'][:120]}\"")
        print(f"         Page: {item.get('page', '')[:80]}")

    print(f"\n  Error-like DOM texts: {len(error_dom_texts)}")

    # ── Summary ───────────────────────────────────────────────────────────
    print_section("SUMMARY")
    print(f"  Session: {SESSION_ID}")
    print(f"  Total events: {len(session.events)}")
    print(f"  Unique texts from events: {len(event_texts)}")
    print(f"  Error-like texts from events: {len(error_like_from_events)}")
    print(f"  Raw DOM texts from snapshots: {len(dom_texts)}")
    print(f"  Error-like DOM texts: {len(error_dom_texts)}")
    print(f"\n  TOTAL texts that would be sent to AI classification:")
    total = len(event_texts) + len(error_dom_texts)
    print(f"    From events: {len(event_texts)} (all event texts go to AI)")
    print(f"    From DOM:    {len(error_dom_texts)} (only error-like DOM texts)")
    print(f"    Total:       {total}")

    if error_like_from_events:
        print(f"\n  *** POTENTIAL ERROR TEXTS FROM EVENTS ***")
        for t in error_like_from_events:
            print(f"    - \"{t['text'][:100]}\" on {t['page'][:60]}")

    if error_dom_texts:
        print(f"\n  *** POTENTIAL ERROR TEXTS FROM DOM ***")
        for t in error_dom_texts:
            print(f"    - \"{t['text'][:100]}\" on {t.get('page', '')[:60]}")

    if not error_like_from_events and not error_dom_texts:
        print(f"\n  NO error-like texts found in this session.")
        print(f"  This means the AI pipeline would NOT detect any text-based errors.")
        print(f"  Check if the page actually shows error messages visible to users.")


if __name__ == "__main__":
    asyncio.run(main())
