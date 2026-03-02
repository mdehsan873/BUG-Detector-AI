# Buglyft Detection Pipeline — Research: Reducing False Positives & Improving Accuracy

## The Core Problem

Our pipeline is **signal-rich but context-poor**. We can detect *that* something went wrong (events, errors, timing), but struggle to verify *what the user actually experienced visually*. This leads to two failure modes:

1. **False positives** — flagging normal behavior as bugs (e.g., login form "no response" when DOM actually changed to show loading → verified)
2. **False negatives** — missing real bugs because we can't see CSS states, caught errors, or incremental DOM mutations

---

## Root Cause: The DOM Gap

PostHog's rrweb captures DOM in two ways:
- **Type 2 (Full Snapshot):** Complete DOM tree — captured at page load, navigation, and periodic checkpoints
- **Type 3 (Incremental Snapshot):** MutationObserver diffs — every text change, node add/remove, attribute change

**We only extract Type 2.** PostHog's player reconstructs full DOM at any timestamp by replaying Type 3 mutations on top of the last Type 2 snapshot. We can't do this yet, so we have timing gaps where we can't see the DOM.

### Impact on Each Detector

| Detector | How DOM gap hurts it |
|----------|---------------------|
| form_no_response | Can't see loading spinners, inline success/error messages between snapshots |
| silent_failure | Can't verify if error was actually shown to user via CSS toast/modal |
| console_error | Can't see if error caused visible UI breakage |
| instant_bounce | Can't see what page looked like when user bounced |
| Hybrid AI clusters | AI gets approximate DOM (~34s away) instead of exact state at error time |

---

## Priority Improvements (Ranked by Impact)

### 1. Replay Incremental Mutations (HIGH IMPACT, MEDIUM EFFORT)

**Problem:** We extract Type 2 full snapshots but ignore Type 3 incremental mutations for DOM state reconstruction.

**Solution:** Use `rrweb-snapshot` npm package's `rebuild()` function to reconstruct DOM at any timestamp:
1. Take the last Type 2 full snapshot before the target timestamp
2. Apply all Type 3 mutations in order up to the target timestamp
3. Serialize the resulting DOM to markdown

**Implementation:**
- Create a Node.js helper script (`rebuild_dom.js`) that takes rrweb JSONL + target timestamp → outputs DOM markdown
- Call from Python via subprocess when building cluster context
- Cache results per session to avoid re-processing

**What it fixes:**
- form_no_response: Can see exact DOM when loading spinner appeared, when "Email verified" appeared
- Hybrid AI: Gets exact DOM at the moment of the error, not ~34s approximation
- silent_failure: Can verify DOM state at exact moment of network error

**Cost:** ~2-3 days of implementation. Requires Node.js runtime.

**References:**
- [rrweb-snapshot npm](https://www.npmjs.com/package/rrweb-snapshot) — `rebuild()` function
- [rrweb replay docs](https://github.com/rrweb-io/rrweb/blob/master/docs/replay.md)

---

### 2. AI-Powered False Positive Filter (HIGH IMPACT, LOW EFFORT)

**Problem:** Algorithmic detectors use hard thresholds (3s, 10s, 5 clicks) that inevitably produce false positives for edge cases.

**Solution:** Add a final AI validation pass before creating issues. For each candidate issue, send the full context to AI and ask: "Is this a real bug, or normal user behavior?"

**Implementation:**
- After Phase 4 (AI Merge), add **Phase 5: AI Validation**
- For each issue, send: title, description, evidence, reproduction steps, DOM context, event timeline
- AI returns: `{is_real_bug: true/false, reasoning: "...", adjusted_confidence: 0.0-1.0}`
- Filter out issues where `is_real_bug == false` or `adjusted_confidence < 0.6`

**Prompt strategy (from research):**
- Use Chain-of-Thought reasoning: "First analyze what the user was trying to do, then what happened, then whether this is expected behavior"
- Include negative examples: "These are NOT bugs: reading a page quickly, navigating back, auth redirects, loading states"
- Cost: ~$0.001-0.01 per issue validation with gpt-4o-mini

**What it fixes:**
- form_no_response false positives (loading states, delayed responses)
- instant_bounce false positives (quick page skimming)
- rage_click false positives (legitimate double-clicks)

**References:**
- [Datadog: Using LLMs to filter false positives](https://www.datadoghq.com/blog/using-llms-to-filter-out-false-positives/)
- [ArXiv: Reducing False Positives with LLMs (2025)](https://arxiv.org/abs/2601.18844) — 94-98% false positive elimination in industrial settings

---

### 3. Network Response Body Capture (HIGH IMPACT, MEDIUM EFFORT)

**Problem:** We only see HTTP status codes, not response bodies. A 200 response with `{"error": "Account locked"}` looks like success to us. A 500 with `{"message": "Rate limited, try again"}` looks identical to a server crash.

**Solution:** Extend PostHog capture to include response bodies (or at least first 500 chars).

**Implementation options:**
- **Option A:** PostHog `rrweb/network@1` plugin supports `recordBody: true` config — just needs to be enabled in the client SDK config. Then extract body in our connector.
- **Option B:** If client config isn't changeable, use a custom PostHog plugin that captures response bodies as custom events.

**What it fixes:**
- network_error: Can distinguish "server crashed" from "rate limited" from "invalid input"
- silent_failure: Can see if error response contained user-facing message
- Hybrid AI: Much richer context for understanding what went wrong

---

### 4. Widen Hybrid Cluster Trigger Detection (MEDIUM IMPACT, LOW EFFORT)

**Problem:** Hybrid enrichment triggers form_no_response at 3s but algo detector now uses 10s. This inconsistency means hybrid clusters miss slow-responding forms.

**Solution:** Align trigger windows:
- Hybrid form_no_response trigger: 3s → 10s (match algo detector)
- Add trigger for: any `submit` event where DOM changes significantly within 15s but no `pageview` (possible inline response that needs AI analysis)

**What it fixes:**
- Catches slow form responses for AI analysis
- Ensures hybrid and algo detectors agree on what's "no response"

---

### 5. CSS State Extraction from rrweb (MEDIUM IMPACT, HIGH EFFORT)

**Problem:** We can't see CSS visual states — `display: none`, `opacity: 0`, `visibility: hidden`, hover states, disabled buttons. An error div might exist in DOM but be invisible via CSS.

**Solution:** When extracting DOM markdown from rrweb snapshots, also check inline styles and computed style data:
- rrweb captures `style` attributes on elements
- Check for `display: none`, `visibility: hidden`, `opacity: 0` (we partially do this already)
- Also check `aria-hidden="true"`, `hidden` attribute
- Annotate elements with visibility status: `[HIDDEN] Error: Something went wrong`

**What it fixes:**
- silent_failure: Can distinguish "error element exists but hidden" from "no error element at all"
- form_no_response: Can detect disabled submit buttons
- console_error: Can verify if error caused visible vs invisible UI breakage

---

### 6. Cross-Session Pattern Correlation (MEDIUM IMPACT, MEDIUM EFFORT)

**Problem:** Single-session detectors can't tell if an issue is a one-off glitch or a systemic problem. A form failing once might be a network hiccup; failing for 20% of users is a real bug.

**Solution:** After single-session analysis, aggregate issues across sessions:
- Group issues by `(page, rule_id, fingerprint)`
- Calculate: occurrence rate, affected user count, failure rate
- **Boost confidence** for issues seen across multiple users
- **Demote confidence** for issues seen in only 1 session (might be user-specific)

**What it fixes:**
- Reduces false positives from one-off network glitches
- Surfaces systemic bugs that affect many users
- Provides better severity scoring

---

### 7. Improve Repro Steps with Semantic Actions (LOW IMPACT, LOW EFFORT)

**Problem:** Current repro steps say "User clicked on 'element' on /login/" — generic and unhelpful. We have the data to be more specific.

**Solution:** Enrich `_event_to_step()` in `rule_engine.py`:
- Include element text: "User clicked 'Sign in' button"
- Include input context: "User typed email in 'Email' field"
- Include form action: "User submitted form to /api/login"
- Include navigation target: "User navigated to Settings page"

**What it fixes:**
- Much clearer reproduction steps for developers
- AI merge has better context for grouping related issues

---

### 8. Session Health Score (LOW IMPACT, LOW EFFORT)

**Problem:** We analyze every session equally. Sessions with 0 errors and normal browsing still get full pipeline treatment, wasting API costs.

**Solution:** Add a pre-filter "session health score":
- Count: network errors, console errors, rage clicks, form submits without response
- If score == 0: skip AI analysis entirely (no issues possible from algorithmic detection)
- If score < 3: run lightweight analysis only
- If score >= 3: run full pipeline

**What it fixes:**
- Reduces API costs by 60-70% (most sessions are healthy)
- Focuses AI budget on sessions that actually have problems

---

## Detector-Specific Fixes

### form_no_response — Current #1 False Positive Source

**Already fixed (this session):**
- Event window: 3s → 10s
- DOM window: 5s → 15s with 60s fallback
- DOM change detection (15% similarity threshold)

**Still needed:**
- [ ] Check for `aria-busy="true"` or `aria-live` regions in DOM (loading indicators)
- [ ] Check if form has `action=""` or `action="#"` (SPA forms that handle submit in JS)
- [ ] Track if form's submit button became disabled after click (common loading pattern)
- [ ] If user typed in same form again after submit → form was interactive, not stuck

### silent_failure — Needs DOM Reconstruction

**Current approach:** Check for error keywords in DOM within ±30s of network error.

**Problems:**
- 30s window too wide — might find unrelated error text
- Can't see CSS-hidden error messages
- Can't see toast notifications (often in portals/shadow DOM)

**Fixes needed:**
- [ ] Reconstruct DOM at exact error timestamp (requires improvement #1)
- [ ] Narrow window to ±5s once we have exact DOM state
- [ ] Check for common toast libraries (react-toastify, sonner) in DOM structure

### instant_bounce — Needs Intent Detection

**Current approach:** User lands and leaves within 2-3s with 0 interactions.

**Problems:**
- Can't distinguish "page broke" from "user saw what they needed"
- No way to detect reading intent

**Fixes needed:**
- [ ] Check if page had error state in DOM when user bounced
- [ ] Check if previous page had a link/button that led here (intentional navigation vs error redirect)
- [ ] Lower confidence if page is a simple info page (no forms, no actions)
- [ ] Check scroll_y — any scrolling at all suggests user engaged, not bounced from error

---

## Data We're Missing (and How to Get It)

| Missing Data | Why It Matters | How to Get It |
|-------------|----------------|---------------|
| HTTP response bodies | Distinguish error types | Enable `recordBody: true` in PostHog network plugin |
| Incremental DOM mutations | Exact DOM at any timestamp | Use rrweb `rebuild()` via Node.js |
| CSS computed styles | See hidden/disabled elements | Extract from rrweb inline styles (partial) |
| Console.log (not just errors) | See debug output, API responses | Enable `level: ['log']` in PostHog console plugin |
| Tab visibility state | Know if user was actually looking | Extract from rrweb visibility events |
| Viewport scroll position | Know what user could see | Already in scroll events, need to correlate with DOM |
| Shadow DOM content | See portal-based toasts/modals | PostHog captures shadow DOM if `recordCrossOriginIframes: true` |

---

## Recommended Implementation Order

| Priority | Improvement | Effort | Impact | False Positive Reduction |
|----------|------------|--------|--------|-------------------------|
| **P0** | AI False Positive Filter (Phase 5) | 1 day | High | ~60-70% reduction |
| **P0** | Align hybrid trigger windows | 2 hours | Medium | ~10% reduction |
| **P1** | rrweb Incremental Mutation Replay | 2-3 days | High | ~30% reduction |
| **P1** | Network response body capture | 1 day (config) | High | ~20% reduction |
| **P1** | Improve repro steps | 0.5 day | Low | Improves quality, not quantity |
| **P2** | Cross-session correlation | 2-3 days | Medium | ~15% reduction |
| **P2** | CSS state extraction | 1-2 days | Medium | ~10% reduction |
| **P3** | Session health score | 0.5 day | Low | Saves cost, not accuracy |

**Estimated total false positive reduction with P0+P1:** ~80-85%

---

## References

- [rrweb Observer Documentation](https://github.com/rrweb-io/rrweb/blob/master/docs/observer.md)
- [rrweb Replay Documentation](https://github.com/rrweb-io/rrweb/blob/master/docs/replay.md)
- [rrweb-snapshot npm package](https://www.npmjs.com/package/rrweb-snapshot) — `rebuild()` function
- [PostHog Session Replay Architecture](https://posthog.com/handbook/engineering/session-replay/session-replay-architecture)
- [PostHog Snapshot API](https://posthog.com/docs/session-replay/snapshot-api)
- [Datadog: Using LLMs to filter false positives from static analysis](https://www.datadoghq.com/blog/using-llms-to-filter-out-false-positives/)
- [ArXiv: Reducing False Positives in Static Bug Detection with LLMs (2025)](https://arxiv.org/abs/2601.18844)
- [ArXiv: Minimizing False Positives via LLM-Enhanced Path Analysis](https://arxiv.org/html/2506.10322v1)
