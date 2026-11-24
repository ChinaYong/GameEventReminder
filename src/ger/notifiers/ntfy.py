"""ntfy notification provider (JSON payload for full UTF-8 support)."""

from __future__ import annotations

from typing import Any

import httpx

from ger.notifiers.base import BaseNotifier, NotificationMessage, register_notifier


@register_notifier("ntfy")
class NtfyNotifier(BaseNotifier):
    DEFAULT_SERVER = "https://ntfy.sh"

    async def send(self, client: httpx.AsyncClient, message: NotificationMessage) -> None:
        if not self.config.topic:
            raise ValueError(f"ntfy notifier '{self.config.id}' missing topic")

        server = (self.config.server or self.DEFAULT_SERVER).rstrip("/")
        payload: dict[str, Any] = {
            "topic": self.config.topic,
            "title": message.title,
            "message": message.body,
            "priority": message.priority if message.priority is not None else self.config.priority,
        }
        tags = message.tags or self.config.tags
        if tags:
            payload["tags"] = tags
        if message.url:
            payload["click"] = message.url

        headers: dict[str, str] = {"Content-Type": "application/json"}
        auth: httpx.BasicAuth | None = None
        if self.config.token:
            headers["Authorization"] = f"Bearer {self.config.token}"
        elif self.config.username and self.config.password:
            auth = httpx.BasicAuth(self.config.username, self.config.password)

        resp = await client.post(
            server,
            json=payload,
            headers=headers,
            auth=auth,
            timeout=20.0,
        )
        resp.raise_for_status()
