"""Base interface and registry for web search providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable

import httpx
from pydantic import BaseModel

from ger.config import SearchProviderConfig


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str
    content: str = ""
    published_date: str | None = None
    provider: str = ""


class BaseSearchProvider(ABC):
    def __init__(self, config: SearchProviderConfig) -> None:
        self.config = config

    @abstractmethod
    async def search(
        self,
        client: httpx.AsyncClient,
        query: str,
        *,
        max_results: int = 5,
    ) -> list[SearchResult]:
        """Execute a web search and return normalized results."""


SEARCH_REGISTRY: dict[str, type[BaseSearchProvider]] = {}


def register_search_provider(name: str) -> Callable[[type[BaseSearchProvider]], type[BaseSearchProvider]]:
    def decorator(cls: type[BaseSearchProvider]) -> type[BaseSearchProvider]:
        SEARCH_REGISTRY[name] = cls
        return cls

    return decorator


def create_search_provider(config: SearchProviderConfig) -> BaseSearchProvider:
    cls = SEARCH_REGISTRY.get(config.type)
    if cls is None:
        raise ValueError(f"Unsupported search provider type: {config.type}")
    return cls(config)
