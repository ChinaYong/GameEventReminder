"""Bark notification provider (POST /push JSON API)."""

from __future__ import annotations

from typing import Any

import httpx

from ger.notifiers.base import BaseNotifier, NotificationMessage, register_notifier


@register_notifier("bark")
class BarkNotifier(BaseNotifier):
    DEFAULT_SERVER = "https://api.day.app"

    async def send(self, client: httpx.AsyncClient, message: NotificationMessage) -> None:
        if not self.config.device_key:
            raise ValueError(f"Bark notifier '{self.config.id}' missing device_key")

        server = (self.config.server or self.DEFAULT_SERVER).rstrip("/")
        payload: dict[str, Any] = {
            "device_key": self.config.device_key,
            "title": message.title,
            "body": message.body,
            "group": message.group or self.config.group or "GameEvents",
            "level": self.config.level or "active",
        }
        if self.config.sound:
            payload["sound"] = self.config.sound
        if self.config.icon:
            payload["icon"] = self.config.icon
        if message.url:
            payload["url"] = message.url

        resp = await client.post(
            f"{server}/push",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=20.0,
        )
        resp.raise_for_status()
