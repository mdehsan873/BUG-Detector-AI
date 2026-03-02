"""
AI-powered session analysis service.

Provider-agnostic: uses the connector abstraction to fetch sessions
from PostHog, FullStory, LogRocket, Clarity, etc.
Then reconstructs user journeys and uses OpenAI to find UX issues
that rule-based detection misses.
"""

import asyncio
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urlparse, urlunparse

from openai import AsyncOpenAI

from app.config import get_settings
from app.connectors.base import NormalizedEvent, NormalizedSession
from app.utils.cost_tracker import CostTracker
from app.utils.logger import logger


def _normalize_url(url: str) -> str:
    """
    Normalize a URL for deduplication: strip fragment (#), query string (?),
    trailing slashes, and lowercase. Works with both full URLs and paths.

    Examples:
        https://example.com/billing/#       → https://example.com/billing
        https://example.com/auth/callback/# → https://example.com/auth/callback
        /billing/                           → /billing
        /verify?token=abc                   → /verify
    """
    if not url:
        return ""
    url = url.strip()
    # If it looks like a full URL, parse properly
    if url.startswith("http://") or url.startswith("https://"):
        parsed = urlparse(url)
        # Rebuild without fragment and query
        cleaned = urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
        return cleaned.rstrip("/").lower()
    # Path-only: strip fragment and query manually
    url = url.split("#")[0].split("?")[0]
    return url.rstrip("/").lower()


def _text_similarity(a: str, b: str) -> float:
    """Return similarity ratio (0.0–1.0) between two strings."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def _is_fuzzy_duplicate(new_title: str, existing_issues: list[dict], new_page: str = "") -> bool:
    """
    Check if a new issue is a fuzzy duplicate of any already-collected issue.
    - Title similarity >=90%: always a duplicate
    - Title similarity >=75% AND same normalized page: also a duplicate
    """
    if not new_title:
        return False
    new_page_norm = _normalize_url(new_page) if new_page else ""
    for existing in existing_issues:
        existing_title = existing.get("title", "")
        if not existing_title:
            continue
        score = _text_similarity(new_title, existing_title)
        if score >= 0.90:
            return True
        # If pages match too, lower the bar — likely same issue with different AI wording
        if score >= 0.75 and new_page_norm:
            existing_page = _normalize_url(existing.get("page_url", ""))
            if existing_page == new_page_norm:
                return True
    return False


SESSION_ANALYSIS_PROMPT = """You are a senior QA engineer reviewing a real user session recording. You must find CONFIRMED BUGS — not speculative UX opinions.

CRITICAL RULES TO AVOID FALSE POSITIVES:
- A user visiting the same page multiple times is NOT a bug — they may be comparing options, re-reading, or coming back intentionally.
- A user navigating back and forth is NOT a bug — this is normal browsing behavior.
- A user spending time on a page is NOT a "dead end" — they may be reading, thinking, or using the page normally.
- A user going through onboarding/billing/settings multiple times may be exploring or deciding — only flag if there is an ERROR or FAILURE.
- Only flag something as an issue if you see CONCRETE EVIDENCE of failure: an error message, a failed network request, a click that provably did nothing, a form that lost data.

WHAT COUNTS AS A REAL BUG:
1. **Errors**: JavaScript exceptions, failed API calls (4xx/5xx), console errors visible in the timeline
2. **Broken UI**: A button/link that was clicked but produced zero response (no navigation, no state change, no network request)
3. **Data Loss**: A form submission that failed, user input that disappeared
4. **Rage Clicks**: 3+ rapid clicks on the same element (explicit rage click events)
5. **Crashes/Freezes**: Page becoming unresponsive, infinite loading states

=== UX / FORM BUG DETECTION ===
Pay special attention to these common UX bugs that users often encounter:

6. **Form Validation Misdirection**: User submits a form but the validation error message appears next to the WRONG field. Look for:
   - SUBMIT event followed by the user clicking/focusing a field that is NOT the one with the error
   - User clicking submit, then scrolling or focusing a different area than where the error is
   - Multiple rapid focus changes between fields after a failed submit (user confused about which field has error)

7. **Invisible/Off-screen Errors**: Error messages that appear but the user cannot see them. Look for:
   - SUBMIT followed by no navigation AND user scrolling up/down (they are looking for the error)
   - Validation errors at top of form but user is scrolled to bottom (viewport/scroll data shows this)
   - User re-clicking submit multiple times without changing any fields (they don't see the error)

8. **Form Submit Failures Without Feedback**: User clicks submit but nothing visible happens. Look for:
   - SUBMIT event → no PAGEVIEW, no NETWORK request, no visible change
   - User clicking submit button repeatedly (not a rage click, but 2-3 clicks with pauses)
   - SUBMIT → NETWORK_ERROR but no error shown to user (user doesn't know it failed)

9. **Input Confusion / Wrong Field Focus**: User interacts with wrong form element. Look for:
   - User clicking on a label/area but the wrong input gets focused
   - Rapid tab/focus cycling through fields (user lost track of which field is active)
   - User typing into one field then immediately clearing it and typing in another (autofocus sent them to wrong field)

10. **Dead/Broken Interactive Elements**: Buttons, links, inputs that don't respond. Look for:
    - DEAD_CLICK events on elements that should be interactive
    - Click on a submit-like element (<button>, input[type=submit]) with no form submission
    - Click on what looks like a link but no navigation occurs

11. **Layout/Overlap Issues**: UI elements blocking each other. Look for:
    - User clicking an area multiple times then clicking slightly offset (element hidden behind another)
    - Scroll position suggesting the user is trying to reach an element that's off-screen
    - Clicks on elements near the bottom of viewport (potential sticky footer overlap)

12. **Form Data Loss**: User fills form but data is lost. Look for:
    - User inputs data → navigates away → comes back → form is empty (no evidence of save)
    - SUBMIT → ERROR → user has to re-fill the entire form
    - Input values disappearing (input event with value, then same field with empty value)

=== SESSION-LEVEL BUG DETECTION ===
These bugs affect the entire session flow, not just a single element:

13. **Refresh as Workaround**: User manually refreshes to fix a broken page. Look for:
    - Three consecutive PAGEVIEWs to the SAME URL (or same pathname) — this is a page refresh and withing 5 seconds of the previous pageview
    - After the refresh, the user successfully interacts with the page (clicks, inputs, scrolls) — proves the first load was broken
    - Multiple refreshes on the same page = severe issue
    - IMPORTANT: Only flag if the user interacts successfully AFTER the refresh. If they refresh and leave, it may just be re-reading
    - A single refresh followed by normal usage is medium severity; multiple refreshes is high
    - NEVER flag auth/registration pages (/login, /register, /signup, /signin, /verify, /confirm, /callback, /forgot-password, /reset-password) as refresh workarounds. Visiting /login → /register → /login → /verify is a NORMAL registration flow, NOT a refresh.

14. **Session Expiry / Auth Kicked Mid-Task**: User loses their session while actively working. Look for:
    - User is actively interacting (clicking, typing) on an authenticated page (/dashboard, /settings, /app, etc.)
    - Then suddenly a PAGEVIEW to /login, /signin, /auth, or /session-expired appears WITHOUT the user clicking a logout button
    - A 401 NETWORK_ERROR followed by redirect to login is the strongest signal
    - ESPECIALLY critical if the user was filling a form — they lose all their work
    - IMPORTANT: If the user clicks a "Logout" or "Sign out" button BEFORE the redirect, that's intentional — NOT a bug

15. **Broken Back Button / Navigation Corruption**: Browser history is broken. Look for:
    - User navigates A → B → C, then back button should go C → B → A
    - If back produces C → A (skipping B) or C → C (staying on same page) or C → unexpected page
    - Two consecutive PAGEVIEWs where the second one goes to a page that doesn't make sense in the navigation flow
    - User hitting back and immediately navigating forward again (the back didn't go where expected)

16. **Double/Duplicate Action**: Same action fires twice. Look for:
    - Two identical NETWORK requests (same endpoint, same method) within 2 seconds after a single CLICK or SUBMIT
    - This can cause duplicate orders, duplicate messages, duplicate database entries
    - SUBMIT followed by two POST requests to the same endpoint = likely missing double-submit protection

=== DOM VISIBILITY DURATION RULES ===
The timeline may include DOM_DIFF entries with visibility durations like "(was visible for 1.2s)".
These durations have ALREADY been adjusted for user inactivity (tab hidden, user away) — idle time is subtracted.
Use these to determine if a removed message was actually a bug:

17. **Flash Error / Disappearing Message** (category: "broken_ui"): An error or feedback message appears and disappears too fast for the user to read. Look for:
    - DOM_DIFF ADDED: error/alert text, then REMOVED with "(was visible for Xms)" or "(was visible for <1s)" — under 1 second
    - This means the UI showed an error but removed it before the user could read it
    - Common cause: React state reset, auto-dismiss timer too short, re-render clearing error state
    - Severity: HIGH if the message was important (login error, form validation, payment failure)

18. **Error message visible for 2+ seconds then removed** → NOT A BUG. The user had time to read it. Do NOT report this as "error not displayed correctly" or "error removed". The error message worked as designed.
    - Exception: Report ONLY if the message text was WRONG (e.g., says "success" but operation failed), appeared next to the WRONG form field, or contradicts other evidence.

19. **Transitional / redirect messages are NOT bugs**: Messages like "Redirecting to login...", "Please wait...", "Signing you in...", "Logging out...", "Verifying your session..." are NORMAL transitional UI states. They are EXPECTED to appear temporarily and then disappear when the redirect/action completes. Do NOT report these as:
    - "message not visible" or "message removed"
    - "flash error" (even if visible < 1s — these are status messages, not errors)
    - "broken UI" or "confusing flow"
    - The ONLY exception: report if the transitional message gets STUCK (visible for > 30s with no navigation happening) — that may indicate a stalled redirect.

20. **Auth redirect flow is NOT a bug**: When a user visits a protected page (e.g. /dashboard) while not logged in, and the app shows "Redirecting to login..." then navigates to /login — this is the CORRECT behavior. The entire flow is: visit protected page → see "Redirecting..." → land on /login. Do NOT report:
    - The "Redirecting to login..." message appearing/disappearing — it worked as designed
    - "Instant bounce" on the protected page — the user was redirected, not bouncing
    - Any issue on the protected page related to the redirect flow
    - The duration of the redirect message (even if 1 minute due to tab being hidden) — subtract user idle/tab-hidden time mentally

WHAT IS NOT A BUG (do NOT report these):
- **Error/validation messages that stayed visible for 2+ seconds** — If a DOM_DIFF shows a message was ADDED and then later REMOVED with "(was visible for Xs)" where X >= 2 seconds, the user SAW the message and it worked as intended. This is NOT a bug. Only report the message as an issue if: (a) the message text is WRONG or misleading, or (b) the message appeared next to the WRONG field, or (c) the message contradicts the actual error.
- **Transitional messages that appeared and disappeared** — "Redirecting...", "Please wait...", "Logging you in...", "Signing out...", "Verifying...", "Loading your..." are EXPECTED to be temporary. Their removal means the action completed successfully. Do NOT flag them.
- **Auth redirects from protected pages** — visiting /dashboard while logged out → seeing "Redirecting to login" → landing on /login is NORMAL. Not a bug, not a bounce, not a broken flow.
- **User idle on a page** — a user not interacting for 15, 30, or even 60 seconds is NOT a stuck page or a bug. They may be reading, thinking, on another tab, or filling a form. Do NOT report user idle time as any kind of issue
- **Auth/Registration flow navigation** — visiting /login multiple times during a session that also includes /register, /signup, /verify, /confirm, or /callback is COMPLETELY NORMAL. The user is going through a registration flow: login page → register → back to login → email verify → dashboard. This is NOT a refresh workaround, NOT a broken page, and NOT a bug. Do NOT report this pattern.
- **Normal auth redirects** — If a user visits /login, then /register, then /login again, this is a normal sign-up flow. The user decided to register, completed registration, and returned to login. This applies to ALL combinations of auth pages (/login, /register, /signup, /signin, /verify, /confirm, /forgot-password, /reset-password, /callback, /onboarding).
- User visiting billing/pricing pages multiple times (they're comparing plans)
- User revisiting onboarding steps (they're learning the product)
- User navigating between pages (normal browsing)
- Slow page with no evidence of actual timeout or failure
- User leaving a page (this is normal behavior)
- Errors mentioning a page URL the user NEVER visited in this session (the error belongs to a different session)
- Issues on pages that do NOT appear in the session timeline — if the user never navigated there, you cannot report an issue there
- **NON-BLOCKING ERRORS**: If an error/exception occurs on a page but the user SUCCESSFULLY navigates to the next page and continues using the app normally, the error is NON-BLOCKING and should NOT be reported as critical/high. Common examples:
  - An error on /auth/callback/ followed by successful redirect to /dashboard — the login WORKED, the error is cosmetic
  - An API returning 500 but the page still loads content — the app has a fallback
  - A JavaScript exception on page load but the user continues interacting normally — the error doesn't affect the user
  Only report these non-blocking errors as "low" severity IF the user was visibly affected (e.g., saw an error message, had to retry). If the user was NOT affected at all, do NOT report it.

For each CONFIRMED issue, return JSON with:
- issues: array of objects, each with:
  - title: concise bug title (max 80 chars)
  - description: 2-3 sentences explaining what happened technically
  - why_issue: 1-2 sentences explaining the real-world impact on the user (e.g. "User lost their form data", "Button is non-functional", "Error shows below password but the empty field is username")
  - reproduction_steps: array of 3-6 short steps to reproduce (e.g. ["Go to /signup", "Leave 'name' field empty", "Fill in email and password", "Click Submit", "Observe: error appears below password field instead of name field"])
  - severity: "critical" | "high" | "medium" | "low"
  - category: "broken_ui" | "error" | "ux_friction" | "dead_end" | "confusing_flow" | "performance" | "data_loss" | "form_validation" | "dead_click" | "refresh_workaround" | "session_expiry" | "broken_navigation" | "double_action"
  - evidence: array of specific timestamped events from the timeline proving this issue
  - page_url: URL where the issue occurred
  - confidence: float 0.0-1.0 (must be >= 0.7 to report)
  - affected_element: (optional) CSS selector or description of the problematic element

If no CONFIRMED issues are found, return {"issues": []}. It is completely fine to return zero issues — most sessions are normal.

REMEMBER: Your job is to find bugs that a developer needs to fix, NOT to critique normal user behavior. Look hard at form interactions, page loading patterns, refresh behavior, and auth redirects — these are the most common and frustrating bugs users encounter."""


# ─── Timeline builder (provider-agnostic) ────────────────────────────────────

def _describe_element(ev: NormalizedEvent) -> str:
    """Build a human-readable description of an element."""
    parts = []
    if ev.tag_name:
        tag_desc = f"<{ev.tag_name}"
        if ev.element_type:
            tag_desc += f" type={ev.element_type}"
        if ev.element_name:
            tag_desc += f" name={ev.element_name}"
        tag_desc += ">"
        parts.append(tag_desc)
    else:
        parts.append("<unknown>")
    if ev.element_text:
        parts.append(f"'{ev.element_text}'")
    if ev.css_selector:
        parts.append(f"({ev.css_selector})")
    return " ".join(parts)


def _build_session_timeline(events: list[NormalizedEvent]) -> str:
    """Convert normalised events into a readable timeline for AI analysis."""
    lines = []
    for ev in events:
        ts = ev.timestamp

        if ev.event_type == "pageview":
            vp = ""
            if ev.viewport_width and ev.viewport_height:
                vp = f" [viewport: {ev.viewport_width}x{ev.viewport_height}]"
            lines.append(f"[{ts}] PAGEVIEW: {ev.pathname or ev.url}{vp}")

        elif ev.event_type == "pageleave":
            lines.append(f"[{ts}] PAGE_LEAVE: {ev.pathname or ev.url}")

        elif ev.event_type == "submit":
            el_desc = _describe_element(ev)
            action = f" → {ev.form_action}" if ev.form_action else ""
            lines.append(f"[{ts}] FORM_SUBMIT: {el_desc}{action} on {ev.url}")

        elif ev.event_type == "input":
            el_desc = _describe_element(ev)
            val_info = ""
            if ev.element_value:
                val_info = f" value='{ev.element_value}'"
            if ev.validation_message:
                val_info += f" ⚠ VALIDATION: {ev.validation_message}"
            lines.append(f"[{ts}] INPUT: {el_desc}{val_info} on {ev.url}")

        elif ev.event_type == "focus":
            el_desc = _describe_element(ev)
            lines.append(f"[{ts}] FOCUS: {el_desc} on {ev.url}")

        elif ev.event_type == "blur":
            el_desc = _describe_element(ev)
            val_info = ""
            if ev.validation_message:
                val_info = f" ⚠ VALIDATION: {ev.validation_message}"
            lines.append(f"[{ts}] BLUR: {el_desc}{val_info} on {ev.url}")

        elif ev.event_type in ("click", "tap"):
            el_desc = _describe_element(ev)
            scroll_info = ""
            if ev.scroll_y is not None:
                scroll_info = f" [scrollY={ev.scroll_y}]"
            lines.append(f"[{ts}] CLICK: {el_desc}{scroll_info} on {ev.url}")

        elif ev.event_type == "dead_click":
            el_desc = _describe_element(ev)
            lines.append(f"[{ts}] DEAD_CLICK: {el_desc} on {ev.url} — click had no effect")

        elif ev.event_type == "rage_click":
            el_desc = _describe_element(ev)
            lines.append(f"[{ts}] RAGE_CLICK: {el_desc} on {ev.url}")

        elif ev.event_type == "error":
            lines.append(f"[{ts}] ERROR: {ev.error_type}: {ev.error_message[:200]} on {ev.url}")

        elif ev.event_type == "network_error":
            lines.append(f"[{ts}] NETWORK_ERROR: {ev.method} {ev.endpoint} → HTTP {ev.status_code}")

        elif ev.event_type == "form_validation":
            el_desc = _describe_element(ev)
            lines.append(f"[{ts}] FORM_VALIDATION_ERROR: {ev.validation_message} on {el_desc} at {ev.url}")

        else:
            lines.append(f"[{ts}] {ev.event_type.upper()}: on {ev.url}")

    return "\n".join(lines)


# ─── UX pattern pre-analysis ─────────────────────────────────────────────────

def _detect_ux_patterns(events: list[NormalizedEvent]) -> str:
    """
    Analyse events for common UX anti-patterns BEFORE sending to AI.
    Returns a hints section to prepend to the AI prompt for richer context.
    """
    hints: list[str] = []

    # Pattern 1: Form submit with no navigation / no response
    for i, ev in enumerate(events):
        if ev.event_type == "submit":
            # Look at next few events for navigation or network activity
            following = events[i + 1: i + 6]
            has_nav = any(e.event_type == "pageview" for e in following)
            has_network = any(e.event_type in ("network_error",) for e in following)
            has_error = any(e.event_type == "error" for e in following)
            has_resubmit = any(e.event_type == "submit" for e in following)

            if not has_nav and has_resubmit:
                hints.append(
                    f"⚠ FORM_SUBMIT_REPEATED: User submitted a form at {ev.url} then submitted again "
                    f"without navigating — possible silent failure or missing feedback."
                )
            elif not has_nav and has_network:
                hints.append(
                    f"⚠ FORM_SUBMIT_FAILED: Form submit at {ev.url} followed by network error — "
                    f"check if user saw an error message."
                )
            elif not has_nav and has_error:
                hints.append(
                    f"⚠ FORM_SUBMIT_ERROR: Form submit at {ev.url} followed by JavaScript error — "
                    f"form handler may be broken."
                )

    # Pattern 2: Rapid focus cycling (user confused about which field to fix)
    focus_events = [(i, ev) for i, ev in enumerate(events) if ev.event_type == "focus" and ev.element_name]
    for j in range(len(focus_events) - 3):
        window = focus_events[j: j + 4]
        names = [e.element_name for _, e in window]
        unique_names = set(names)
        if len(unique_names) >= 3:
            # 4 focus events across 3+ different fields in a short window
            first_ts = window[0][1].timestamp
            last_ts = window[-1][1].timestamp
            hints.append(
                f"⚠ RAPID_FOCUS_CYCLING: User cycled through fields {list(unique_names)} "
                f"between {first_ts} and {last_ts} on {window[0][1].url} — "
                f"possible confusion about which field needs attention."
            )

    # Pattern 3: Submit → scroll (looking for error message)
    for i, ev in enumerate(events):
        if ev.event_type == "submit":
            following = events[i + 1: i + 5]
            scroll_after = [e for e in following if e.scroll_y is not None and ev.scroll_y is not None
                           and abs((e.scroll_y or 0) - (ev.scroll_y or 0)) > 200]
            if scroll_after:
                hints.append(
                    f"⚠ SUBMIT_THEN_SCROLL: After form submit at {ev.url}, user scrolled significantly — "
                    f"may be searching for an error message that appeared off-screen."
                )

    # Pattern 4: Dead clicks on interactive-looking elements
    dead_clicks = [ev for ev in events if ev.event_type == "dead_click"]
    if len(dead_clicks) >= 2:
        elements = set()
        for dc in dead_clicks:
            elements.add(dc.element_text or dc.css_selector or dc.tag_name)
        hints.append(
            f"⚠ MULTIPLE_DEAD_CLICKS: {len(dead_clicks)} clicks on non-responsive elements: "
            f"{', '.join(list(elements)[:5])} — UI elements may appear clickable but aren't."
        )

    # Pattern 5: Validation message present
    validation_events = [ev for ev in events if ev.validation_message]
    if validation_events:
        for ve in validation_events:
            hints.append(
                f"⚠ VALIDATION_ERROR: Field '{ve.element_name or ve.css_selector}' has validation error: "
                f"'{ve.validation_message}' at {ve.url}"
            )

    # Pattern 6: Refresh as workaround (same-URL pageview pairs)
    # IMPORTANT: Exclude auth/registration flow pages — visiting /login multiple
    # times during a register→login→verify flow is completely normal.
    interactive_types = {"click", "tap", "input", "submit", "focus", "dead_click", "rage_click"}
    auth_flow_pages = {"/login", "/signin", "/sign-in", "/register", "/signup", "/sign-up",
                       "/verify", "/confirm", "/callback", "/auth", "/logout", "/sign_out",
                       "/log_in", "/sign_in", "/forgot-password", "/reset-password",
                       "/onboarding", "/welcome"}
    pageview_events = [(i, ev) for i, ev in enumerate(events) if ev.event_type == "pageview"]
    all_paths = [_normalize_url(pv.pathname or pv.url) for _, pv in pageview_events]

    for k in range(len(pageview_events) - 1):
        idx_a, pv_a = pageview_events[k]
        idx_b, pv_b = pageview_events[k + 1]
        url_a = _normalize_url(pv_a.pathname or pv_a.url)
        url_b = _normalize_url(pv_b.pathname or pv_b.url)

        if url_a and url_a == url_b:
            # Skip auth/registration flow pages — revisiting /login after /register is normal
            is_auth_page = any(ap in url_a for ap in auth_flow_pages)
            if is_auth_page:
                continue

            # Also skip if the broader session context shows a registration/auth flow
            # (e.g., /login → /register → /login → /verify is normal)
            session_has_register = any(
                any(ap in p for ap in {"/register", "/signup", "/sign-up", "/verify", "/confirm"})
                for p in all_paths
            )
            if session_has_register and any(ap in url_a for ap in {"/login", "/signin", "/sign-in"}):
                continue

            # Same URL visited twice — check if user interacted BETWEEN them (if not, it's a refresh)
            between = events[idx_a + 1: idx_b]
            had_interaction = any(e.event_type in interactive_types for e in between)

            # Check if user interacts successfully AFTER the second pageview
            after = events[idx_b + 1: idx_b + 8]
            has_post_interaction = any(e.event_type in interactive_types for e in after)

            if not had_interaction and has_post_interaction:
                hints.append(
                    f"⚠ REFRESH_WORKAROUND: User refreshed {url_a} (no interaction before refresh, "
                    f"successful interaction after) — first page load was likely broken."
                )
            elif not had_interaction and not has_post_interaction:
                hints.append(
                    f"⚠ REFRESH_NO_HELP: User refreshed {url_a} but still didn't interact after — "
                    f"page may be persistently broken."
                )

    # Pattern 7: Session expiry / auth kicked mid-task
    auth_pages = {"/login", "/signin", "/sign-in", "/auth", "/session-expired", "/logout",
                  "/sign_in", "/log_in", "/authenticate", "/sso"}
    for i, ev in enumerate(events):
        if ev.event_type == "pageview":
            current_path = _normalize_url(ev.pathname or ev.url)
            # Check if this is a redirect TO a login page
            is_auth_page = any(ap in current_path for ap in auth_pages)
            if is_auth_page and i > 0:
                # Look back: was user actively interacting before this?
                preceding = events[max(0, i - 8): i]
                was_active = sum(1 for e in preceding if e.event_type in interactive_types) >= 2
                clicked_logout = any(
                    e.event_type in ("click", "tap") and
                    ("logout" in (e.element_text or "").lower() or "sign out" in (e.element_text or "").lower()
                     or "log out" in (e.element_text or "").lower())
                    for e in preceding
                )
                had_401 = any(
                    e.event_type == "network_error" and e.status_code == 401
                    for e in preceding
                )

                if was_active and not clicked_logout:
                    severity_hint = "CRITICAL" if had_401 else "HIGH"
                    # Check if user was filling a form
                    was_in_form = any(e.event_type in ("input", "submit", "focus") for e in preceding)
                    form_note = " User was filling a form — DATA LOSS likely!" if was_in_form else ""
                    hints.append(
                        f"⚠ SESSION_EXPIRY [{severity_hint}]: User was actively interacting then got "
                        f"redirected to {current_path} without clicking logout.{form_note}"
                        f"{' 401 error detected.' if had_401 else ''}"
                    )

    # Pattern 8: Double/duplicate network requests after single action
    for i, ev in enumerate(events):
        if ev.event_type in ("click", "submit") and i + 2 < len(events):
            following = events[i + 1: i + 5]
            network_reqs = [
                e for e in following
                if e.event_type == "network_error" and e.endpoint and e.method
            ]
            # Check for duplicate endpoint+method pairs
            seen_endpoints: dict[str, int] = {}
            for nr in network_reqs:
                key = f"{nr.method}:{nr.endpoint}"
                seen_endpoints[key] = seen_endpoints.get(key, 0) + 1
            for key, count in seen_endpoints.items():
                if count >= 2:
                    hints.append(
                        f"⚠ DOUBLE_REQUEST: After {ev.event_type} at {ev.url}, "
                        f"duplicate network request detected: {key} fired {count} times — "
                        f"possible missing debounce/double-submit protection."
                    )

    # Pattern 9: Error text visible in element text or error messages
    _re_hints = re
    _error_hint_re = _re_hints.compile(
        r"(failed\s+to\s+fetch|something\s+went\s+wrong|unexpected\s+error|server\s+error"
        r"|an?\s+error\s+(has\s+)?occur|network\s+error|request\s+failed|connection\s+(refused|timed?\s*out)"
        r"|unable\s+to\s+(load|connect|process|complete|fetch)|failed\s+to\s+(load|submit|save|create|register|sign|log)"
        r"|oops|error\s*[:\!]|try\s+again\s+later|service\s+unavailable|unfortunately)",
        _re_hints.IGNORECASE,
    )
    for ev in events:
        texts_to_scan = [ev.element_text, ev.error_message, ev.validation_message]
        # Also check raw $el_text
        raw_props = ev.raw.get("properties", {}) if ev.raw else {}
        raw_el_text = raw_props.get("$el_text", "")
        if raw_el_text:
            texts_to_scan.append(str(raw_el_text)[:200])
        exc_msg = raw_props.get("$exception_message", "")
        if exc_msg:
            texts_to_scan.append(str(exc_msg)[:200])

        for text in texts_to_scan:
            if text and _error_hint_re.search(text):
                hints.append(
                    f"⚠ ERROR_TEXT_VISIBLE: Error text found in event at {ev.url}: "
                    f"\"{text[:100]}\" — this may be a user-visible error message. "
                    f"Event type: {ev.event_type}, timestamp: {ev.timestamp}"
                )
                break  # One hint per event

    if not hints:
        return ""

    return "\n=== UX PATTERN HINTS (pre-analyzed) ===\n" + "\n".join(hints) + "\n=== END HINTS ===\n"


# ─── AI memory: dismissed patterns ────────────────────────────────────────────

def _build_dismissed_memory_prompt(dismissed_patterns: list[dict] | None) -> str:
    """
    Build a prompt section that tells the AI about previously dismissed issues.
    These act as AI memory — the AI learns not to report similar patterns again.
    """
    if not dismissed_patterns:
        return ""

    lines = []
    for i, p in enumerate(dismissed_patterns[:30], 1):  # Limit to 30 to save tokens
        title = p.get("title", "Unknown")
        category = p.get("category", "")
        page = p.get("page_url", "")
        desc = p.get("description", "")
        parts = [f"{i}. \"{title}\""]
        if category:
            parts.append(f"[{category}]")
        if page:
            parts.append(f"on {page}")
        if desc:
            # Truncate long descriptions
            parts.append(f"— {desc[:120]}")
        lines.append(" ".join(parts))

    return (
        "\n=== DISMISSED PATTERNS (AI Memory) ===\n"
        "The following issues were previously reviewed by the team and marked as NOT bugs.\n"
        "Do NOT report issues that are the same as or similar to these patterns.\n"
        "If you see something that matches any of these dismissed patterns, SKIP it entirely.\n\n"
        + "\n".join(lines)
        + "\n=== END DISMISSED PATTERNS ===\n\n"
    )


# ─── Timestamp helpers ────────────────────────────────────────────────────────


def _parse_ts(ts_str: str) -> float | None:
    """Parse an ISO 8601 timestamp string to epoch seconds. Returns None on failure."""
    if not ts_str:
        return None
    try:
        ts_str = ts_str.strip()
        if ts_str.endswith("Z"):
            ts_str = ts_str[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts_str)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English text."""
    return len(text) // 4


# ─── Unified AI analysis (single function per session) ───────────────────────


def _build_interleaved_timeline(
    events: list[NormalizedEvent],
    dom_texts: list[dict] | None = None,
    max_dom_chars: int = 3000,
) -> str:
    """
    Build a single interleaved timeline that weaves together:
    - User events (clicks, pageviews, errors, network, etc.)
    - DOM text changes from recording snapshots (only error-like or changed text)

    DOM entries are inserted at the correct timestamp position so the AI sees:
    [T1] CLICK: <button> 'Submit' on /signup
    [T1+1s] DOM_CHANGE: [UI-ERROR]: "Password must be 8+ characters" near [INPUT name="email"]
    [T1+2s] NETWORK_ERROR: POST /api/register → 422

    This gives AI full temporal context in one view.
    """
    # Build event timeline entries with sortable timestamps
    timeline_entries: list[tuple[float, str]] = []

    for ev in events:
        ts = ev.timestamp
        ts_epoch = _parse_ts(ts) or 0.0

        if ev.event_type == "pageview":
            vp = ""
            if ev.viewport_width and ev.viewport_height:
                vp = f" [viewport: {ev.viewport_width}x{ev.viewport_height}]"
            timeline_entries.append((ts_epoch, f"[{ts}] PAGEVIEW: {ev.pathname or ev.url}{vp}"))

        elif ev.event_type == "pageleave":
            timeline_entries.append((ts_epoch, f"[{ts}] PAGE_LEAVE: {ev.pathname or ev.url}"))

        elif ev.event_type == "submit":
            el_desc = _describe_element(ev)
            action = f" → {ev.form_action}" if ev.form_action else ""
            timeline_entries.append((ts_epoch, f"[{ts}] FORM_SUBMIT: {el_desc}{action} on {ev.url}"))

        elif ev.event_type == "input":
            el_desc = _describe_element(ev)
            val_info = ""
            if ev.element_value:
                val_info = f" value='{ev.element_value}'"
            if ev.validation_message:
                val_info += f" ⚠ VALIDATION: {ev.validation_message}"
            timeline_entries.append((ts_epoch, f"[{ts}] INPUT: {el_desc}{val_info} on {ev.url}"))

        elif ev.event_type == "focus":
            el_desc = _describe_element(ev)
            timeline_entries.append((ts_epoch, f"[{ts}] FOCUS: {el_desc} on {ev.url}"))

        elif ev.event_type == "blur":
            el_desc = _describe_element(ev)
            val_info = ""
            if ev.validation_message:
                val_info = f" ⚠ VALIDATION: {ev.validation_message}"
            timeline_entries.append((ts_epoch, f"[{ts}] BLUR: {el_desc}{val_info} on {ev.url}"))

        elif ev.event_type in ("click", "tap"):
            el_desc = _describe_element(ev)
            scroll_info = ""
            if ev.scroll_y is not None:
                scroll_info = f" [scrollY={ev.scroll_y}]"
            timeline_entries.append((ts_epoch, f"[{ts}] CLICK: {el_desc}{scroll_info} on {ev.url}"))

        elif ev.event_type == "dead_click":
            el_desc = _describe_element(ev)
            timeline_entries.append((ts_epoch, f"[{ts}] DEAD_CLICK: {el_desc} on {ev.url} — click had no effect"))

        elif ev.event_type == "rage_click":
            el_desc = _describe_element(ev)
            timeline_entries.append((ts_epoch, f"[{ts}] RAGE_CLICK: {el_desc} on {ev.url}"))

        elif ev.event_type == "error":
            timeline_entries.append((ts_epoch, f"[{ts}] ERROR: {ev.error_type}: {ev.error_message[:200]} on {ev.url}"))

        elif ev.event_type == "network_error":
            timeline_entries.append((ts_epoch, f"[{ts}] NETWORK_ERROR: {ev.method} {ev.endpoint} → HTTP {ev.status_code}"))

        elif ev.event_type == "form_validation":
            el_desc = _describe_element(ev)
            timeline_entries.append((ts_epoch, f"[{ts}] FORM_VALIDATION_ERROR: {ev.validation_message} on {el_desc} at {ev.url}"))

        else:
            timeline_entries.append((ts_epoch, f"[{ts}] {ev.event_type.upper()}: on {ev.url}"))

    # ── Interleave DOM text changes ──────────────────────────────────────
    # For each DOM text snapshot: extract only error-like lines and form/UI changes
    # to keep token budget reasonable. Insert them at the right timestamp.
    if dom_texts:
        _ERROR_HINT_RE = re.compile(
            r"(error|fail|invalid|denied|expired|refused|unavailable|forbidden"
            r"|not found|timed?\s*out|unauthorized|exception|crash|broke"
            r"|something went wrong|try again|oops|unable to|cannot|couldn.t"
            r"|unexpected|sorry|problem|warning|alert|critical"
            r"|could not|failed to|rejected|blocked|disabled"
            r"|no access|no permission|not allowed|bad request|server error"
            r"|500|404|403|401|network|offline|connection|reset)",
            re.IGNORECASE,
        )
        _UI_CHANGE_RE = re.compile(
            r"\[(FORM|INPUT|BUTTON|ALERT|UI-|LABEL|ERROR|DIALOG|SELECT|TEXTAREA)",
            re.IGNORECASE,
        )

        dom_chars_used = 0
        seen_dom_lines: set[str] = set()

        for dt in dom_texts:
            if dom_chars_used >= max_dom_chars:
                break

            dt_text = dt.get("text", "")
            dt_page = dt.get("page", "")
            dt_ts = dt.get("timestamp", "")
            is_markdown = dt.get("is_markdown", False)
            dt_epoch = _parse_ts(dt_ts) or 0.0

            if not dt_text or not dt_page:
                continue

            if is_markdown:
                # Extract only interesting lines: error-like text and form/UI elements
                lines = dt_text.split("\n")
                interesting_lines: list[str] = []
                for li, line in enumerate(lines):
                    line_stripped = line.strip()
                    if not line_stripped or len(line_stripped) < 3:
                        continue
                    is_error_like = _ERROR_HINT_RE.search(line_stripped)
                    is_ui_element = _UI_CHANGE_RE.search(line_stripped)
                    if is_error_like or is_ui_element:
                        # Include with ±1 line of context
                        dedup_key = line_stripped[:80].lower()
                        if dedup_key in seen_dom_lines:
                            continue
                        seen_dom_lines.add(dedup_key)

                        ctx_start = max(0, li - 1)
                        ctx_end = min(len(lines), li + 2)
                        context = " | ".join(
                            l.strip() for l in lines[ctx_start:ctx_end] if l.strip()
                        )
                        if len(context) > 300:
                            context = context[:300] + "…"
                        interesting_lines.append(context)

                if interesting_lines:
                    dom_snippet = "\n  ".join(interesting_lines[:10])
                    entry = f"[{dt_ts}] DOM_STATE on {dt_page}:\n  {dom_snippet}"
                    dom_chars_used += len(entry)
                    timeline_entries.append((dt_epoch, entry))
            else:
                # Legacy flat text — only include if error-like
                if _ERROR_HINT_RE.search(dt_text):
                    dedup_key = dt_text[:80].lower()
                    if dedup_key not in seen_dom_lines:
                        seen_dom_lines.add(dedup_key)
                        entry = f"[{dt_ts}] DOM_TEXT on {dt_page}: \"{dt_text[:200]}\""
                        dom_chars_used += len(entry)
                        timeline_entries.append((dt_epoch, entry))

    # Sort all entries by timestamp
    timeline_entries.sort(key=lambda x: x[0])

    return "\n".join(entry for _, entry in timeline_entries)


def _format_duration(seconds: float) -> str:
    """Format seconds into a human-readable duration string."""
    if seconds < 0.5:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = seconds % 60
    return f"{minutes}m{secs:.0f}s"


def _compute_dom_diffs(
    dom_texts: list[dict],
    events: list[NormalizedEvent] | None = None,
) -> list[dict]:
    """
    Compute DOM diffs between consecutive snapshots with visibility duration.

    Instead of passing full page snapshots each time, only include lines
    that CHANGED compared to the previous snapshot for the same page.

    Tracks WHEN each line first appeared, so when it's removed we can report
    how long it was visible. This helps the AI distinguish:
    - "error shown for 0.2s then removed" → flash bug, user couldn't read it
    - "error shown for 5s then user navigated away" → user likely saw it
    - "error still on screen" → persistent error

    **Inactive time subtraction**: If events are provided, we detect idle gaps
    (periods > 10s with no user events) and subtract them from visibility
    duration. This prevents inflated durations when the user was on another
    tab or AFK (e.g. "1m0s" when the user was actually away for 50s).

    The first snapshot per page is passed in full (filtered to interesting lines).
    Subsequent snapshots for the same page only include the diff + durations.

    Returns list of {"text": str, "page": str, "timestamp": str, "is_markdown": bool, "is_diff": bool}
    """
    if not dom_texts:
        return []

    # ── Pre-compute idle gaps from events (for duration correction) ──────
    # An "idle gap" is a period between consecutive events longer than 10s
    # where the user was likely on another tab or AFK.
    idle_gaps: list[tuple[float, float]] = []  # list of (gap_start, gap_end)
    if events:
        sorted_events = sorted(
            events,
            key=lambda e: _parse_ts(e.timestamp) or 0.0,
        )
        for i in range(1, len(sorted_events)):
            prev_ts = _parse_ts(sorted_events[i - 1].timestamp)
            curr_ts = _parse_ts(sorted_events[i].timestamp)
            if prev_ts is not None and curr_ts is not None:
                gap = curr_ts - prev_ts
                if gap > 10.0:  # 10 second idle threshold
                    idle_gaps.append((prev_ts, curr_ts))

    def _subtract_idle_time(appeared_at: float, removed_at: float) -> float:
        """
        Calculate effective visibility by subtracting idle periods
        that fall within the [appeared_at, removed_at] window.
        """
        raw_duration = removed_at - appeared_at
        if not idle_gaps or raw_duration <= 0:
            return raw_duration

        idle_total = 0.0
        for gap_start, gap_end in idle_gaps:
            # Find overlap between [appeared_at, removed_at] and [gap_start, gap_end]
            overlap_start = max(appeared_at, gap_start)
            overlap_end = min(removed_at, gap_end)
            if overlap_start < overlap_end:
                idle_total += (overlap_end - overlap_start)

        active_duration = raw_duration - idle_total
        return max(active_duration, 0.1)  # floor at 0.1s to avoid negatives

    # Track last seen lines AND when they first appeared, per page
    # page_key → {line_text: appeared_epoch}
    page_line_timestamps: dict[str, dict[str, float]] = {}
    diffed: list[dict] = []

    # Sort by timestamp to process in order
    sorted_texts = sorted(dom_texts, key=lambda d: _parse_ts(d.get("timestamp", "")) or 0.0)

    for dt in sorted_texts:
        text = dt.get("text", "")
        page = dt.get("page", "")
        ts = dt.get("timestamp", "")
        is_markdown = dt.get("is_markdown", False)

        if not text or not page:
            continue

        if not is_markdown:
            # Non-markdown: pass through as-is
            diffed.append({**dt, "is_diff": False})
            continue

        current_epoch = _parse_ts(ts) or 0.0
        current_lines = set(
            line.strip() for line in text.split("\n")
            if line.strip() and len(line.strip()) >= 3
        )

        page_key = _normalize_url(page)
        prev_line_ts = page_line_timestamps.get(page_key)

        if prev_line_ts is None:
            # First snapshot for this page — pass full content
            diffed.append({**dt, "is_diff": False})
            # Record when each line appeared
            page_line_timestamps[page_key] = {
                line: current_epoch for line in current_lines
            }
        else:
            prev_lines = set(prev_line_ts.keys())
            new_lines = current_lines - prev_lines
            removed_lines = prev_lines - current_lines

            if new_lines or removed_lines:
                diff_parts: list[str] = []

                if new_lines:
                    diff_parts.append("ADDED:")
                    # Preserve original line order for added lines
                    for line in text.split("\n"):
                        if line.strip() in new_lines:
                            diff_parts.append(f"  + {line.strip()}")

                if removed_lines:
                    diff_parts.append("REMOVED:")
                    for rl in list(removed_lines)[:10]:
                        # Calculate how long this text was visible
                        appeared_at = prev_line_ts.get(rl, 0.0)
                        if appeared_at and current_epoch:
                            # Subtract idle time for accurate duration
                            visible_secs = _subtract_idle_time(appeared_at, current_epoch)
                            duration_str = _format_duration(visible_secs)
                            diff_parts.append(
                                f"  - {rl}  (was visible for {duration_str})"
                            )
                        else:
                            diff_parts.append(f"  - {rl}")

                diff_text = "\n".join(diff_parts)
                if diff_text.strip():
                    diffed.append({
                        "text": diff_text,
                        "page": page,
                        "timestamp": ts,
                        "is_markdown": True,
                        "is_diff": True,
                    })
            # else: no changes → skip entirely

            # Update tracked lines: remove old, add new with their timestamps
            new_line_ts = {
                line: ts_val
                for line, ts_val in prev_line_ts.items()
                if line in current_lines  # keep existing lines with original timestamps
            }
            for line in new_lines:
                new_line_ts[line] = current_epoch  # new lines get current timestamp
            page_line_timestamps[page_key] = new_line_ts

    return diffed


def _build_full_dom_timeline(
    events: list[NormalizedEvent],
    dom_texts: list[dict],
) -> str:
    """
    Build a timeline with FULL DOM markdown content interleaved with events.
    Used in Tier 1 when everything fits within token budget.
    """
    timeline_entries: list[tuple[float, str]] = []

    # Events
    for ev in events:
        ts = ev.timestamp
        ts_epoch = _parse_ts(ts) or 0.0

        if ev.event_type == "pageview":
            vp = ""
            if ev.viewport_width and ev.viewport_height:
                vp = f" [viewport: {ev.viewport_width}x{ev.viewport_height}]"
            timeline_entries.append((ts_epoch, f"[{ts}] PAGEVIEW: {ev.pathname or ev.url}{vp}"))
        elif ev.event_type == "error":
            timeline_entries.append((ts_epoch, f"[{ts}] ERROR: {ev.error_type}: {ev.error_message[:200]} on {ev.url}"))
        elif ev.event_type == "network_error":
            timeline_entries.append((ts_epoch, f"[{ts}] NETWORK_ERROR: {ev.method} {ev.endpoint} → HTTP {ev.status_code}"))
        elif ev.event_type == "submit":
            el_desc = _describe_element(ev)
            action = f" → {ev.form_action}" if ev.form_action else ""
            timeline_entries.append((ts_epoch, f"[{ts}] FORM_SUBMIT: {el_desc}{action} on {ev.url}"))
        elif ev.event_type == "input":
            el_desc = _describe_element(ev)
            val_info = ""
            if ev.element_value:
                val_info = f" value='{ev.element_value}'"
            if ev.validation_message:
                val_info += f" ⚠ VALIDATION: {ev.validation_message}"
            timeline_entries.append((ts_epoch, f"[{ts}] INPUT: {el_desc}{val_info} on {ev.url}"))
        elif ev.event_type in ("click", "tap"):
            el_desc = _describe_element(ev)
            scroll_info = f" [scrollY={ev.scroll_y}]" if ev.scroll_y is not None else ""
            timeline_entries.append((ts_epoch, f"[{ts}] CLICK: {el_desc}{scroll_info} on {ev.url}"))
        elif ev.event_type == "dead_click":
            el_desc = _describe_element(ev)
            timeline_entries.append((ts_epoch, f"[{ts}] DEAD_CLICK: {el_desc} on {ev.url} — click had no effect"))
        elif ev.event_type == "rage_click":
            el_desc = _describe_element(ev)
            timeline_entries.append((ts_epoch, f"[{ts}] RAGE_CLICK: {el_desc} on {ev.url}"))
        elif ev.event_type == "focus":
            el_desc = _describe_element(ev)
            timeline_entries.append((ts_epoch, f"[{ts}] FOCUS: {el_desc} on {ev.url}"))
        elif ev.event_type == "blur":
            el_desc = _describe_element(ev)
            val_info = f" ⚠ VALIDATION: {ev.validation_message}" if ev.validation_message else ""
            timeline_entries.append((ts_epoch, f"[{ts}] BLUR: {el_desc}{val_info} on {ev.url}"))
        elif ev.event_type == "pageleave":
            timeline_entries.append((ts_epoch, f"[{ts}] PAGE_LEAVE: {ev.pathname or ev.url}"))
        elif ev.event_type == "form_validation":
            el_desc = _describe_element(ev)
            timeline_entries.append((ts_epoch, f"[{ts}] FORM_VALIDATION_ERROR: {ev.validation_message} on {el_desc} at {ev.url}"))
        else:
            timeline_entries.append((ts_epoch, f"[{ts}] {ev.event_type.upper()}: on {ev.url}"))

    # Full DOM entries
    for dt in dom_texts:
        dt_text = dt.get("text", "")
        dt_page = dt.get("page", "")
        dt_ts = dt.get("timestamp", "")
        is_diff = dt.get("is_diff", False)
        dt_epoch = _parse_ts(dt_ts) or 0.0

        if not dt_text:
            continue

        label = "DOM_DIFF" if is_diff else "DOM_SNAPSHOT"
        # Cap individual DOM entry at 2000 chars
        content = dt_text[:2000]
        if len(dt_text) > 2000:
            content += "\n... (truncated)"
        entry = f"[{dt_ts}] {label} on {dt_page}:\n{content}"
        timeline_entries.append((dt_epoch, entry))

    timeline_entries.sort(key=lambda x: x[0])
    return "\n".join(entry for _, entry in timeline_entries)


# ── Chunked analysis (Tier 3) ─────────────────────────────────────────────

_CHUNK_SUMMARY_PROMPT = """You are reviewing a CHUNK of a user session recording. Previous findings from earlier chunks are provided as context.

Analyze ONLY the events in THIS chunk. Report any bugs or UX issues you find. Use the same format and rules as a full session analysis.

PREVIOUS FINDINGS (from earlier chunks):
{prev_findings}

If you see issues that CONFIRM or EXTEND previous findings, include them with updated evidence.
If you see new issues, report them normally.

Return JSON with {{"issues": [...], "chunk_summary": "2-3 sentence summary of what happened in this chunk"}}"""


async def _analyze_chunked(
    session: NormalizedSession,
    dom_texts: list[dict] | None,
    dismissed_patterns: list[dict] | None,
    cost_tracker: CostTracker | None,
    max_tokens_per_chunk: int = 8000,
) -> list[dict]:
    """
    Tier 3: Chunked analysis with context carry-forward.
    Splits the interleaved timeline into time-window chunks, each fitting
    within max_tokens_per_chunk. Carries forward a summary of previous findings.
    """
    settings = get_settings()
    client = AsyncOpenAI(api_key=settings.openai_api_key)

    # Build the full interleaved timeline with DOM diffs to minimise size
    diffed_dom = _compute_dom_diffs(dom_texts, events=session.events) if dom_texts else None
    full_timeline = _build_interleaved_timeline(session.events, diffed_dom)

    if not full_timeline.strip():
        return []

    # Split into chunks by line groups
    lines = full_timeline.split("\n")
    chunks: list[str] = []
    current_chunk_lines: list[str] = []
    current_chars = 0
    max_chars = max_tokens_per_chunk * 4  # ~4 chars per token

    for line in lines:
        if current_chars + len(line) > max_chars and current_chunk_lines:
            chunks.append("\n".join(current_chunk_lines))
            current_chunk_lines = []
            current_chars = 0
        current_chunk_lines.append(line)
        current_chars += len(line) + 1

    if current_chunk_lines:
        chunks.append("\n".join(current_chunk_lines))

    if not chunks:
        return []

    logger.info(
        f"Tier 3 chunked analysis for {session.id}: "
        f"{len(chunks)} chunks from {len(lines)} timeline lines"
    )

    # Process each chunk with context carry-forward
    # We also carry the last few raw lines from the previous chunk so the AI
    # can see what happened right at the boundary (not just a summary).
    _OVERLAP_LINES = 5
    all_issues: list[dict] = []
    prev_summary = "No previous findings — this is the first chunk."
    prev_tail = ""  # last few lines from previous chunk for overlap context
    dismissed_section = _build_dismissed_memory_prompt(dismissed_patterns)
    ux_hints = _detect_ux_patterns(session.events)

    for chunk_idx, chunk_text in enumerate(chunks):
        system_prompt = _CHUNK_SUMMARY_PROMPT.format(prev_findings=prev_summary)

        # Prepend tail of previous chunk for boundary context
        overlap_section = ""
        if prev_tail:
            overlap_section = (
                f"\n--- END OF PREVIOUS CHUNK (last events for context) ---\n"
                f"{prev_tail}\n"
                f"--- START OF CURRENT CHUNK ---\n"
            )

        user_prompt = f"""Session: {session.id} | Chunk {chunk_idx + 1}/{len(chunks)}
{dismissed_section}{ux_hints}{overlap_section}
=== CHUNK TIMELINE ===
{chunk_text}
=== END CHUNK ===

Find bugs and UX issues in this chunk. Return JSON."""

        # Save tail lines for next chunk's overlap
        chunk_lines = chunk_text.split("\n")
        prev_tail = "\n".join(chunk_lines[-_OVERLAP_LINES:]) if len(chunk_lines) >= _OVERLAP_LINES else chunk_text

        try:
            from app.utils.retry import with_retries

            t0 = time.perf_counter()
            response = await with_retries(
                lambda: client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": SESSION_ANALYSIS_PROMPT + "\n\n" + system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    response_format={"type": "json_object"},
                    temperature=0.1,
                    max_tokens=1500,
                ),
                max_retries=2,
                base_delay=2.0,
                retryable_exceptions=(ConnectionError, TimeoutError, Exception),
                operation=f"OpenAI analyze chunk {chunk_idx}",
            )
            duration_ms = (time.perf_counter() - t0) * 1000

            if cost_tracker:
                cost_tracker.record(
                    function=f"analyze_chunked_{chunk_idx}",
                    model="gpt-4o-mini",
                    response=response,
                    session_id=session.id,
                    duration_ms=duration_ms,
                )

            content = response.choices[0].message.content
            if content:
                parsed = json.loads(content)
                chunk_issues = parsed.get("issues", [])
                all_issues.extend(chunk_issues)
                # Carry forward the summary
                chunk_summary = parsed.get("chunk_summary", "")
                if chunk_issues:
                    issue_titles = [i.get("title", "") for i in chunk_issues]
                    prev_summary = (
                        f"Chunks 1-{chunk_idx + 1}: {chunk_summary} "
                        f"Issues found so far: {'; '.join(issue_titles)}"
                    )
                elif chunk_summary:
                    prev_summary = f"Chunks 1-{chunk_idx + 1}: {chunk_summary} No issues found yet."

        except Exception as e:
            logger.error(f"Chunked analysis failed for chunk {chunk_idx}: {e}")

    # Attach session metadata to each issue
    for issue in all_issues:
        issue["session_id"] = session.id
        issue["distinct_id"] = session.distinct_id

    return all_issues


# ── Token budget constants ─────────────────────────────────────────────────
# gpt-4o-mini has 128k context, but we want to stay well under for cost
_MAX_INPUT_TOKENS = 12000   # target max for the user prompt
_OVERHEAD_TOKENS = 2500     # system prompt + metadata + response buffer


def _validate_issues(issues: list[dict], session: NormalizedSession) -> list[dict]:
    """
    Enrich and validate AI-generated issues against the actual session data.
    - Adds fingerprints, timestamps, element info
    - Filters out issues on pages not visited
    - Filters non-blocking errors on transient auth pages
    """
    # Build validation data
    visited_urls: set[str] = set()
    for ev in session.events:
        if ev.url:
            visited_urls.add(ev.url)
        if ev.pathname:
            visited_urls.add(ev.pathname)
    visited_lower = {_normalize_url(u) for u in visited_urls}

    # Page sequence for non-blocking error detection
    page_sequence = [
        _normalize_url(ev.pathname)
        for ev in session.events
        if ev.event_type == "pageview" and ev.pathname
    ]

    # Detect transient pages (auth callbacks etc.)
    transient_pages: set[str] = set()
    auth_patterns = ["/auth/callback", "/callback", "/oauth", "/login/callback", "/sso"]
    for page in page_sequence:
        if any(p in page for p in auth_patterns):
            idx = page_sequence.index(page)
            if idx < len(page_sequence) - 1:
                transient_pages.add(page)

    # Session start timestamp
    session_start_ts = session.start_time or (session.events[0].timestamp if session.events else "")

    # URL → earliest event timestamp
    url_to_earliest_ts: dict[str, str] = {}
    for ev in session.events:
        for u in [ev.url, ev.pathname]:
            u_key = _normalize_url(u)
            if u_key and ev.timestamp:
                if u_key not in url_to_earliest_ts or ev.timestamp < url_to_earliest_ts[u_key]:
                    url_to_earliest_ts[u_key] = ev.timestamp

    # Error timestamps and interaction events with element info
    error_timestamps: list[str] = []
    interaction_events: list[NormalizedEvent] = []
    for ev in session.events:
        if ev.event_type in ("error", "rage_click"):
            if ev.timestamp:
                error_timestamps.append(ev.timestamp)
        if ev.event_type in ("rage_click", "click", "tap"):
            if ev.tag_name or ev.element_text:
                interaction_events.append(ev)
    error_timestamps.sort()

    # Enrich and validate each issue
    validated: list[dict] = []
    for issue in issues:
        issue["session_id"] = session.id
        issue["distinct_id"] = session.distinct_id

        # Normalise fingerprint
        raw_title = (issue.get("title") or "").lower().strip()
        norm_title = re.sub(r'\b(the|a|an|on|in|at|for|of|to|and|or|is|was|with)\b', '', raw_title)
        norm_title = re.sub(r'\s+', ' ', norm_title).strip()
        raw_url = _normalize_url(issue.get("page_url") or "")
        try:
            from urllib.parse import urlparse as _urlparse
            parsed_url = _urlparse(raw_url)
            norm_url = parsed_url.path.rstrip("/")
        except Exception:
            norm_url = raw_url
        category = issue.get("category", "")
        issue["fingerprint"] = hashlib.sha256(
            f"ai:{category}:{norm_title}:{norm_url}".encode()
        ).hexdigest()

        # Attach real event timestamp
        issue_url = _normalize_url(issue.get("page_url") or "")
        matched_ts = None
        if issue_url:
            matched_ts = url_to_earliest_ts.get(issue_url)
            if not matched_ts:
                for u_key, u_ts in url_to_earliest_ts.items():
                    if issue_url in u_key or u_key in issue_url:
                        matched_ts = u_ts
                        break
        if not matched_ts and error_timestamps:
            matched_ts = error_timestamps[0]
        if not matched_ts:
            matched_ts = session_start_ts

        issue["_event_timestamp"] = matched_ts
        issue["_session_start"] = session_start_ts

        # Enrich with element info
        issue_category = issue.get("category", "")
        if issue_category in ("broken_ui", "ux_friction", "dead_end") or "rage" in issue.get("title", "").lower() or "click" in issue.get("title", "").lower():
            for ie in interaction_events:
                ie_url = _normalize_url(ie.url)
                if issue_url and (issue_url in ie_url or ie_url in issue_url):
                    if ie.tag_name or ie.element_text:
                        issue["_element_tag"] = ie.tag_name
                        issue["_element_text"] = ie.element_text
                        issue["_element_selector"] = ie.css_selector
                        break
            if "_element_tag" not in issue:
                for ie in interaction_events:
                    if ie.tag_name or ie.element_text:
                        issue["_element_tag"] = ie.tag_name
                        issue["_element_text"] = ie.element_text
                        issue["_element_selector"] = ie.css_selector or ""
                        break

        # Validate: skip issues on pages not visited
        if issue_url and visited_lower:
            url_found = any(issue_url in v or v in issue_url for v in visited_lower)
            if not url_found:
                logger.info(f"Filtered out AI issue '{issue.get('title')}' — page_url '{issue_url}' was never visited in session {session.id}")
                continue

        # Filter non-blocking errors on transient pages
        issue_path = ""
        if issue_url:
            from urllib.parse import urlparse as _urlparse2
            try:
                issue_path = _normalize_url(_urlparse2(issue_url).path)
            except Exception:
                issue_path = issue_url
        if issue_path and issue_path in transient_pages:
            severity = issue.get("severity", "medium")
            if severity in ("critical", "high", "medium"):
                logger.info(f"Filtered out non-blocking error '{issue.get('title')}' on transient page '{issue_path}' in session {session.id}")
                continue

        validated.append(issue)

    return validated


async def analyze_session_unified(
    session: NormalizedSession,
    dom_texts: list[dict] | None = None,
    dismissed_patterns: list[dict] | None = None,
    cost_tracker: CostTracker | None = None,
) -> list[dict]:
    """
    Single unified AI analysis per session with 3-tier token management:

    Tier 1 (best): If events + full DOM diffs fit within token budget → one call
    Tier 2 (good): If Tier 1 too big, use DOM diffing to reduce → one call
    Tier 3 (fallback): If still too big, chunk with context carry-forward → N calls

    Returns list of detected issues.
    """
    settings = get_settings()
    client = AsyncOpenAI(api_key=settings.openai_api_key)

    if not session.events:
        return []

    # ── Tier selection: decide how to fit content within token budget ─────
    # Step 1: Try Tier 1 — full DOM with diffs interleaved
    tier_used = 1
    diffed_dom = _compute_dom_diffs(dom_texts, events=session.events) if dom_texts else None
    timeline = _build_full_dom_timeline(session.events, diffed_dom or [])

    if _estimate_tokens(timeline) > _MAX_INPUT_TOKENS:
        # Step 2: Tier 2 — filtered interleaved timeline (only error-like DOM lines)
        tier_used = 2
        timeline = _build_interleaved_timeline(session.events, diffed_dom, max_dom_chars=3000)

    if _estimate_tokens(timeline) > _MAX_INPUT_TOKENS:
        # Step 3: Tier 3 — chunked with context carry-forward
        tier_used = 3
        logger.info(
            f"Session {session.id[:12]}…: Tier 3 chunked analysis "
            f"(timeline ~{_estimate_tokens(timeline)} tokens exceeds {_MAX_INPUT_TOKENS})"
        )
        raw_issues = await _analyze_chunked(
            session, dom_texts, dismissed_patterns, cost_tracker,
        )
        return _validate_issues(raw_issues, session)

    if not timeline.strip():
        return []

    logger.debug(
        f"Session {session.id[:12]}…: Tier {tier_used} analysis "
        f"(~{_estimate_tokens(timeline)} tokens)"
    )

    # Pre-analyse for UX patterns
    ux_hints = _detect_ux_patterns(session.events)

    # Build dismissed patterns memory section for AI
    dismissed_section = _build_dismissed_memory_prompt(dismissed_patterns)

    # Extract the set of pages actually visited
    visited_urls: set[str] = set()
    for ev in session.events:
        if ev.url:
            visited_urls.add(ev.url)
        if ev.pathname:
            visited_urls.add(ev.pathname)

    visited_pages_str = "\n".join(f"  - {u}" for u in sorted(visited_urls)) if visited_urls else "  (none detected)"

    # Summarise form interactions for extra context
    form_fields_seen: set[str] = set()
    submits = 0
    for ev in session.events:
        if ev.event_type in ("input", "focus", "blur") and ev.element_name:
            form_fields_seen.add(ev.element_name)
        if ev.event_type == "submit":
            submits += 1
    form_summary = ""
    if form_fields_seen:
        form_summary = f"\nFORM INTERACTIONS: {submits} submit(s), fields interacted: {', '.join(sorted(form_fields_seen))}\n"

    # Note how many DOM snapshots were included
    dom_note = ""
    if dom_texts:
        dom_note = (
            f"\nDOM RECORDING DATA: {len(dom_texts)} snapshot(s) included in the timeline as DOM_STATE/DOM_TEXT entries. "
            "These show the actual page content the user saw — error messages, form fields, buttons, labels, alerts. "
            "Use them to confirm whether errors were visible to the user and in which form field/section they appeared.\n"
        )

    user_prompt = f"""Analyze this user session recording:

Session ID: {session.id}
User: {session.distinct_id}
Total Events: {len(session.events)}

PAGES ACTUALLY VISITED IN THIS SESSION:
{visited_pages_str}
{form_summary}{dom_note}
IMPORTANT: Only report issues that are VISIBLE in the timeline below. If an error mentions a URL that is NOT in the list above, the error belongs to a different session — DO NOT report it.
{dismissed_section}{ux_hints}
=== INTERLEAVED SESSION TIMELINE (events + DOM changes) ===
{timeline}
=== END TIMELINE ===

Find all bugs and UX issues visible in this session. The timeline includes both user events AND DOM state changes (visible page content). Pay special attention to:
- DOM_STATE entries showing error messages, validation errors, or alerts — these are CONFIRMED visible to the user
- Form interactions where DOM_STATE shows error text near the wrong field (UX misplacement bug)
- ERROR/NETWORK_ERROR events followed by DOM_STATE showing error text (confirmed user-visible error)
- ERROR/NETWORK_ERROR events NOT followed by any DOM error text (silent failure — user doesn't know something broke)
- Dead clicks and broken interactive elements
- Same-URL pageviews (user refreshed to fix something?)
- Sudden redirects to login/auth pages (session expired?)
- Duplicate network requests after a single action
Return JSON."""

    try:
        from app.utils.retry import with_retries

        t0 = time.perf_counter()
        response = await with_retries(
            lambda: client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": SESSION_ANALYSIS_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
                max_tokens=2000,
            ),
            max_retries=2,
            base_delay=2.0,
            retryable_exceptions=(ConnectionError, TimeoutError, Exception),
            operation="OpenAI unified analysis",
        )
        duration_ms = (time.perf_counter() - t0) * 1000

        # Track cost
        if cost_tracker:
            rec = cost_tracker.record(
                function="analyze_session_unified",
                model="gpt-4o-mini",
                response=response,
                session_id=session.id,
                duration_ms=duration_ms,
            )
            logger.debug(
                f"analyze_session_unified {session.id[:12]}…: "
                f"{rec.total_tokens} tokens, ${rec.cost_usd:.4f}, {duration_ms:.0f}ms"
            )

        content = response.choices[0].message.content
        if not content:
            return []

        parsed = json.loads(content)
        issues = parsed.get("issues", [])
        return _validate_issues(issues, session)

    except Exception as e:
        logger.error(f"AI session analysis failed for {session.id}: {e}")
        return []



# ─── Processed-session tracking ──────────────────────────────────────────────

def _get_processed_session_ids(db_project_id: str) -> set[str]:
    """Load already-processed session IDs from the DB."""
    from app.database import get_supabase
    db = get_supabase()
    try:
        result = (
            db.table("processed_sessions")
            .select("session_id")
            .eq("project_id", db_project_id)
            .execute()
        )
        return {r["session_id"] for r in (result.data or [])}
    except Exception as e:
        logger.warning(f"Could not load processed sessions: {e}")
        return set()


def _mark_sessions_processed(db_project_id: str, session_ids: list[str]) -> None:
    """Mark session IDs as processed so they won't be analyzed again."""
    if not session_ids:
        return
    from app.database import get_supabase
    db = get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {"project_id": db_project_id, "session_id": sid, "processed_at": now}
        for sid in session_ids
    ]
    try:
        for i in range(0, len(rows), 50):
            batch = rows[i : i + 50]
            db.table("processed_sessions").upsert(batch, on_conflict="project_id,session_id").execute()
    except Exception as e:
        logger.error(f"Failed to mark sessions as processed: {e}")


# ─── Main pipeline ───────────────────────────────────────────────────────────

def _correlate_cross_session_issues(
    all_issues: list[dict],
    total_sessions: int,
) -> list[dict]:
    """
    Phase 6: Cross-Session Correlation.

    Groups issues by (page, rule_id) and adjusts confidence based on
    how many sessions exhibit the same issue:
    - Issues seen in 3+ sessions → confidence += 0.1 (systemic bugs)
    - Issues seen in only 1 session with low confidence → confidence -= 0.15
    """
    from collections import defaultdict
    from urllib.parse import urlparse

    # Group issues by (page_path, rule_id) — coarse fingerprint
    groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    for i, issue in enumerate(all_issues):
        page = issue.get("affected_url", "") or ""
        try:
            page_path = urlparse(page).path.rstrip("/") or "/"
        except Exception:
            page_path = page
        rule_id = issue.get("rule_id", "") or issue.get("category", "") or "unknown"
        groups[(page_path, rule_id)].append(i)

    # Count unique sessions per group
    for (page_path, rule_id), indices in groups.items():
        session_ids = set()
        for idx in indices:
            sid = all_issues[idx].get("session_id", "") or all_issues[idx].get("metadata", {}).get("session_id", "")
            if sid:
                session_ids.add(sid)

        count = len(session_ids) if session_ids else len(indices)

        for idx in indices:
            issue = all_issues[idx]
            conf = issue.get("confidence", 0.7)

            if count >= 3:
                # Systemic — boost confidence
                conf = min(conf + 0.1, 1.0)
            elif count == 1 and conf < 0.8:
                # Single occurrence + low confidence — demote
                conf = max(conf - 0.15, 0.1)

            issue["confidence"] = round(conf, 2)
            issue.setdefault("metadata", {})["cross_session"] = {
                "session_count": count,
                "total_sessions": total_sessions,
                "rate": round(count / max(total_sessions, 1), 3),
            }

    logger.info(
        f"Cross-session correlation: {len(groups)} issue groups across {total_sessions} sessions"
    )
    return all_issues


async def run_session_analysis(
    connector,
    since: datetime,
    max_sessions: int = 30,
    progress_callback=None,
    db_project_id: str | None = None,
    min_users: int = 2,
    ai_confidence_threshold: float = 0.80,
    skip_page_patterns: list[str] | None = None,
    dismissed_patterns: list[dict] | None = None,
    cost_tracker: CostTracker | None = None,
) -> dict:
    """
    3-phase analysis pipeline (provider-agnostic):

    Phase 1: Rule Engine — cross-session pattern detection (rage clicks, error loops, etc.)
    Phase 2: Algorithmic Detector — per-session instant detection (network errors, flash
             errors, console errors, instant bounce, form no-response, silent failures)
    Phase 3: AI Unified — per-session deep analysis for issues requiring contextual
             reasoning (wrong validation, misleading messages, confusing flows)

    Each phase deduplicates against issues already found by earlier phases.
    """
    from app.services.rule_engine import RuleEngine, _extract_steps_before

    sessions: list[NormalizedSession] = await connector.fetch_sessions(since, limit=max_sessions)
    logger.info(f"Fetched {len(sessions)} sessions from last 24h")

    # Filter already-processed
    if db_project_id:
        processed_ids = _get_processed_session_ids(db_project_id)
        original = len(sessions)
        sessions = [s for s in sessions if s.id not in processed_ids]
        skipped = original - len(sessions)
        if skipped > 0:
            logger.info(f"Skipped {skipped} already-processed sessions for project {db_project_id}")

    total = len(sessions)

    if not sessions:
        return {"sessions_analyzed": 0, "issues_found": 0, "issues": []}

    logger.info(f"Starting hybrid analysis ({connector.provider}): {total} sessions")

    if progress_callback:
        try:
            await progress_callback(0, total, 0)
        except Exception:
            pass

    # ── Phase 1: Rule engine (cross-session patterns) ──────────────────
    engine = RuleEngine(
        min_users=min_users,
        min_occurrences=min_users,
        skip_page_patterns=skip_page_patterns or [],
    )
    detected = engine.analyze(sessions)

    all_issues: list[dict] = []
    seen_fingerprints: set[str] = set()
    # Track (category, page) pairs for cross-method dedup (rule vs AI)
    seen_category_page: set[str] = set()

    for issue in detected:
        issue_dict = issue.to_dict()
        issue_dict["session_id"] = issue.sample_sessions[0] if issue.sample_sessions else ""
        issue_dict["distinct_id"] = issue.affected_users[0] if issue.affected_users else ""
        all_issues.append(issue_dict)
        if issue.fingerprint:
            seen_fingerprints.add(issue.fingerprint)
        # Track category+page for dedup against AI issues
        cat = issue_dict.get("category", "")
        page = _normalize_url(issue_dict.get("page_url") or "")
        if cat and page:
            seen_category_page.add(f"{cat}||{page}")

    logger.info(f"Rule engine found {len(all_issues)} issues")

    # ── Pre-compute: Fetch DOM texts + recording signals, enrich events ──
    dom_texts_by_session: dict[str, list[dict]] = {}
    dom_diffs_by_session: dict[str, list[dict]] = {}
    enriched_count = 0
    try:
        for i, session in enumerate(sessions[:20]):  # limit to 20 sessions to avoid rate limits
            try:
                # Use combined fetch if connector supports it (avoids fetching blobs twice)
                if hasattr(connector, "fetch_session_dom_and_signals"):
                    dom_texts, rec_signals = await connector.fetch_session_dom_and_signals(session.id)
                    # Enrich session events with recording signals (network errors, console logs)
                    if rec_signals:
                        sessions[i] = connector.enrich_session_events(session, rec_signals)
                        session = sessions[i]
                        enriched_count += 1
                else:
                    dom_texts = await connector.fetch_session_dom_texts(session.id)

                if dom_texts:
                    dom_texts_by_session[session.id] = dom_texts
                    dom_diffs_by_session[session.id] = _compute_dom_diffs(
                        dom_texts, events=session.events
                    )
                    logger.debug(f"Session {session.id}: extracted {len(dom_texts)} DOM texts")
            except Exception as e:
                logger.debug(f"DOM text fetch failed for {session.id}: {e}")
        if dom_texts_by_session:
            logger.info(
                f"Fetched DOM texts from {len(dom_texts_by_session)} sessions"
                + (f", enriched {enriched_count} sessions with recording signals" if enriched_count else "")
            )
    except Exception as e:
        logger.warning(f"DOM text fetching phase failed: {e}")

    # ── Phase 2: Algorithmic per-session detection (instant, zero cost) ──
    from app.services.algorithmic_detector import AlgorithmicDetector

    algo_detector = AlgorithmicDetector(skip_page_patterns=skip_page_patterns)
    algo_issue_count = 0

    for session in sessions:
        try:
            session_dom_diffs = dom_diffs_by_session.get(session.id)
            session_dom_texts = dom_texts_by_session.get(session.id)
            algo_issues = algo_detector.detect(
                session,
                dom_diffs=session_dom_diffs,
                dom_texts=session_dom_texts,
            )

            for issue in algo_issues:
                issue_dict = issue.to_dict()
                issue_dict["session_id"] = session.id
                issue_dict["distinct_id"] = session.distinct_id or ""

                # Dedup: skip if fingerprint already seen (from rule engine)
                fp = issue.fingerprint
                if fp and fp in seen_fingerprints:
                    continue

                # Fuzzy title dedup against existing issues
                if _is_fuzzy_duplicate(issue_dict.get("title", ""), all_issues, issue_dict.get("page_url", "")):
                    logger.debug(f"Skipped fuzzy duplicate algo issue: '{issue_dict.get('title')}'")
                    continue

                all_issues.append(issue_dict)
                algo_issue_count += 1
                if fp:
                    seen_fingerprints.add(fp)
                cat = issue_dict.get("category", "")
                page = _normalize_url(issue_dict.get("page_url") or "")
                if cat and page:
                    seen_category_page.add(f"{cat}||{page}")

        except Exception as e:
            logger.error(f"Algorithmic detection failed for session {session.id}: {e}")

    logger.info(f"Algorithmic detector found {algo_issue_count} issues")

    # ── Phase 2.5: Hybrid enrichment (focused micro-AI on event clusters) ──
    # Groups related signals (network error + console error + DOM change) into
    # tight time-window clusters, then makes focused micro-AI calls for precise
    # descriptions. Replaces generic algo titles with actionable ones.
    from app.services.hybrid_enrichment import (
        build_event_clusters,
        analyze_session_clusters,
        enrich_or_replace_algo_issues,
        count_session_triggers,
    )

    hybrid_issue_count = 0
    sessions_fully_covered: set[str] = set()

    async def _process_session_hybrid(sess: NormalizedSession) -> tuple[str, list[dict]]:
        """Build clusters and analyze them for a single session."""
        try:
            clusters = build_event_clusters(
                sess,
                dom_texts=dom_texts_by_session.get(sess.id),
                dom_diffs=dom_diffs_by_session.get(sess.id),
                skip_page_patterns=skip_page_patterns,
            )
            if not clusters:
                return sess.id, []
            enriched = await analyze_session_clusters(sess, clusters, cost_tracker)
            return sess.id, enriched
        except Exception as e:
            logger.error(f"Hybrid enrichment failed for {sess.id}: {e}")
            return sess.id, []

    # Process all sessions in parallel
    hybrid_results = await asyncio.gather(*[
        _process_session_hybrid(s) for s in sessions
    ])

    for session_id, hybrid_issues in hybrid_results:
        if not hybrid_issues:
            continue

        # Find the session object
        session_obj = next((s for s in sessions if s.id == session_id), None)
        if not session_obj:
            continue

        # Enrich existing algo issues or add new ones
        all_issues = enrich_or_replace_algo_issues(
            all_issues, hybrid_issues, session_obj, seen_fingerprints,
        )

        # Count new issues added by hybrid
        for h in hybrid_issues:
            if h.get("confidence", 0) >= 0.70:
                hybrid_issue_count += 1

        # Track if all triggers in this session were covered
        trigger_count = count_session_triggers(session_obj)
        cluster_count = len(hybrid_issues)
        if trigger_count > 0 and cluster_count >= trigger_count:
            sessions_fully_covered.add(session_id)

    if hybrid_issue_count > 0:
        logger.info(
            f"Hybrid enrichment: {hybrid_issue_count} issues enriched/added, "
            f"{len(sessions_fully_covered)} sessions fully covered"
        )

    # ── Phase 3: Unified AI per-session analysis (events + DOM together) ──
    # Only uses AI for issues that require contextual reasoning.
    # Skips sessions fully covered by hybrid enrichment.
    # Skips fingerprints already found by rule engine or algo detector.
    ai_issue_count = 0
    for idx, session in enumerate(sessions):
        # Skip sessions where all incidents were already enriched by hybrid phase
        if session.id in sessions_fully_covered:
            logger.debug(f"Session {session.id}: skipping full AI — covered by hybrid enrichment")
            if progress_callback:
                try:
                    await progress_callback(idx + 1, total, len(all_issues))
                except Exception:
                    pass
            continue
        try:
            # Single unified AI call per session: events + DOM interleaved
            session_dom_texts = dom_texts_by_session.get(session.id)
            ai_issues = await analyze_session_unified(
                session,
                dom_texts=session_dom_texts,
                dismissed_patterns=dismissed_patterns,
                cost_tracker=cost_tracker,
            )

            for ai_issue in ai_issues:
                # Filter by confidence threshold
                confidence = ai_issue.get("confidence", 0.5)
                if confidence < ai_confidence_threshold:
                    logger.debug(
                        f"Filtered AI issue '{ai_issue.get('title')}' — "
                        f"confidence {confidence:.0%} < {ai_confidence_threshold:.0%}"
                    )
                    continue

                # Skip if rule engine or algo detector already caught this (by fingerprint)
                fp = ai_issue.get("fingerprint", "")
                if fp and fp in seen_fingerprints:
                    continue

                # Skip if same category+page already found by rule engine or algo
                ai_cat = ai_issue.get("category", "")
                ai_page = _normalize_url(ai_issue.get("page_url") or "")
                cat_page_key = f"{ai_cat}||{ai_page}"
                if ai_cat and ai_page and cat_page_key in seen_category_page:
                    logger.debug(
                        f"Skipped AI issue '{ai_issue.get('title')}' — "
                        f"already flagged {ai_cat} on {ai_page}"
                    )
                    continue

                # Fuzzy title dedup: skip if similar to an existing issue
                if _is_fuzzy_duplicate(ai_issue.get("title", ""), all_issues, ai_issue.get("page_url", "")):
                    logger.debug(f"Skipped fuzzy duplicate AI issue: '{ai_issue.get('title')}'")
                    continue

                # Inject real user steps from session events (not AI-generated)
                if not ai_issue.get("reproduction_steps") or ai_issue.get("reproduction_steps") == []:
                    event_ts = ai_issue.get("_event_timestamp", "")
                    if event_ts:
                        ai_issue["reproduction_steps"] = _extract_steps_before(session, event_ts)
                    elif session.events:
                        ai_issue["reproduction_steps"] = _extract_steps_before(
                            session, session.events[-1].timestamp
                        )

                all_issues.append(ai_issue)
                ai_issue_count += 1
                if fp:
                    seen_fingerprints.add(fp)
                if ai_cat and ai_page:
                    seen_category_page.add(cat_page_key)

        except Exception as e:
            logger.error(f"AI analysis failed for session {session.id}: {e}")

        # Log per-session cost
        if cost_tracker:
            cost_tracker.log_session_cost(session.id)

        if progress_callback:
            try:
                await progress_callback(idx + 1, total, len(all_issues))
            except Exception:
                pass

    logger.info(f"Unified AI analysis found {ai_issue_count} issues (confidence >= {ai_confidence_threshold:.0%})")

    if progress_callback:
        try:
            await progress_callback(total, total, len(all_issues))
        except Exception:
            pass

    # ── Phase 4: AI Issue Merge ──────────────────────────────────────────
    # Many issues from different detectors are symptoms of the same root cause.
    # E.g. a failed DELETE produces: network_error + console_error + silent_failure.
    # This step uses AI to group them by root cause into clean, merged issues.
    from app.services.hybrid_enrichment import merge_related_issues_with_ai

    if len(all_issues) > 2:
        pre_merge_count = len(all_issues)
        # Pass first session for context (single-session analysis) or None for multi
        ctx_session = sessions[0] if len(sessions) == 1 else None
        all_issues = await merge_related_issues_with_ai(
            all_issues,
            session=ctx_session,
            cost_tracker=cost_tracker,
        )
        logger.info(f"AI merge: {pre_merge_count} → {len(all_issues)} issues")

    # ── Phase 5: AI False Positive Validation ────────────────────────────
    # Final AI pass to filter out false positives using Chain-of-Thought.
    from app.services.hybrid_enrichment import validate_issues_with_ai

    if all_issues:
        pre_validation_count = len(all_issues)
        all_issues = await validate_issues_with_ai(
            all_issues,
            cost_tracker=cost_tracker,
        )
        filtered = pre_validation_count - len(all_issues)
        if filtered > 0:
            logger.info(f"Phase 5 validation: removed {filtered} false positives")

    # ── Phase 6: Cross-Session Correlation ───────────────────────────────
    # Boost confidence for issues seen across multiple sessions, demote single-occurrence.
    if len(sessions) > 1 and all_issues:
        all_issues = _correlate_cross_session_issues(all_issues, total_sessions=len(sessions))

    # Mark all sessions as processed
    analyzed_ids = [s.id for s in sessions]
    if db_project_id and analyzed_ids:
        _mark_sessions_processed(db_project_id, analyzed_ids)

    # ── Log AI cost summary ─────────────────────────────────────────────
    cost_summary = {}
    if cost_tracker:
        cost_tracker.log_summary(analysis_id=db_project_id or "")
        cost_summary = cost_tracker.summary()

    rule_count = len(all_issues) - algo_issue_count - hybrid_issue_count - ai_issue_count
    logger.info(
        f"Analysis complete ({connector.provider}): "
        f"{total} sessions, {len(all_issues)} issues "
        f"({rule_count} rule + {algo_issue_count} algo + {hybrid_issue_count} hybrid + {ai_issue_count} AI)"
    )

    return {
        "sessions_analyzed": total,
        "issues_found": len(all_issues),
        "issues": all_issues,
        "ai_cost": cost_summary,
    }
