# Microsoft 365 Copilot OpenAI Proxy

Use Microsoft 365 Copilot through OpenAI-compatible clients, local scripts, and coding tools.

This project runs a local proxy that talks to the same `substrate.office.com` WebSocket API used by the M365 Copilot web UI, then exposes it as OpenAI-style HTTP endpoints.

No Azure app registration. No admin consent. Sign in with your normal M365 Copilot browser session.

## Zero Dependencies

This is a standard-library-only build: it has **no third-party packages** and needs **nothing installed** (no `pip install`, no `uv sync`). It runs on any stock Python 3.11+, which makes it usable on a locked-down corporate machine. Internally it replaces the original stack with the standard library:

| Original dependency | Replaced with (stdlib) |
|---|---|
| `fastapi` + `uvicorn` | `http.server` (`server.py`, `app.py`) |
| `httpx` | `urllib.request` |
| `pydantic` / `pydantic-settings` | `dataclasses` + manual `.env` parsing |
| `websockets` | a minimal RFC 6455 client over `socket` + `ssl` (`wsclient.py`) |

The only non-stdlib package referenced anywhere is `pytest`, and that is **optional** — it is used solely to run the test suite, never by the proxy itself.

## Quick Start

From this folder:

```powershell
python -m m365_copilot_openai_proxy serve
```

The server starts at:

```text
http://127.0.0.1:8000
```

On first run, the proxy opens a dedicated Edge window. Sign in to M365 Copilot there once. The proxy will capture the required Substrate token and write it to `.env`.

The dedicated Edge profile is stored at:

```text
%USERPROFILE%\.m365-copilot-openai-proxy\edge-profile
```

If startup says it is waiting for a token, click the Copilot message box and type one character. You do not need to send the message.

> The interactive `[q] quit / [r] refresh` server console uses `msvcrt`, which exists only on Windows. On macOS/Linux the server still runs; quit it with `Ctrl-C`.

## Test It

```powershell
$body = @{
  model = "m365-copilot"
  messages = @(
    @{ role = "user"; content = "Say hello in one short sentence." }
  )
} | ConvertTo-Json -Depth 10

$r = Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/v1/chat/completions" `
  -ContentType "application/json" `
  -Body $body

$r.choices[0].message.content
```

## Connect A Client

Use these settings for any OpenAI-compatible client:

| Setting | Value |
|---|---|
| Base URL | `http://127.0.0.1:8000/v1` |
| API Key | `dummy` |
| Model | `m365-copilot` |
| Persistent model | `m365-copilot:persist` |

## Persistent Sessions

By default, requests are stateless from the Copilot side.

To reuse the same Copilot conversation across turns, send a stable header:

```http
X-M365-Session-Id: my-work-session
```

Or use the model suffix:

```text
m365-copilot:persist
```

Header mode is better when your client supports custom headers, because each workspace or coding-agent session can choose its own id. If your client only lets you change the model name, use `m365-copilot:persist`.

If a client uses `m365-copilot:persist` without sending a `user` field, all requests share one default persistent session until the proxy restarts.

## Token Refresh

M365 Copilot browser tokens usually expire in about 1 hour. The proxy refreshes them from the dedicated signed-in Edge window.

Auto-refresh is on by default:

```powershell
python -m m365_copilot_openai_proxy serve
```

Useful controls:

```powershell
python -m m365_copilot_openai_proxy serve --refresh-before-seconds 300
python -m m365_copilot_openai_proxy serve --no-auto-refresh
python -m m365_copilot_openai_proxy serve --no-capture-on-start
python -m m365_copilot_openai_proxy serve --no-launch-edge
```

On Windows you can also press `r` in the server console to refresh the token manually.

### Manual Fallback

```powershell
python -m m365_copilot_openai_proxy set-token
```

Then paste a fresh Substrate WebSocket URL:

1. Open the signed-in M365 Copilot Edge window.
2. Open DevTools (`F12`) -> **Network** tab.
3. Filter by `substrate`.
4. Click the WebSocket entry.
5. Go to **Headers** -> right-click the **Request URL** -> **Copy link address**.
6. Paste it into the terminal.

The command extracts `access_token` automatically and writes it to `.env`.

## Token Health

```powershell
Invoke-RestMethod http://127.0.0.1:8000/healthz
Invoke-RestMethod http://127.0.0.1:8000/v1/token/status
```

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /healthz` | Service health plus token status |
| `GET /v1/token/status` | Token validity, expiry time, and seconds remaining |
| `GET /v1/models` | OpenAI-compatible model list |
| `POST /v1/chat/completions` | OpenAI Chat Completions, streaming supported |
| `POST /v1/responses` | OpenAI Responses API, streaming supported |
| `POST /v1/messages` | Anthropic Messages API style endpoint |

## Running The Tests

The proxy itself needs no dependencies, but the tests use `pytest`. If you have it available:

```powershell
python -m pytest
```

If `pytest` cannot be installed on your machine, that is fine: it only affects running the test suite, not the proxy.

## Security Notes

- The proxy listens on `127.0.0.1` by default.
- The browser token is stored locally in `.env`.
- `.env`, `.venv/`, and Python cache files are ignored by Git.
- The proxy does not send your token to any external service besides Microsoft 365 Copilot's own `substrate.office.com` endpoint.
- Anyone who can read your `.env` can use the token until it expires. Treat it like a secret.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `M365_ACCESS_TOKEN` | optional at startup | Browser WebSocket token. If missing, startup capture can fill `.env`. |
| `M365_TIME_ZONE` | `Asia/Tokyo` | Optional. Time zone sent to Copilot. |
| `M365_MODEL_ALIAS` | `m365-copilot` | Optional. Model name returned by `/v1/models`. |

## Limitations

- This is an unofficial local proxy over the browser-facing M365 Copilot API.
- Token refresh depends on a signed-in Edge profile.
- Tool calls are not supported.
- Token usage numbers are placeholders.
- System prompts and prior conversation history are translated into plain text context.

## License

Apache License 2.0. See [LICENSE](LICENSE).

Original project: <https://github.com/kuchris/m365-copilot-openai-proxy>. This folder is a standard-library-only port of it.

## Token Automation Details

See [TOKEN_REFRESH.md](TOKEN_REFRESH.md) for the deeper Edge CDP refresh notes.
