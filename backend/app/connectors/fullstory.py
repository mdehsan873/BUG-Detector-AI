"""
FullStory session replay connector.

Uses the FullStory REST API v2 to fetch sessions and events,
then normalises them into NormalizedSession / NormalizedEvent.

API docs: https://developer.fullstory.com/
"""

from __future__ import annotations

from datetime import datetime

import httpx

from app.connectors.base import SessionConnector, NormalizedSession, NormalizedEvent
from app.utils.logger import logger

FULLSTORY_API = "https://api.fullstory.com"


def _normalise_event(raw: dict) -> NormalizedEvent:
    """Convert a FullStory event into a NormalizedEvent."""
    event_kind = raw.get("EventType", raw.get("event_type", "custom")).lower()
    timestamp = raw.get("EventStart", raw.get("timestamp", ""))
    url = raw.get("PageUrl", raw.get("page_url", ""))
    pathname = raw.get("PagePath", raw.get("page_path", ""))

    tag_name = raw.get("ElementTag", raw.get("TargetTag", ""))
    el_text = (raw.get("ElementText", raw.get("TargetText", "")) or "")[:100]
    css_selector = raw.get("TargetSelector", raw.get("ElementSelector", ""))

    # Extract form-related info
    element_type = raw.get("ElementType", raw.get("InputType", ""))
    element_name = raw.get("ElementName", raw.get("InputName", raw.get("TargetName", "")))
    validation_msg = raw.get("ValidationMessage", "")

    # Map FullStory event types → canonical types
    etype = "custom"
    error_msg = ""
    error_type = ""
    status_code = None
    method = ""
    endpoint = ""
    form_action = ""

    if event_kind in ("navigate", "pageview", "page"):
        etype = "pageview"
    elif event_kind in ("pageleave", "page_exit"):
        etype = "pageleave"
    elif event_kind in ("submit", "form_submit"):
        etype = "submit"
        form_action = raw.get("FormAction", "")
    elif event_kind in ("focus", "focusin"):
        etype = "focus"
    elif event_kind in ("blur", "focusout"):
        etype = "blur"
    elif event_kind in ("change", "input"):
        etype = "input"
    elif event_kind == "click":
        # Detect dead clicks
        if tag_name and tag_name.lower() not in ("a", "button", "input", "select", "textarea", "label", "summary"):
            etype = "dead_click"
        else:
            etype = "click"
    elif event_kind in ("rage_click", "rageclick", "thrashed_cursor"):
        etype = "rage_click"
    elif event_kind in ("dead_click", "deadclick"):
        etype = "dead_click"
    elif event_kind in ("error", "exception", "console_error"):
        etype = "error"
        error_msg = (raw.get("ErrorMessage", raw.get("Message", "")) or "")[:500]
        error_type = raw.get("ErrorType", raw.get("Name", ""))
    elif event_kind in ("request", "network", "xhr"):
        sc = raw.get("StatusCode", raw.get("status_code"))
        if sc and int(str(sc)) >= 400:
            etype = "network_error"
            status_code = int(str(sc))
            method = raw.get("Method", raw.get("RequestMethod", ""))
            endpoint = raw.get("RequestUrl", raw.get("Url", url))

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
        raw=raw,
    )


class FullStoryConnector(SessionConnector):
    provider = "fullstory"

    def __init__(self, api_key: str, project_id: str = "", host: str = "", **_kw):
        self.api_key = api_key
        self.org_id = project_id  # FullStory uses org_id

    async def fetch_sessions(
        self,
        since: datetime,
        limit: int = 50,
    ) -> list[NormalizedSession]:
        headers = {
            "Authorization": f"Basic {self.api_key}",
            "Content-Type": "application/json",
        }

        sessions: list[NormalizedSession] = []
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                # FullStory v2 search sessions endpoint
                search_body = {
                    "start": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "limit": limit,
                }
                resp = await client.post(
                    f"{FULLSTORY_API}/v2/sessions/search",
                    headers=headers,
                    json=search_body,
                )
                resp.raise_for_status()
                results = resp.json().get("sessions", resp.json().get("results", []))

                for raw_session in results[:limit]:
                    session_id = raw_session.get("SessionId", raw_session.get("Id", raw_session.get("id", "")))
                    user_id = raw_session.get("UserId", raw_session.get("uid", ""))

                    # Fetch events for this session
                    raw_events = await self._fetch_session_events(client, headers, session_id)
                    norm_events = [_normalise_event(e) for e in raw_events]

                    if len(norm_events) < 2:
                        continue

                    sessions.append(NormalizedSession(
                        id=session_id,
                        distinct_id=user_id,
                        start_time=raw_session.get("CreatedTime", raw_session.get("start_time", "")),
                        end_time=raw_session.get("UpdatedTime", raw_session.get("end_time", "")),
                        events=norm_events,
                        replay_url=self.build_replay_url(session_id),
                        metadata={"provider": "fullstory"},
                    ))

            except httpx.HTTPError as exc:
                logger.error(f"FullStory API error: {exc}")
            except Exception as exc:
                logger.error(f"FullStory connector error: {exc}")

        return sessions

    async def _fetch_session_events(
        self,
        client: httpx.AsyncClient,
        headers: dict,
        session_id: str,
    ) -> list[dict]:
        """Fetch events for a specific session."""
        try:
            resp = await client.get(
                f"{FULLSTORY_API}/v2/sessions/{session_id}/events",
                headers=headers,
            )
            resp.raise_for_status()
            return resp.json().get("events", resp.json().get("results", []))
        except httpx.HTTPError as exc:
            logger.warning(f"FullStory events fetch failed for {session_id}: {exc}")
            return []

    def build_replay_url(self, session_id: str) -> str:
        return f"https://app.fullstory.com/ui/session/{session_id}"
