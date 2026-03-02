"""
LogRocket session replay connector.

Uses the LogRocket REST API to fetch sessions and events,
then normalises them into NormalizedSession / NormalizedEvent.

API docs: https://docs.logrocket.com/reference
"""

from __future__ import annotations

from datetime import datetime

import httpx

from app.connectors.base import SessionConnector, NormalizedSession, NormalizedEvent
from app.utils.logger import logger

LOGROCKET_API = "https://api.logrocket.com"


def _normalise_event(raw: dict) -> NormalizedEvent:
    """Convert a LogRocket event into a NormalizedEvent."""
    event_kind = raw.get("type", raw.get("event_type", "custom")).lower()
    timestamp = raw.get("time", raw.get("timestamp", ""))
    url = raw.get("url", raw.get("href", ""))
    pathname = raw.get("path", "")

    tag_name = raw.get("tagName", raw.get("element_tag", ""))
    el_text = (raw.get("text", raw.get("element_text", "")) or "")[:100]
    css_selector = raw.get("selector", raw.get("cssSelector", ""))

    # Extract form-related info
    element_type = raw.get("inputType", raw.get("elementType", ""))
    element_name = raw.get("inputName", raw.get("elementName", raw.get("name", "")))
    validation_msg = raw.get("validationMessage", "")

    etype = "custom"
    error_msg = ""
    error_type = ""
    status_code = None
    method = ""
    endpoint = ""
    form_action = ""

    if event_kind in ("navigation", "pageview", "page_load"):
        etype = "pageview"
    elif event_kind in ("page_exit", "pageleave"):
        etype = "pageleave"
    elif event_kind in ("submit", "form_submit"):
        etype = "submit"
        form_action = raw.get("formAction", "")
    elif event_kind in ("focus", "focusin"):
        etype = "focus"
    elif event_kind in ("blur", "focusout"):
        etype = "blur"
    elif event_kind in ("input", "change"):
        etype = "input"
    elif event_kind == "click":
        etype = "click"
    elif event_kind == "tap":
        etype = "click"
    elif event_kind in ("rage_click", "rageclick"):
        etype = "rage_click"
    elif event_kind == "dead_click":
        etype = "dead_click"
    elif event_kind in ("error", "exception", "console.error", "unhandled_exception"):
        etype = "error"
        error_msg = (raw.get("message", raw.get("error_message", "")) or "")[:500]
        error_type = raw.get("name", raw.get("error_type", ""))
    elif event_kind in ("network", "xhr", "fetch"):
        sc = raw.get("statusCode", raw.get("status"))
        if sc and int(str(sc)) >= 400:
            etype = "network_error"
            status_code = int(str(sc))
            method = raw.get("method", "")
            endpoint = raw.get("requestUrl", raw.get("url", url))

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


class LogRocketConnector(SessionConnector):
    provider = "logrocket"

    def __init__(self, api_key: str, project_id: str = "", host: str = "", **_kw):
        self.api_key = api_key
        self.app_id = project_id  # LogRocket app slug e.g. "org/app-name"

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
                # LogRocket sessions search
                search_body = {
                    "query": {
                        "start_time": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "sort": "latest",
                    },
                    "limit": limit,
                }
                resp = await client.post(
                    f"{LOGROCKET_API}/v1/orgs/{self.app_id}/sessions/search",
                    headers=headers,
                    json=search_body,
                )
                resp.raise_for_status()
                results = resp.json().get("sessions", resp.json().get("results", []))

                for raw_session in results[:limit]:
                    session_id = raw_session.get("id", raw_session.get("sessionId", ""))
                    user_id = raw_session.get("userID", raw_session.get("email", raw_session.get("userId", "")))

                    # Fetch events for this session
                    raw_events = await self._fetch_session_events(client, headers, session_id)
                    norm_events = [_normalise_event(e) for e in raw_events]

                    if len(norm_events) < 2:
                        continue

                    sessions.append(NormalizedSession(
                        id=session_id,
                        distinct_id=user_id,
                        start_time=raw_session.get("startTime", raw_session.get("created_at", "")),
                        end_time=raw_session.get("endTime", raw_session.get("updated_at", "")),
                        events=norm_events,
                        replay_url=self.build_replay_url(session_id),
                        metadata={"provider": "logrocket"},
                    ))

            except httpx.HTTPError as exc:
                logger.error(f"LogRocket API error: {exc}")
            except Exception as exc:
                logger.error(f"LogRocket connector error: {exc}")

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
                f"{LOGROCKET_API}/v1/orgs/{self.app_id}/sessions/{session_id}/events",
                headers=headers,
            )
            resp.raise_for_status()
            return resp.json().get("events", resp.json().get("results", []))
        except httpx.HTTPError as exc:
            logger.warning(f"LogRocket events fetch failed for {session_id}: {exc}")
            return []

    def build_replay_url(self, session_id: str) -> str:
        return f"https://app.logrocket.com/{self.app_id}/sessions/{session_id}"
