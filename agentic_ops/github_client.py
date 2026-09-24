"""GitHub REST client: App auth, PR diff, reviews, check runs, webhook signatures."""

from __future__ import annotations

import hashlib
import hmac
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
import jwt

from .models import Finding, ReviewComment
from .reviewer import format_comment

API_VERSION = "2022-11-28"


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    if not secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def app_jwt(app_id: str, private_key_pem: str) -> str:
    now = int(time.time())
    return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(app_id)}, private_key_pem, algorithm="RS256")


def installation_token(app_id: str, private_key_pem: str, installation_id: int, api_url: str) -> str:
    resp = httpx.post(
        f"{api_url}/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {app_jwt(app_id, private_key_pem)}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["token"]


class GitHubClient:
    def __init__(self, token: str, api_url: str = "https://api.github.com"):
        self.token = token
        self.api_url = api_url.rstrip("/")
        self._http = httpx.Client(
            base_url=self.api_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
            },
            timeout=60,
        )

    def _req(self, method: str, url: str, **kw: Any) -> httpx.Response:
        resp = self._http.request(method, url, **kw)
        resp.raise_for_status()
        return resp

    # ---- pull requests
    def get_pr(self, owner: str, repo: str, number: int) -> dict:
        return self._req("GET", f"/repos/{owner}/{repo}/pulls/{number}").json()

    def get_pr_diff(self, owner: str, repo: str, number: int) -> str:
        return self._req(
            "GET", f"/repos/{owner}/{repo}/pulls/{number}", headers={"Accept": "application/vnd.github.v3.diff"}
        ).text

    def create_review(
        self,
        owner: str,
        repo: str,
        number: int,
        commit_id: str,
        body: str,
        comments: list[ReviewComment],
        event: str = "COMMENT",
    ) -> dict:
        payload = {
            "commit_id": commit_id,
            "body": body,
            "event": event,
            "comments": [
                {"path": c.path, "line": c.line, "side": "RIGHT", "body": format_comment(c)} for c in comments
            ],
        }
        try:
            return self._req("POST", f"/repos/{owner}/{repo}/pulls/{number}/reviews", json=payload).json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 422 or not comments:
                raise
            # A line could not be anchored (e.g. diff moved on). Fall back to a single body-only review.
            fallback = (
                body
                + "\n\n### Comments\n"
                + "\n\n".join(f"**`{c.path}:{c.line}`**\n{format_comment(c)}" for c in comments)
            )
            payload.update(body=fallback, comments=[])
            return self._req("POST", f"/repos/{owner}/{repo}/pulls/{number}/reviews", json=payload).json()

    def create_check_run(
        self,
        owner: str,
        repo: str,
        head_sha: str,
        name: str,
        conclusion: str,
        title: str,
        summary: str,
        findings: list[Finding] | None = None,
    ) -> dict:
        level = {"error": "failure", "warning": "warning", "info": "notice"}
        annotations = [
            {
                "path": f.path,
                "start_line": max(f.line, 1),
                "end_line": max(f.line, 1),
                "annotation_level": level[f.severity],
                "title": f"{f.tool}:{f.rule}",
                "message": f.message[:1000] or f.rule,
            }
            for f in (findings or [])
            if f.path
        ][:50]  # API limit per request
        payload = {
            "name": name,
            "head_sha": head_sha,
            "status": "completed",
            "conclusion": conclusion,
            "output": {"title": title, "summary": summary[:65_000], "annotations": annotations},
        }
        return self._req("POST", f"/repos/{owner}/{repo}/check-runs", json=payload).json()

    def comment(self, owner: str, repo: str, number: int, body: str) -> dict:
        return self._req("POST", f"/repos/{owner}/{repo}/issues/{number}/comments", json={"body": body}).json()

    # ---- metrics helpers
    def paginate(self, url: str, params: dict | None = None, max_pages: int = 20) -> list[dict]:
        items: list[dict] = []
        params = {"per_page": 100, **(params or {})}
        for page in range(1, max_pages + 1):
            batch = self._req("GET", url, params={**params, "page": page}).json()
            if not batch:
                break
            items.extend(batch)
            if len(batch) < params["per_page"]:
                break
        return items


def checkout_pr(owner: str, repo: str, number: int, token: str, dest: Path, host: str = "github.com") -> Path:
    """Shallow-fetch the PR head into dest so scanners can run on real files."""
    dest.mkdir(parents=True, exist_ok=True)
    url = f"https://x-access-token:{token}@{host}/{owner}/{repo}.git"
    for cmd in (
        ["git", "init", "-q"],
        ["git", "remote", "add", "origin", url],
        ["git", "fetch", "-q", "--depth", "1", "origin", f"pull/{number}/head"],
        ["git", "checkout", "-q", "FETCH_HEAD"],
    ):
        subprocess.run(cmd, cwd=dest, check=True, capture_output=True)
    return dest
