"""Exa Search API provider (pure httpx)."""

from __future__ import annotations

import httpx

from ger.search.base import BaseSearchProvider, SearchResult, register_search_provider


@register_search_provider("exa")
class ExaSearchProvider(BaseSearchProvider):
    DEFAULT_BASE_URL = "https://api.exa.ai"

    async def search(
        self,
        client: httpx.AsyncClient,
        query: str,
        *,
        max_results: int = 5,
    ) -> list[SearchResult]:
        if not self.config.api_key:
            raise ValueError(f"Exa search provider '{self.config.id}' missing api_key")

        base_url = (self.config.base_url or self.DEFAULT_BASE_URL).rstrip("/")
        payload = {
            "query": query,
            "type": "auto",
            "numResults": max_results,
            "contents": {
                "text": {"maxCharacters": 2500},
                "highlights": {"numSentences": 4},
            },
        }
        headers = {
            "x-api-key": self.config.api_key,
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
            highlights = item.get("highlights") or []
            snippet = " ".join(str(h).strip() for h in highlights if h).strip()
            text = str(item.get("text") or "").strip()
            if not snippet:
                snippet = text[:500]
            results.append(
                SearchResult(
                    title=str(item.get("title") or url).strip(),
                    url=url,
                    snippet=snippet,
                    content=text[:2500] if text else snippet,
                    published_date=item.get("publishedDate"),
                    provider=self.config.id,
                )
            )
        return results
