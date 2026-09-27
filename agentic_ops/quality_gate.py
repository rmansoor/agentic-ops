"""Deterministic quality gate: Ruff, Semgrep, ESLint, SARIF (CodeQL etc.) and SonarQube, plus policy."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import httpx

from .config import Settings
from .models import Finding, GateResult
from .policy import Policy

PY_EXT = {".py"}
JS_EXT = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}


def _rel(path: str, root: Path) -> str:
    p = Path(path)
    try:
        return p.resolve().relative_to(root.resolve()).as_posix() if p.is_absolute() else p.as_posix()
    except ValueError:
        return p.as_posix()


def _run(cmd: list[str], cwd: Path, timeout: int = 600) -> str:
    # Scanners exit non-zero when they find issues; we only care about stdout.
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return proc.stdout


# --------------------------------------------------------------------------- Parsers (pure, testable)


def parse_ruff(output: str, root: Path) -> list[Finding]:
    findings = []
    for item in json.loads(output or "[]"):
        code = item.get("code") or "ruff"
        severity = "error" if code.startswith(("S", "F")) else "warning"
        findings.append(
            Finding(
                tool="ruff",
                rule=code,
                path=_rel(item["filename"], root),
                line=(item.get("location") or {}).get("row", 0),
                severity=severity,
                message=item.get("message", ""),
            )
        )
    return findings


def parse_semgrep(output: str, root: Path) -> list[Finding]:
    data = json.loads(output or "{}")
    sev_map = {"ERROR": "error", "WARNING": "warning", "INFO": "info"}
    return [
        Finding(
            tool="semgrep",
            rule=r["check_id"].split(".")[-1],
            path=_rel(r["path"], root),
            line=r["start"]["line"],
            severity=sev_map.get(r.get("extra", {}).get("severity", "WARNING"), "warning"),
            message=r.get("extra", {}).get("message", "").strip(),
        )
        for r in data.get("results", [])
    ]


def parse_eslint(output: str, root: Path) -> list[Finding]:
    findings = []
    for f in json.loads(output or "[]"):
        for m in f.get("messages", []):
            findings.append(
                Finding(
                    tool="eslint",
                    rule=m.get("ruleId") or "eslint",
                    path=_rel(f["filePath"], root),
                    line=m.get("line", 0),
                    severity="error" if m.get("severity") == 2 else "warning",
                    message=m.get("message", ""),
                )
            )
    return findings


def parse_sarif(data: dict, root: Path) -> list[Finding]:
    """Ingest SARIF from CodeQL, SonarQube, Semgrep, or any other SARIF producer."""
    level_map = {"error": "error", "warning": "warning", "note": "info", "none": "info"}
    findings = []
    for run in data.get("runs", []):
        tool = run.get("tool", {}).get("driver", {}).get("name", "sarif").lower()
        for res in run.get("results", []):
            loc = (res.get("locations") or [{}])[0].get("physicalLocation", {})
            findings.append(
                Finding(
                    tool=tool,
                    rule=res.get("ruleId", "sarif"),
                    path=_rel(loc.get("artifactLocation", {}).get("uri", ""), root),
                    line=loc.get("region", {}).get("startLine", 0),
                    severity=level_map.get(res.get("level", "warning"), "warning"),
                    message=res.get("message", {}).get("text", ""),
                )
            )
    return findings


def to_sarif(findings: list[Finding]) -> dict:
    level = {"error": "error", "warning": "warning", "info": "note"}
    return {
        "version": "2.1.0",
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "runs": [
            {
                "tool": {"driver": {"name": "agentic-ops", "informationUri": "https://github.com"}},
                "results": [
                    {
                        "ruleId": f"{f.tool}/{f.rule}",
                        "level": level[f.severity],
                        "message": {"text": f.message},
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {"uri": f.path},
                                    "region": {"startLine": max(f.line, 1)},
                                }
                            }
                        ],
                    }
                    for f in findings
                ],
            }
        ],
    }


# --------------------------------------------------------------------------- Runners


def run_ruff(files: list[str], root: Path) -> list[Finding] | None:
    targets = [f for f in files if Path(f).suffix in PY_EXT and (root / f).is_file()]
    if not targets or not shutil.which("ruff"):
        return None
    return parse_ruff(_run(["ruff", "check", "--output-format", "json", *targets], root), root)


def run_semgrep(files: list[str], root: Path, config: str) -> list[Finding] | None:
    targets = [f for f in files if (root / f).is_file()]
    if not targets or not shutil.which("semgrep"):
        return None
    if (root / config).is_file():
        resolved = str((root / config).resolve())
    elif Path(config).is_file():
        resolved = str(Path(config).resolve())
    else:
        resolved = "p/default"  # Semgrep registry ruleset (needs network)
    # --disable-version-check avoids a network call that can stall ~100s in locked-down CI/containers.
    cmd = ["semgrep", "scan", "--json", "--quiet", "--metrics=off", "--disable-version-check", "--config", resolved]
    return parse_semgrep(_run(cmd + targets, root), root)


def run_eslint(files: list[str], root: Path) -> list[Finding] | None:
    targets = [f for f in files if Path(f).suffix in JS_EXT and (root / f).is_file()]
    has_config = any((root / n).exists() for n in ("eslint.config.js", "eslint.config.mjs", ".eslintrc.json"))
    if not targets or not has_config or not shutil.which("npx"):
        return None
    return parse_eslint(_run(["npx", "--no-install", "eslint", "-f", "json", *targets], root), root)


def sonarqube_status(settings: Settings) -> list[Finding] | None:
    if not (settings.sonar_host_url and settings.sonar_token and settings.sonar_project_key):
        return None
    resp = httpx.get(
        f"{settings.sonar_host_url.rstrip('/')}/api/qualitygates/project_status",
        params={"projectKey": settings.sonar_project_key},
        auth=(settings.sonar_token, ""),
        timeout=30,
    )
    resp.raise_for_status()
    status = resp.json().get("projectStatus", {})
    if status.get("status") != "ERROR":
        return []
    failed = [c for c in status.get("conditions", []) if c.get("status") == "ERROR"]
    return [
        Finding(
            tool="sonarqube",
            rule=c.get("metricKey", "quality_gate"),
            path="",
            severity="error",
            message=f"SonarQube quality gate failed: {c.get('metricKey')}={c.get('actualValue')} "
            f"(threshold {c.get('errorThreshold')})",
        )
        for c in failed
    ] or [Finding(tool="sonarqube", rule="quality_gate", path="", severity="error", message="Gate failed")]


def run_gate(
    repo_dir: str | Path,
    changed_files: list[str],
    policy: Policy,
    settings: Settings,
    sarif_files: list[str] | None = None,
    added_lines: dict[str, set[int]] | None = None,
) -> GateResult:
    """Run scanners on `changed_files` and apply the policy.

    With `added_lines` (path -> new-file line numbers the change adds) and gate_scope "lines", findings on
    untouched lines are dropped, so pre-existing issues in a legacy file don't block an unrelated change.
    """
    root = Path(repo_dir)
    findings: list[Finding] = []
    tools_run: list[str] = []
    for name, result in (
        ("ruff", run_ruff(changed_files, root)),
        ("semgrep", run_semgrep(changed_files, root, settings.semgrep_config)),
        ("eslint", run_eslint(changed_files, root)),
        ("sonarqube", sonarqube_status(settings)),
    ):
        if result is not None:
            tools_run.append(name)
            findings.extend(result)
    for sarif in sarif_files or []:
        findings.extend(parse_sarif(json.loads(Path(sarif).read_text()), root))
        tools_run.append(f"sarif:{Path(sarif).name}")
    # Only gate on findings in files this change touched (SonarQube project-level findings have no path).
    changed = set(changed_files)
    findings = [f for f in findings if not f.path or f.path in changed]
    if added_lines is not None and settings.gate_scope == "lines":
        findings = [
            f for f in findings if not f.path or not f.line or f.line in added_lines.get(f.path, set())
        ]
    return policy.evaluate(changed_files, findings, tools_run)
