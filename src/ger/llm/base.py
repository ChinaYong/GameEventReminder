"""Base LLM client interface, structured event schema, and JSON parsing helpers."""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any, Callable

import httpx
from pydantic import BaseModel, Field

from ger.config import LLMProviderConfig
from ger.models import DEFAULT_ENABLED_EVENT_TYPES, EventType

_FENCE_PATTERN = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


class ExtractedEventItem(BaseModel):
    title: str
    event_type: str = EventType.INGAME_EVENT.value
    start_time: str | None = None
    end_time: str | None = None
    url: str | None = None
    notes: str | None = None
    needs_review: bool = False


class ExtractedEventBatch(BaseModel):
    events: list[ExtractedEventItem] = Field(default_factory=list)


EXTRACTION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "活动或节点名称，简洁明确"},
                    "event_type": {
                        "type": "string",
                        "enum": list(DEFAULT_ENABLED_EVENT_TYPES),
                        "description": (
                            "标准活动类别：ingame_event(游戏内道具活动) | web_event(官方网页道具活动) | "
                            "checkin_redeem(前瞻/限时兑换码与签到福利截止，不含直播预告本身) | banner(卡池更新) | version(大版本更新/维护)"
                        ),
                    },
                    "start_time": {
                        "type": ["string", "null"],
                        "description": "ISO-8601 开始时间，带时区偏移，如 2026-11-25T10:00:00+08:00；不确定则 null",
                    },
                    "end_time": {
                        "type": ["string", "null"],
                        "description": "ISO-8601 结束时间，带时区偏移，如 2026-12-08T03:59:00+08:00；单点活动或不确定则 null",
                    },
                    "url": {
                        "type": ["string", "null"],
                        "description": "信息来源链接",
                    },
                    "notes": {
                        "type": ["string", "null"],
                        "description": "补充说明（如活动奖励、兑换码失效说明或时间模糊原因）",
                    },
                    "needs_review": {
                        "type": "boolean",
                        "description": "如果时间是估算/模糊表述（如'版本更新后'）或来源不确定，设为 true",
                    },
                },
                "required": ["title", "event_type", "needs_review"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["events"],
    "additionalProperties": False,
}

# Gemini responseSchema uses OpenAPI 3.0 subset (nullable: true instead of type union list)
GEMINI_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "events": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "title": {"type": "STRING"},
                    "event_type": {
                        "type": "STRING",
                        "enum": list(DEFAULT_ENABLED_EVENT_TYPES),
                    },
                    "start_time": {"type": "STRING", "nullable": True},
                    "end_time": {"type": "STRING", "nullable": True},
                    "url": {"type": "STRING", "nullable": True},
                    "notes": {"type": "STRING", "nullable": True},
                    "needs_review": {"type": "BOOLEAN"},
                },
                "required": ["title", "event_type", "needs_review"],
            },
        }
    },
    "required": ["events"],
}


_REGION_LABELS = {
    "cn": "国服（中国大陆服，北京时间 UTC+8）",
    "global": "国际服（Global）",
    "jp": "日服（JP，UTC+9）",
    "tw": "台服/港澳台服（TW，UTC+8）",
    "kr": "韩服（KR，UTC+9）",
}


def format_region_label(region: str) -> str:
    return _REGION_LABELS.get(region.lower().strip(), region)


def build_extraction_system_prompt(
    timezone_name: str,
    current_local_iso: str,
    region: str = "cn",
) -> str:
    region_desc = format_region_label(region)
    return (
        "你是一个严谨的游戏活动排期信息提取器。\n"
        f"当前准确本地时间：{current_local_iso}（时区：{timezone_name}）。\n"
        f"目标服务器区服：{region_desc}。\n"
        "标准活动分类定义（event_type 必须严格从以下 5 类中选择）：\n"
        "- ingame_event：游戏客户端内参与、可稳定获取抽卡资源/道具/限定外观/材料的限时活动。\n"
        "- web_event：绑定游戏账号即可稳定领取游戏内道具的官方网页/H5 活动。严禁收录微博/B站/TapTap 转发抽奖、二创征集、晒图评奖、线下展会或周边贩售等纯营销抽奖活动。\n"
        "- checkin_redeem：限时登录签到、福利领取截止、以及**已公布的前瞻/官方限时兑换码**（必须在 notes 中列出具体兑换码字符串并给出失效截止时间 end_time）。**严禁收录单纯的“前瞻直播播出时间预告”**，只收录直播后已公布的具体兑换码与领取截止时间。\n"
        "- banner：限定角色、武器或装备 UP 卡池的开启与结束。\n"
        "- version：大版本更新上线与停服维护。\n"
        "提取规则：\n"
        "1. 严格仅依据提供的实时资料提取活动，禁止凭记忆编造日期或活动。\n"
        f"2. 默认仅提取 {region_desc} 的活动排期，忽略其他区服（除非目标区服本身即为该区服）。\n"
        "3. 所有时间必须输出为标准 ISO-8601 格式且带明确时区偏移（例如 2026-11-25T10:00:00+08:00）。\n"
        "4. 如果原文仅给出日期未给出具体时分，可将开始时间补为 00:00:00 或 10:00:00、结束时间补为 23:59:00，同时将 needs_review 设为 true 并在 notes 中注明。\n"
        "5. 如果开始和结束时间都无法从资料中确定，则丢弃该条，不要输出。\n"
        f"6. 严格忽略已经结束的活动（即结束时间早于 {current_local_iso} 的历史活动），仅输出当前正在进行中或即将开始的活动。\n"
        "7. 若用户提示中提供了系统已有活动列表，遇到同一活动时必须沿用完全一致的 title 以便系统去重更新，切勿为同一活动更换不同标题。\n"
        "8. 仅输出合法 JSON 对象，格式为 {\"events\": [...]}。"
    )


def build_extraction_user_prompt(
    *,
    game: str,
    context_text: str,
    include_types: list[str] | None = None,
    extra_instructions: str = "",
    existing_events_text: str = "",
) -> str:
    parts = [f"目标游戏：{game}"]
    if include_types:
        parts.append(f"仅提取以下活动类别：{', '.join(include_types)}")
    if extra_instructions:
        parts.append(f"补充筛选要求：{extra_instructions}")
    if existing_events_text:
        parts.append(
            "\n系统中已记录的该游戏未结束活动（同一活动请严格复用下方已有 title，不要重复造新名字）：\n"
            + existing_events_text
        )
    parts.append("\n以下是实时收集到的网页/搜索参考资料：\n")
    parts.append(context_text)
    return "\n".join(parts)


def parse_extracted_batch(raw_text: str) -> ExtractedEventBatch:
    """Parse LLM JSON output defensively (handles markdown fences or surrounding prose)."""
    text = raw_text.strip()
    fence = _FENCE_PATTERN.search(text)
    if fence:
        text = fence.group(1).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(f"LLM did not return valid JSON: {raw_text[:200]!r}")
        data = json.loads(text[start : end + 1])

    if isinstance(data, list):
        data = {"events": data}
    return ExtractedEventBatch.model_validate(data)


class BaseLLMClient(ABC):
    def __init__(self, config: LLMProviderConfig) -> None:
        self.config = config

    @property
    def supports_native_search(self) -> bool:
        return False

    @abstractmethod
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
        """Extract structured game events from collected text/search context."""

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
        """Use native search grounding (if supported by provider) to gather and extract events."""
        raise NotImplementedError(
            f"LLM provider '{self.config.id}' ({self.config.type}) does not support native search"
        )


LLM_REGISTRY: dict[str, type[BaseLLMClient]] = {}


def register_llm_client(name: str) -> Callable[[type[BaseLLMClient]], type[BaseLLMClient]]:
    def decorator(cls: type[BaseLLMClient]) -> type[BaseLLMClient]:
        LLM_REGISTRY[name] = cls
        return cls

    return decorator


def create_llm_client(config: LLMProviderConfig) -> BaseLLMClient:
    cls = LLM_REGISTRY.get(config.type)
    if cls is None:
        raise ValueError(f"Unsupported LLM provider type: {config.type}")
    return cls(config)
