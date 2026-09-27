import dataclasses
import json
import shutil
from pathlib import Path

import pytest

from agentic_ops.agent import CodebaseAgent
from agentic_ops.diff import file_as_diff, parse_diff
from agentic_ops.evals import match
from agentic_ops.github_client import verify_signature
from agentic_ops.indexer import CodeIndex, chunk_file, tokenize
from agentic_ops.llm import FakeLLM, LLMError, LLMResponse, ToolCall, structured_call, to_anthropic_messages
from agentic_ops.models import Finding, ReviewComment, ReviewResult
from agentic_ops.policy import Policy, path_matches
from agentic_ops.quality_gate import parse_eslint, parse_ruff, parse_sarif, parse_semgrep, run_gate, to_sarif
from agentic_ops.reviewer import REVIEW_TOOL, postprocess, review_diff

ROOT = Path(__file__).resolve().parents[1]


# ----------------------------------------------------------------------------- diff
def test_parse_diff_line_numbers(sample_diff):
    files = {f.path: f for f in parse_diff(sample_diff)}
    assert files["app/users.py"].added_lines == {2, 3, 4, 5}
    assert files["docs/readme.md"].added_lines == {2}
    assert files["old.py"].is_deleted
    assert "L5    + " in files["app/users.py"].annotated()


def test_file_as_diff_marks_all_lines_added():
    fd = file_as_diff("a.py", "x = 1\ny = 2\n")
    assert fd.added_lines == {1, 2}


# ----------------------------------------------------------------------------- policy
@pytest.mark.parametrize(
    ("path", "pattern", "ok"),
    [
        ("src/auth/login.py", "**/auth/**", True),
        ("auth/login.py", "**/auth/**", True),
        ("src/authz.py", "**/auth/**", False),
        ("docs/a/b.md", "docs/**", True),
        ("README.md", "**/*.md", True),
        ("src/x.py", "*.py", False),
    ],
)
def test_glob(path, pattern, ok):
    assert path_matches(path, [pattern]) is ok


def test_policy_tiers_and_blocking():
    policy = Policy.load(ROOT / "policy.yml")
    err = Finding(tool="ruff", rule="S602", path="src/auth/login.py", line=3, severity="warning", message="x")
    gate = policy.evaluate(["src/auth/login.py", "docs/a.md"], [err], ["ruff"])
    assert gate.tier == "high" and gate.blocked and gate.require_human_review

    gate = policy.evaluate(["src/app.py"], [err.model_copy(update={"path": "src/app.py"})], ["ruff"])
    assert gate.tier == "medium" and not gate.blocked  # warnings don't block medium

    gate = policy.evaluate(["docs/a.md", "README.md"], [], [])
    assert gate.tier == "low" and gate.auto_approve

    gate = policy.evaluate(["docs/a.md", "src/app.py"], [], [])
    assert gate.tier == "medium" and not gate.auto_approve


# ----------------------------------------------------------------------------- scanner parsers
def test_parsers(tmp_path):
    ruff = json.dumps(
        [{"code": "S602", "message": "shell", "filename": str(tmp_path / "a.py"), "location": {"row": 4, "column": 1}}]
    )
    assert parse_ruff(ruff, tmp_path)[0].model_dump() == {
        "tool": "ruff",
        "rule": "S602",
        "path": "a.py",
        "line": 4,
        "severity": "error",
        "message": "shell",
    }

    semgrep = json.dumps(
        {
            "results": [
                {
                    "check_id": "semgrep.rules.subprocess-shell-true",
                    "path": "a.py",
                    "start": {"line": 2},
                    "extra": {"severity": "ERROR", "message": "m"},
                }
            ]
        }
    )
    assert parse_semgrep(semgrep, tmp_path)[0].rule == "subprocess-shell-true"

    eslint = json.dumps(
        [
            {
                "filePath": str(tmp_path / "b.ts"),
                "messages": [{"ruleId": "no-eval", "severity": 2, "message": "e", "line": 9}],
            }
        ]
    )
    assert parse_eslint(eslint, tmp_path)[0].severity == "error"

    findings = parse_semgrep(semgrep, tmp_path)
    round_trip = parse_sarif(to_sarif(findings), tmp_path)
    assert round_trip[0].path == "a.py" and round_trip[0].line == 2 and round_trip[0].severity == "error"


@pytest.mark.skipif(not (shutil.which("semgrep") and shutil.which("ruff")), reason="scanners not installed")
def test_real_gate_blocks_shell_true(tmp_path, settings):
    (tmp_path / "semgrep").mkdir()
    shutil.copy(ROOT / "semgrep" / "rules.yml", tmp_path / "semgrep" / "rules.yml")
    (tmp_path / "svc.py").write_text(
        "import subprocess\n\n\ndef run(cmd):\n    return subprocess.run(cmd, shell=True)\n"
    )
    gate = run_gate(tmp_path, ["svc.py"], Policy.load(None), settings)
    rules = {f.rule for f in gate.findings}
    assert "subprocess-shell-true" in rules
    assert gate.blocked
    assert set(gate.tools_run) >= {"ruff", "semgrep"}


@pytest.mark.skipif(not shutil.which("semgrep"), reason="semgrep not installed")
def test_gate_ignores_preexisting_findings_on_untouched_lines(tmp_path, settings):
    (tmp_path / "semgrep").mkdir()
    shutil.copy(ROOT / "semgrep" / "rules.yml", tmp_path / "semgrep" / "rules.yml")
    (tmp_path / "svc.py").write_text(
        "import subprocess\n\n\ndef run(cmd):\n    return subprocess.run(cmd, shell=True)\n\n\nX = 1\n"
    )
    # The change only added line 8; the shell=True on line 5 was already there.
    assert not run_gate(tmp_path, ["svc.py"], Policy.load(None), settings, added_lines={"svc.py": {8}}).findings
    # Touching line 5 surfaces it again.
    assert run_gate(tmp_path, ["svc.py"], Policy.load(None), settings, added_lines={"svc.py": {5}}).blocked
    # gate_scope="files" keeps the old whole-file behaviour.
    whole_file = dataclasses.replace(settings, gate_scope="files")
    assert run_gate(tmp_path, ["svc.py"], Policy.load(None), whole_file, added_lines={"svc.py": {8}}).blocked


# ----------------------------------------------------------------------------- llm plumbing
def test_structured_call_retries_on_schema_error():
    bad = LLMResponse(tool_calls=[ToolCall("1", "submit_review", {"summary": "s", "comments": [{"path": "a"}]})])
    good = LLMResponse(tool_calls=[ToolCall("2", "submit_review", {"summary": "ok", "comments": []})])
    llm = FakeLLM([bad, good])
    out = structured_call(llm, "sys", "p", REVIEW_TOOL, ReviewResult)
    assert out.summary == "ok"
    assert llm.calls[1]["messages"][-1]["role"] == "tool"  # validation error fed back


def test_structured_call_gives_up():
    llm = FakeLLM([LLMResponse(text="no"), LLMResponse(text="still no")])
    with pytest.raises(LLMError):
        structured_call(llm, "sys", "p", REVIEW_TOOL, ReviewResult)


def test_anthropic_message_conversion_groups_tool_results():
    tc1, tc2 = ToolCall("a", "search_code", {"query": "x"}), ToolCall("b", "read_file", {"path": "y"})
    msgs = to_anthropic_messages(
        [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "", "tool_calls": [tc1, tc2]},
            {"role": "tool", "tool_call_id": "a", "name": "search_code", "content": "r1"},
            {"role": "tool", "tool_call_id": "b", "name": "read_file", "content": "r2"},
        ]
    )
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert [b["tool_use_id"] for b in msgs[2]["content"]] == ["a", "b"]


# ----------------------------------------------------------------------------- reviewer
def test_postprocess_filters_and_snaps(sample_diff):
    files = parse_diff(sample_diff)
    raw = ReviewResult(
        summary="s",
        comments=[
            ReviewComment(path="app/users.py", line=5, severity="critical", category="security", body="shell=True"),
            ReviewComment(path="app/users.py", line=7, severity="major", category="bug", body="snaps to 5"),
            ReviewComment(path="app/users.py", line=40, severity="major", category="bug", body="too far, dropped"),
            ReviewComment(path="app/users.py", line=3, severity="nit", category="style", body="below floor"),
            ReviewComment(path="nope.py", line=1, severity="critical", category="bug", body="not in diff"),
        ],
    )
    out = postprocess(raw, files, min_severity="minor")
    assert [(c.line, c.body) for c in out.comments] == [(5, "shell=True"), (5, "snaps to 5")]


def test_review_diff_end_to_end_with_fake_llm(sample_diff):
    call = ToolCall(
        "1",
        "submit_review",
        {
            "summary": "Risky shell call.",
            "comments": [
                {
                    "path": "app/users.py",
                    "line": 5,
                    "severity": "critical",
                    "category": "security",
                    "body": "Command injection via shell=True.",
                    "suggestion": "    return subprocess.run(cmd)",
                }
            ],
        },
    )
    llm = FakeLLM([LLMResponse(tool_calls=[call])])
    review = review_diff(llm, parse_diff(sample_diff))
    assert len(review.comments) == 1
    prompt = llm.calls[0]["messages"][0]["content"]
    assert "L5" in prompt and "docs/readme.md" in prompt and "old.py" not in prompt
    assert "untrusted" in llm.calls[0]["system"]


# ----------------------------------------------------------------------------- index + agent
def test_chunking_and_search(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "hooks.py").write_text(
        "import hmac\n\nSECRET = 'x'\n\n\ndef verify_webhook_signature(body, sig):\n    return hmac.compare_digest(body, sig)\n"
        "\n\nclass Router:\n    def route(self):\n        return 1\n"
    )
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("verify webhook signature")
    chunks = chunk_file("pkg/hooks.py", (tmp_path / "pkg" / "hooks.py").read_text())
    assert {c.symbol for c in chunks} >= {"verify_webhook_signature", "Router", "<module>"}
    idx = CodeIndex.build(tmp_path)
    top = idx.search("where is the webhook signature verified", k=1)[0][1]
    assert top.symbol == "verify_webhook_signature"
    assert all("node_modules" not in c.path for c in idx.chunks)
    assert "signature" in tokenize("verifyWebhookSignature")


def test_agent_tool_loop(tmp_path):
    (tmp_path / "svc.py").write_text("def charge(amount):\n    return amount * 1.07\n")
    llm = FakeLLM(
        [
            LLMResponse(tool_calls=[ToolCall("t1", "search_code", {"query": "charge tax"})]),
            LLMResponse(tool_calls=[ToolCall("t2", "read_file", {"path": "../../etc/passwd"})]),
            LLMResponse(text="Tax is applied in `svc.py:2` (7%)."),
        ]
    )
    answer = CodebaseAgent(llm, tmp_path).ask("Where is tax applied?")
    assert "svc.py:2" in answer.answer
    assert len(answer.steps) == 2
    tool_results = [m for m in llm.calls[2]["messages"] if m["role"] == "tool"]
    assert "charge" in tool_results[0]["content"]
    assert "escapes repository root" in tool_results[1]["content"]


# ----------------------------------------------------------------------------- evals / github
def test_eval_matching():
    comments = [
        ReviewComment(path="a.py", line=11, severity="major", category="bug", body="x"),
        ReviewComment(path="a.py", line=50, severity="major", category="bug", body="noise"),
    ]
    tp, misses, fps = match(comments, [{"path": "a.py", "line": 10}, {"path": "b.py", "line": 1}])
    assert tp == 1 and misses == [{"path": "b.py", "line": 1}] and len(fps) == 1


def test_signature():
    import hashlib
    import hmac

    body = b'{"a":1}'
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert verify_signature("s3cret", body, sig)
    assert not verify_signature("s3cret", body, "sha256=deadbeef")
    assert not verify_signature("", body, sig)
