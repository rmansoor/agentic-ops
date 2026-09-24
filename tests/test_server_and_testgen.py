import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from agentic_ops.config import Settings
from agentic_ops.llm import FakeLLM, LLMResponse, ToolCall
from agentic_ops.server import create_app
from agentic_ops.testgen import generate_tests, module_import_path, public_functions


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_webhook_signature_and_routing(tmp_path, monkeypatch):
    settings = Settings(github_webhook_secret="whsec", repo_dir=str(tmp_path))
    queued = []
    monkeypatch.setattr("agentic_ops.server.review_remote_pr", lambda *a, **k: queued.append(a))
    monkeypatch.setattr("agentic_ops.server.installation_token", lambda *a, **k: "tok")
    client = TestClient(create_app(settings, llm=FakeLLM([])))

    assert client.post("/github/webhook", content=b"{}", headers={"X-GitHub-Event": "ping"}).status_code == 401

    body = json.dumps(
        {
            "action": "opened",
            "pull_request": {"number": 7, "draft": False},
            "repository": {"full_name": "acme/api"},
            "installation": {"id": 42},
        }
    ).encode()
    resp = client.post(
        "/github/webhook",
        content=body,
        headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": _sign("whsec", body)},
    )
    assert resp.json() == {"ok": True, "queued": "acme/api#7"}
    assert queued and queued[0][2:5] == ("acme", "api", 7)

    body = json.dumps({"action": "closed"}).encode()
    resp = client.post(
        "/github/webhook",
        content=body,
        headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": _sign("whsec", body)},
    )
    assert resp.json()["skipped"] is True


def test_ide_review_endpoint(tmp_path):
    settings = Settings(repo_dir=str(tmp_path), api_token="t0k", policy_path=str(tmp_path / "none.yml"))
    call = ToolCall(
        "1",
        "submit_review",
        {
            "summary": "Looks risky.",
            "comments": [
                {"path": "src/a.py", "line": 2, "severity": "major", "category": "bug", "body": "Divide by zero."}
            ],
        },
    )
    client = TestClient(create_app(settings, llm=FakeLLM([LLMResponse(tool_calls=[call])])))
    payload = {"path": "src/a.py", "content": "def avg(xs):\n    return sum(xs) / len(xs)\n"}

    assert client.post("/api/review", json=payload).status_code == 401
    resp = client.post("/api/review", json=payload, headers={"Authorization": "Bearer t0k"})
    data = resp.json()
    assert resp.status_code == 200
    assert data["comments"][0]["line"] == 2 and data["tier"] == "medium"


def test_ask_endpoint(tmp_path):
    (tmp_path / "x.py").write_text("def hello():\n    return 'hi'\n")
    settings = Settings(repo_dir=str(tmp_path))
    client = TestClient(create_app(settings, llm=FakeLLM([LLMResponse(text="`x.py:1` defines hello.")])))
    assert client.post("/api/ask", json={"question": "where is hello?"}).json()["answer"].startswith("`x.py:1`")


# ----------------------------------------------------------------------------- testgen
def test_function_discovery_and_import_path(tmp_path):
    src = "def a():\n    pass\n\ndef _b():\n    pass\n\nclass C:\n    def m(self):\n        pass\n"
    assert [f.name for f in public_functions(src)] == ["a", "C.m"]
    from pathlib import Path

    assert module_import_path(Path("src/pkg/mod.py")) == "pkg.mod"


def test_testgen_repairs_then_keeps_passing_tests(tmp_path):
    (tmp_path / "mathx.py").write_text("def add(a, b):\n    return a + b\n")
    failing = "from mathx import add\n\ndef test_add():\n    assert add(1, 1) == 3\n"
    passing = "from mathx import add\n\ndef test_add():\n    assert add(1, 1) == 2\n\ndef test_neg():\n    assert add(-1, 1) == 0\n"
    llm = FakeLLM(
        [
            LLMResponse(tool_calls=[ToolCall("1", "submit_tests", {"code": failing})]),
            LLMResponse(tool_calls=[ToolCall("2", "submit_tests", {"code": passing, "notes": "fixed"})]),
        ]
    )
    result = generate_tests(llm, tmp_path, "mathx.py", out_dir="tests_gen")
    assert result.passed and result.attempts == 2
    assert (tmp_path / "tests_gen" / "test_mathx_generated.py").read_text() == passing
    assert (
        "assert 2 == 3" in llm.calls[1]["messages"][0]["content"]
        or "pytest output" in llm.calls[1]["messages"][0]["content"]
    )


def test_testgen_discards_tests_that_never_pass(tmp_path):
    (tmp_path / "m.py").write_text("def f():\n    return 1\n")
    bad = "from m import f\n\ndef test_f():\n    assert f() == 2\n"
    llm = FakeLLM([LLMResponse(tool_calls=[ToolCall(str(i), "submit_tests", {"code": bad})]) for i in range(2)])
    result = generate_tests(llm, tmp_path, "m.py", out_dir="gen")
    assert not result.passed and result.test_file is None
    assert not (tmp_path / "gen" / "test_m_generated.py").exists()
