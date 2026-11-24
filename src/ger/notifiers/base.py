"""Base notifier interface and registry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable

import httpx
from pydantic import BaseModel, Field

from ger.config import NotifierConfig


class NotificationMessage(BaseModel):
    title: str
    body: str
    url: str | None = None
    priority: int | None = None  # 1 (min) .. 5 (max)
    tags: list[str] = Field(default_factory=list)
    group: str | None = None


class BaseNotifier(ABC):
    def __init__(self, config: NotifierConfig) -> None:
        self.config = config

    @property
    def notifier_id(self) -> str:
        return self.config.id

    @abstractmethod
    async def send(self, client: httpx.AsyncClient, message: NotificationMessage) -> None:
        """Deliver a notification."""


NOTIFIER_REGISTRY: dict[str, type[BaseNotifier]] = {}


def register_notifier(name: str) -> Callable[[type[BaseNotifier]], type[BaseNotifier]]:
    def decorator(cls: type[BaseNotifier]) -> type[BaseNotifier]:
        NOTIFIER_REGISTRY[name] = cls
        return cls

    return decorator


def create_notifier(config: NotifierConfig) -> BaseNotifier:
    cls = NOTIFIER_REGISTRY.get(config.type)
    if cls is None:
        raise ValueError(f"Unsupported notifier type: {config.type}")
    return cls(config)
