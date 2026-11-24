"""Generic OpenAI-compatible Chat Completions client (pure httpx)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ger.llm.base import (
    EXTRACTION_JSON_SCHEMA,
    BaseLLMClient,
    ExtractedEventBatch,
    build_extraction_system_prompt,
    build_extraction_user_prompt,
    parse_extracted_batch,
    register_llm_client,
)

logger = logging.getLogger(__name__)


@register_llm_client("openai_compat")
class OpenAICompatLLMClient(BaseLLMClient):
    DEFAULT_BASE_URL = "https://api.openai.com/v1"

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
        if not self.config.api_key:
            raise ValueError(f"LLM provider '{self.config.id}' missing api_key")

        base_url = (self.config.base_url or self.DEFAULT_BASE_URL).rstrip("/")
        url = f"{base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        messages = [
            {
                "role": "system",
                "content": build_extraction_system_prompt(
                    timezone_name, current_local_iso, region=region
                ),
            },
            {
                "role": "user",
                "content": build_extraction_user_prompt(
                    game=game,
                    context_text=context_text,
                    include_types=include_types,
                    extra_instructions=extra_instructions,
                    existing_events_text=existing_events_text,
                ),
            },
        ]

        # Try strict json_schema first; gracefully degrade to json_object / plain JSON
        # for OpenAI-compatible relays or models that do not support json_schema.
        response_formats: list[dict[str, Any] | None] = [
            {
                "type": "json_schema",
                "json_schema": {
                    "name": "game_event_extraction",
                    "strict": True,
                    "schema": EXTRACTION_JSON_SCHEMA,
                },
            },
            {"type": "json_object"},
            None,
        ]

        last_error: Exception | None = None
        for fmt in response_formats:
            payload: dict[str, Any] = {
                "model": self.config.model,
                "messages": messages,
                "temperature": self.config.temperature,
            }
            if fmt is not None:
                payload["response_format"] = fmt

            resp = await client.post(
                url,
                json=payload,
                headers=headers,
                timeout=self.config.timeout_seconds,
            )
            if resp.status_code == 400 and fmt is not None:
                logger.warning(
                    "LLM '%s' rejected response_format=%s (HTTP 400), falling back",
                    self.config.id,
                    fmt.get("type"),
                )
                last_error = httpx.HTTPStatusError(
                    f"HTTP 400 with response_format={fmt.get('type')}: {resp.text[:300]}",
                    request=resp.request,
                    response=resp,
                )
                continue

            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise ValueError(f"Unexpected non-string message content from LLM '{self.config.id}'")
            return parse_extracted_batch(content)

        assert last_error is not None
        raise last_error
