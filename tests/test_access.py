from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from agent import access as A
from agent.main import write_env_token

BASE = "http://localhost:3000"
REST = f"{BASE}/signalk/v1/api/vessels/self"


class FakeHTTP:
    """Stands in for urllib, recording what was asked for."""

    def __init__(self, replies: list[Any]) -> None:
        self.replies = list(replies)
        self.calls: list[str] = []

    def __call__(self, request: Any, timeout: float = 0) -> Any:
        url = request.full_url if hasattr(request, "full_url") else str(request)
        self.calls.append(url)
        reply = self.replies.pop(0) if self.replies else {}
        if isinstance(reply, Exception):
            raise reply
        return _Response(reply)


class _Response:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(REST, code, "nope", {}, None)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def patch_urlopen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(A.json, "load", lambda response: json.loads(response.read()))


# ------------------------------------------------------------------- probe --


def test_a_readable_server_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(A.urllib.request, "urlopen", FakeHTTP([{"latitude": 36.8}]))

    probe = A.check_read_access(REST, {})

    assert probe.ok and not probe.unauthorized


def test_no_fix_yet_is_still_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    """404 means the server answered: it just has no position. That is fine."""
    monkeypatch.setattr(A.urllib.request, "urlopen", FakeHTTP([http_error(404)]))

    assert A.check_read_access(REST, {}).ok


def test_401_is_the_case_that_matters(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blind agent must be loud: this is what the startup warning keys on."""
    monkeypatch.setattr(A.urllib.request, "urlopen", FakeHTTP([http_error(401)]))

    probe = A.check_read_access(REST, {})

    assert probe.unauthorized and not probe.ok


def test_an_unreachable_server_is_not_an_auth_problem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        A.urllib.request, "urlopen", FakeHTTP([urllib.error.URLError("connection refused")])
    )

    probe = A.check_read_access(REST, {})

    assert not probe.ok and not probe.unauthorized


# ----------------------------------------------------------------- request --


def test_an_approved_request_returns_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeHTTP(
        [
            {"state": "PENDING", "href": "/signalk/v1/requests/abc"},
            {"state": "PENDING"},
            {
                "state": "COMPLETED",
                "accessRequest": {"permission": "APPROVED", "token": "tok-123"},
            },
        ]
    )
    monkeypatch.setattr(A.urllib.request, "urlopen", fake)

    token, detail = A.request_token(BASE, sleep=lambda _s: None, now=_ticker())

    assert token == "tok-123"
    assert "/signalk/v1/access/requests" in fake.calls[0]
    assert detail


def test_a_denied_request_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        A.urllib.request,
        "urlopen",
        FakeHTTP(
            [
                {"state": "PENDING", "href": "/signalk/v1/requests/abc"},
                {"state": "COMPLETED", "accessRequest": {"permission": "DENIED"}},
            ]
        ),
    )

    token, detail = A.request_token(BASE, sleep=lambda _s: None, now=_ticker())

    assert token is None
    assert "denied" in detail


def test_security_turned_off_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """404 on the request endpoint means no security, so no token is needed."""
    monkeypatch.setattr(A.urllib.request, "urlopen", FakeHTTP([http_error(404)]))

    token, detail = A.request_token(BASE, sleep=lambda _s: None, now=_ticker())

    assert token is None
    assert "security turned off" in detail


def test_nobody_approves_it_and_we_give_up(monkeypatch: pytest.MonkeyPatch) -> None:
    replies: list[Any] = [{"state": "PENDING", "href": "/signalk/v1/requests/abc"}]
    replies += [{"state": "PENDING"}] * 50
    monkeypatch.setattr(A.urllib.request, "urlopen", FakeHTTP(replies))

    token, detail = A.request_token(BASE, timeout=9, sleep=lambda _s: None, now=_ticker())

    assert token is None
    assert "in time" in detail


def test_the_client_id_is_stable() -> None:
    """Asking twice must not queue a second request for the same boat."""
    assert A.client_id("boat-pi") == A.client_id("boat-pi")
    assert A.client_id("boat-pi") != A.client_id("boat-test")


def _ticker():
    """A clock that advances a second per call, so timeouts are testable."""
    state = {"t": 0.0}

    def now() -> float:
        state["t"] += 1.0
        return state["t"]

    return now


# --------------------------------------------------------------------- env --


def test_the_token_is_written_to_env(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("SIGNAL_ACCOUNT=+301\n", encoding="utf-8")

    assert write_env_token("tok-123", env)

    body = env.read_text(encoding="utf-8")
    assert "SIGNAL_ACCOUNT=+301" in body
    assert "SIGNALK_TOKEN=tok-123" in body


def test_an_old_token_is_replaced_not_duplicated(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("SIGNALK_TOKEN=old\nSIGNAL_ACCOUNT=+301\n", encoding="utf-8")

    write_env_token("new", env)

    body = env.read_text(encoding="utf-8")
    assert body.count("SIGNALK_TOKEN=") == 1
    assert "SIGNALK_TOKEN=new" in body
    assert "old" not in body


def test_a_missing_env_file_is_created_private(tmp_path: Path) -> None:
    env = tmp_path / ".env"

    assert write_env_token("tok", env)

    assert env.read_text(encoding="utf-8").strip() == "SIGNALK_TOKEN=tok"
    assert oct(env.stat().st_mode)[-3:] == "600"


def test_a_filesystem_that_cannot_chmod_still_saves_the_token(tmp_path, monkeypatch) -> None:
    """Found live: the repo on a gvfs SMB mount, where chmod raises ENOTSUP.

    The token was on disk and the agent said it had failed, which sends the
    crew off to paste by hand into a file that already has it. Tightening the
    permissions is a separate question from saving the token.
    """
    env = tmp_path / ".env"

    def refuse(self, mode):
        raise OSError(95, "Operation not supported")

    monkeypatch.setattr(Path, "chmod", refuse)

    assert write_env_token("tok-123", env) is True
    assert "SIGNALK_TOKEN=tok-123" in env.read_text(encoding="utf-8")


def test_a_write_that_really_fails_still_reports_failure(tmp_path, monkeypatch) -> None:
    env = tmp_path / ".env"

    def refuse(self, *args, **kwargs):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(Path, "write_text", refuse)

    assert write_env_token("tok-123", env) is False
    assert not env.exists()
