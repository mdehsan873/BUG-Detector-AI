#!/usr/bin/env python3
"""
Real integration test — fetches live PostHog sessions and runs
the full Buglyft detection pipeline (Algo → Hybrid → AI), then
prints every detected issue so you can verify accuracy.

Usage:
    # Run against all projects in Supabase:
    python test_real_sessions.py

    # Limit to N sessions (faster, cheaper):
    python test_real_sessions.py --max-sessions 5

    # Run only Phase 2 (algo) + Phase 2.5 (hybrid) — skip expensive Phase 3 AI:
    python test_real_sessions.py --skip-ai

    # Test a specific session ID (skips fetch, just analyses that session):
    python test_real_sessions.py --session-id "0196b2ab-xxxx-xxxx-xxxx"

    # Verbose: also print event timeline and DOM snapshots:
    python test_real_sessions.py --verbose

    # Combine flags:
    python test_real_sessions.py --max-sessions 3 --verbose --skip-ai
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from textwrap import indent

# ── Ensure backend is on the path ──────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from app.connectors import get_connector
from app.connectors.base import NormalizedSession, NormalizedEvent
from app.services.algorithmic_detector import AlgorithmicDetector
from app.services.session_analysis_service import _compute_dom_diffs
from app.services.hybrid_enrichment import (
    build_event_clusters,
    build_cluster_context,
    analyze_session_clusters,
    enrich_or_replace_algo_issues,
    count_session_triggers,
    merge_related_issues_with_ai,
    _event_to_line,
)
from app.utils.cost_tracker import CostTracker


# ── Colors for terminal output ─────────────────────────────────────────────

class C:
    BOLD    = "\033[1m"
    RED     = "\033[91m"
    GREEN   = "\033[92m"
    YELLOW  = "\033[93m"
    BLUE    = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN    = "\033[96m"
    DIM     = "\033[2m"
    RESET   = "\033[0m"

SEVERITY_COLOR = {
    "critical": C.RED,
    "high":     C.RED,
    "medium":   C.YELLOW,
    "low":      C.DIM,
}


# ── Pretty Printers ────────────────────────────────────────────────────────

def print_header(text: str):
    width = 80
    print(f"\n{C.BOLD}{C.CYAN}{'═' * width}{C.RESET}")
    print(f"{C.BOLD}{C.CYAN}  {text}{C.RESET}")
    print(f"{C.BOLD}{C.CYAN}{'═' * width}{C.RESET}\n")


def print_subheader(text: str):
    print(f"\n{C.BOLD}{C.BLUE}── {text} {'─' * max(0, 74 - len(text))}{C.RESET}\n")


def print_session_info(session: NormalizedSession, dom_count: int):
    event_types = {}
    for ev in session.events:
        event_types[ev.event_type] = event_types.get(ev.event_type, 0) + 1

    print(f"  {C.BOLD}Session:{C.RESET}  {session.id}")
    print(f"  {C.BOLD}User:{C.RESET}     {session.distinct_id}")
    print(f"  {C.BOLD}Time:{C.RESET}     {session.start_time} → {session.end_time}")
    print(f"  {C.BOLD}Events:{C.RESET}   {len(session.events)} total")
    print(f"  {C.BOLD}Types:{C.RESET}    {', '.join(f'{k}={v}' for k, v in sorted(event_types.items()))}")
    print(f"  {C.BOLD}DOM:{C.RESET}      {dom_count} snapshots")
    print(f"  {C.BOLD}Replay:{C.RESET}   {session.replay_url}")


def print_event_timeline(session: NormalizedSession, limit: int = 50):
    print(f"\n  {C.DIM}Event Timeline (first {limit}):{C.RESET}")
    for i, ev in enumerate(session.events[:limit]):
        ts = ev.timestamp.split("T")[1][:12] if "T" in ev.timestamp else ev.timestamp
        color = C.RED if ev.event_type in ("error", "network_error") else C.DIM

        line = f"    {C.DIM}{ts}{C.RESET} {color}{ev.event_type:<18}{C.RESET}"

        if ev.event_type == "pageview":
            line += f" {ev.pathname or ev.url}"
        elif ev.event_type == "network_error":
            line += f" {C.RED}{ev.method} {ev.endpoint} → {ev.status_code}{C.RESET}"
        elif ev.event_type == "error":
            line += f" {C.RED}{ev.error_type}: {ev.error_message[:80]}{C.RESET}"
        elif ev.event_type in ("click", "dead_click", "rage_click"):
            line += f" <{ev.tag_name}> '{ev.element_text[:40]}'"
        elif ev.event_type == "submit":
            line += f" → {ev.form_action}"
        elif ev.event_type == "input":
            line += f" <{ev.tag_name} name={ev.element_name}>"

        print(line)

    if len(session.events) > limit:
        print(f"    {C.DIM}... and {len(session.events) - limit} more events{C.RESET}")


def print_dom_snapshots(dom_texts: list[dict], limit: int = 5):
    print(f"\n  {C.DIM}DOM Snapshots (first {limit}):{C.RESET}")
    for i, dom in enumerate(dom_texts[:limit]):
        ts = dom["timestamp"].split("T")[1][:12] if "T" in dom["timestamp"] else dom["timestamp"]
        text_preview = dom["text"][:200].replace("\n", " ↵ ")
        print(f"    {C.DIM}[{i}] {ts} | {dom['page']}{C.RESET}")
        print(f"        {text_preview}")
    if len(dom_texts) > limit:
        print(f"    {C.DIM}... and {len(dom_texts) - limit} more snapshots{C.RESET}")


def print_issue(issue: dict, idx: int):
    sev = issue.get("severity", "medium")
    sev_color = SEVERITY_COLOR.get(sev, C.DIM)
    source = issue.get("_enriched_by", issue.get("rule_id", "unknown"))

    print(f"  {C.BOLD}Issue #{idx + 1}{C.RESET}")
    print(f"    {C.BOLD}Title:{C.RESET}       {sev_color}{issue.get('title', 'N/A')}{C.RESET}")
    print(f"    {C.BOLD}Severity:{C.RESET}    {sev_color}{sev.upper()}{C.RESET}")
    print(f"    {C.BOLD}Category:{C.RESET}    {issue.get('category', 'N/A')}")
    print(f"    {C.BOLD}Page:{C.RESET}        {issue.get('page_url', 'N/A')}")
    print(f"    {C.BOLD}Confidence:{C.RESET}  {issue.get('confidence', 0):.0%}")
    print(f"    {C.BOLD}Source:{C.RESET}      {source}")
    print(f"    {C.BOLD}Description:{C.RESET} {issue.get('description', 'N/A')}")
    print(f"    {C.BOLD}Impact:{C.RESET}      {issue.get('why_issue', 'N/A')}")

    steps = issue.get("reproduction_steps", [])
    if steps:
        print(f"    {C.BOLD}Repro Steps:{C.RESET}")
        for j, step in enumerate(steps[:10]):
            print(f"      {j + 1}. {step}")

    evidence = issue.get("evidence", [])
    if evidence and isinstance(evidence, list):
        print(f"    {C.BOLD}Evidence:{C.RESET}    {len(evidence)} item(s)")
        for ev in evidence[:3]:
            if isinstance(ev, dict):
                ev_summary = ", ".join(f"{k}={str(v)[:50]}" for k, v in ev.items() if k != "raw")
                print(f"      - {ev_summary}")

    print()


def print_cluster_info(clusters, session_id: str):
    if not clusters:
        return
    print(f"\n  {C.BOLD}{C.MAGENTA}Clusters Found: {len(clusters)}{C.RESET}")
    for c in clusters:
        trigger_desc = ", ".join(
            f"{e.event_type}({e.status_code or e.error_type or e.form_action or ''})"
            for e in c.trigger_events
        )
        print(f"    {C.MAGENTA}[{c.cluster_id}]{C.RESET} type={c.cluster_type} "
              f"page={c.page_url} triggers=({trigger_desc}) "
              f"events={len(c.events)} dom={len(c.dom_snapshots)}")


# ── Fetch projects from Supabase ───────────────────────────────────────────

def fetch_projects() -> list[dict]:
    """Load all projects from Supabase to get PostHog credentials."""
    from app.database import get_supabase
    from app.services.crypto_service import decrypt_token

    db = get_supabase()
    rows = (
        db.table("projects")
        .select("id, name, session_provider, provider_api_key, provider_project_id, "
                "provider_host, skip_page_patterns, min_sessions_threshold")
        .execute()
        .data
    )

    projects = []
    for row in rows:
        try:
            api_key = decrypt_token(row["provider_api_key"])
            projects.append({
                "id": row["id"],
                "name": row.get("name", "Unnamed"),
                "provider": row.get("session_provider", "posthog"),
                "api_key": api_key,
                "project_id": row.get("provider_project_id", ""),
                "host": row.get("provider_host", "eu.posthog.com"),
                "skip_pages": row.get("skip_page_patterns") or [],
                "min_users": row.get("min_sessions_threshold", 2),
            })
        except Exception as e:
            print(f"  {C.RED}⚠ Skipping project {row.get('name', row['id'])}: {e}{C.RESET}")

    return projects


# ── Run analysis on a single session ───────────────────────────────────────

async def analyze_single_session(
    connector,
    session: NormalizedSession,
    skip_pages: list[str],
    skip_ai: bool,
    verbose: bool,
    cost_tracker: CostTracker,
) -> dict:
    """
    Run the full detection pipeline on a single session and return results.
    Returns: {
        "algo_issues": list[dict],
        "clusters": list,
        "hybrid_issues": list[dict],
        "final_issues": list[dict],
    }
    """
    result = {
        "algo_issues": [],
        "clusters": [],
        "hybrid_issues": [],
        "final_issues": [],
    }

    # ── Fetch DOM texts + recording signals ──────────────────────────
    dom_texts = []
    rec_signals = []
    try:
        if hasattr(connector, "fetch_session_dom_and_signals"):
            dom_texts, rec_signals = await connector.fetch_session_dom_and_signals(session.id)
        else:
            dom_texts = await connector.fetch_session_dom_texts(session.id)
    except Exception as e:
        print(f"  {C.YELLOW}⚠ DOM fetch failed: {e}{C.RESET}")

    # Enrich session events with recording signals (network errors, console logs)
    if rec_signals:
        print(f"\n  {C.CYAN}Recording signals found: "
              f"{sum(1 for s in rec_signals if s['type'] == 'network_error')} network errors, "
              f"{sum(1 for s in rec_signals if s['type'] == 'console_error')} console errors{C.RESET}")

        if hasattr(connector, "enrich_session_events"):
            old_count = len(session.events)
            session = connector.enrich_session_events(session, rec_signals)
            new_count = len(session.events)
            if new_count > old_count:
                print(f"  {C.GREEN}Enriched: {old_count} → {new_count} events "
                      f"(+{new_count - old_count} from recording){C.RESET}")
            else:
                print(f"  {C.GREEN}Enriched: replaced generic error messages with detailed ones{C.RESET}")

        if verbose:
            print(f"\n  {C.DIM}Recording Signals:{C.RESET}")
            for sig in rec_signals[:20]:
                if sig["type"] == "network_error":
                    print(f"    {C.RED}NET: {sig['method']} {sig['url'][:60]} → {sig['status_code']}{C.RESET}")
                elif sig["type"] == "console_error":
                    print(f"    {C.YELLOW}LOG [{sig['level']}]: {sig['message'][:80]}{C.RESET}")

    dom_diffs = _compute_dom_diffs(dom_texts, events=session.events) if dom_texts else []

    print_session_info(session, len(dom_texts))

    if verbose:
        print_event_timeline(session)
        if dom_texts:
            print_dom_snapshots(dom_texts)

    # ── Phase 2: Algorithmic Detection ─────────────────────────────────
    print_subheader("Phase 2: Algorithmic Detection")

    detector = AlgorithmicDetector(skip_page_patterns=skip_pages)
    algo_detected = detector.detect(session, dom_diffs=dom_diffs, dom_texts=dom_texts)

    algo_issues = []
    for issue in algo_detected:
        issue_dict = issue.to_dict()
        issue_dict["session_id"] = session.id
        issue_dict["distinct_id"] = session.distinct_id or ""
        algo_issues.append(issue_dict)

    result["algo_issues"] = algo_issues

    if algo_issues:
        print(f"  {C.GREEN}Found {len(algo_issues)} algorithmic issue(s):{C.RESET}\n")
        for i, iss in enumerate(algo_issues):
            print_issue(iss, i)
    else:
        print(f"  {C.DIM}No algorithmic issues found.{C.RESET}")

    # ── Phase 2.5: Hybrid Enrichment (Event Clustering + Micro-AI) ─────
    print_subheader("Phase 2.5: Hybrid Enrichment")

    clusters = build_event_clusters(
        session,
        dom_texts=dom_texts or None,
        dom_diffs=dom_diffs or None,
        skip_page_patterns=skip_pages,
    )
    result["clusters"] = clusters
    print_cluster_info(clusters, session.id)

    # ── Dump cluster context as JSON for debugging ───────────────────
    if clusters:
        cluster_dump = []
        for cl in clusters:
            # Build the exact context that would be sent to AI
            ai_context = build_cluster_context(cl, session)

            # Separate out the components for easy inspection
            console_logs = []
            network_errors = []
            user_actions = []
            other_events = []
            for ev in cl.events:
                ev_line = _event_to_line(ev)
                if ev.event_type == "error":
                    console_logs.append({
                        "timestamp": ev.timestamp,
                        "error_type": ev.error_type or "",
                        "message": (ev.error_message or "")[:300],
                        "page": ev.url or ev.pathname or "",
                    })
                elif ev.event_type == "network_error":
                    network_errors.append({
                        "timestamp": ev.timestamp,
                        "method": ev.method or "",
                        "endpoint": (ev.endpoint or "")[:200],
                        "status_code": ev.status_code,
                        "page": ev.url or ev.pathname or "",
                    })
                elif ev.event_type in ("pageview", "pageleave", "click", "tap",
                                       "input", "submit", "scroll", "focus",
                                       "dead_click", "rage_click"):
                    user_actions.append({
                        "timestamp": ev.timestamp,
                        "type": ev.event_type,
                        "description": ev_line,
                    })
                else:
                    other_events.append({
                        "timestamp": ev.timestamp,
                        "type": ev.event_type,
                        "description": ev_line,
                    })

            # DOM snapshots in the window
            dom_in_window = []
            for ds in cl.dom_snapshots:
                dom_in_window.append({
                    "timestamp": ds.get("timestamp", ""),
                    "page": ds.get("page", ""),
                    "dom_md": ds.get("text", ""),  # full DOM for verification
                })

            # DOM diffs in the window
            diffs_in_window = []
            for dd in cl.dom_diffs:
                diffs_in_window.append({
                    "timestamp": dd.get("timestamp", ""),
                    "page": dd.get("page", ""),
                    "is_diff": dd.get("is_diff", False),
                    "text": dd.get("text", "")[:500],
                })

            cluster_dump.append({
                "cluster_id": cl.cluster_id,
                "cluster_type": cl.cluster_type,
                "center_ts": cl.center_ts,
                "page_url": cl.page_url,
                "trigger_count": len(cl.trigger_events),
                "event_count": len(cl.events),
                "dom_snapshot_count": len(cl.dom_snapshots),
                "dom_diff_count": len(cl.dom_diffs),
                "console_logs": console_logs,
                "network_errors": network_errors,
                "user_actions": user_actions,
                "other_events": other_events,
                "dom_snapshots": dom_in_window,
                "dom_diffs": diffs_in_window,
                "ai_context_sent": ai_context,
            })

        dump_path = f"cluster_debug_{session.id[:12]}.json"
        with open(dump_path, "w") as f:
            json.dump(cluster_dump, f, indent=2, default=str)
        print(f"\n  {C.CYAN}📋 Cluster debug JSON dumped to: {dump_path}{C.RESET}")

    hybrid_issues = []
    if clusters:
        if skip_ai:
            print(f"\n  {C.YELLOW}⚠ --skip-ai: Skipping micro-AI calls for {len(clusters)} cluster(s).{C.RESET}")
            print(f"  {C.DIM}Clusters show what WOULD be sent to AI for enrichment.{C.RESET}")
        else:
            print(f"\n  {C.CYAN}Sending {len(clusters)} cluster(s) to AI for enrichment...{C.RESET}")
            try:
                hybrid_issues = await analyze_session_clusters(session, clusters, cost_tracker)
                result["hybrid_issues"] = hybrid_issues
            except Exception as e:
                print(f"  {C.RED}⚠ Hybrid AI failed: {e}{C.RESET}")

    if hybrid_issues:
        print(f"\n  {C.GREEN}Hybrid AI returned {len(hybrid_issues)} enriched issue(s):{C.RESET}\n")
        for i, h in enumerate(hybrid_issues):
            sev = h.get("severity", "medium")
            sev_color = SEVERITY_COLOR.get(sev, C.DIM)
            print(f"    {C.BOLD}Hybrid #{i + 1}:{C.RESET} {sev_color}{h.get('title', 'N/A')}{C.RESET}")
            print(f"      Severity: {sev.upper()} | Confidence: {h.get('confidence', 0):.0%} | "
                  f"Category: {h.get('category', 'N/A')}")
            print(f"      Description: {h.get('description', 'N/A')}")
            print(f"      Impact: {h.get('why_issue', 'N/A')}")
            print()

    # ── Merge: Enrich algo issues with hybrid results ──────────────────
    seen_fps: set[str] = set()
    if hybrid_issues:
        final_issues = enrich_or_replace_algo_issues(
            [iss.copy() for iss in algo_issues],  # copy so we keep originals
            hybrid_issues,
            session,
            seen_fps,
        )
    else:
        final_issues = algo_issues

    # ── Phase 4: AI Issue Merge ─────────────────────────────────────────
    if not skip_ai and len(final_issues) > 2:
        print_subheader("Phase 4: AI Issue Merge")
        print(f"  Merging {len(final_issues)} issues by root cause...")
        final_issues = await merge_related_issues_with_ai(
            final_issues,
            session=session,
            cost_tracker=cost_tracker,
        )
        print(f"  {C.GREEN}After merge: {len(final_issues)} distinct issue(s){C.RESET}")

    print_subheader("Final Issues")

    result["final_issues"] = final_issues

    if final_issues:
        print(f"  {C.GREEN}{C.BOLD}Total: {len(final_issues)} issue(s) for this session{C.RESET}\n")
        for i, iss in enumerate(final_issues):
            print_issue(iss, i)
    else:
        print(f"  {C.DIM}No issues detected in this session.{C.RESET}")

    # ── Trigger coverage ───────────────────────────────────────────────
    trigger_count = count_session_triggers(session)
    cluster_count = len(clusters)
    covered = trigger_count > 0 and cluster_count >= trigger_count
    print(f"  {C.DIM}Triggers: {trigger_count} | Clusters: {cluster_count} | "
          f"Fully covered: {'YES → Phase 3 would be SKIPPED' if covered else 'NO → Phase 3 needed'}{C.RESET}")

    return result


# ── Main ───────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="Test Buglyft detection on real PostHog sessions")
    parser.add_argument("--max-sessions", type=int, default=10,
                        help="Max sessions to fetch per project (default: 10)")
    parser.add_argument("--skip-ai", action="store_true",
                        help="Skip AI calls (only run algo + clustering, no micro-AI)")
    parser.add_argument("--session-id", type=str, default=None,
                        help="Test a specific session ID (fetches all and filters)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show event timelines and DOM snapshots")
    parser.add_argument("--hours", type=int, default=24,
                        help="How many hours back to fetch sessions (default: 24)")
    args = parser.parse_args()

    print_header("Buglyft Real Session Analysis Test")

    # ── Load projects ──────────────────────────────────────────────────
    print(f"  Loading projects from Supabase...")
    projects = fetch_projects()
    if not projects:
        print(f"  {C.RED}No projects found in Supabase. Add a project first.{C.RESET}")
        return

    print(f"  Found {len(projects)} project(s)\n")

    cost_tracker = CostTracker()
    total_issues = 0
    total_sessions = 0

    for proj in projects:
        print_header(f"Project: {proj['name']} ({proj['provider']})")
        print(f"  {C.DIM}Project ID: {proj['project_id']} | Host: {proj['host']}{C.RESET}\n")

        # ── Create connector ───────────────────────────────────────────
        connector = get_connector(
            provider=proj["provider"],
            api_key=proj["api_key"],
            project_id=proj["project_id"],
            host=proj["host"],
        )

        # ── Fetch sessions ─────────────────────────────────────────────
        since = datetime.now(timezone.utc) - timedelta(hours=args.hours)
        print(f"  Fetching sessions from last {args.hours}h (limit={args.max_sessions})...")

        try:
            sessions = await connector.fetch_sessions(since=since, limit=args.max_sessions)
        except Exception as e:
            print(f"  {C.RED}⚠ Failed to fetch sessions: {e}{C.RESET}")
            continue

        print(f"  Fetched {len(sessions)} session(s)\n")

        if not sessions:
            print(f"  {C.DIM}No sessions found in the last {args.hours}h.{C.RESET}")
            continue

        # Filter to specific session if requested
        if args.session_id:
            sessions = [s for s in sessions if s.id == args.session_id]
            if not sessions:
                print(f"  {C.RED}Session {args.session_id} not found in last {args.hours}h.{C.RESET}")
                print(f"  {C.DIM}Try increasing --hours or check the session ID.{C.RESET}")
                continue

        # ── Analyze each session ───────────────────────────────────────
        for idx, session in enumerate(sessions):
            print_subheader(f"Session {idx + 1}/{len(sessions)}")

            t0 = time.time()
            result = await analyze_single_session(
                connector=connector,
                session=session,
                skip_pages=proj["skip_pages"],
                skip_ai=args.skip_ai,
                verbose=args.verbose,
                cost_tracker=cost_tracker,
            )
            elapsed = time.time() - t0

            n_issues = len(result["final_issues"])
            total_issues += n_issues
            total_sessions += 1

            print(f"\n  {C.DIM}⏱ Session processed in {elapsed:.1f}s{C.RESET}")
            print(f"  {'─' * 76}")

    # ── Summary ────────────────────────────────────────────────────────
    print_header("Summary")
    print(f"  Sessions analyzed:  {total_sessions}")
    print(f"  Total issues found: {total_issues}")

    if not args.skip_ai:
        summary = cost_tracker.summary()
        if summary.get("total_calls", 0) > 0:
            print(f"\n  {C.BOLD}AI Cost:{C.RESET}")
            print(f"    Calls:          {summary.get('total_calls', 0)}")
            print(f"    Prompt tokens:  {summary.get('total_prompt_tokens', 0):,}")
            print(f"    Output tokens:  {summary.get('total_completion_tokens', 0):,}")
            print(f"    Total cost:     ${summary.get('total_cost_usd', 0):.4f}")

            by_fn = summary.get("by_function", {})
            if by_fn:
                print(f"\n    {C.DIM}By function:{C.RESET}")
                for fn, data in by_fn.items():
                    print(f"      {fn}: {data['calls']} calls, "
                          f"${data['cost_usd']:.4f}, "
                          f"avg {data.get('avg_duration_ms', 0):.0f}ms")

    print(f"\n{C.BOLD}{C.GREEN}Done!{C.RESET}\n")


if __name__ == "__main__":
    asyncio.run(main())
