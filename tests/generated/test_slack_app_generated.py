import pytest
import types
import sys

from unittest.mock import Mock, patch

import agentic_ops.slack_app as slack_app

def make_settings(**kwargs):
    # Minimal viable settings object
    Settings = type("Settings", (), {})
    s = Settings()
    s.slack_bot_token = kwargs.get("slack_bot_token", "xoxb-token")
    s.slack_signing_secret = kwargs.get("slack_signing_secret", "sign")
    s.repo_dir = kwargs.get("repo_dir", "/repo")
    s.github_app_id = kwargs.get("github_app_id", None)
    s.github_private_key = kwargs.get("github_private_key", None)
    s.github_api_url = kwargs.get("github_api_url", "https://api.github.com")
    s.github_token = kwargs.get("github_token", None)
    return s

@pytest.fixture
def fake_llm_factory():
    class DummyResult:
        def __init__(self, answer, steps=None):
            self.answer = answer
            self.steps = steps or []
    class DummyAgent:
        def __init__(self, llm, repo_dir):
            self.llm = llm
            self.repo_dir = repo_dir
        def ask(self, question):
            if question == "no steps":
                return DummyResult("ans", [])
            return DummyResult("42", [f"step{i}" for i in range(7)])
    def llm_factory():
        return Mock(name="llmclient")
    # Patch CodebaseAgent to our dummy
    orig = slack_app.CodebaseAgent
    slack_app.CodebaseAgent = DummyAgent
    yield llm_factory
    slack_app.CodebaseAgent = orig

@patch.dict(sys.modules, {"slack_bolt": None})
def test_returns_none_if_slack_not_configured():
    # No token
    settings = make_settings(slack_bot_token=None)
    ret = slack_app.build_slack_app(settings, Mock())
    assert ret is None
    # No signing_secret
    settings = make_settings(slack_signing_secret=None)
    ret = slack_app.build_slack_app(settings, Mock())
    assert ret is None

@patch("agentic_ops.slack_app.log")
@patch.dict(sys.modules, {"slack_bolt": None})
def test_returns_none_if_slack_bolt_missing(mock_log):
    settings = make_settings()
    ret = slack_app.build_slack_app(settings, Mock())
    assert ret is None
    assert mock_log.warning.called
    assert "slack_bolt not installed" in mock_log.warning.call_args[0][0]

@pytest.fixture
def mock_slack_bolt(monkeypatch):
    class DummyApp:
        def __init__(self, token=None, signing_secret=None):
            self.token = token
            self.signing_secret = signing_secret
            self.commands = {}
            self.events = {}
        def command(self, name):
            def deco(fn):
                self.commands[name] = fn
                return fn
            return deco
        def event(self, name):
            def deco(fn):
                self.events[name] = fn
                return fn
            return deco
    m = types.SimpleNamespace(App=DummyApp)
    monkeypatch.setitem(sys.modules, "slack_bolt", m)
    return DummyApp

@patch("agentic_ops.slack_app.CodebaseAgent")
def test_build_slack_app_creates_app(mock_agent, mock_slack_bolt, fake_llm_factory):
    settings = make_settings()
    app = slack_app.build_slack_app(settings, fake_llm_factory)
    assert hasattr(app, "commands")
    assert "/askcode" in app.commands
    assert "/reviewpr" in app.commands
    assert "app_mention" in app.events
    # Should have set correct token & secret
    assert app.token == settings.slack_bot_token
    assert app.signing_secret == settings.slack_signing_secret

@pytest.mark.usefixtures("mock_slack_bolt", "fake_llm_factory")
def test_ask_command_behavior(monkeypatch):
    settings = make_settings()
    llm_calls = {}
    def fake_answer(question):
        llm_calls["called"] = question
        if not question:
            return "no Q"
        return f"ANSWER:{question}"
    monkeypatch.setattr(slack_app, "CodebaseAgent", lambda llm, repo: type("X", (), {"ask": lambda self, q: type("Y", (), {"answer": fake_answer(q), "steps": ["1"]})()})())
    app = slack_app.build_slack_app(settings, lambda: None)
    func = app.commands["/askcode"]
    ack = Mock()
    respond = Mock()
    # Normal usage
    func(ack, respond, {"text": "how work?"})
    ack.assert_called_once()
    respond.assert_called_with("ANSWER:how work?\n\n_Steps:_\n• `1`")
    # Empty text returns usage message
    ack.reset_mock()
    respond.reset_mock()
    func(ack, respond, {"text": "   "})
    ack.assert_called_once()
    respond.assert_called_with("Usage: `/askcode where do we validate webhook signatures?`")
    # No text key
    ack.reset_mock()
    respond.reset_mock()
    func(ack, respond, {})
    ack.assert_called_once()
    respond.assert_called_with("Usage: `/askcode where do we validate webhook signatures?`")

@pytest.mark.usefixtures("mock_slack_bolt", "fake_llm_factory")
def test_reviewpr_command(monkeypatch):
    settings = make_settings(github_token="tok")
    # Patch _github_for to always return true
    monkeypatch.setattr(slack_app, "_github_for", lambda *a, **k: "GH")
    # Mock review_remote_pr to return dummy object
    dummy_gate = type("G", (), {"blocked": False, "tier": "hi"})()
    dummy_review = type("R", (), {"comments": [1, 2], "summary": "hi sum"})()
    dummy_result = type("X", (), {"gate": dummy_gate, "review": dummy_review})()
    monkeypatch.setattr(slack_app, "review_remote_pr", lambda *a, **kw: dummy_result)
    app = slack_app.build_slack_app(settings, lambda: None)
    func = app.commands["/reviewpr"]
    ack = Mock()
    respond = Mock()
    # Correct PR URL, not blocked
    func(ack, respond, {"text": "https://github.com/a/b/pull/123"})
    ack.assert_called_once()
    assert respond.call_args[0][0].startswith("Gate passed ✅")
    # No PR URL
    ack.reset_mock(); respond.reset_mock()
    func(ack, respond, {"text": "nourgou"})
    respond.assert_called_with("Usage: `/reviewpr https://github.com/org/repo/pull/123`")
    # _github_for returns None
    monkeypatch.setattr(slack_app, "_github_for", lambda *a, **k: None)
    ack.reset_mock(); respond.reset_mock()
    func(ack, respond, {"text": "https://github.com/a/b/pull/123"})
    respond.assert_called_with("GitHub credentials are not configured on the server.")
    # Blocked case
    monkeypatch.setattr(slack_app, "_github_for", lambda *a, **k: "GH")
    dummy_blocked_gate = type("G", (), {"blocked": True, "tier": "med"})()
    dummy_review = type("R", (), {"comments": [], "summary": "fail"})()
    dummy_result2 = type("X", (), {"gate": dummy_blocked_gate, "review": dummy_review})()
    monkeypatch.setattr(slack_app, "review_remote_pr", lambda *a, **kw: dummy_result2)
    ack.reset_mock(); respond.reset_mock()
    func(ack, respond, {"text": "https://github.com/a/b/pull/123"})
    assert "blocked" in respond.call_args[0][0]

@pytest.mark.usefixtures("mock_slack_bolt", "fake_llm_factory")
def test_app_mention(monkeypatch):
    settings = make_settings()
    # Patch CodebaseAgent so .ask returns known structure
    class DummyAgent:
        def __init__(self, llm, repo_dir): pass
        def ask(self, question):
            return type("X", (), {"answer": f"RESP:{question}", "steps": ["u"]})()
    monkeypatch.setattr(slack_app, "CodebaseAgent", DummyAgent)
    app = slack_app.build_slack_app(settings, lambda: None)
    func = app.events["app_mention"]
    say = Mock()
    # Normal mention with question
    ev = {"text": "<@foo> hello?", "thread_ts": "12", "ts": "82"}
    func(ev, say)
    say.assert_called_with(text="RESP:hello?\n\n_Steps:_\n• `u`", thread_ts="12")
    # Mention with no question
    ev = {"text": "<@bar>   ", "ts": "87"}
    func(ev, say)
    say.assert_called_with(text="Ask me about the codebase.", thread_ts="87")

@pytest.mark.usefixtures("mock_slack_bolt", "fake_llm_factory")
def test_answer_no_steps(monkeypatch):
    settings = make_settings()
    class Dummy:
        def ask(self, q):
            return type("R", (), {"answer": "justans", "steps": []})()
    monkeypatch.setattr(slack_app, "CodebaseAgent", lambda a, r: Dummy())
    app = slack_app.build_slack_app(settings, lambda: None)
    # Extract answer fn indirectly
    # Use the app_mention event
    fn = app.events["app_mention"]
    event = {"text": "<@bot> no steps", "ts": "1"}
    collected = {}
    def say(**kwargs): collected.update(kwargs)
    fn(event, say)
    # Should not include steps section
    assert collected["text"] == "justans"

