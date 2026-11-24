"""Web-grounded AI duplicate reviewer for gray-zone candidate events."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx
from pydantic import BaseModel

from ger.config import AppConfig
from ger.llm import create_llm_client
from ger.llm.gemini import GeminiLLMClient
from ger.llm.openai_compat import OpenAICompatLLMClient
from ger.models import EventRecord, RawEvent
from ger.search import SearchResult, create_search_provider
from ger.time_utils import format_local

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)

DEDUP_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_duplicate": {"type": "boolean"},
        "matched_event_id": {"type": ["integer", "null"]},
        "reason": {"type": "string"},
    },
    "required": ["is_duplicate", "matched_event_id", "reason"],
    "additionalProperties": False,
}

GEMINI_DEDUP_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "is_duplicate": {"type": "BOOLEAN"},
        "matched_event_id": {"type": "INTEGER", "nullable": True},
        "reason": {"type": "STRING"},
    },
    "required": ["is_duplicate", "matched_event_id", "reason"],
}


class DedupVerdict(BaseModel):
    is_duplicate: bool = False
    matched_event_id: int | None = None
    reason: str = ""


def _parse_verdict(raw_text: str) -> DedupVerdict:
    text = raw_text.strip()
    fence = _FENCE_RE.search(text)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError(f"Dedup reviewer returned non-JSON: {raw_text[:200]!r}")
        data = json.loads(text[start : end + 1])
    return DedupVerdict.model_validate(data)


def _format_candidate_prompt(
    raw: RawEvent,
    candidates: list[EventRecord],
    tz_name: str,
    web_context: str = "",
) -> str:
    cand_lines = []
    for c in candidates:
        cand_lines.append(
            f"- ID={c.id} | 类型={c.event_type} | 标题={c.title} | "
            f"开始={format_local(c.start_time, tz_name)} | 结束={format_local(c.end_time, tz_name)}"
        )
    parts = [
        f"游戏：{raw.game}（国服）",
        "【待入库新事件】",
        f"类型={raw.event_type} | 标题={raw.title} | "
        f"开始={format_local(raw.start_time, tz_name)} | 结束={format_local(raw.end_time, tz_name)}",
        "\n【数据库中的疑似重复候选事件】",
        "\n".join(cand_lines),
    ]
    if web_context:
        parts.append("\n【联网核实参考资料】\n" + web_context)
    parts.append(
        "\n请判断【待入库新事件】与上方候选中的某一条是否实质上是**同一个游戏活动**（例如：卡池编号与具体角色名祈愿、活动简称与官方全称、同一前瞻直播的不同表述）。\n"
        "如果只是同时段开启的两个不同活动，请判定 is_duplicate=false。\n"
        '仅输出 JSON：{"is_duplicate": true/false, "matched_event_id": 候选ID或null, "reason": "简短依据"}。'
    )
    return "\n".join(parts)


async def review_gray_zone_duplicate(
    client: httpx.AsyncClient,
    config: AppConfig,
    raw: RawEvent,
    candidates: list[EventRecord],
) -> DedupVerdict | None:
    """Use web-grounded AI to decide whether `raw` duplicates one of `candidates`.

    Requires web search capability: either Gemini with `use_google_search=true`,
    or `dedup.search_providers` (Tavily/Exa) paired with an OpenAI-compatible/Gemini model.
    """
    dedup_cfg = config.dedup
    if not dedup_cfg.ai_review_enabled or not dedup_cfg.llm_provider or not candidates:
        return None

    llm_cfg = config.get_llm_provider(dedup_cfg.llm_provider)
    llm_client = create_llm_client(llm_cfg)
    system_prompt = (
        "你是游戏活动去重审查专家。你必须依据联网搜索到的客观事实判断两个游戏活动条目是否为同一活动的不同称呼，"
        "避免重复推送通知，也不要把同时段的不同独立活动错误合并。"
    )

    valid_ids = {c.id for c in candidates}

    try:
        # Path A: Gemini native Google Search (two-pass grounded review)
        if isinstance(llm_client, GeminiLLMClient) and llm_client.supports_native_search and not dedup_cfg.search_providers:
            prompt_pass1 = _format_candidate_prompt(raw, candidates, config.timezone)
            pass1_payload = {
                "contents": [{"role": "user", "parts": [{"text": prompt_pass1}]}],
                "tools": [{"google_search": {}}],
                "generationConfig": {"temperature": 0.1},
            }
            resp1 = await client.post(
                llm_client._endpoint(),
                json=pass1_payload,
                headers=llm_client._headers(),
                timeout=llm_cfg.timeout_seconds,
            )
            resp1.raise_for_status()
            grounded_notes = llm_client._extract_candidate_text(resp1.json())

            pass2_prompt = _format_candidate_prompt(
                raw, candidates, config.timezone, web_context=grounded_notes
            )
            pass2_payload = {
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "contents": [{"role": "user", "parts": [{"text": pass2_prompt}]}],
                "generationConfig": {
                    "temperature": 0.0,
                    "responseMimeType": "application/json",
                    "responseSchema": GEMINI_DEDUP_SCHEMA,
                },
            }
            resp2 = await client.post(
                llm_client._endpoint(),
                json=pass2_payload,
                headers=llm_client._headers(),
                timeout=llm_cfg.timeout_seconds,
            )
            resp2.raise_for_status()
            verdict = _parse_verdict(llm_client._extract_candidate_text(resp2.json()))
        else:
            # Path B: External web search (Tavily/Exa) + OpenAI-compat / Gemini verdict
            if not dedup_cfg.search_providers:
                logger.warning(
                    "dedup.ai_review_enabled is true but neither Gemini google_search nor dedup.search_providers is configured; skipping AI dedup review"
                )
                return None

            cand_titles = " ".join(c.title for c in candidates[:3])
            query = f"{raw.game} 国服 {raw.title} {cand_titles}".strip()[:200]
            results: list[SearchResult] = []
            seen_urls: set[str] = set()
            for sp_id in dedup_cfg.search_providers:
                provider = create_search_provider(config.get_search_provider(sp_id))
                try:
                    found = await provider.search(
                        client, query, max_results=dedup_cfg.max_search_results
                    )
                    for item in found:
                        if item.url not in seen_urls:
                            seen_urls.add(item.url)
                            results.append(item)
                except Exception as exc:
                    logger.warning("Dedup search provider '%s' failed: %s", sp_id, exc)

            web_context = "\n---\n".join(
                f"标题: {r.title}\n链接: {r.url}\n摘要: {r.snippet or r.content[:600]}"
                for r in results
            )
            user_prompt = _format_candidate_prompt(
                raw, candidates, config.timezone, web_context=web_context
            )

            if isinstance(llm_client, OpenAICompatLLMClient):
                base_url = (llm_cfg.base_url or llm_client.DEFAULT_BASE_URL).rstrip("/")
                headers = {
                    "Authorization": f"Bearer {llm_cfg.api_key}",
                    "Content-Type": "application/json",
                }
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ]
                verdict = None
                for fmt in (
                    {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "dedup_verdict",
                            "strict": True,
                            "schema": DEDUP_JSON_SCHEMA,
                        },
                    },
                    {"type": "json_object"},
                    None,
                ):
                    payload: dict[str, Any] = {
                        "model": llm_cfg.model,
                        "messages": messages,
                        "temperature": 0.0,
                    }
                    if fmt is not None:
                        payload["response_format"] = fmt
                    resp = await client.post(
                        f"{base_url}/chat/completions",
                        json=payload,
                        headers=headers,
                        timeout=llm_cfg.timeout_seconds,
                    )
                    if resp.status_code == 400 and fmt is not None:
                        continue
                    resp.raise_for_status()
                    content = resp.json()["choices"][0]["message"]["content"]
                    verdict = _parse_verdict(content)
                    break
                if verdict is None:
                    return None
            elif isinstance(llm_client, GeminiLLMClient):
                payload = {
                    "systemInstruction": {"parts": [{"text": system_prompt}]},
                    "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
                    "generationConfig": {
                        "temperature": 0.0,
                        "responseMimeType": "application/json",
                        "responseSchema": GEMINI_DEDUP_SCHEMA,
                    },
                }
                resp = await client.post(
                    llm_client._endpoint(),
                    json=payload,
                    headers=llm_client._headers(),
                    timeout=llm_cfg.timeout_seconds,
                )
                resp.raise_for_status()
                verdict = _parse_verdict(llm_client._extract_candidate_text(resp.json()))
            else:
                return None

        if verdict.is_duplicate and verdict.matched_event_id not in valid_ids:
            logger.warning(
                "AI dedup reviewer returned invalid matched_event_id=%s (valid=%s); ignoring",
                verdict.matched_event_id,
                valid_ids,
            )
            return None

        logger.info(
            "AI dedup verdict for [%s] '%s': is_duplicate=%s matched_id=%s (%s)",
            raw.game,
            raw.title,
            verdict.is_duplicate,
            verdict.matched_event_id,
            verdict.reason,
        )
        return verdict
    except Exception as exc:
        logger.warning("AI dedup review failed for '%s': %s", raw.title, exc)
        return None
