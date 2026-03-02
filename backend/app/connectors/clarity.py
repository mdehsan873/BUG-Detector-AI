"""
Microsoft Clarity session replay connector.

Uses the Clarity Export API to fetch session recordings and events,
then normalises them into NormalizedSession / NormalizedEvent.

API docs: https://learn.microsoft.com/en-us/clarity/setup-and-installation/clarity-api
"""

from __future__ import annotations

from datetime import datetime

import httpx

from app.connectors.base import SessionConnector, NormalizedSession, NormalizedEvent
from app.utils.logger import logger

CLARITY_API = "https://www.clarity.ms/export-data/api/v1"


def _normalise_event(raw: dict) -> NormalizedEvent:
    """Convert a Clarity event into a NormalizedEvent."""
    event_kind = raw.get("Type", raw.get("type", raw.get("event_type", "custom"))).lower()
    timestamp = raw.get("Timestamp", raw.get("timestamp", raw.get("time", "")))
    url = raw.get("PageUrl", raw.get("Url", raw.get("url", "")))
    pathname = raw.get("PagePath", raw.get("path", ""))

    tag_name = raw.get("Tag", raw.get("element_tag", ""))
    el_text = (raw.get("Text", raw.get("element_text", "")) or "")[:100]
    css_selector = raw.get("Selector", raw.get("css_selector", ""))

    # Extract form-related info
    element_type = raw.get("InputType", raw.get("ElementType", ""))
    element_name = raw.get("InputName", raw.get("ElementName", raw.get("Name", "")))
    validation_msg = raw.get("ValidationMessage", "")
    scroll_y = raw.get("ScrollY", raw.get("scroll_y"))
    viewport_w = raw.get("ViewportWidth", raw.get("viewport_width"))
    viewport_h = raw.get("ViewportHeight", raw.get("viewport_height"))

    etype = "custom"
    error_msg = ""
    error_type = ""
    status_code = None
    method = ""
    endpoint = ""
    form_action = ""

    if event_kind in ("pageview", "page", "navigation", "page_view"):
        etype = "pageview"
    elif event_kind in ("pageleave", "page_exit", "unload"):
        etype = "pageleave"
    elif event_kind in ("submit", "form_submit"):
        etype = "submit"
        form_action = raw.get("FormAction", "")
    elif event_kind in ("focus", "focusin"):
        etype = "focus"
    elif event_kind in ("blur", "focusout"):
        etype = "blur"
    elif event_kind in ("input", "change"):
        etype = "input"
    elif event_kind in ("click", "tap"):
        etype = "click"
    elif event_kind in ("rage_click", "rageclick", "excessive_clicking"):
        etype = "rage_click"
    elif event_kind == "dead_click":
        etype = "dead_click"
    elif event_kind in ("error", "exception", "script_error", "javascript_error"):
        etype = "error"
        error_msg = (raw.get("Message", raw.get("ErrorMessage", "")) or "")[:500]
        error_type = raw.get("ErrorType", raw.get("Name", ""))
    elif event_kind in ("network", "xhr", "fetch", "api_call"):
        sc = raw.get("StatusCode", raw.get("status_code"))
        if sc and int(str(sc)) >= 400:
            etype = "network_error"
            status_code = int(str(sc))
            method = raw.get("Method", "")
            endpoint = raw.get("RequestUrl", url)
    elif event_kind in ("scroll", "resize"):
        etype = event_kind

    return NormalizedEvent(
        timestamp=timestamp,
        event_type=etype,
        url=url,
        pathname=pathname,
        tag_name=tag_name,
        element_text=el_text,
        css_selector=css_selector,
        element_type=element_type,
        element_name=element_name,
        validation_message=validation_msg,
        form_action=form_action,
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


class ClarityConnector(SessionConnector):
    provider = "clarity"

    def __init__(self, api_key: str, project_id: str = "", host: str = "", **_kw):
        self.api_key = api_key
        self.project_id = project_id  # Clarity project ID

    async def fetch_sessions(
        self,
        since: datetime,
        limit: int = 50,
    ) -> list[NormalizedSession]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        sessions: list[NormalizedSession] = []
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                # Clarity export API for sessions
                params = {
                    "projectId": self.project_id,
                    "startDate": since.strftime("%Y-%m-%d"),
                    "numOfDays": 1,
                    "limit": limit,
                }
                resp = await client.get(
                    f"{CLARITY_API}/project/{self.project_id}/sessions",
                    headers=headers,
                    params=params,
                )
                resp.raise_for_status()
                results = resp.json().get("Sessions", resp.json().get("sessions", resp.json().get("results", [])))

                for raw_session in results[:limit]:
                    session_id = raw_session.get("SessionId", raw_session.get("Id", raw_session.get("id", "")))
                    user_id = raw_session.get("UserId", raw_session.get("CustomUserId", ""))

                    # Fetch events for this session
                    raw_events = await self._fetch_session_events(client, headers, session_id)
                    norm_events = [_normalise_event(e) for e in raw_events]

                    if len(norm_events) < 2:
                        continue

                    sessions.append(NormalizedSession(
                        id=session_id,
                        distinct_id=user_id,
                        start_time=raw_session.get("StartTime", raw_session.get("start_time", "")),
                        end_time=raw_session.get("EndTime", raw_session.get("end_time", "")),
                        events=norm_events,
                        replay_url=self.build_replay_url(session_id),
                        metadata={"provider": "clarity"},
                    ))

            except httpx.HTTPError as exc:
                logger.error(f"Clarity API error: {exc}")
            except Exception as exc:
                logger.error(f"Clarity connector error: {exc}")

        return sessions

    async def _fetch_session_events(
        self,
        client: httpx.AsyncClient,
        headers: dict,
        session_id: str,
    ) -> list[dict]:
        """Fetch events for a specific Clarity session."""
        try:
            resp = await client.get(
                f"{CLARITY_API}/project/{self.project_id}/sessions/{session_id}/events",
                headers=headers,
            )
            resp.raise_for_status()
            return resp.json().get("Events", resp.json().get("events", []))
        except httpx.HTTPError as exc:
            logger.warning(f"Clarity events fetch failed for {session_id}: {exc}")
            return []

    def build_replay_url(self, session_id: str) -> str:
        return f"https://clarity.microsoft.com/projects/{self.project_id}/session/{session_id}"
