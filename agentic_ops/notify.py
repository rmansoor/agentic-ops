"""Slack notifications (no-op when Slack is not configured)."""

from __future__ import annotations

import logging

import httpx

from .config import Settings

log = logging.getLogger("agentic_ops")


def notify_slack(settings: Settings, text: str) -> bool:
    if not (settings.slack_bot_token and settings.slack_notify_channel):
        return False
    try:
        resp = httpx.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {settings.slack_bot_token}"},
            json={"channel": settings.slack_notify_channel, "text": text, "unfurl_links": False},
            timeout=15,
        )
        ok = resp.json().get("ok", False)
        if not ok:
            log.warning("Slack notify failed: %s", resp.text[:200])
        return ok
    except httpx.HTTPError as exc:
        log.warning("Slack notify error: %s", exc)
        return False
