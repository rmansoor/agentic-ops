"""Command-line entry point: `agentic-ops <command>`."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from .config import Settings


def _settings() -> Settings:
    return Settings.from_env()


def cmd_index(args, s: Settings) -> int:
    from .indexer import CodeIndex

    idx = CodeIndex.build(args.repo_dir)
    path = idx.save()
    print(f"Indexed {len(idx.chunks)} chunks from {len({c.path for c in idx.chunks})} files -> {path}")
    return 0


def cmd_gate(args, s: Settings) -> int:
    from .diff import git_diff, parse_diff
    from .policy import Policy
    from .quality_gate import run_gate, to_sarif

    files = parse_diff(git_diff(args.base, args.head, args.repo_dir))
    gate = run_gate(
        args.repo_dir,
        [f.path for f in files if not f.is_deleted],
        Policy.load(Path(args.repo_dir) / s.policy_path),
        s,
        args.sarif,
        {f.path: f.added_lines for f in files if not f.is_deleted},
    )
    if args.sarif_out:
        Path(args.sarif_out).write_text(json.dumps(to_sarif(gate.findings), indent=2))
    print(gate.model_dump_json(indent=2))
    return 1 if gate.blocked else 0


def cmd_review(args, s: Settings) -> int:
    from .diff import git_diff
    from .llm import make_llm
    from .pipeline import run_pipeline

    diff_text = Path(args.diff_file).read_text() if args.diff_file else git_diff(args.base, args.head, args.repo_dir)
    llm = None if args.no_llm else make_llm(s)
    result = run_pipeline(llm, diff_text, args.repo_dir, s, args.sarif, use_llm=not args.no_llm)
    print(result.markdown)
    for c in result.review.comments:
        print(f"\n{c.path}:{c.line} [{c.severity}/{c.category}] {c.body}")
    return 1 if result.gate.blocked else 0


def cmd_ci(args, s: Settings) -> int:
    """GitHub Actions mode: uses GITHUB_TOKEN, GITHUB_REPOSITORY and the pull_request event payload."""
    from .diff import git_diff
    from .github_client import GitHubClient
    from .llm import make_llm
    from .pipeline import publish, run_pipeline

    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    pr = event["pull_request"]
    owner, repo = os.environ["GITHUB_REPOSITORY"].split("/", 1)
    diff_text = git_diff(pr["base"]["sha"], pr["head"]["sha"], args.repo_dir)
    use_llm = not args.no_llm and bool(s.anthropic_api_key or s.openai_api_key)
    result = run_pipeline(make_llm(s) if use_llm else None, diff_text, args.repo_dir, s, args.sarif, use_llm=use_llm)
    if not s.github_token:
        print("GITHUB_TOKEN missing", file=sys.stderr)
        return 2
    publish(GitHubClient(s.github_token, s.github_api_url), owner, repo, pr["number"], pr["head"]["sha"], result, s)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as fh:
            fh.write(result.markdown + "\n")
    print(result.markdown)
    return 1 if result.gate.blocked else 0


def cmd_ask(args, s: Settings) -> int:
    from .agent import CodebaseAgent
    from .llm import make_llm

    result = CodebaseAgent(make_llm(s), args.repo_dir).ask(" ".join(args.question))
    print(result.answer)
    if args.verbose:
        print("\nSteps:\n" + "\n".join(f"  - {x}" for x in result.steps))
    return 0


def cmd_testgen(args, s: Settings) -> int:
    from .llm import make_llm
    from .testgen import generate_tests

    llm = make_llm(s)
    exit_code = 0
    for f in args.files:
        r = generate_tests(llm, args.repo_dir, f, args.out_dir, args.coverage, args.repairs)
        state = "PASS" if r.passed else "FAIL"
        print(
            f"[{state}] {r.source} -> {r.test_file or '(discarded)'} functions={len(r.functions)} attempts={r.attempts}"
        )
        if r.notes:
            print(f"    notes: {r.notes}")
        exit_code |= 0 if r.passed else 1
    return exit_code


def cmd_eval(args, s: Settings) -> int:
    from .evals import run_evals
    from .llm import make_llm

    report = run_evals(make_llm(s), args.cases, s.min_severity)
    print(json.dumps(report, indent=2))
    ok = report["precision"] >= args.min_precision and report["recall"] >= args.min_recall
    print(f"\nprecision={report['precision']} recall={report['recall']} -> {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def cmd_metrics(args, s: Settings) -> int:
    from .github_client import GitHubClient
    from .metrics import collect, to_markdown

    owner, repo = args.repo.split("/", 1)
    m = collect(GitHubClient(s.github_token or "", s.github_api_url), owner, repo, args.days, s.bot_login)
    print(json.dumps(m, indent=2) if args.json else to_markdown(m))
    return 0


def cmd_serve(args, s: Settings) -> int:
    import uvicorn

    from .server import create_app

    uvicorn.run(create_app(s), host=args.host, port=args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agentic-ops", description=__doc__)
    p.add_argument("--repo-dir", default=".", help="repository root (default: .)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("index", help="build the codebase index").set_defaults(fn=cmd_index)

    for name, fn, help_ in (
        ("gate", cmd_gate, "run the quality gate on a git diff"),
        ("review", cmd_review, "quality gate + AI review of a local diff"),
    ):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--base", default="origin/main")
        sp.add_argument("--head", default="HEAD")
        sp.add_argument("--sarif", action="append", default=[], help="extra SARIF file (CodeQL etc.)")
        if name == "gate":
            sp.add_argument("--sarif-out", help="write findings as SARIF (for GitHub code scanning)")
        else:
            sp.add_argument("--diff-file", help="review a saved .diff instead of git")
            sp.add_argument("--no-llm", action="store_true")
        sp.set_defaults(fn=fn)

    ci = sub.add_parser("ci", help="GitHub Actions: gate + review + post results")
    ci.add_argument("--sarif", action="append", default=[])
    ci.add_argument("--no-llm", action="store_true")
    ci.set_defaults(fn=cmd_ci)

    ask = sub.add_parser("ask", help="ask the codebase-aware agent a question")
    ask.add_argument("question", nargs="+")
    ask.add_argument("-v", "--verbose", action="store_true")
    ask.set_defaults(fn=cmd_ask)

    tg = sub.add_parser("testgen", help="generate pytest tests for untested functions")
    tg.add_argument("files", nargs="+")
    tg.add_argument("--out-dir", default="tests/generated")
    tg.add_argument("--coverage", help="coverage.py JSON report (coverage json)")
    tg.add_argument("--repairs", type=int, default=1)
    tg.set_defaults(fn=cmd_testgen)

    ev = sub.add_parser("eval", help="run reviewer evals on golden PRs")
    ev.add_argument("--cases", default="evals/golden")
    ev.add_argument("--min-precision", type=float, default=0.6)
    ev.add_argument("--min-recall", type=float, default=0.6)
    ev.set_defaults(fn=cmd_eval)

    mt = sub.add_parser("metrics", help="developer productivity metrics from GitHub")
    mt.add_argument("--repo", required=True, help="owner/name")
    mt.add_argument("--days", type=int, default=30)
    mt.add_argument("--json", action="store_true")
    mt.set_defaults(fn=cmd_metrics)

    sv = sub.add_parser("serve", help="run webhook + IDE + Slack server")
    sv.add_argument("--host", default="0.0.0.0")  # noqa: S104 - container service
    sv.add_argument("--port", type=int, default=8080)
    sv.set_defaults(fn=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    return args.fn(args, _settings())


if __name__ == "__main__":
    sys.exit(main())
