"""
Abstract base class for session replay connectors.

Every provider (PostHog, FullStory, LogRocket, Clarity …) implements this
interface so the AI analysis pipeline is provider-agnostic.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime

@dataclass
class NormalizedEvent:
    """A single user-session event in a provider-neutral format."""

    timestamp: str                  # ISO-8601
    event_type: str                 # pageview | pageleave | click | rage_click | error | network_error
                                    # | submit | input | focus | blur | scroll | resize | form_validation
                                    # | dead_click | custom
    url: str = ""                   # current page URL
    pathname: str = ""              # just the path portion
    tag_name: str = ""              # e.g. "button", "a", "input"
    element_text: str = ""          # visible text of clicked element
    css_selector: str = ""          # CSS selector of clicked element
    element_type: str = ""          # input type attr: "text", "password", "email", "submit", "checkbox"
    element_name: str = ""          # input name/id attr for form fields
    element_value: str = ""         # current value (masked for passwords)
    form_action: str = ""           # form action URL on submit
    validation_message: str = ""    # browser or custom validation error message
    error_message: str = ""         # for error events
    error_type: str = ""            # e.g. "TypeError"
    status_code: int | None = None  # for network errors
    method: str = ""                # HTTP method for network errors
    endpoint: str = ""              # request URL for network errors
    response_body: str = ""         # first 500 chars of HTTP response body (if captured)
    request_body: str = ""          # first 500 chars of HTTP request body (if captured)
    viewport_width: int | None = None   # viewport width at event time
    viewport_height: int | None = None  # viewport height at event time
    scroll_y: int | None = None     # scroll position
    raw: dict = field(default_factory=dict)  # original provider payload


@dataclass
class NormalizedSession:
    """A full session in provider-neutral format."""

    id: str                                 # session ID from the provider
    distinct_id: str = ""                   # user identifier
    start_time: str = ""                    # ISO-8601
    end_time: str = ""                      # ISO-8601
    events: list[NormalizedEvent] = field(default_factory=list)
    replay_url: str = ""                    # link to the session replay in the provider's UI
    metadata: dict = field(default_factory=dict)


class SessionConnector(abc.ABC):
    """
    Abstract connector.

    Subclasses must implement ``fetch_sessions`` which returns a list of
    ``NormalizedSession`` objects ready for AI analysis.
    """

    provider: str = "unknown"

    @abc.abstractmethod
    async def fetch_sessions(
        self,
        since: datetime,
        limit: int = 50,
    ) -> list[NormalizedSession]:
        """Fetch and normalise recent sessions from the provider."""
        ...

    def build_replay_url(self, session_id: str) -> str:
        """Return a URL that opens this session's replay in the provider UI."""
        return ""

    async def fetch_session_dom_texts(self, session_id: str) -> list[dict]:
        """
        Fetch DOM text content from session recording snapshots.
        Returns list of {"text": str, "page": str, "timestamp": str}.
        Default: returns empty (no snapshot support).
        """
        return []
