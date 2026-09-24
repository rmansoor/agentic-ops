#!/usr/bin/env bash
# Agentic Ops bootstrap: installs everything, verifies it, and prints the wiring steps.
#
#   ./scripts/bootstrap.sh                 # install + test + index this repo
#   ./scripts/bootstrap.sh --target ../api # also index another repo for the agent / Slack
#   ./scripts/bootstrap.sh --with-vscode   # also build the VS Code extension (.vsix)
#   ./scripts/bootstrap.sh --serve         # start the server when done
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET=""
WITH_VSCODE=0
SERVE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --target) TARGET="$2"; shift 2 ;;
    --with-vscode) WITH_VSCODE=1; shift ;;
    --serve) SERVE=1; shift ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

bold() { printf "\n\033[1m==> %s\033[0m\n" "$*"; }
need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing required tool: $1"; exit 1; }; }

cd "$ROOT"
bold "Checking prerequisites"
need git
PY="$(command -v python3.12 || command -v python3.11 || command -v python3 || true)"
[[ -n "$PY" ]] || { echo "Python 3.11+ is required"; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || { echo "Python 3.11+ is required (found $("$PY" --version))"; exit 1; }
echo "python: $("$PY" --version)  git: $(git --version | cut -d' ' -f3)"

bold "Creating virtualenv (.venv) and installing agentic-ops + scanners"
[[ -d .venv ]] || "$PY" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install -q --upgrade pip
python -m pip install -q -e ".[all,dev]"
echo "ruff $(ruff --version | cut -d' ' -f2), semgrep $(semgrep --version)"

bold "Configuring environment"
if [[ ! -f .env ]]; then
  cp .env.example .env
  TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(24))')"
  sed -i.bak "s/^AGENTIC_OPS_API_TOKEN=.*/AGENTIC_OPS_API_TOKEN=${TOKEN}/" .env && rm -f .env.bak
  echo "Created .env (IDE API token generated). Add ANTHROPIC_API_KEY or OPENAI_API_KEY."
else
  echo ".env already exists, leaving it alone."
fi

bold "Running the test suite"
python -m pytest -q

bold "Validating Semgrep rules"
if semgrep --validate --config semgrep/rules.yml --metrics=off --disable-version-check --quiet >/dev/null 2>&1; then
  echo "rules OK"
else
  echo "skipped: 'semgrep --validate' needs access to semgrep.dev (the test suite already exercises the rules)"
fi

bold "Building the codebase index"
agentic-ops --repo-dir "$ROOT" index
if [[ -n "$TARGET" ]]; then
  agentic-ops --repo-dir "$TARGET" index
  sed -i.bak "s|^AGENTIC_OPS_REPO_DIR=.*|AGENTIC_OPS_REPO_DIR=$(cd "$TARGET" && pwd)|" .env && rm -f .env.bak
fi

if [[ "$WITH_VSCODE" == 1 ]]; then
  bold "Building the VS Code extension"
  need npm
  (cd vscode-extension && npm install --silent --no-audit --no-fund && npm run compile --silent \
    && npx --yes @vscode/vsce package --no-dependencies --allow-missing-repository --skip-license)
  echo "Install with: code --install-extension vscode-extension/agentic-ops-vscode-0.1.0.vsix"
fi

KEY_SET="$(grep -E '^(ANTHROPIC|OPENAI)_API_KEY=.+' .env || true)"
bold "Done. Next steps"
cat <<EOF
 1. LLM key:        ${KEY_SET:+set ✓}${KEY_SET:-edit .env and set ANTHROPIC_API_KEY (or LLM_PROVIDER=openai + OPENAI_API_KEY)}
 2. Try it locally:
      source .venv/bin/activate
      agentic-ops review --base origin/main              # gate + AI review of your branch
      agentic-ops ask "where do we validate webhooks?" -v # codebase-aware agent
      agentic-ops testgen path/to/module.py             # generate + run tests
      agentic-ops eval                                  # reviewer precision/recall
 3. CI/CD:  copy .github/workflows/ + policy.yml + semgrep/ into your repo and add the
            ANTHROPIC_API_KEY secret. Every PR gets a check run + inline review.
 4. Git (GitHub App, org-wide): create an app from deploy/github-app-manifest.json, save the key
            as github-app.pem, set GITHUB_APP_ID + GITHUB_WEBHOOK_SECRET, then:
            docker compose up --build     (webhook URL: https://<host>/github/webhook)
 5. Slack:  create a Slack app with slash commands /askcode and /reviewpr and the app_mention
            event pointing to https://<host>/slack/events; set SLACK_BOT_TOKEN + SLACK_SIGNING_SECRET.
 6. IDE:    ./scripts/bootstrap.sh --with-vscode, install the .vsix, set agenticOps.serverUrl
            and agenticOps.apiToken (value of AGENTIC_OPS_API_TOKEN in .env).
 7. Metrics: agentic-ops metrics --repo your-org/your-repo --days 30
EOF

if [[ "$SERVE" == 1 ]]; then
  bold "Starting server on :8080"
  exec agentic-ops serve --port 8080
fi
