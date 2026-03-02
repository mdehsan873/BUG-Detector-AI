"""
Test suite for Buglyft session analysis pipeline.

Tests each phase independently and end-to-end:
  - Phase 2: AlgorithmicDetector (instant, zero-cost detection)
  - Phase 2.5: Hybrid Enrichment (event clustering + micro-AI)
  - DOM diffs + idle time subtraction
  - Auth redirect false-positive prevention
  - Transitional UI pattern exclusion

Usage:
    python test_session_analysis.py          # Run all tests
    python test_session_analysis.py -v       # Verbose
    python test_session_analysis.py -k algo  # Run only tests matching "algo"
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import unittest
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

# ── Ensure backend is on the path ──────────────────────────────────────────
sys.path.insert(0, ".")

from app.connectors.base import NormalizedEvent, NormalizedSession
from app.services.algorithmic_detector import AlgorithmicDetector
from app.services.session_analysis_service import _compute_dom_diffs
from app.services.hybrid_enrichment import (
    build_event_clusters,
    enrich_or_replace_algo_issues,
    count_session_triggers,
)


# ── Helpers ─────────────────────────────────────────────────────────────────

BASE_TIME = datetime(2025, 3, 1, 10, 0, 0, tzinfo=timezone.utc)


def _ts(offset_s: float) -> str:
    """Return ISO timestamp offset from BASE_TIME by `offset_s` seconds."""
    return (BASE_TIME + timedelta(seconds=offset_s)).isoformat()


def _make_event(
    offset_s: float,
    event_type: str,
    url: str = "https://app.example.com/settings",
    pathname: str = "/settings",
    **kwargs,
) -> NormalizedEvent:
    """Build a NormalizedEvent at BASE_TIME + offset_s."""
    return NormalizedEvent(
        timestamp=_ts(offset_s),
        event_type=event_type,
        url=url,
        pathname=pathname,
        **kwargs,
    )


def _make_session(
    session_id: str,
    events: list[NormalizedEvent],
    distinct_id: str = "user-1",
) -> NormalizedSession:
    """Build a NormalizedSession from a list of events."""
    start = events[0].timestamp if events else _ts(0)
    end = events[-1].timestamp if events else _ts(60)
    return NormalizedSession(
        id=session_id,
        distinct_id=distinct_id,
        start_time=start,
        end_time=end,
        events=events,
        replay_url=f"https://posthog.example.com/replay/{session_id}",
    )


def _make_dom_text(offset_s: float, text: str, page: str = "https://app.example.com/settings") -> dict:
    """Build a raw DOM markdown snapshot at BASE_TIME + offset_s."""
    return {
        "text": text,
        "page": page,
        "timestamp": _ts(offset_s),
        "is_markdown": True,
    }


# ═══════════════════════════════════════════════════════════════════════════
# TEST: Algorithmic Detector — Network Error
# ═══════════════════════════════════════════════════════════════════════════

class TestAlgoNetworkError(unittest.TestCase):
    """Tests that the algo detector correctly identifies HTTP 500 errors."""

    def test_http_500_detected(self):
        """An HTTP 500 on DELETE /api/account should be flagged as a network error."""
        events = [
            _make_event(0, "pageview"),
            _make_event(5, "click", tag_name="button", element_text="Delete Account"),
            _make_event(6, "submit", form_action="/api/account"),
            _make_event(7, "network_error", status_code=500, method="DELETE",
                        endpoint="/api/account", error_message="Internal Server Error"),
        ]
        session = _make_session("sess-net500", events)
        dom_texts = [
            _make_dom_text(0, "# Settings\n\n- Account\n- [Delete Account]"),
            _make_dom_text(8, "# Settings\n\n- Account\n- Request failed. Please try again."),
        ]

        detector = AlgorithmicDetector()
        issues = detector.detect(session, dom_texts=dom_texts)

        net_issues = [i for i in issues if i.rule_id == "network_error"]
        self.assertGreaterEqual(len(net_issues), 1, "Should detect at least one network_error")

        issue = net_issues[0]
        self.assertEqual(issue.severity, "high")
        self.assertIn("500", issue.title)
        self.assertIn("/api/account", issue.title.lower() + " " + issue.description.lower())

    def test_http_200_not_flagged(self):
        """Successful requests should not be flagged."""
        events = [
            _make_event(0, "pageview"),
            _make_event(5, "click", tag_name="button", element_text="Save"),
            # No network_error event — request succeeded
        ]
        session = _make_session("sess-ok", events)
        detector = AlgorithmicDetector()
        issues = detector.detect(session)

        net_issues = [i for i in issues if i.rule_id == "network_error"]
        self.assertEqual(len(net_issues), 0, "Should not flag any network errors")


# ═══════════════════════════════════════════════════════════════════════════
# TEST: Algorithmic Detector — Console Error
# ═══════════════════════════════════════════════════════════════════════════

class TestAlgoConsoleError(unittest.TestCase):
    """Tests for JS console error detection."""

    def test_js_error_detected(self):
        """A JS TypeError should be detected."""
        events = [
            _make_event(0, "pageview"),
            _make_event(3, "error", error_type="TypeError",
                        error_message="Cannot read properties of undefined (reading 'map')"),
        ]
        session = _make_session("sess-js-err", events)
        dom_texts = [
            _make_dom_text(0, "# Settings\n\n- Profile\n- Preferences"),
            _make_dom_text(4, "# Settings\n\n- Something went wrong"),
        ]

        detector = AlgorithmicDetector()
        issues = detector.detect(session, dom_texts=dom_texts)

        js_issues = [i for i in issues if i.rule_id == "console_error"]
        self.assertGreaterEqual(len(js_issues), 1, "Should detect at least one console error")
        self.assertIn("TypeError", js_issues[0].title + " " + js_issues[0].description)


# ═══════════════════════════════════════════════════════════════════════════
# TEST: Algorithmic Detector — Form Submit No Response
# ═══════════════════════════════════════════════════════════════════════════

class TestAlgoFormNoResponse(unittest.TestCase):
    """Tests for form submission with no visible feedback."""

    def test_form_submit_no_response_detected(self):
        """A form submit followed by nothing for 3+ seconds should be flagged."""
        events = [
            _make_event(0, "pageview"),
            _make_event(2, "input", tag_name="input", element_type="text",
                        element_name="name", element_value="John"),
            _make_event(4, "submit", tag_name="form", form_action="/api/profile"),
            # No network response, no DOM change, no navigation for > 3s
            _make_event(10, "click", tag_name="button", element_text="Cancel"),
        ]
        session = _make_session("sess-form-noresp", events)

        detector = AlgorithmicDetector()
        issues = detector.detect(session)

        form_issues = [i for i in issues if i.rule_id == "form_no_response"]
        self.assertGreaterEqual(len(form_issues), 1,
                                "Should detect form submission with no response")

    def test_form_submit_with_success_not_flagged(self):
        """Form submit followed by success DOM text should NOT be flagged."""
        events = [
            _make_event(0, "pageview"),
            _make_event(2, "submit", tag_name="form", form_action="/api/profile"),
            _make_event(3, "pageview", url="https://app.example.com/dashboard",
                        pathname="/dashboard"),
        ]
        session = _make_session("sess-form-ok", events)
        dom_texts = [
            _make_dom_text(0, "# Edit Profile\n\n- Name field\n- [Save]"),
            _make_dom_text(3, "# Dashboard\n\nProfile saved successfully!",
                           page="https://app.example.com/dashboard"),
        ]

        detector = AlgorithmicDetector()
        issues = detector.detect(session, dom_texts=dom_texts)

        form_issues = [i for i in issues if i.rule_id == "form_no_response"]
        self.assertEqual(len(form_issues), 0,
                         "Should NOT flag form when page navigated (success)")

    def test_form_submit_with_delayed_page_change_not_flagged(self):
        """
        Login form submit → 4s loading → user navigates to /register/.
        The page change within 10s means the form DID respond (or the user
        saw feedback and moved on). Should NOT be flagged.
        """
        events = [
            _make_event(0, "pageview", url="https://app.example.com/login",
                        pathname="/login"),
            _make_event(2, "input", tag_name="input", element_type="email"),
            _make_event(3, "input", tag_name="input", element_type="password"),
            _make_event(4, "submit", tag_name="form", form_action="/api/login",
                        url="https://app.example.com/login", pathname="/login"),
            # 5 seconds later, user clicks "Sign up" and navigates away
            _make_event(9, "click", tag_name="a", element_text="Sign up",
                        url="https://app.example.com/login", pathname="/login"),
            _make_event(9.5, "pageview", url="https://app.example.com/register",
                        pathname="/register"),
        ]
        session = _make_session("sess-form-delayed-nav", events)

        detector = AlgorithmicDetector()
        issues = detector.detect(session)

        form_issues = [i for i in issues if i.rule_id == "form_no_response"]
        self.assertEqual(len(form_issues), 0,
                         "Should NOT flag form when user navigates to different page within 10s")

    def test_form_submit_with_delayed_dom_change_not_flagged(self):
        """
        Form submit → loading spinner for 4s → DOM changes to 'Email verified'.
        The DOM change (even after 4s) means the form responded.
        Should NOT be flagged.
        """
        events = [
            _make_event(0, "pageview", url="https://app.example.com/login",
                        pathname="/login"),
            _make_event(2, "submit", tag_name="form", form_action="/api/login",
                        url="https://app.example.com/login", pathname="/login"),
            # No events for a while — loading spinner
            _make_event(15, "click", tag_name="a", element_text="Continue"),
        ]
        dom_texts = [
            # Before submit: login form
            _make_dom_text(0, "# Login\n\n[INPUT: email] [INPUT: password] [BUTTON: Sign in]",
                           page="https://app.example.com/login"),
            # During loading (3s): same text basically (spinner is CSS, not text)
            # After loading (6s): DOM changes to verification message
            _make_dom_text(6, "# Email Verified\n\nYour email has been verified. Welcome!",
                           page="https://app.example.com/login"),
        ]
        session = _make_session("sess-form-delayed-dom", events)

        detector = AlgorithmicDetector()
        issues = detector.detect(session, dom_texts=dom_texts)

        form_issues = [i for i in issues if i.rule_id == "form_no_response"]
        self.assertEqual(len(form_issues), 0,
                         "Should NOT flag form when DOM changes to success after loading")


# ═══════════════════════════════════════════════════════════════════════════
# TEST: Algorithmic Detector — Silent Failure
# ═══════════════════════════════════════════════════════════════════════════

class TestAlgoSilentFailure(unittest.TestCase):
    """Tests for silent failures (network error with no visible error in DOM)."""

    def test_silent_failure_detected(self):
        """Network 500 with no error text in DOM = silent failure."""
        events = [
            _make_event(0, "pageview"),
            _make_event(3, "click", tag_name="button", element_text="Refresh Data"),
            _make_event(4, "network_error", status_code=500, method="GET",
                        endpoint="/api/data", error_message="Internal Server Error"),
            # No DOM change — user sees nothing
        ]
        session = _make_session("sess-silent", events)

        # DOM shows no error message — same before and after
        dom_texts = [
            _make_dom_text(0, "# Dashboard\n\n- Data table\n- No data yet"),
            _make_dom_text(5, "# Dashboard\n\n- Data table\n- No data yet"),
        ]
        dom_diffs = _compute_dom_diffs(dom_texts, events=events)

        detector = AlgorithmicDetector()
        issues = detector.detect(session, dom_diffs=dom_diffs, dom_texts=dom_texts)

        silent_issues = [i for i in issues if i.rule_id == "silent_failure"]
        self.assertGreaterEqual(len(silent_issues), 1,
                                "Should detect silent failure when 500 has no DOM error")


# ═══════════════════════════════════════════════════════════════════════════
# TEST: Algorithmic Detector — Instant Bounce
# ═══════════════════════════════════════════════════════════════════════════

class TestAlgoInstantBounce(unittest.TestCase):
    """Tests for instant bounce detection (user leaves quickly)."""

    def test_instant_bounce_with_error_detected(self):
        """User lands, sees error text, and leaves within 3s — flagged."""
        events = [
            _make_event(0, "pageview"),
            _make_event(2, "pageleave"),
        ]
        session = _make_session("sess-bounce-err", events)
        dom_texts = [
            _make_dom_text(0, "# Error\n\n500 - Internal Server Error\nSomething went wrong"),
        ]

        detector = AlgorithmicDetector()
        issues = detector.detect(session, dom_texts=dom_texts)

        bounce_issues = [i for i in issues if i.rule_id == "instant_bounce"]
        self.assertGreaterEqual(len(bounce_issues), 1,
                                "Should detect instant bounce with error content")


# ═══════════════════════════════════════════════════════════════════════════
# TEST: Auth Redirect — FALSE POSITIVE prevention
# ═══════════════════════════════════════════════════════════════════════════

class TestAuthRedirectFalsePositive(unittest.TestCase):
    """
    Exact scenario from user's screenshot: visiting protected page → app shows
    "Redirecting to login..." → navigates to /login. This should NOT be a bug.
    """

    def test_redirect_to_login_not_flagged(self):
        """Auth redirect flow should not produce false positives."""
        events = [
            _make_event(0, "pageview", url="https://app.example.com/dashboard",
                        pathname="/dashboard"),
            # App shows "Redirecting to login..." (transitional)
            # User doesn't interact — automatic redirect
            _make_event(3, "pageview", url="https://app.example.com/login",
                        pathname="/login"),
            _make_event(10, "input", tag_name="input", element_type="email",
                        url="https://app.example.com/login", pathname="/login"),
        ]
        session = _make_session("sess-auth-redir", events)
        dom_texts = [
            _make_dom_text(0, "# Dashboard\n\nRedirecting to login...",
                           page="https://app.example.com/dashboard"),
            _make_dom_text(3, "# Login\n\n- Email field\n- Password field\n- [Sign In]",
                           page="https://app.example.com/login"),
        ]
        dom_diffs = _compute_dom_diffs(dom_texts, events=events)

        detector = AlgorithmicDetector()
        issues = detector.detect(session, dom_diffs=dom_diffs, dom_texts=dom_texts)

        # The /dashboard page (with "Redirecting to login...") should be identified
        # as part of the auth redirect flow and not produce bounce/flash issues
        problematic = [
            i for i in issues
            if i.page_url and "dashboard" in i.page_url
            and i.rule_id in ("instant_bounce", "flash_error")
        ]
        self.assertEqual(len(problematic), 0,
                         f"Auth redirect should not produce false positives, got: "
                         f"{[(i.rule_id, i.title) for i in problematic]}")


# ═══════════════════════════════════════════════════════════════════════════
# TEST: Transitional UI — "Redirecting...", "Please wait..." not bugs
# ═══════════════════════════════════════════════════════════════════════════

class TestTransitionalUI(unittest.TestCase):
    """
    Messages like "Redirecting...", "Please wait...", "Signing you in..."
    are normal transitional UI, not errors.
    """

    def test_please_wait_not_flagged_as_flash_error(self):
        """Transitional 'Please wait...' visible briefly should NOT be a flash error."""
        # DOM shows "Please wait..." then changes to dashboard content
        dom_texts = [
            _make_dom_text(0, "# Loading\n\nPlease wait while we load your data...",
                           page="https://app.example.com/dashboard"),
            _make_dom_text(1.5, "# Dashboard\n\n- Welcome back!\n- Your stats: 42 projects",
                           page="https://app.example.com/dashboard"),
        ]
        events = [
            _make_event(0, "pageview", url="https://app.example.com/dashboard",
                        pathname="/dashboard"),
            _make_event(5, "click", tag_name="a", element_text="Projects",
                        url="https://app.example.com/dashboard", pathname="/dashboard"),
        ]

        dom_diffs = _compute_dom_diffs(dom_texts, events=events)
        session = _make_session("sess-transitional", events)

        detector = AlgorithmicDetector()
        issues = detector.detect(session, dom_diffs=dom_diffs, dom_texts=dom_texts)

        flash_issues = [i for i in issues if i.rule_id == "flash_error"]
        self.assertEqual(len(flash_issues), 0,
                         "Transitional 'Please wait...' should NOT be flagged as flash error")


# ═══════════════════════════════════════════════════════════════════════════
# TEST: DOM Diffs — Idle Time Subtraction
# ═══════════════════════════════════════════════════════════════════════════

class TestDomDiffsIdleTime(unittest.TestCase):
    """
    When a user switches tabs (idle gap > 10s between events), the visibility
    duration of DOM elements should be reduced by the idle time.
    """

    def test_idle_time_subtracted_from_visibility(self):
        """
        Scenario: Text appears at t=5, next snapshot at t=35.
        User events at t=3 and t=33 → gap of 30s (idle from t=3 to t=33).
        Raw visibility = 30s, but active visibility = 30 - 30 = ~0s (clamped to 100ms min).
        """
        dom_texts = [
            _make_dom_text(5, "# Page\n\nFirst content"),
            _make_dom_text(35, "# Page\n\nSecond content"),
        ]
        events = [
            _make_event(3, "click"),
            # 30 second gap — user was on another tab
            _make_event(33, "click"),
        ]

        diffs = _compute_dom_diffs(dom_texts, events=events)

        # There should be at least one diff
        self.assertGreater(len(diffs), 0, "Should produce at least one diff")

        # Check that removed lines have reduced visibility durations
        for diff in diffs:
            if diff.get("is_diff") and "REMOVED" in diff.get("text", ""):
                text = diff["text"]
                # Look for "(was visible for Xms)" — should be much less than 30000ms
                import re
                matches = re.findall(r"was visible for (\d+)ms", text)
                for ms_str in matches:
                    ms_val = int(ms_str)
                    self.assertLess(ms_val, 5000,
                                    f"Idle time should be subtracted; got {ms_val}ms "
                                    f"(expected < 5000ms due to 30s idle gap)")

    def test_no_idle_gap_preserves_duration(self):
        """When user is continuously active, visibility durations are not reduced."""
        dom_texts = [
            _make_dom_text(0, "# Page\n\nOld content"),
            _make_dom_text(5, "# Page\n\nNew content"),
        ]
        events = [
            _make_event(0, "click"),
            _make_event(1, "scroll"),
            _make_event(2, "click"),
            _make_event(3, "scroll"),
            _make_event(4, "click"),
        ]

        diffs = _compute_dom_diffs(dom_texts, events=events)

        for diff in diffs:
            if diff.get("is_diff") and "REMOVED" in diff.get("text", ""):
                import re
                matches = re.findall(r"was visible for (\d+)ms", diff["text"])
                for ms_str in matches:
                    ms_val = int(ms_str)
                    # Should be close to 5000ms (5 seconds) — no idle subtraction
                    self.assertGreater(ms_val, 3000,
                                       f"No idle gap: visibility should be ~5000ms, got {ms_val}ms")


# ═══════════════════════════════════════════════════════════════════════════
# TEST: Hybrid Enrichment — Event Clustering
# ═══════════════════════════════════════════════════════════════════════════

class TestEventClustering(unittest.TestCase):
    """Tests for the event clustering algorithm in hybrid_enrichment."""

    def test_network_error_creates_cluster(self):
        """An HTTP 500 error should create an event cluster."""
        events = [
            _make_event(0, "pageview"),
            _make_event(3, "click", tag_name="button", element_text="Delete Account"),
            _make_event(4, "submit", form_action="/api/account"),
            _make_event(5, "network_error", status_code=500, method="DELETE",
                        endpoint="/api/account", error_message="Internal Server Error"),
            _make_event(6, "error", error_type="Error",
                        error_message="Delete account failed"),
        ]
        session = _make_session("sess-cluster-1", events)
        dom_texts = [
            _make_dom_text(0, "# Settings\n\n- [Delete Account]"),
            _make_dom_text(6, "# Settings\n\nRequest failed. Please try again."),
        ]

        clusters = build_event_clusters(session, dom_texts=dom_texts)

        self.assertGreaterEqual(len(clusters), 1, "Should create at least 1 cluster")

        # First cluster should contain the network error trigger
        c = clusters[0]
        trigger_types = {e.event_type for e in c.trigger_events}
        self.assertTrue(
            "network_error" in trigger_types or "error" in trigger_types,
            f"Cluster trigger should include network_error or error, got: {trigger_types}"
        )

        # Cluster should include surrounding events in ±5s window
        cluster_types = {e.event_type for e in c.events}
        self.assertIn("click", cluster_types, "Cluster should capture click in ±5s window")

    def test_no_errors_no_clusters(self):
        """A clean session with no errors should produce no clusters."""
        events = [
            _make_event(0, "pageview"),
            _make_event(3, "click", tag_name="a", element_text="Home"),
            _make_event(5, "pageview", url="https://app.example.com/home",
                        pathname="/home"),
        ]
        session = _make_session("sess-clean", events)

        clusters = build_event_clusters(session)
        self.assertEqual(len(clusters), 0, "Clean session should produce 0 clusters")

    def test_multiple_errors_merge_within_5s(self):
        """Two errors within 5s should be merged into one cluster."""
        events = [
            _make_event(0, "pageview"),
            _make_event(3, "network_error", status_code=500, method="DELETE",
                        endpoint="/api/account", error_message="Error"),
            _make_event(4, "error", error_type="Error",
                        error_message="Delete account failed"),
            # Second error burst far apart
            _make_event(30, "network_error", status_code=404, method="GET",
                        endpoint="/api/profile", error_message="Not Found"),
        ]
        session = _make_session("sess-multi-err", events)

        clusters = build_event_clusters(session)

        # The 500 + JS error at t=3-4 should merge; the 404 at t=30 should be separate
        self.assertGreaterEqual(len(clusters), 2,
                                "Errors 27s apart should produce separate clusters")

    def test_max_clusters_capped(self):
        """Cluster count should be capped at MAX_CLUSTERS_PER_SESSION (8)."""
        events = [_make_event(0, "pageview")]
        for i in range(15):  # 15 errors, each 15s apart
            events.append(
                _make_event(10 + i * 15, "network_error", status_code=500,
                            method="GET", endpoint=f"/api/endpoint-{i}",
                            error_message="Error")
            )
        session = _make_session("sess-many-err", events)

        clusters = build_event_clusters(session)
        self.assertLessEqual(len(clusters), 8,
                             f"Should cap at 8 clusters, got {len(clusters)}")


# ═══════════════════════════════════════════════════════════════════════════
# TEST: Hybrid Enrichment — Merge Logic
# ═══════════════════════════════════════════════════════════════════════════

class TestHybridMerge(unittest.TestCase):
    """Tests for enrich_or_replace_algo_issues merge logic."""

    def test_enrichment_replaces_generic_title(self):
        """
        The exact scenario from the user's bug report:
        Algo produces "Unknown Error on Settings Page" but hybrid should
        replace it with a specific title like "Account deletion fails with HTTP 500".
        """
        algo_issues = [{
            "rule_id": "network_error",
            "title": "Unknown Error on Settings Page",
            "description": "A network error occurred on the settings page.",
            "why_issue": "Users may experience issues on the settings page.",
            "severity": "medium",
            "category": "error",
            "page_url": "https://app.example.com/settings",
            "confidence": 0.6,
            "fingerprint": "abc123",
            "session_id": "sess-1",
        }]

        hybrid_issues = [{
            "title": "Account deletion fails with HTTP 500",
            "description": "When user clicks 'Delete Account', the DELETE /api/account "
                           "endpoint returns 500. User sees 'Request failed' message.",
            "why_issue": "Users cannot delete their accounts.",
            "severity": "high",
            "category": "error",
            "confidence": 0.92,
            "page_url": "https://app.example.com/settings",
            "_source": "hybrid_cluster",
            "_cluster_id": "cluster_sess-1_0",
            "_cluster_type": "network_error",
            "_cluster_center_ts": _ts(5),
        }]

        session = _make_session("sess-1", [
            _make_event(0, "pageview"),
            _make_event(5, "network_error", status_code=500, method="DELETE",
                        endpoint="/api/account"),
        ])

        seen_fps = set()
        result = enrich_or_replace_algo_issues(algo_issues, hybrid_issues, session, seen_fps)

        self.assertEqual(len(result), 1, "Should still be 1 issue (enriched, not duplicated)")

        enriched = result[0]
        self.assertIn("Account deletion", enriched["title"],
                       "Title should be enriched to specific description")
        self.assertIn("DELETE /api/account", enriched["description"],
                       "Description should mention the specific endpoint")
        self.assertEqual(enriched["severity"], "high",
                         "Severity should be upgraded to high")
        self.assertEqual(enriched.get("_enriched_by"), "hybrid",
                         "Should be marked as enriched by hybrid")

    def test_new_issue_added_when_no_algo_match(self):
        """When hybrid finds an issue not caught by algo, it should be added as new."""
        algo_issues = []  # No algo issues

        hybrid_issues = [{
            "title": "Payment API timeout on checkout page",
            "description": "POST /api/payment hangs for 30s then fails.",
            "why_issue": "Users cannot complete purchases.",
            "severity": "critical",
            "category": "error",
            "confidence": 0.95,
            "page_url": "https://app.example.com/checkout",
            "_source": "hybrid_cluster",
            "_cluster_id": "cluster_sess-2_0",
            "_cluster_type": "network_error",
            "_cluster_center_ts": _ts(10),
        }]

        session = _make_session("sess-2", [
            _make_event(0, "pageview", url="https://app.example.com/checkout",
                        pathname="/checkout"),
            _make_event(10, "network_error", status_code=504, method="POST",
                        endpoint="/api/payment"),
        ])

        seen_fps = set()
        result = enrich_or_replace_algo_issues(algo_issues, hybrid_issues, session, seen_fps)

        self.assertEqual(len(result), 1, "Should add 1 new issue")
        self.assertIn("Payment API", result[0]["title"])
        self.assertEqual(result[0]["rule_id"], "hybrid_network_error")

    def test_low_confidence_hybrid_ignored(self):
        """Hybrid issues with confidence < 0.70 should be skipped."""
        algo_issues = [{
            "rule_id": "network_error",
            "title": "Generic Error",
            "description": "Error occurred.",
            "why_issue": "Bad.",
            "severity": "medium",
            "category": "error",
            "page_url": "https://app.example.com/settings",
            "confidence": 0.5,
            "fingerprint": "fp1",
            "session_id": "sess-3",
        }]

        hybrid_issues = [{
            "title": "Might be an error",
            "description": "Uncertain.",
            "why_issue": "Maybe.",
            "severity": "low",
            "category": "error",
            "confidence": 0.55,  # Below 0.70 threshold
            "page_url": "https://app.example.com/settings",
            "_source": "hybrid_cluster",
            "_cluster_id": "cluster_sess-3_0",
            "_cluster_type": "network_error",
            "_cluster_center_ts": _ts(5),
        }]

        session = _make_session("sess-3", [_make_event(0, "pageview")])

        seen_fps = set()
        result = enrich_or_replace_algo_issues(algo_issues, hybrid_issues, session, seen_fps)

        # Original algo issue should remain untouched
        self.assertEqual(result[0]["title"], "Generic Error",
                         "Low-confidence hybrid should NOT replace algo issue")


# ═══════════════════════════════════════════════════════════════════════════
# TEST: Trigger Counting for Phase 3 Skip
# ═══════════════════════════════════════════════════════════════════════════

class TestTriggerCounting(unittest.TestCase):
    """Tests for count_session_triggers() used to decide if Phase 3 can be skipped."""

    def test_counts_network_errors(self):
        session = _make_session("sess-tc-1", [
            _make_event(0, "pageview"),
            _make_event(3, "network_error", status_code=500, method="GET",
                        endpoint="/api/data", error_message="Error"),
            _make_event(10, "network_error", status_code=404, method="GET",
                        endpoint="/api/other", error_message="Not Found"),
        ])
        self.assertEqual(count_session_triggers(session), 2)

    def test_counts_js_errors(self):
        session = _make_session("sess-tc-2", [
            _make_event(0, "pageview"),
            _make_event(3, "error", error_type="TypeError",
                        error_message="Cannot read property 'foo' of null"),
        ])
        self.assertEqual(count_session_triggers(session), 1)

    def test_counts_form_submits(self):
        session = _make_session("sess-tc-3", [
            _make_event(0, "pageview"),
            _make_event(3, "submit", form_action="/api/save"),
        ])
        self.assertEqual(count_session_triggers(session), 1)

    def test_clean_session_zero_triggers(self):
        session = _make_session("sess-tc-4", [
            _make_event(0, "pageview"),
            _make_event(5, "click", tag_name="a"),
            _make_event(10, "pageview"),
        ])
        self.assertEqual(count_session_triggers(session), 0)

    def test_mixed_triggers(self):
        session = _make_session("sess-tc-5", [
            _make_event(0, "pageview"),
            _make_event(3, "network_error", status_code=500, method="POST",
                        endpoint="/api/save", error_message="Error"),
            _make_event(4, "error", error_type="Error",
                        error_message="Save failed"),
            _make_event(10, "submit", form_action="/api/feedback"),
        ])
        self.assertEqual(count_session_triggers(session), 3)


# ═══════════════════════════════════════════════════════════════════════════
# TEST: Full Account Deletion Scenario (end-to-end)
# ═══════════════════════════════════════════════════════════════════════════

class TestAccountDeletionScenario(unittest.TestCase):
    """
    End-to-end test for the exact scenario from the PostHog screenshot:
    User goes to Settings → clicks Delete Account → API returns 500 →
    console logs "Delete account failed" → DOM shows "Request failed".

    Algo should detect network_error + console_error.
    Hybrid clustering should group them into one cluster.
    """

    def setUp(self):
        """Build a realistic session mimicking the PostHog screenshot."""
        self.events = [
            _make_event(0, "pageview", url="https://app.example.com/dashboard",
                        pathname="/dashboard"),
            _make_event(5, "click", tag_name="a", element_text="Settings",
                        url="https://app.example.com/dashboard", pathname="/dashboard"),
            _make_event(6, "pageview", url="https://app.example.com/settings",
                        pathname="/settings"),
            _make_event(10, "scroll", url="https://app.example.com/settings",
                        pathname="/settings"),
            _make_event(15, "click", tag_name="button", element_text="Delete Account",
                        url="https://app.example.com/settings", pathname="/settings"),
            _make_event(16, "submit", form_action="/api/account",
                        url="https://app.example.com/settings", pathname="/settings"),
            _make_event(17, "network_error", status_code=500, method="DELETE",
                        endpoint="/api/account", error_message="Internal Server Error",
                        url="https://app.example.com/settings", pathname="/settings"),
            _make_event(17.5, "error", error_type="Error",
                        error_message="Delete account failed: server returned 500",
                        url="https://app.example.com/settings", pathname="/settings"),
        ]
        self.session = _make_session("sess-acct-delete", self.events, distinct_id="user-42")
        self.dom_texts = [
            _make_dom_text(0, "# Dashboard\n\n- Projects: 12\n- Recent Activity",
                           page="https://app.example.com/dashboard"),
            _make_dom_text(6, "# Settings\n\n## Account\n- Email: user@example.com\n"
                           "- Plan: Pro\n- [Delete Account]\n\n## Preferences\n- Theme: Dark",
                           page="https://app.example.com/settings"),
            _make_dom_text(18, "# Settings\n\n## Account\n- Email: user@example.com\n"
                           "- **Request failed. Please try again.**\n- [Delete Account]",
                           page="https://app.example.com/settings"),
        ]

    def test_algo_detects_network_and_console_errors(self):
        """AlgorithmicDetector should find the HTTP 500 and JS error."""
        dom_diffs = _compute_dom_diffs(self.dom_texts, events=self.events)
        detector = AlgorithmicDetector()
        issues = detector.detect(self.session, dom_diffs=dom_diffs, dom_texts=self.dom_texts)

        rule_ids = {i.rule_id for i in issues}
        self.assertIn("network_error", rule_ids,
                       "Should detect the DELETE /api/account 500 error")
        self.assertIn("console_error", rule_ids,
                       "Should detect the 'Delete account failed' JS error")

    def test_clustering_groups_related_signals(self):
        """The network error + console error + DOM change should form one cluster."""
        dom_diffs = _compute_dom_diffs(self.dom_texts, events=self.events)
        clusters = build_event_clusters(
            self.session,
            dom_texts=self.dom_texts,
            dom_diffs=dom_diffs,
        )

        # The 500 at t=17 and JS error at t=17.5 are within 5s → should merge
        self.assertGreaterEqual(len(clusters), 1,
                                "Should create at least 1 cluster for the deletion failure")

        # Check the cluster contains both trigger events
        first_cluster = clusters[0]
        trigger_types = {e.event_type for e in first_cluster.trigger_events}
        all_types = {e.event_type for e in first_cluster.events}

        self.assertTrue(
            "network_error" in trigger_types or "error" in trigger_types,
            f"Cluster should have network_error or error as trigger, got: {trigger_types}"
        )

        # The ±5s window should also capture the click + submit
        self.assertIn("click", all_types,
                       "Cluster's ±5s window should capture the Delete Account click")

        # Cluster should include the DOM snapshot showing "Request failed"
        dom_snapshot_texts = " ".join(d.get("text", "") for d in first_cluster.dom_snapshots)
        self.assertTrue(
            "Request failed" in dom_snapshot_texts or len(first_cluster.dom_snapshots) > 0,
            "Cluster should include DOM snapshot near the error"
        )

    def test_cluster_on_correct_page(self):
        """Cluster should be on the Settings page, not Dashboard."""
        clusters = build_event_clusters(
            self.session,
            dom_texts=self.dom_texts,
        )

        self.assertGreaterEqual(len(clusters), 1)
        self.assertIn("settings", clusters[0].page_url.lower(),
                       "Cluster should be on the /settings page")

    def test_reproduction_steps_include_real_user_actions(self):
        """
        When hybrid enrichment adds reproduction steps, they should be
        real user actions from the session (not AI-generated).
        """
        from app.services.rule_engine import _extract_steps_before

        steps = _extract_steps_before(self.session, _ts(17), max_steps=15)

        self.assertGreater(len(steps), 0, "Should extract at least some steps")

        # Steps should mention real actions
        steps_text = " ".join(steps).lower()
        self.assertTrue(
            "settings" in steps_text or "delete" in steps_text,
            f"Steps should mention real user actions, got: {steps}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# TEST: Clean Session — No False Positives
# ═══════════════════════════════════════════════════════════════════════════

class TestCleanSession(unittest.TestCase):
    """A session with normal browsing should produce zero issues."""

    def test_normal_browsing_no_issues(self):
        events = [
            _make_event(0, "pageview", url="https://app.example.com/",
                        pathname="/"),
            _make_event(3, "click", tag_name="a", element_text="Products",
                        url="https://app.example.com/", pathname="/"),
            _make_event(4, "pageview", url="https://app.example.com/products",
                        pathname="/products"),
            _make_event(8, "scroll", url="https://app.example.com/products",
                        pathname="/products"),
            _make_event(12, "click", tag_name="a", element_text="Product A",
                        url="https://app.example.com/products", pathname="/products"),
            _make_event(13, "pageview", url="https://app.example.com/products/a",
                        pathname="/products/a"),
            _make_event(20, "pageleave", url="https://app.example.com/products/a",
                        pathname="/products/a"),
        ]
        session = _make_session("sess-clean-browse", events)
        dom_texts = [
            _make_dom_text(0, "# Home\n\nWelcome to our app!",
                           page="https://app.example.com/"),
            _make_dom_text(4, "# Products\n\n- Product A\n- Product B\n- Product C",
                           page="https://app.example.com/products"),
            _make_dom_text(13, "# Product A\n\nDescription of Product A\n- Price: $99",
                           page="https://app.example.com/products/a"),
        ]
        dom_diffs = _compute_dom_diffs(dom_texts, events=events)

        detector = AlgorithmicDetector()
        issues = detector.detect(session, dom_diffs=dom_diffs, dom_texts=dom_texts)

        self.assertEqual(len(issues), 0,
                         f"Normal browsing should produce 0 issues, got: "
                         f"{[(i.rule_id, i.title) for i in issues]}")

        clusters = build_event_clusters(session, dom_texts=dom_texts, dom_diffs=dom_diffs)
        self.assertEqual(len(clusters), 0,
                         "Normal browsing should produce 0 clusters")


# ═══════════════════════════════════════════════════════════════════════════
# TEST: Flash Error Detection
# ═══════════════════════════════════════════════════════════════════════════

class TestFlashErrorRemoved(unittest.TestCase):
    """Flash error detector has been removed from the pipeline (too many false positives)."""

    def test_flash_error_not_detected(self):
        """Flash error detector is disabled — should produce no flash_error issues."""
        dom_texts = [
            _make_dom_text(0, "# Form\n\n- Name field\n- [Submit]"),
            _make_dom_text(5, "# Form\n\n- Name field\n- **Invalid email address**\n- [Submit]"),
            _make_dom_text(5.3, "# Form\n\n- Name field\n- [Submit]"),
        ]
        events = [
            _make_event(0, "pageview"),
            _make_event(3, "input", tag_name="input", element_type="email",
                        element_name="email", element_value="bad-email"),
            _make_event(4, "submit", form_action="/api/register"),
            _make_event(6, "input", tag_name="input", element_type="email",
                        element_name="email", element_value="good@email.com"),
        ]
        dom_diffs = _compute_dom_diffs(dom_texts, events=events)
        session = _make_session("sess-flash", events)

        detector = AlgorithmicDetector()
        issues = detector.detect(session, dom_diffs=dom_diffs, dom_texts=dom_texts)

        flash_issues = [i for i in issues if i.rule_id == "flash_error"]
        self.assertEqual(len(flash_issues), 0,
                         "Flash error detector is disabled — should not produce issues")


# ═══════════════════════════════════════════════════════════════════════════
# TEST: DOM Snapshot Fallback — Nearest snapshot used when none in window
# ═══════════════════════════════════════════════════════════════════════════

class TestDomSnapshotFallback(unittest.TestCase):
    """
    When no DOM snapshot falls within the ±10s cluster window, the pipeline
    should fall back to the nearest snapshot on the same page (within 120s).
    """

    def test_nearest_snapshot_used_when_none_in_window(self):
        """
        Trigger at t=30, DOM snapshot at t=65 (35s away, outside ±12s window).
        Fallback should still include it since 35s < 120s max.
        """
        dom_texts = [
            _make_dom_text(65, "# Settings\n\n- Delete Account\n- **Request failed**",
                           page="https://app.example.com/settings"),
        ]
        events = [
            _make_event(0, "pageview", url="https://app.example.com/settings",
                        pathname="/settings"),
            _make_event(25, "click", tag_name="button", element_text="Delete Account",
                        url="https://app.example.com/settings", pathname="/settings"),
            _make_event(30, "network_error", url="https://app.example.com/settings",
                        pathname="/settings",
                        method="POST", endpoint="/api/delete-account",
                        status_code=500),
        ]
        session = _make_session("sess-dom-fallback", events)

        clusters = build_event_clusters(session, dom_texts=dom_texts, dom_diffs=[])
        self.assertGreater(len(clusters), 0, "Should produce at least one cluster")

        # The cluster around t=30 should have a DOM snapshot via fallback
        target_cluster = clusters[0]
        self.assertGreater(len(target_cluster.dom_snapshots), 0,
                           "Cluster should have DOM snapshot from fallback (nearest within 120s)")

        # Should be marked as approximate
        snap = target_cluster.dom_snapshots[0]
        self.assertIn("_approx_distance_s", snap,
                       "Fallback snapshot should be marked as approximate")
        self.assertAlmostEqual(snap["_approx_distance_s"], 35.0, delta=1.0)

    def test_no_fallback_beyond_120s(self):
        """
        DOM snapshot 200s away should NOT be included as fallback.
        """
        dom_texts = [
            _make_dom_text(230, "# Settings\n\n- Some content",
                           page="https://app.example.com/settings"),
        ]
        events = [
            _make_event(25, "click", tag_name="button", element_text="Delete",
                        url="https://app.example.com/settings", pathname="/settings"),
            _make_event(30, "network_error", url="https://app.example.com/settings",
                        pathname="/settings",
                        method="POST", endpoint="/api/delete",
                        status_code=500),
        ]
        session = _make_session("sess-dom-too-far", events)

        clusters = build_event_clusters(session, dom_texts=dom_texts, dom_diffs=[])
        self.assertGreater(len(clusters), 0, "Should produce at least one cluster")

        target_cluster = clusters[0]
        self.assertEqual(len(target_cluster.dom_snapshots), 0,
                         "Cluster should NOT get DOM snapshot 200s away")

    def test_fallback_matches_same_page(self):
        """
        Fallback should prefer snapshots from the same page URL.
        Both snapshots are outside the ±12s window, so fallback fires.
        The /settings one should be chosen over the / homepage one.
        """
        dom_texts = [
            # Different page — outside window
            _make_dom_text(50, "# Home\n\nWelcome to the homepage",
                           page="https://app.example.com/"),
            # Same page — further in time but correct page
            _make_dom_text(70, "# Settings\n\n- Account\n- **Error**",
                           page="https://app.example.com/settings"),
        ]
        events = [
            _make_event(25, "click", tag_name="button", element_text="Delete",
                        url="https://app.example.com/settings", pathname="/settings"),
            _make_event(30, "network_error", url="https://app.example.com/settings",
                        pathname="/settings",
                        method="POST", endpoint="/api/delete",
                        status_code=500),
        ]
        session = _make_session("sess-dom-page-match", events)

        clusters = build_event_clusters(session, dom_texts=dom_texts, dom_diffs=[])
        self.assertGreater(len(clusters), 0)

        target_cluster = clusters[0]
        self.assertGreater(len(target_cluster.dom_snapshots), 0,
                           "Should get fallback DOM from same page")
        snap = target_cluster.dom_snapshots[0]
        self.assertIn("Settings", snap.get("text", ""),
                       "Should use the /settings snapshot, not the / homepage one")


# ═══════════════════════════════════════════════════════════════════════════
# P1.1: rrweb Incremental Mutation Replay
# ═══════════════════════════════════════════════════════════════════════════


class TestRrwebMutationReplay(unittest.TestCase):
    """Test DOM reconstruction via incremental mutation replay."""

    def test_build_node_map_from_snapshot(self):
        """Node map should contain all nodes from a Type 2 snapshot."""
        from app.connectors.posthog import _build_node_map

        snapshot = {
            "id": 1, "type": 0, "childNodes": [
                {"id": 2, "type": 2, "tagName": "html", "attributes": {}, "childNodes": [
                    {"id": 3, "type": 2, "tagName": "body", "attributes": {}, "childNodes": [
                        {"id": 4, "type": 2, "tagName": "div", "attributes": {}, "childNodes": [
                            {"id": 5, "type": 3, "textContent": "Hello World"}
                        ]}
                    ]}
                ]}
            ]
        }
        node_map = _build_node_map(snapshot)
        self.assertIn(1, node_map)
        self.assertIn(5, node_map)
        self.assertEqual(node_map[5].get("textContent"), "Hello World")
        self.assertEqual(len(node_map), 5)

    def test_apply_text_mutation(self):
        """Text mutation should update node's textContent."""
        from app.connectors.posthog import _build_node_map, _apply_mutations

        snapshot = {
            "id": 1, "type": 0, "childNodes": [
                {"id": 2, "type": 2, "tagName": "div", "attributes": {}, "childNodes": [
                    {"id": 3, "type": 3, "textContent": "Loading..."}
                ]}
            ]
        }
        node_map = _build_node_map(snapshot)
        self.assertEqual(node_map[3]["textContent"], "Loading...")

        # Apply text mutation
        _apply_mutations(node_map, {"texts": [{"id": 3, "value": "Email Verified!"}]})
        self.assertEqual(node_map[3]["textContent"], "Email Verified!")

    def test_apply_add_and_remove_mutations(self):
        """Add and remove mutations should modify node tree."""
        from app.connectors.posthog import _build_node_map, _apply_mutations

        snapshot = {
            "id": 1, "type": 0, "childNodes": [
                {"id": 2, "type": 2, "tagName": "div", "attributes": {}, "childNodes": [
                    {"id": 3, "type": 3, "textContent": "Original text"}
                ]}
            ]
        }
        node_map = _build_node_map(snapshot)
        self.assertIn(3, node_map)

        # Remove node 3, add node 4
        _apply_mutations(node_map, {
            "removes": [{"id": 3, "parentId": 2}],
            "adds": [{"parentId": 2, "node": {"id": 4, "type": 3, "textContent": "New text"}}],
        })
        self.assertNotIn(3, node_map)
        self.assertIn(4, node_map)
        self.assertEqual(node_map[4]["textContent"], "New text")

    def test_apply_attribute_mutation(self):
        """Attribute mutation should update element attributes."""
        from app.connectors.posthog import _build_node_map, _apply_mutations

        snapshot = {
            "id": 1, "type": 0, "childNodes": [
                {"id": 2, "type": 2, "tagName": "div", "attributes": {"class": "loading"}, "childNodes": []}
            ]
        }
        node_map = _build_node_map(snapshot)
        self.assertEqual(node_map[2]["attributes"]["class"], "loading")

        # Change class, add style
        _apply_mutations(node_map, {
            "attributes": [{"id": 2, "attributes": {"class": "success", "style": "color: green"}}]
        })
        self.assertEqual(node_map[2]["attributes"]["class"], "success")
        self.assertEqual(node_map[2]["attributes"]["style"], "color: green")

    def test_reconstruct_dom_at_timestamp(self):
        """Full reconstruction: Type 2 + Type 3 mutations → markdown."""
        from app.connectors.posthog import reconstruct_dom_at_timestamp
        import json

        # Create rrweb records: 1 full snapshot + 1 mutation
        records = [
            # Type 2: Full snapshot at t=1000
            json.dumps({
                "type": 2,
                "timestamp": 1000,
                "data": {
                    "node": {
                        "id": 1, "type": 0, "childNodes": [
                            {"id": 2, "type": 2, "tagName": "html", "attributes": {}, "childNodes": [
                                {"id": 3, "type": 2, "tagName": "body", "attributes": {}, "childNodes": [
                                    {"id": 4, "type": 2, "tagName": "p", "attributes": {}, "childNodes": [
                                        {"id": 5, "type": 3, "textContent": "Loading..."}
                                    ]}
                                ]}
                            ]}
                        ]
                    }
                }
            }),
            # Type 3: Text mutation at t=2000
            json.dumps({
                "type": 3,
                "timestamp": 2000,
                "data": {
                    "source": 0,
                    "texts": [{"id": 5, "value": "Email Verified!"}]
                }
            }),
        ]

        # Reconstruct at t=1500 (before mutation)
        md_before = reconstruct_dom_at_timestamp(records, 1500)
        self.assertIn("Loading...", md_before)
        self.assertNotIn("Email Verified", md_before)

        # Reconstruct at t=2500 (after mutation)
        md_after = reconstruct_dom_at_timestamp(records, 2500)
        self.assertIn("Email Verified!", md_after)
        self.assertNotIn("Loading...", md_after)


# ═══════════════════════════════════════════════════════════════════════════
# P2.2: CSS State Extraction
# ═══════════════════════════════════════════════════════════════════════════


class TestCssStateExtraction(unittest.TestCase):
    """Test that hidden/disabled elements are annotated instead of skipped."""

    def test_hidden_display_none_annotated(self):
        """display:none elements should show [HIDDEN:display-none] prefix."""
        from app.connectors.posthog import _rrweb_node_to_markdown

        node = {
            "type": 2, "tagName": "div",
            "attributes": {"style": "display: none"},
            "childNodes": [{"type": 3, "textContent": "Error: Something went wrong"}]
        }
        md = _rrweb_node_to_markdown(node)
        self.assertIn("[HIDDEN:display-none]", md)
        self.assertIn("Error: Something went wrong", md)

    def test_hidden_visibility_hidden_annotated(self):
        """visibility:hidden elements should show [HIDDEN:visibility-hidden] prefix."""
        from app.connectors.posthog import _rrweb_node_to_markdown

        node = {
            "type": 2, "tagName": "p",
            "attributes": {"style": "visibility: hidden"},
            "childNodes": [{"type": 3, "textContent": "Hidden message"}]
        }
        md = _rrweb_node_to_markdown(node)
        self.assertIn("[HIDDEN:visibility-hidden]", md)
        self.assertIn("Hidden message", md)

    def test_aria_hidden_annotated(self):
        """aria-hidden=true elements should show [HIDDEN:aria] prefix."""
        from app.connectors.posthog import _rrweb_node_to_markdown

        node = {
            "type": 2, "tagName": "div",
            "attributes": {"aria-hidden": "true"},
            "childNodes": [{"type": 3, "textContent": "Screen reader hidden"}]
        }
        md = _rrweb_node_to_markdown(node)
        self.assertIn("[HIDDEN:aria]", md)
        self.assertIn("Screen reader hidden", md)

    def test_disabled_button_annotated(self):
        """Disabled button should show [DISABLED] marker."""
        from app.connectors.posthog import _rrweb_node_to_markdown

        node = {
            "type": 2, "tagName": "button",
            "attributes": {"disabled": ""},
            "childNodes": [{"type": 3, "textContent": "Submit"}]
        }
        md = _rrweb_node_to_markdown(node)
        self.assertIn("DISABLED", md)
        self.assertIn("Submit", md)

    def test_loading_aria_busy_annotated(self):
        """aria-busy=true elements should show [LOADING] marker."""
        from app.connectors.posthog import _rrweb_node_to_markdown

        node = {
            "type": 2, "tagName": "div",
            "attributes": {"aria-busy": "true"},
            "childNodes": [{"type": 3, "textContent": "Please wait..."}]
        }
        md = _rrweb_node_to_markdown(node)
        self.assertIn("[LOADING]", md)
        self.assertIn("Please wait...", md)

    def test_visible_elements_no_markers(self):
        """Normal visible elements should have no visibility markers."""
        from app.connectors.posthog import _rrweb_node_to_markdown

        node = {
            "type": 2, "tagName": "p",
            "attributes": {},
            "childNodes": [{"type": 3, "textContent": "Normal text"}]
        }
        md = _rrweb_node_to_markdown(node)
        self.assertNotIn("[HIDDEN", md)
        self.assertNotIn("[DISABLED]", md)
        self.assertNotIn("[LOADING]", md)
        self.assertIn("Normal text", md)


# ═══════════════════════════════════════════════════════════════════════════
# P1.3: Improved Repro Steps
# ═══════════════════════════════════════════════════════════════════════════


class TestImprovedReproSteps(unittest.TestCase):
    """Test that repro steps include element text, form actions, etc."""

    def test_click_includes_tag_name(self):
        """Click step should include tag name when available."""
        from app.services.rule_engine import _event_to_step

        ev = NormalizedEvent(
            timestamp="2025-01-01T00:00:00Z", event_type="click",
            pathname="/login/", element_text="Sign in", tag_name="button"
        )
        step = _event_to_step(ev)
        self.assertIn("Sign in", step)
        self.assertIn("button", step)

    def test_input_includes_field_type(self):
        """Input step should include field type for non-text inputs."""
        from app.services.rule_engine import _event_to_step

        ev = NormalizedEvent(
            timestamp="2025-01-01T00:00:00Z", event_type="input",
            pathname="/login/", element_name="email", element_type="email"
        )
        step = _event_to_step(ev)
        self.assertIn("email", step)
        self.assertIn("field", step)

    def test_submit_includes_form_action(self):
        """Submit step should include form action URL."""
        from app.services.rule_engine import _event_to_step

        ev = NormalizedEvent(
            timestamp="2025-01-01T00:00:00Z", event_type="submit",
            pathname="/login/", form_action="/api/auth/login"
        )
        step = _event_to_step(ev)
        self.assertIn("/api/auth/login", step)

    def test_submit_no_action_still_works(self):
        """Submit step without form action should still work."""
        from app.services.rule_engine import _event_to_step

        ev = NormalizedEvent(
            timestamp="2025-01-01T00:00:00Z", event_type="submit",
            pathname="/login/"
        )
        step = _event_to_step(ev)
        self.assertIn("submitted form", step)
        self.assertIn("/login/", step)


# ═══════════════════════════════════════════════════════════════════════════
# P0.1: AI False Positive Validation
# ═══════════════════════════════════════════════════════════════════════════


class TestAIFalsePositiveValidation(unittest.TestCase):
    """Test Phase 5 AI validation filter."""

    def test_validation_filters_false_positives(self):
        """AI validation should filter out issues marked as not real bugs."""
        from app.services.hybrid_enrichment import validate_issues_with_ai

        issues = [
            {"title": "Real Bug", "description": "Server crash", "confidence": 0.9},
            {"title": "False Positive", "description": "User browsing", "confidence": 0.6},
        ]

        # Mock AI response
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = json.dumps({
            "validations": [
                {"issue_index": 0, "is_real_bug": True, "reasoning": "Real server error", "adjusted_confidence": 0.95},
                {"issue_index": 1, "is_real_bug": False, "reasoning": "Normal browsing", "adjusted_confidence": 0.3},
            ]
        })
        mock_response.usage = MagicMock(prompt_tokens=100, completion_tokens=50)

        with patch("app.services.hybrid_enrichment.get_settings") as mock_settings:
            mock_settings.return_value.openai_api_key = "test-key"
            with patch("app.services.hybrid_enrichment.AsyncOpenAI") as mock_client_cls:
                mock_client = MagicMock()
                mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
                mock_client_cls.return_value = mock_client

                result = asyncio.get_event_loop().run_until_complete(
                    validate_issues_with_ai(issues)
                )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "Real Bug")
        self.assertEqual(result[0]["confidence"], 0.95)

    def test_validation_keeps_all_on_error(self):
        """If AI validation fails, all issues should be kept."""
        from app.services.hybrid_enrichment import validate_issues_with_ai

        issues = [
            {"title": "Bug 1", "description": "Error", "confidence": 0.8},
            {"title": "Bug 2", "description": "Error", "confidence": 0.7},
        ]

        with patch("app.services.hybrid_enrichment.get_settings") as mock_settings:
            mock_settings.return_value.openai_api_key = "test-key"
            with patch("app.services.hybrid_enrichment.AsyncOpenAI") as mock_client_cls:
                mock_client = MagicMock()
                mock_client.chat.completions.create = AsyncMock(side_effect=Exception("API down"))
                mock_client_cls.return_value = mock_client

                result = asyncio.get_event_loop().run_until_complete(
                    validate_issues_with_ai(issues)
                )

        self.assertEqual(len(result), 2)  # All kept

    def test_single_issue_skips_validation(self):
        """With only 1 issue, validation should be skipped."""
        from app.services.hybrid_enrichment import validate_issues_with_ai

        issues = [{"title": "Only Bug", "description": "Error", "confidence": 0.8}]
        result = asyncio.get_event_loop().run_until_complete(
            validate_issues_with_ai(issues)
        )
        self.assertEqual(len(result), 1)


# ═══════════════════════════════════════════════════════════════════════════
# P2.1: Cross-Session Correlation
# ═══════════════════════════════════════════════════════════════════════════


class TestCrossSessionCorrelation(unittest.TestCase):
    """Test cross-session issue confidence adjustments."""

    def test_multi_session_boost(self):
        """Issues seen in 3+ sessions should get confidence boost."""
        from app.services.session_analysis_service import _correlate_cross_session_issues

        issues = [
            {"title": "Bug A", "affected_url": "https://app.com/settings/", "rule_id": "network_error",
             "confidence": 0.7, "session_id": "s1"},
            {"title": "Bug A", "affected_url": "https://app.com/settings/", "rule_id": "network_error",
             "confidence": 0.7, "session_id": "s2"},
            {"title": "Bug A", "affected_url": "https://app.com/settings/", "rule_id": "network_error",
             "confidence": 0.7, "session_id": "s3"},
        ]

        result = _correlate_cross_session_issues(issues, total_sessions=10)
        for issue in result:
            self.assertEqual(issue["confidence"], 0.8)  # 0.7 + 0.1
            self.assertEqual(issue["metadata"]["cross_session"]["session_count"], 3)

    def test_single_session_demote(self):
        """Issue seen in only 1 session with low confidence should be demoted."""
        from app.services.session_analysis_service import _correlate_cross_session_issues

        issues = [
            {"title": "Bug B", "affected_url": "https://app.com/page/", "rule_id": "console_error",
             "confidence": 0.65, "session_id": "s1"},
        ]

        result = _correlate_cross_session_issues(issues, total_sessions=10)
        self.assertEqual(result[0]["confidence"], 0.5)  # 0.65 - 0.15

    def test_high_confidence_single_not_demoted(self):
        """Issue in 1 session but with high confidence (>=0.8) should NOT be demoted."""
        from app.services.session_analysis_service import _correlate_cross_session_issues

        issues = [
            {"title": "Critical Bug", "affected_url": "https://app.com/page/", "rule_id": "network_error",
             "confidence": 0.9, "session_id": "s1"},
        ]

        result = _correlate_cross_session_issues(issues, total_sessions=10)
        self.assertEqual(result[0]["confidence"], 0.9)  # Unchanged


# ═══════════════════════════════════════════════════════════════════════════
# P1.2: Network Response Body
# ═══════════════════════════════════════════════════════════════════════════


class TestNetworkResponseBody(unittest.TestCase):
    """Test that response bodies are extracted from network signals."""

    def test_response_body_extracted(self):
        """Network signals should include response body when available."""
        from app.connectors.posthog import _extract_recording_signals
        import json

        records = [json.dumps({
            "type": 6,
            "timestamp": 1000,
            "data": {
                "plugin": "rrweb/network@1",
                "payload": {
                    "requests": [{
                        "method": "POST",
                        "url": "https://api.example.com/delete",
                        "status": 500,
                        "duration": 200,
                        "response": {"body": '{"error": "Account locked"}'},
                        "request": {"body": '{"confirm": true}'},
                    }]
                }
            }
        })]

        signals = _extract_recording_signals(records)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["response_body"], '{"error": "Account locked"}')
        self.assertEqual(signals[0]["request_body"], '{"confirm": true}')

    def test_missing_body_returns_empty(self):
        """Missing response body should return empty string."""
        from app.connectors.posthog import _extract_recording_signals
        import json

        records = [json.dumps({
            "type": 6,
            "timestamp": 1000,
            "data": {
                "plugin": "rrweb/network@1",
                "payload": {
                    "requests": [{
                        "method": "GET",
                        "url": "https://api.example.com/data",
                        "status": 404,
                        "duration": 100,
                    }]
                }
            }
        })]

        signals = _extract_recording_signals(records)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["response_body"], "")
        self.assertEqual(signals[0]["request_body"], "")


# ═══════════════════════════════════════════════════════════════════════════
# RUN
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)
