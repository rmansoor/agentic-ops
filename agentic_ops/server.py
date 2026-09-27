"""HTTP service: GitHub App webhook, IDE API, Slack events, health check."""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from .agent import CodebaseAgent
from .config import Settings
from .diff import file_as_diff, parse_diff
from .github_client import GitHubClient, installation_token, verify_signature
from .indexer import CodeIndex
from .llm import LLMClient, make_llm
from .pipeline import review_remote_pr
from .policy import Policy
from .quality_gate import run_gate
from .reviewer import review_diff
from .slack_app import build_slack_app

log = logging.getLogger("agentic_ops")
REVIEW_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}


class ReviewRequest(BaseModel):
    path: str | None = None
    content: str | None = None
    diff: str | None = None


class AskRequest(BaseModel):
    question: str


def create_app(settings: Settings | None = None, llm: LLMClient | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="Agentic Ops", version="0.1.0")

    def get_llm() -> LLMClient:
        return llm or make_llm(settings)

    def require_token(authorization: str | None) -> None:
        if settings.api_token and authorization != f"Bearer {settings.api_token}":
            raise HTTPException(status_code=401, detail="invalid API token")

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "provider": settings.llm_provider, "model": settings.llm_model}

    # ------------------------------------------------------------------ GitHub App webhook
    def _review_job(owner: str, repo: str, number: int, installation_id: int) -> None:
        try:
            token = installation_token(
                settings.github_app_id or "",
                settings.github_private_key or "",
                installation_id,
                settings.github_api_url,
            )
            review_remote_pr(get_llm(), GitHubClient(token, settings.github_api_url), owner, repo, number, settings)
        except Exception:
            log.exception("Review failed for %s/%s#%s", owner, repo, number)

    @app.post("/github/webhook")
    async def github_webhook(
        request: Request,
        background: BackgroundTasks,
        x_github_event: str = Header(default=""),
        x_hub_signature_256: str | None = Header(default=None),
    ) -> dict:
        body = await request.body()
        if not verify_signature(settings.github_webhook_secret or "", body, x_hub_signature_256):
            raise HTTPException(status_code=401, detail="bad signature")
        payload = json.loads(body or b"{}")
        if x_github_event == "ping":
            return {"ok": True, "pong": True}
        if x_github_event != "pull_request" or payload.get("action") not in REVIEW_ACTIONS:
            return {"ok": True, "skipped": True}
        pr = payload["pull_request"]
        if pr.get("draft"):
            return {"ok": True, "skipped": "draft"}
        owner, repo = payload["repository"]["full_name"].split("/", 1)
        background.add_task(_review_job, owner, repo, pr["number"], payload["installation"]["id"])
        return {"ok": True, "queued": f"{owner}/{repo}#{pr['number']}"}

    # ------------------------------------------------------------------ IDE API (VS Code extension)
    @app.post("/api/review")
    def api_review(req: ReviewRequest, authorization: str | None = Header(default=None)) -> dict:
        require_token(authorization)
        if req.diff:
            files = parse_diff(req.diff)
            repo_dir = Path(settings.repo_dir)
            live = [f for f in files if not f.is_deleted]
            gate = run_gate(
                repo_dir,
                [f.path for f in live],
                Policy.load(settings.policy_path),
                settings,
                added_lines={f.path: f.added_lines for f in live},
            )
        elif req.path and req.content is not None:
            files = [file_as_diff(req.path, req.content)]
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / req.path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(req.content)
                gate = run_gate(tmp, [req.path], Policy.load(settings.policy_path), settings)
        else:
            raise HTTPException(status_code=400, detail="send either `diff` or `path` + `content`")
        index = CodeIndex.load_or_build(settings.repo_dir)
        review = review_diff(get_llm(), files, gate, index, settings.min_severity, settings.max_comments)
        return {
            "summary": review.summary,
            "comments": [c.model_dump() for c in review.comments],
            "findings": [f.model_dump() for f in gate.findings],
            "blocked": gate.blocked,
            "tier": gate.tier,
        }

    @app.post("/api/ask")
    def api_ask(req: AskRequest, authorization: str | None = Header(default=None)) -> dict:
        require_token(authorization)
        result = CodebaseAgent(get_llm(), settings.repo_dir).ask(req.question)
        return {"answer": result.answer, "steps": result.steps}

    # ------------------------------------------------------------------ Slack
    slack = build_slack_app(settings, get_llm)
    if slack is not None:
        from slack_bolt.adapter.fastapi import SlackRequestHandler

        handler = SlackRequestHandler(slack)

        @app.post("/slack/events")
        async def slack_events(request: Request):
            return await handler.handle(request)

    return app
