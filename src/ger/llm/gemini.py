"""Google Gemini REST API client with native Google Search Grounding (two-pass pipeline)."""

from __future__ import annotations

from typing import Any

import httpx

from ger.llm.base import (
    GEMINI_RESPONSE_SCHEMA,
    BaseLLMClient,
    ExtractedEventBatch,
    build_extraction_system_prompt,
    build_extraction_user_prompt,
    format_region_label,
    parse_extracted_batch,
    register_llm_client,
)


@register_llm_client("gemini")
class GeminiLLMClient(BaseLLMClient):
    DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

    @property
    def supports_native_search(self) -> bool:
        return bool(self.config.use_google_search)

    def _endpoint(self) -> str:
        base_url = (self.config.base_url or self.DEFAULT_BASE_URL).rstrip("/")
        model = self.config.model
        if not model.startswith("models/"):
            model = f"models/{model}"
        return f"{base_url}/{model}:generateContent"

    def _headers(self) -> dict[str, str]:
        if not self.config.api_key:
            raise ValueError(f"Gemini LLM provider '{self.config.id}' missing api_key")
        return {
            "x-goog-api-key": self.config.api_key,
            "Content-Type": "application/json",
        }

    @staticmethod
    def _extract_candidate_text(data: dict[str, Any]) -> str:
        candidates = data.get("candidates") or []
        if not candidates:
            raise ValueError(f"Gemini returned no candidates: {data}")
        parts = candidates[0].get("content", {}).get("parts") or []
        texts = [str(p["text"]) for p in parts if "text" in p]
        return "\n".join(texts).strip()

    @staticmethod
    def _extract_grounding_sources(data: dict[str, Any]) -> list[str]:
        candidates = data.get("candidates") or []
        if not candidates:
            return []
        meta = candidates[0].get("groundingMetadata") or {}
        chunks = meta.get("groundingChunks") or []
        lines: list[str] = []
        seen: set[str] = set()
        for chunk in chunks:
            web = chunk.get("web") or {}
            uri = str(web.get("uri") or "").strip()
            title = str(web.get("title") or "").strip()
            if uri and uri not in seen:
                seen.add(uri)
                lines.append(f"- {title}: {uri}" if title else f"- {uri}")
        return lines

    async def extract_events_from_context(
        self,
        client: httpx.AsyncClient,
        *,
        game: str,
        context_text: str,
        timezone_name: str,
        current_local_iso: str,
        region: str = "cn",
        include_types: list[str] | None = None,
        extra_instructions: str = "",
        existing_events_text: str = "",
    ) -> ExtractedEventBatch:
        system_text = build_extraction_system_prompt(
            timezone_name, current_local_iso, region=region
        )
        user_text = build_extraction_user_prompt(
            game=game,
            context_text=context_text,
            include_types=include_types,
            extra_instructions=extra_instructions,
            existing_events_text=existing_events_text,
        )
        payload = {
            "systemInstruction": {"parts": [{"text": system_text}]},
            "contents": [{"role": "user", "parts": [{"text": user_text}]}],
            "generationConfig": {
                "temperature": self.config.temperature,
                "responseMimeType": "application/json",
                "responseSchema": GEMINI_RESPONSE_SCHEMA,
            },
        }
        resp = await client.post(
            self._endpoint(),
            json=payload,
            headers=self._headers(),
            timeout=self.config.timeout_seconds,
        )
        resp.raise_for_status()
        text = self._extract_candidate_text(resp.json())
        return parse_extracted_batch(text)

    async def search_and_extract_events(
        self,
        client: httpx.AsyncClient,
        *,
        game: str,
        queries: list[str],
        timezone_name: str,
        current_local_iso: str,
        region: str = "cn",
        include_types: list[str] | None = None,
        extra_instructions: str = "",
        existing_events_text: str = "",
    ) -> ExtractedEventBatch:
        """Two-pass pipeline:
        Pass 1: Grounded web retrieval via tools=[{"google_search": {}}] (plain text).
        Pass 2: Strict JSON Schema structuring from grounded facts + citation URLs.
        """
        region_desc = format_region_label(region)
        query_block = (
            "\n".join(f"- {q}" for q in queries)
            if queries
            else f"- {game} {region_desc} 最新活动时间公告"
        )
        type_hint = f"关注类别：{', '.join(include_types)}。" if include_types else ""
        research_prompt = (
            f"当前时间：{current_local_iso}（{timezone_name}）。\n"
            f"请使用 Google 搜索查询游戏《{game}》{region_desc}当前正在进行及即将开始的官方活动排期：\n"
            "包括游戏内限时道具活动、官方网页/H5道具活动（排除纯转发抽奖营销）、已公布的前瞻/限时兑换码（列出具体兑换码与过期截止时间，不要收录前瞻直播播出预告本身）、限时签到福利、卡池更新及大版本更新。\n"
            f"注意：仅关注 {region_desc}，严格忽略已结束的历史活动与其他区服排期。\n"
            f"参考检索关键词：\n{query_block}\n"
            f"{type_hint}\n"
            f"{extra_instructions}\n"
            "请详细列出每个活动的名称、类型、确切开始时间与结束时间（注明时区）、简要说明以及来源网址。"
        )

        # Pass 1: Search Grounding
        pass1_payload = {
            "contents": [{"role": "user", "parts": [{"text": research_prompt}]}],
            "tools": [{"google_search": {}}],
            "generationConfig": {"temperature": 0.1},
        }
        resp1 = await client.post(
            self._endpoint(),
            json=pass1_payload,
            headers=self._headers(),
            timeout=self.config.timeout_seconds,
        )
        resp1.raise_for_status()
        data1 = resp1.json()
        grounded_text = self._extract_candidate_text(data1)
        citations = self._extract_grounding_sources(data1)

        if citations:
            grounded_text = f"{grounded_text}\n\n[Google Search 引用来源]\n" + "\n".join(citations)

        # Pass 2: Strict Structured Extraction
        return await self.extract_events_from_context(
            client,
            game=game,
            context_text=grounded_text,
            timezone_name=timezone_name,
            current_local_iso=current_local_iso,
            region=region,
            include_types=include_types,
            extra_instructions=extra_instructions,
            existing_events_text=existing_events_text,
        )
