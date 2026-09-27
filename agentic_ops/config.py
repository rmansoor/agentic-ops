"""Settings loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MODELS = {"anthropic": "claude-sonnet-4-5", "openai": "gpt-4.1"}


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader: KEY=VALUE lines, does not override variables already set."""
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_bool(name: str, default: bool = False) -> bool:
    value = _env(name)
    return default if value is None else value.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # LLM
    llm_provider: str = "anthropic"
    llm_model: str = DEFAULT_MODELS["anthropic"]
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    # GitHub
    github_api_url: str = "https://api.github.com"
    github_token: str | None = None
    github_app_id: str | None = None
    github_private_key: str | None = None
    github_webhook_secret: str | None = None
    bot_login: str = "agentic-ops[bot]"
    allow_auto_approve: bool = False
    # Review behaviour
    min_severity: str = "minor"
    max_comments: int = 25
    policy_path: str = "policy.yml"
    semgrep_config: str = "semgrep/rules.yml"
    # "lines": gate only on findings on lines the change adds; "files": anywhere in a touched file.
    gate_scope: str = "lines"
    # Slack
    slack_bot_token: str | None = None
    slack_signing_secret: str | None = None
    slack_notify_channel: str | None = None
    # SonarQube (optional)
    sonar_host_url: str | None = None
    sonar_token: str | None = None
    sonar_project_key: str | None = None
    # Server / IDE
    api_token: str | None = None
    repo_dir: str = "."

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        provider = (_env("LLM_PROVIDER", "anthropic") or "anthropic").lower()
        private_key = _env("GITHUB_APP_PRIVATE_KEY")
        key_path = _env("GITHUB_APP_PRIVATE_KEY_PATH")
        if not private_key and key_path and Path(key_path).is_file():
            private_key = Path(key_path).read_text()
        return cls(
            llm_provider=provider,
            llm_model=_env("LLM_MODEL", DEFAULT_MODELS.get(provider, "")) or "",
            anthropic_api_key=_env("ANTHROPIC_API_KEY"),
            openai_api_key=_env("OPENAI_API_KEY"),
            github_api_url=_env("GITHUB_API_URL", "https://api.github.com") or "https://api.github.com",
            github_token=_env("GITHUB_TOKEN"),
            github_app_id=_env("GITHUB_APP_ID"),
            github_private_key=private_key,
            github_webhook_secret=_env("GITHUB_WEBHOOK_SECRET"),
            bot_login=_env("AGENTIC_OPS_BOT_LOGIN", "agentic-ops[bot]") or "agentic-ops[bot]",
            allow_auto_approve=_env_bool("AGENTIC_OPS_ALLOW_AUTO_APPROVE"),
            min_severity=_env("AGENTIC_OPS_MIN_SEVERITY", "minor") or "minor",
            max_comments=int(_env("AGENTIC_OPS_MAX_COMMENTS", "25") or 25),
            policy_path=_env("AGENTIC_OPS_POLICY", "policy.yml") or "policy.yml",
            semgrep_config=_env("AGENTIC_OPS_SEMGREP_CONFIG", "semgrep/rules.yml") or "semgrep/rules.yml",
            gate_scope=(_env("AGENTIC_OPS_GATE_SCOPE", "lines") or "lines").lower(),
            slack_bot_token=_env("SLACK_BOT_TOKEN"),
            slack_signing_secret=_env("SLACK_SIGNING_SECRET"),
            slack_notify_channel=_env("SLACK_NOTIFY_CHANNEL"),
            sonar_host_url=_env("SONAR_HOST_URL"),
            sonar_token=_env("SONAR_TOKEN"),
            sonar_project_key=_env("SONAR_PROJECT_KEY"),
            api_token=_env("AGENTIC_OPS_API_TOKEN"),
            repo_dir=_env("AGENTIC_OPS_REPO_DIR", ".") or ".",
        )
