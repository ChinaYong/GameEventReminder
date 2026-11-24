"""Tavily Search API provider (pure httpx)."""

from __future__ import annotations

import httpx

from ger.search.base import BaseSearchProvider, SearchResult, register_search_provider


@register_search_provider("tavily")
class TavilySearchProvider(BaseSearchProvider):
    DEFAULT_BASE_URL = "https://api.tavily.com"

    async def search(
        self,
        client: httpx.AsyncClient,
        query: str,
        *,
        max_results: int = 5,
    ) -> list[SearchResult]:
        if not self.config.api_key:
            raise ValueError(f"Tavily search provider '{self.config.id}' missing api_key")

        base_url = (self.config.base_url or self.DEFAULT_BASE_URL).rstrip("/")
        payload = {
            "query": query,
            "search_depth": "advanced",
            "max_results": max_results,
            "include_raw_content": False,
            "include_answer": False,
        }
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

        resp = await client.post(
            f"{base_url}/search",
            json=payload,
            headers=headers,
            timeout=self.config.timeout_seconds,
        )
        resp.raise_for_status()
        data = resp.json()

        results: list[SearchResult] = []
        for item in data.get("results", []):
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            snippet = str(item.get("content") or "").strip()
            raw_content = str(item.get("raw_content") or "").strip()
            results.append(
                SearchResult(
                    title=str(item.get("title") or url).strip(),
                    url=url,
                    snippet=snippet,
                    content=raw_content[:3000] if raw_content else snippet,
                    published_date=item.get("published_date"),
                    provider=self.config.id,
                )
            )
        return results
