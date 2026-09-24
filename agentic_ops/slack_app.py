"""Slack integration: /askcode, /reviewpr, and @mentions answered by the codebase agent."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

from .agent import CodebaseAgent
from .config import Settings
from .github_client import GitHubClient, installation_token
from .llm import LLMClient
from .pipeline import review_remote_pr

log = logging.getLogger("agentic_ops")
PR_URL = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)")


def build_slack_app(settings: Settings, llm_factory: Callable[[], LLMClient]):
    """Returns a slack_bolt App, or None if Slack is not configured / slack_bolt isn't installed."""
    if not (settings.slack_bot_token and settings.slack_signing_secret):
        return None
    try:
        from slack_bolt import App
    except ImportError:
        log.warning("slack_bolt not installed; pip install 'agentic-ops[slack]'")
        return None

    app = App(token=settings.slack_bot_token, signing_secret=settings.slack_signing_secret)

    def answer(question: str) -> str:
        result = CodebaseAgent(llm_factory(), settings.repo_dir).ask(question)
        trail = "\n".join(f"• `{s}`" for s in result.steps[:6])
        return f"{result.answer}\n\n_Steps:_\n{trail}" if trail else result.answer

    @app.command("/askcode")
    def ask_command(ack, respond, command):
        ack("Searching the codebase…")  # Slack needs an ack within 3s; work continues after.
        question = (command.get("text") or "").strip()
        if not question:
            respond("Usage: `/askcode where do we validate webhook signatures?`")
            return
        respond(answer(question))

    @app.command("/reviewpr")
    def review_command(ack, respond, command):
        ack("Reviewing…")
        m = PR_URL.search(command.get("text") or "")
        if not m:
            respond("Usage: `/reviewpr https://github.com/org/repo/pull/123`")
            return
        owner, repo, number = m.group(1), m.group(2), int(m.group(3))
        gh = _github_for(settings, owner, repo)
        if gh is None:
            respond("GitHub credentials are not configured on the server.")
            return
        result = review_remote_pr(llm_factory(), gh, owner, repo, number, settings, post=True)
        status = "blocked ❌" if result.gate.blocked else "passed ✅"
        respond(
            f"Gate {status} · tier `{result.gate.tier}` · {len(result.review.comments)} comment(s) posted.\n"
            f"{result.review.summary}"
        )

    @app.event("app_mention")
    def on_mention(event, say):
        question = re.sub(r"<@[^>]+>", "", event.get("text", "")).strip()
        say(
            text=answer(question) if question else "Ask me about the codebase.",
            thread_ts=event.get("thread_ts") or event["ts"],
        )

    return app


def _github_for(settings: Settings, owner: str, repo: str) -> GitHubClient | None:
    if settings.github_app_id and settings.github_private_key:
        import httpx

        from .github_client import app_jwt

        resp = httpx.get(
            f"{settings.github_api_url}/repos/{owner}/{repo}/installation",
            headers={
                "Authorization": f"Bearer {app_jwt(settings.github_app_id, settings.github_private_key)}",
                "Accept": "application/vnd.github+json",
            },
            timeout=30,
        )
        resp.raise_for_status()
        token = installation_token(
            settings.github_app_id, settings.github_private_key, resp.json()["id"], settings.github_api_url
        )
        return GitHubClient(token, settings.github_api_url)
    if settings.github_token:
        return GitHubClient(settings.github_token, settings.github_api_url)
    return None
