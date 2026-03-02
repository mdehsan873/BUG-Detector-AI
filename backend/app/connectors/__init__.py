"""
Session replay connectors.

Each connector normalises a provider's API into a common format
so the AI analysis pipeline works identically regardless of source.
"""

from app.connectors.base import SessionConnector, NormalizedSession, NormalizedEvent
from app.connectors.posthog import PostHogConnector
from app.connectors.fullstory import FullStoryConnector
from app.connectors.logrocket import LogRocketConnector
from app.connectors.clarity import ClarityConnector

PROVIDERS: dict[str, type[SessionConnector]] = {
    "posthog": PostHogConnector,
    "fullstory": FullStoryConnector,
    "logrocket": LogRocketConnector,
    "clarity": ClarityConnector,
}

PROVIDER_LABELS: dict[str, str] = {
    "posthog": "PostHog",
    "fullstory": "FullStory",
    "logrocket": "LogRocket",
    "clarity": "Microsoft Clarity",
}


def get_connector(provider: str, **kwargs) -> SessionConnector:
    """Factory: return the right connector for the given provider name."""
    cls = PROVIDERS.get(provider)
    if cls is None:
        raise ValueError(f"Unknown session provider: {provider}")
    return cls(**kwargs)


__all__ = [
    "SessionConnector",
    "NormalizedSession",
    "NormalizedEvent",
    "PostHogConnector",
    "FullStoryConnector",
    "LogRocketConnector",
    "ClarityConnector",
    "PROVIDERS",
    "PROVIDER_LABELS",
    "get_connector",
]
