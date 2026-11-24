"""Configuration schema and YAML loader with ${ENV_VAR} expansion."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from ger.models import DEFAULT_ENABLED_EVENT_TYPES, EventType, normalize_event_type
from ger.time_utils import parse_offset

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")


def _parse_dotenv_file(dotenv_path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE .env file without external dependencies."""
    parsed: dict[str, str] = {}
    if not dotenv_path.is_file():
        return parsed
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if not key:
            continue
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        parsed[key] = val
    return parsed


def _load_dotenv_files(config_path: Path) -> dict[str, str]:
    """Load .env from cwd and config directory (process os.environ takes highest priority)."""
    merged: dict[str, str] = {}
    candidates = [Path.cwd() / ".env", config_path.resolve().parent / ".env"]
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        merged.update(_parse_dotenv_file(resolved))
    merged.update(os.environ)
    return merged


def _expand_env(value: Any, env: dict[str, str]) -> Any:
    if isinstance(value, str):
        def _repl(match: re.Match[str]) -> str:
            var_name = match.group(1)
            default_val = match.group(2)
            env_val = env.get(var_name)
            # Match POSIX ${VAR:-default}: fall back when unset or empty string
            if env_val:
                return env_val
            if default_val is not None:
                return default_val
            return ""

        return _ENV_PATTERN.sub(_repl, value)
    if isinstance(value, list):
        return [_expand_env(item, env) for item in value]
    if isinstance(value, dict):
        return {k: _expand_env(v, env) for k, v in value.items()}
    return value


class ReminderRuleConfig(BaseModel):
    start_offsets: list[str] = Field(default_factory=lambda: ["-24h"])
    end_offsets: list[str] = Field(default_factory=lambda: ["-24h"])
    grace_period_minutes: int = 120

    @field_validator("start_offsets", "end_offsets")
    @classmethod
    def _validate_offsets(cls, values: list[str]) -> list[str]:
        for item in values:
            parse_offset(item)
        return values


class NotificationConfig(BaseModel):
    """Master notification switch and daily digest schedule."""

    enabled: bool = True
    mode: Literal["digest", "realtime"] = "digest"
    digest_cron: str = "15 12 * * *"  # Default: send 1 consolidated digest daily at 12:15
    digest_lookback_hours: int = 26  # Capture a full 24h cycle (+2h buffer) into the queue


class SchedulerConfig(BaseModel):
    sync_cron: str = "0 8 * * *"
    dispatch_interval_seconds: int = 60
    notify_on_sync_issues: bool = True


class ApiConfig(BaseModel):
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8765
    auth_token: str = ""


class DedupConfig(BaseModel):
    """Configuration for rule-based + optional web-grounded AI duplicate review."""

    auto_merge_similarity: float = 0.80
    gray_zone_min_similarity: float = 0.25
    ai_review_enabled: bool = False
    llm_provider: str = ""
    search_providers: list[str] = Field(default_factory=list)
    max_search_results: int = 3


class LLMProviderConfig(BaseModel):
    id: str
    type: Literal["openai_compat", "gemini"]
    api_key: str = ""
    base_url: str = ""
    model: str
    timeout_seconds: float = 90.0
    temperature: float = 0.1
    use_google_search: bool = False  # Gemini native Search Grounding


class SearchProviderConfig(BaseModel):
    id: str
    type: Literal["tavily", "exa"]
    api_key: str = ""
    base_url: str = ""
    timeout_seconds: float = 30.0


class NotifierConfig(BaseModel):
    id: str
    type: Literal["ntfy", "bark"]
    enabled: bool = True
    # ntfy fields
    server: str = ""
    topic: str = ""
    token: str = ""
    username: str = ""
    password: str = ""
    priority: int = 3
    tags: list[str] = Field(default_factory=lambda: ["calendar"])
    # bark fields
    device_key: str = ""
    group: str = "GameEvents"
    sound: str = ""
    icon: str = ""
    level: str = "active"  # active | timeSensitive | passive | critical


class SourceConfig(BaseModel):
    id: str
    type: str
    enabled: bool = True
    options: dict[str, Any] = Field(default_factory=dict)


def _default_type_reminders() -> dict[str, ReminderRuleConfig]:
    return {
        EventType.CHECKIN_REDEEM.value: ReminderRuleConfig(
            start_offsets=["0m"],
            end_offsets=["-24h"],
            grace_period_minutes=120,
        ),
        EventType.VERSION.value: ReminderRuleConfig(
            start_offsets=["-24h"],
            end_offsets=[],
            grace_period_minutes=120,
        ),
    }


class AppConfig(BaseModel):
    timezone: str = "Asia/Shanghai"
    default_region: str = "cn"
    watched_games: list[str] = Field(default_factory=list)  # Master switch of canonical game names; empty = unrestricted
    ignore_ended_events: bool = True
    cleanup_ended_after_days: int = 3  # -1 disables automatic cleanup; 0 cleans immediately after end
    keep_sync_runs: int = 30
    enabled_event_types: list[str] = Field(default_factory=lambda: list(DEFAULT_ENABLED_EVENT_TYPES))
    db_path: str = "./data/ger.db"
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    dedup: DedupConfig = Field(default_factory=DedupConfig)
    reminders: ReminderRuleConfig = Field(default_factory=ReminderRuleConfig)
    type_reminders: dict[str, ReminderRuleConfig] = Field(default_factory=_default_type_reminders)
    game_reminders: dict[str, ReminderRuleConfig] = Field(default_factory=dict)
    llm_providers: list[LLMProviderConfig] = Field(default_factory=list)
    search_providers: list[SearchProviderConfig] = Field(default_factory=list)
    notifiers: list[NotifierConfig] = Field(default_factory=list)
    sources: list[SourceConfig] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _coerce_null_sections(cls, data: Any) -> Any:
        """YAML keys whose children are all commented out parse as None; drop them to use defaults."""
        if not isinstance(data, dict):
            return data
        nullable_sections = (
            "watched_games",
            "notifications",
            "scheduler",
            "api",
            "dedup",
            "reminders",
            "type_reminders",
            "game_reminders",
            "enabled_event_types",
            "llm_providers",
            "search_providers",
            "notifiers",
            "sources",
        )
        cleaned = dict(data)
        for key in nullable_sections:
            if key in cleaned and cleaned[key] is None:
                del cleaned[key]
        return cleaned

    @field_validator("watched_games")
    @classmethod
    def _clean_watched_games(cls, values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for v in values:
            name = str(v).strip()
            if name and name not in seen:
                seen.add(name)
                result.append(name)
        return result

    def is_game_watched(self, canonical_game: str) -> bool:
        """Return True if `canonical_game` is enabled by the master switch (or switch is empty)."""
        if not self.watched_games:
            return True
        return canonical_game.strip() in self.watched_games

    @field_validator("enabled_event_types")
    @classmethod
    def _normalize_enabled_types(cls, values: list[str]) -> list[str]:
        return [normalize_event_type(v) for v in values if str(v).strip()]

    def get_reminder_rule(self, game: str, event_type: str | None = None) -> ReminderRuleConfig:
        """Priority: game-specific rule > event_type-specific rule > global default."""
        if game in self.game_reminders:
            return self.game_reminders[game]
        if event_type:
            norm = normalize_event_type(event_type)
            if norm in self.type_reminders:
                return self.type_reminders[norm]
        return self.reminders

    def get_llm_provider(self, provider_id: str) -> LLMProviderConfig:
        for provider in self.llm_providers:
            if provider.id == provider_id:
                return provider
        raise KeyError(f"LLM provider '{provider_id}' not found in config")

    def get_search_provider(self, provider_id: str) -> SearchProviderConfig:
        for provider in self.search_providers:
            if provider.id == provider_id:
                return provider
        raise KeyError(f"Search provider '{provider_id}' not found in config")


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    env = _load_dotenv_files(config_path)
    expanded = _expand_env(raw, env)
    return AppConfig.model_validate(expanded)
