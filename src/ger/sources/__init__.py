"""Source plugin exports and registration."""

from ger.sources import ai_collector as _ai_collector  # noqa: F401
from ger.sources import gameg_site as _gameg_site  # noqa: F401
from ger.sources import ical_feed as _ical_feed  # noqa: F401
from ger.sources import miyoushe as _miyoushe  # noqa: F401
from ger.sources import rss as _rss  # noqa: F401
from ger.sources.base import (
    SOURCE_REGISTRY,
    BaseSource,
    build_stable_event_key,
    create_source,
    extract_text_from_html,
    register_source,
)

__all__ = [
    "BaseSource",
    "SOURCE_REGISTRY",
    "build_stable_event_key",
    "create_source",
    "extract_text_from_html",
    "register_source",
]
