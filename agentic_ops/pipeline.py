"""End-to-end orchestration shared by the webhook server, CI mode, CLI and Slack."""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .diff import FileDiff, parse_diff
from .github_client import GitHubClient, checkout_pr
from .indexer import CodeIndex
from .llm import LLMClient
from .models import GateResult, ReviewResult
from .notify import notify_slack
from .policy import Policy
from .quality_gate import run_gate
from .reviewer import format_summary, review_diff

log = logging.getLogger("agentic_ops")

CHECK_NAME = "agentic-ops / quality-gate"


@dataclass
class PipelineResult:
    gate: GateResult
    review: ReviewResult
    files: list[FileDiff]

    @property
    def markdown(self) -> str:
        return format_summary(self.review, self.gate)


def run_pipeline(
    llm: LLMClient,
    diff_text: str,
    repo_dir: str | Path,
    settings: Settings,
    sarif_files: list[str] | None = None,
    use_llm: bool = True,
) -> PipelineResult:
    files = parse_diff(diff_text)
    changed = [f.path for f in files if not f.is_deleted]
    # The repo's own policy.yml wins; then the service's; then built-in defaults.
    repo_policy = Path(repo_dir) / settings.policy_path
    policy = Policy.load(repo_policy if repo_policy.is_file() else settings.policy_path)
    added = {f.path: f.added_lines for f in files if not f.is_deleted}
    gate = run_gate(repo_dir, changed, policy, settings, sarif_files, added)
    if use_llm:
        index = CodeIndex.load_or_build(repo_dir)
        review = review_diff(llm, files, gate, index, settings.min_severity, settings.max_comments)
    else:
        review = ReviewResult(summary="LLM review skipped.", comments=[])
    return PipelineResult(gate, review, files)


def publish(
    gh: GitHubClient, owner: str, repo: str, number: int, head_sha: str, result: PipelineResult, settings: Settings
) -> None:
    gate = result.gate
    conclusion = "failure" if gate.blocked else ("neutral" if gate.require_human_review else "success")
    title = "Blocked by policy" if gate.blocked else ("Needs human review" if gate.require_human_review else "Passed")
    gh.create_check_run(owner, repo, head_sha, CHECK_NAME, conclusion, title, result.markdown, gate.findings)
    event = (
        "APPROVE" if (gate.auto_approve and settings.allow_auto_approve and not result.review.comments) else "COMMENT"
    )
    gh.create_review(owner, repo, number, head_sha, result.markdown, result.review.comments, event=event)
    if gate.blocked or gate.require_human_review:
        notify_slack(
            settings,
            f"*{owner}/{repo}#{number}*: {title} (tier `{gate.tier}`, {len(gate.findings)} finding(s), "
            f"{len(result.review.comments)} AI comment(s)) https://github.com/{owner}/{repo}/pull/{number}",
        )


def review_remote_pr(
    llm: LLMClient, gh: GitHubClient, owner: str, repo: str, number: int, settings: Settings, post: bool = True
) -> PipelineResult:
    """Used by the webhook and Slack: fetch diff + shallow checkout, run everything, publish."""
    pr = gh.get_pr(owner, repo, number)
    diff_text = gh.get_pr_diff(owner, repo, number)
    with tempfile.TemporaryDirectory(prefix="agentic-ops-") as tmp:
        repo_dir = checkout_pr(owner, repo, number, gh.token, Path(tmp))
        result = run_pipeline(llm, diff_text, repo_dir, settings)
    if post:
        publish(gh, owner, repo, number, pr["head"]["sha"], result, settings)
    log.info(
        "Reviewed %s/%s#%s: blocked=%s comments=%d",
        owner,
        repo,
        number,
        result.gate.blocked,
        len(result.review.comments),
    )
    return result
