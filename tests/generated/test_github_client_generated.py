import pytest
import types
from pathlib import Path
from unittest.mock import MagicMock
import httpx
import jwt
import types
import agentic_ops.github_client as ghc

class DummyRC:
    def __init__(self, path, line, text="test text"):
        self.path = path
        self.line = line
        self.text = text
    def __str__(self):
        return self.text

def test_app_jwt(monkeypatch):
    # monkeypatch time so we have known timestamp
    monkeypatch.setattr(ghc.time, "time", lambda: 1234567890)
    # monkeypatch jwt.encode to check call
    called = {}
    def fake_encode(payload, key, algorithm):
        called.update(dict(payload=payload, key=key, algorithm=algorithm))
        return "token_here"
    monkeypatch.setattr(ghc.jwt, "encode", fake_encode)
    tok = ghc.app_jwt("my-app", "fake-key")
    assert tok == "token_here"
    assert called["payload"] == {'iat': 1234567830, 'exp': 1234568430, 'iss': "my-app"}
    assert called["key"] == "fake-key"
    assert called["algorithm"] == "RS256"

def test_installation_token(monkeypatch):
    # Patch app_jwt to return a known value
    monkeypatch.setattr(ghc, "app_jwt", lambda app_id, key: "jwt-token")
    resp_obj = MagicMock()
    resp_obj.json.return_value = {"token": "tkn"}
    resp_obj.raise_for_status.return_value = None
    called = {}
    def fake_post(url, headers, timeout):
        called["url"] = url
        called["headers"] = headers
        called["timeout"] = timeout
        return resp_obj
    monkeypatch.setattr(ghc.httpx, "post", fake_post)
    token = ghc.installation_token("aid", "priv", 42, "https://apibase")
    assert token == "tkn"
    assert called["url"].endswith("/app/installations/42/access_tokens")
    assert called["headers"]["Authorization"].startswith("Bearer ")
    assert called["timeout"] == 30
    resp_obj.raise_for_status.assert_called_once()
    resp_obj.json.assert_called_once()

class DummyResp:
    def __init__(self, status_code=200, json_data=None, text_data=None):
        self._json = json_data
        self._text = text_data
        self.status_code = status_code
        self.called = []
    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("fail", request=None, response=self)
    def json(self):
        if callable(self._json):
            return self._json()
        return self._json or {}
    @property
    def text(self):
        return self._text or ""

class DummyHttp:
    def __init__(self):
        self.calls = []
        self.next_response = None
    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if isinstance(self.next_response, Exception):
            raise self.next_response
        return self.next_response

def make_client():
    client = ghc.GitHubClient("TOKEN", "https://api.url")
    http = DummyHttp()
    client._http = http
    return client, http

def test_githubclient_get_pr():
    client, http = make_client()
    dummy_json = {"foo": 1}
    http.next_response = DummyResp(json_data=dummy_json)
    out = client.get_pr("alice", "proj", 33)
    assert out == dummy_json
    assert http.calls[-1][0] == "GET"
    assert http.calls[-1][1] == "/repos/alice/proj/pulls/33"

def test_githubclient_get_pr_diff():
    client, http = make_client()
    http.next_response = DummyResp(text_data="diff-data")
    out = client.get_pr_diff("bob", "r", 1)
    assert out == "diff-data"
    m, url, params = http.calls[-1]
    assert m == "GET"
    assert url == "/repos/bob/r/pulls/1"
    assert "headers" in params
    assert params["headers"]["Accept"].startswith("application/vnd.github.v3.diff")

def test_githubclient_create_review_success(monkeypatch):
    # Patch format_comment so we get deterministic value
    monkeypatch.setattr(ghc, "format_comment", lambda c: f"fc-{c.path}-{c.line}")
    client, http = make_client()
    return_value = {"ok": True}
    http.next_response = DummyResp(json_data=return_value)
    comments = [types.SimpleNamespace(path="a.py", line=1), types.SimpleNamespace(path="b.py", line=2)]
    out = client.create_review("a", "b", 3, "sha1", "mainbody", comments)
    assert out == return_value
    last = http.calls[-1]
    assert last[0] == "POST"
    assert last[1] == "/repos/a/b/pulls/3/reviews"
    body = last[2]["json"]
    assert body["commit_id"] == "sha1"
    assert body["body"] == "mainbody"
    assert body["event"] == "COMMENT"
    assert len(body["comments"]) == 2
    assert body["comments"][0]["side"] == "RIGHT"
    assert body["comments"][0]["body"].startswith("fc-")

def test_githubclient_create_review_fallback(monkeypatch):
    # Simulate raise on first call, success on fallback
    called = {}
    def fake_format_comment(c):
        return f"X{c.path}:{c.line}"
    monkeypatch.setattr(ghc, "format_comment", fake_format_comment)
    client, http = make_client()
    # On first POST, raise 422
    err_resp = DummyResp(status_code=422)
    exc = httpx.HTTPStatusError("fail", request=None, response=err_resp)
    # On fallback, return ok
    resp_ok = DummyResp(json_data={"re":1})
    http.next_response = exc
    def fake_request(method, url, **kw):
        if not called.get("raised"):
            called["raised"] = True
            raise exc
        called["fallback"] = True
        return resp_ok
    http.request = fake_request
    comments = [types.SimpleNamespace(path="a.py", line=5)]
    out = client.create_review("o", "r", 2, "cid", "BODY", comments)
    assert out == {"re":1}
    assert called["raised"]
    assert called["fallback"]

def test_githubclient_create_review_raises_on_422_without_comments(monkeypatch):
    # 422 should raise if comments is empty
    client, http = make_client()
    exc = httpx.HTTPStatusError("fail", request=None, response=DummyResp(status_code=422))
    http.request = lambda *a, **kw: (_ for _ in ()).throw(exc)
    with pytest.raises(httpx.HTTPStatusError):
        client.create_review("x", "y", 1, "sha", "b", [])

def test_githubclient_create_check_run(monkeypatch):
    # Patch Finding, and pass various findings
    class F:
        def __init__(self, path, line, severity, tool, rule, message):
            self.path=path; self.line=line; self.severity=severity; self.tool=tool
            self.rule=rule; self.message=message
    client, http = make_client()
    http.next_response = DummyResp(json_data={"foo":"bar"})
    findings = [F("file.py", 2, "warning", "T", "R", "A mess"),
                F("", 2, "info", "T", "R", "No path"),   # no path, should be skipped
                F("f2.py", 1, "error", "T2", "X", "X1")]
    out = client.create_check_run("me", "repo", "sha", "NAME", "success", "Title", "summary", findings)
    assert out == {"foo": "bar"}
    call = http.calls[-1]
    j = call[2]["json"]
    assert j["name"] == "NAME"
    assert j["head_sha"] == "sha"
    assert j["conclusion"] == "success"
    anns = j["output"]["annotations"]
    assert len(anns) == 2
    assert anns[0]["path"] == "file.py"
    assert anns[0]["annotation_level"] == "warning"
    assert anns[1]["annotation_level"] == "failure"
    assert anns[1]["title"] == "T2:X"

def test_githubclient_create_check_run_limit(monkeypatch):
    # Should only send up to 50 annotations
    class F:
        def __init__(self, path, line, severity, tool, rule, message):
            self.path=path; self.line=line; self.severity=severity; self.tool=tool
            self.rule=rule; self.message=message
    client, http = make_client()
    http.next_response = DummyResp(json_data={"zz":1})
    findings = [F(f"p{i}.py", i+1, "info", "T", "R", f"msg{i}") for i in range(80)]
    out = client.create_check_run("org", "r", "sha1", "NAME", "neutral", "TIT", "SUM", findings)
    j = http.calls[-1][2]["json"]
    assert len(j["output"]["annotations"]) == 50

def test_githubclient_comment():
    client, http = make_client()
    http.next_response = DummyResp(json_data={"cid":3})
    out = client.comment("X", "Y", 9, "body")
    assert out == {"cid":3}
    call = http.calls[-1]
    assert call[0] == "POST" and "/issues/9/comments" in call[1]
    assert call[2]["json"]["body"] == "body"

def test_githubclient_paginate_break_on_empty():
    client, http = make_client()
    # Will return [{x}], then [], should break
    batches = [[{"id":1}], []]
    def respjson():
        return batches.pop(0)
    http.next_response = DummyResp(json_data=respjson)
    out = client.paginate("/something")
    assert out == [{"id":1}]

def test_githubclient_paginate_maxpages():
    """Test that only results from first page are returned if length < per_page."""
    client, http = make_client()
    # Always return 2 items, so after max_pages should stop
    count = [0]
    def respjson():
        count[0] += 1
        # Always return two items, but the paginate function will break on first loop, because 2 < 100
        return [{"id":count[0]}, {"id":-count[0]}]
    http.next_response = DummyResp(json_data=respjson)
    out = client.paginate("/test", max_pages=4)
    # By implementation, only first page is returned, since less than per_page=100 is returned at first call
    assert len(out) == 2
    assert out[0]["id"] == 1 and out[1]["id"] == -1
    # Make sure it did only one page (since break condition triggers)
    assert len([c for c in http.calls if c[0]=="GET"]) == 1

def test_checkout_pr(tmp_path, monkeypatch):
    called = []
    def fake_run(cmd, cwd, check, capture_output):
        called.append((cmd, cwd, check, capture_output))
        class DummyProc: pass
        return DummyProc()
    monkeypatch.setattr(ghc.subprocess, "run", fake_run)
    dest = tmp_path / "dir"
    ret = ghc.checkout_pr("A", "B", 531, "toktok", dest, host="hub.site")
    assert ret == dest
    assert dest.exists() and dest.is_dir()
    # Four git invocations
    cmds = [x[0] for x in called]
    assert cmds[0][:2] == ["git", "init"]
    # The second command must be ['git', 'remote', 'add', 'origin', URL]
    assert cmds[1][:4] == ["git", "remote", "add", "origin"]
    assert cmds[1][4] == "https://x-access-token:toktok@hub.site/A/B.git"
    assert "pull/531/head" in cmds[2][-1]
    assert cmds[3][1] == "checkout"
    # Directory creation was done
    assert dest.exists()
