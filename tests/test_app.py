from __future__ import annotations

import base64
import json
import time

from m365_copilot_openai_proxy.app import create_app
from m365_copilot_openai_proxy.cli import (
    _find_m365_page,
    _is_substrate_token,
    _needs_substrate_token,
    _read_token,
    _seconds_remaining,
    _write_token,
)
from m365_copilot_openai_proxy.config import Settings
from m365_copilot_openai_proxy.session_store import PersistentSessionStore
from m365_copilot_openai_proxy.substrate_client import (
    SubstrateCopilotClient,
    SubstrateCopilotError,
)


# --- a tiny in-process test client (replaces fastapi.testclient) -----------

class _Resp:
    def __init__(self, response):
        self.status_code = response.status
        self._response = response

    def json(self):
        return json.loads(self._response.body)

    @property
    def text(self) -> str:
        if self._response.stream is not None:
            return "".join(self._response.stream)
        return self._response.body.decode("utf-8")


class ProxyClient:
    def __init__(self, app):
        self.app = app

    @staticmethod
    def _headers(headers):
        return {k.lower(): v for k, v in (headers or {}).items()}

    def get(self, path, headers=None):
        return _Resp(self.app.handle("GET", path, self._headers(headers), b""))

    def post(self, path, json=None, headers=None):
        body = (json_dumps(json)).encode("utf-8") if json is not None else b""
        return _Resp(self.app.handle("POST", path, self._headers(headers), body))


def json_dumps(obj) -> str:
    import json as _json

    return _json.dumps(obj)


# --- synchronous fakes -----------------------------------------------------

class FakeCopilotClient:
    def __init__(self):
        self.calls: list[tuple[str, list[str]]] = []
        self.sessions: list[object | None] = []

    def chat(self, prompt, additional_context, session=None):
        self.calls.append((prompt, additional_context))
        self.sessions.append(session)
        return "copilot reply"

    def chat_stream(self, prompt, additional_context, session=None):
        self.calls.append((prompt, additional_context))
        self.sessions.append(session)
        yield "hello"
        yield " world"


class FailingStreamCopilotClient(FakeCopilotClient):
    def chat_stream(self, prompt, additional_context, session=None):
        self.calls.append((prompt, additional_context))
        self.sessions.append(session)
        raise SubstrateCopilotError("upstream broke")
        yield ""  # pragma: no cover


def build_client(fake: FakeCopilotClient) -> ProxyClient:
    settings = Settings(M365_ACCESS_TOKEN="fake-token")
    app = create_app(settings=settings, copilot_client_factory=lambda: fake)
    return ProxyClient(app)


def make_jwt(exp: int, aud: str = "https://substrate.office.com/sydney") -> str:
    def encode(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode({'aud': aud, 'exp': exp, 'oid': 'oid', 'tid': 'tid'})}.sig"


# --- tests -----------------------------------------------------------------

def test_models_endpoint() -> None:
    client = build_client(FakeCopilotClient())
    response = client.get("/v1/models")
    assert response.status_code == 200
    assert response.json()["data"][0]["id"] == "m365-copilot"


def test_app_starts_without_token_for_startup_capture() -> None:
    app = create_app(settings=Settings(M365_ACCESS_TOKEN=""))
    client = ProxyClient(app)
    response = client.get("/v1/token/status")
    assert response.status_code == 200
    assert response.json()["valid"] is False


def test_token_status_reports_expiry() -> None:
    settings = Settings(M365_ACCESS_TOKEN=make_jwt(int(time.time()) + 3600))
    client = ProxyClient(create_app(settings=settings, copilot_client_factory=lambda: FakeCopilotClient()))
    response = client.get("/v1/token/status")
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert body["expires_at"]
    assert body["seconds_remaining"] > 0


def test_healthz_includes_token_remaining_time() -> None:
    settings = Settings(M365_ACCESS_TOKEN=make_jwt(int(time.time()) + 3600))
    client = ProxyClient(create_app(settings=settings, copilot_client_factory=lambda: FakeCopilotClient()))
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["token"]["valid"] is True
    assert body["token"]["seconds_remaining"] > 0


def test_token_status_rejects_non_substrate_token() -> None:
    settings = Settings(M365_ACCESS_TOKEN=make_jwt(int(time.time()) + 3600, aud="394866fc-eedb"))
    client = ProxyClient(create_app(settings=settings, copilot_client_factory=lambda: FakeCopilotClient()))
    response = client.get("/v1/token/status")
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert body["error"] == "Access token is not a substrate.office.com token."


def test_substrate_client_rejects_non_substrate_token() -> None:
    token = make_jwt(int(time.time()) + 3600, aud="394866fc-eedb")
    try:
        SubstrateCopilotClient(token)
    except SubstrateCopilotError as exc:
        assert "not a substrate.office.com token" in str(exc)
    else:
        raise AssertionError("SubstrateCopilotClient accepted a non-Substrate token")


def test_default_client_factory_reloads_token_from_env(tmp_path, monkeypatch) -> None:
    first_token = make_jwt(int(time.time()) + 3600)
    second_token = make_jwt(int(time.time()) + 7200)
    env_path = tmp_path / ".env"
    env_path.write_text(f"M365_ACCESS_TOKEN={first_token}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    seen_tokens: list[str] = []

    class RecordingCopilotClient(FakeCopilotClient):
        def __init__(self, access_token: str, _time_zone: str, _proxy=None):
            super().__init__()
            seen_tokens.append(access_token)

    monkeypatch.setattr(
        "m365_copilot_openai_proxy.app.SubstrateCopilotClient",
        RecordingCopilotClient,
    )
    settings = Settings(M365_ACCESS_TOKEN=first_token)
    client = ProxyClient(create_app(settings=settings))

    time.sleep(0.01)
    env_path.write_text(f"M365_ACCESS_TOKEN={second_token}\n", encoding="utf-8")
    response = client.post(
        "/v1/chat/completions",
        json={"model": "ignored", "messages": [{"role": "user", "content": "Hello"}]},
    )

    assert response.status_code == 200
    assert seen_tokens == [second_token]


def test_proxy_arg_is_passed_to_client(monkeypatch) -> None:
    seen: dict = {}

    class RecordingCopilotClient(FakeCopilotClient):
        def __init__(self, access_token, time_zone, proxy=None):
            super().__init__()
            seen["proxy"] = proxy

    monkeypatch.setattr(
        "m365_copilot_openai_proxy.app.SubstrateCopilotClient",
        RecordingCopilotClient,
    )
    app = create_app(settings=Settings(M365_ACCESS_TOKEN="t"), proxy="http://corp:8080")
    client = ProxyClient(app)
    response = client.post(
        "/v1/chat/completions",
        json={"model": "ignored", "messages": [{"role": "user", "content": "Hi"}]},
    )
    assert response.status_code == 200
    assert seen["proxy"] == "http://corp:8080"


def test_cli_reads_current_token_from_env(tmp_path, monkeypatch) -> None:
    token = make_jwt(int(time.time()) + 3600)
    (tmp_path / ".env").write_text(f"M365_ACCESS_TOKEN='{token}'\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert _read_token() == token


def test_cli_write_token_ignores_commented_token_line(tmp_path, monkeypatch) -> None:
    token = make_jwt(int(time.time()) + 3600)
    env_path = tmp_path / ".env"
    env_path.write_text("# M365_ACCESS_TOKEN=old\nOTHER=value\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _write_token(token)
    assert _read_token() == token
    assert env_path.read_text(encoding="utf-8").count("M365_ACCESS_TOKEN=") == 2


def test_cli_seconds_remaining_uses_jwt_exp() -> None:
    remaining = _seconds_remaining(make_jwt(int(time.time()) + 3600))
    assert 0 < remaining <= 3600


def test_cli_accepts_only_substrate_tokens() -> None:
    assert _is_substrate_token(make_jwt(int(time.time()) + 3600))
    assert not _is_substrate_token(make_jwt(int(time.time()) + 3600, aud="394866fc-eedb"))


def test_cli_knows_when_startup_capture_is_needed() -> None:
    assert _needs_substrate_token(None)
    assert _needs_substrate_token(make_jwt(int(time.time()) + 3600, aud="394866fc-eedb"))
    assert _needs_substrate_token(make_jwt(int(time.time()) - 1))
    assert not _needs_substrate_token(make_jwt(int(time.time()) + 3600))


def test_cli_startup_refresh_can_do_full_fallback(monkeypatch) -> None:
    from m365_copilot_openai_proxy.cli import _startup_capture_loop

    seen_allow_nudge: list[bool] = []
    capture_called = False

    def fake_refresh(_port, *, allow_nudge=True):
        seen_allow_nudge.append(allow_nudge)
        return allow_nudge

    def fake_capture(_port, _timeout):
        nonlocal capture_called
        capture_called = True
        return False

    monkeypatch.setattr("m365_copilot_openai_proxy.cli._wait_for_m365_page", lambda _p, _t: True)
    monkeypatch.setattr("m365_copilot_openai_proxy.cli._try_auto_refresh", fake_refresh)
    monkeypatch.setattr("m365_copilot_openai_proxy.cli._capture_token_to_env", fake_capture)
    monkeypatch.setattr("m365_copilot_openai_proxy.cli.time.sleep", lambda _s: None)

    _startup_capture_loop(9222, timeout_seconds=1)

    assert seen_allow_nudge[-1] is True
    assert capture_called is False


def test_cli_startup_refresh_waits_for_m365_page(monkeypatch) -> None:
    from m365_copilot_openai_proxy.cli import _startup_capture_loop

    calls: list[str] = []

    monkeypatch.setattr(
        "m365_copilot_openai_proxy.cli._wait_for_m365_page",
        lambda _p, _t: calls.append("wait") or True,
    )
    monkeypatch.setattr(
        "m365_copilot_openai_proxy.cli._try_auto_refresh",
        lambda _p, **_k: calls.append("refresh") or True,
    )

    _startup_capture_loop(9222, timeout_seconds=1)
    assert calls == ["wait", "refresh"]


def test_cli_finds_real_m365_page_not_devtools() -> None:
    tabs = [
        {"type": "page", "url": "devtools://devtools/bundled/devtools_app.html?remoteBase=https://m365.cloud.microsoft/chat"},
        {"type": "page", "url": "https://m365.cloud.microsoft/chat"},
    ]
    assert _find_m365_page(tabs) == tabs[1]


def test_openai_chat_completion_translates_history() -> None:
    fake = FakeCopilotClient()
    client = build_client(fake)
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "ignored",
            "messages": [
                {"role": "system", "content": "Be concise."},
                {"role": "user", "content": "First question"},
                {"role": "assistant", "content": "First answer"},
                {"role": "user", "content": "Second question"},
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "copilot reply"
    assert fake.calls == [
        (
            "Second question",
            [
                "System instructions:\nBe concise.",
                "Prior conversation transcript:\nUser: First question\nAssistant: First answer",
            ],
        )
    ]
    assert fake.sessions == [None]


def test_openai_persistent_session_header_reuses_session() -> None:
    fake = FakeCopilotClient()
    client = build_client(fake)
    body = {"model": "m365-copilot", "messages": [{"role": "user", "content": "Hello"}]}
    first = client.post("/v1/chat/completions", headers={"X-M365-Session-Id": "work"}, json=body)
    second = client.post("/v1/chat/completions", headers={"X-M365-Session-Id": "work"}, json=body)
    assert first.status_code == 200
    assert second.status_code == 200
    assert fake.sessions[0] is fake.sessions[1]
    assert fake.sessions[0] is not None


def test_openai_persistent_model_suffix_uses_user_as_session_key() -> None:
    fake = FakeCopilotClient()
    client = build_client(fake)
    for user in ("alice", "alice", "bob"):
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "m365-copilot:persist",
                "user": user,
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )
        assert response.status_code == 200
    assert fake.sessions[0] is fake.sessions[1]
    assert fake.sessions[0] is not fake.sessions[2]


def test_persistent_session_turn_flags_are_reserved_in_order() -> None:
    session = PersistentSessionStore().get("work")
    first_turn = session.reserve_turn()
    second_turn = session.reserve_turn()
    assert first_turn.conversation_id == second_turn.conversation_id
    assert first_turn.client_session_id == second_turn.client_session_id
    assert first_turn.is_start_of_session is True
    assert second_turn.is_start_of_session is False


def test_openai_streaming_returns_sse() -> None:
    client = build_client(FakeCopilotClient())
    payload = client.post(
        "/v1/chat/completions",
        json={"model": "ignored", "stream": True, "messages": [{"role": "user", "content": "Hello"}]},
    ).text
    assert '"role": "assistant"' in payload
    assert '"content": "hello"' in payload
    assert '"content": " world"' in payload
    assert "data: [DONE]" in payload


def test_openai_streaming_returns_error_event_on_upstream_failure() -> None:
    client = build_client(FailingStreamCopilotClient())
    payload = client.post(
        "/v1/chat/completions",
        json={"model": "ignored", "stream": True, "messages": [{"role": "user", "content": "Hello"}]},
    ).text
    assert '"type": "upstream_error"' in payload
    assert '"message": "upstream broke"' in payload
    assert "data: [DONE]" in payload


def test_responses_streaming_returns_error_event_on_upstream_failure() -> None:
    client = build_client(FailingStreamCopilotClient())
    payload = client.post(
        "/v1/responses",
        json={"model": "ignored", "stream": True, "input": "Hello"},
    ).text
    assert '"type": "error"' in payload
    assert '"message": "upstream broke"' in payload


def test_anthropic_messages_endpoint() -> None:
    client = build_client(FakeCopilotClient())
    response = client.post(
        "/v1/messages",
        json={"model": "ignored", "system": "Be concise.", "messages": [{"role": "user", "content": "Hello"}]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "message"
    assert body["content"][0]["text"] == "copilot reply"


def test_anthropic_streaming_returns_error_event_on_upstream_failure() -> None:
    client = build_client(FailingStreamCopilotClient())
    payload = client.post(
        "/v1/messages",
        json={"model": "ignored", "stream": True, "messages": [{"role": "user", "content": "Hello"}]},
    ).text
    assert "event: error" in payload
    assert '"message": "upstream broke"' in payload


def test_responses_requires_final_user_message() -> None:
    client = build_client(FakeCopilotClient())
    response = client.post(
        "/v1/responses",
        json={
            "model": "ignored",
            "input": [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi"},
            ],
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "The final Responses input message must be a user message."
