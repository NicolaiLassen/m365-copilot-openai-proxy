# Using the proxy with Codex

You can point the [Codex CLI](https://github.com/openai/codex) at this proxy as a
custom OpenAI-compatible provider.

## Caveat first

Codex is an *agentic* coding tool: it drives a model through **tool / function
calls** (apply_patch, shell, etc.). This proxy does **not** implement
tool/function calling, because the M365 Copilot web API behind it does not
expose that protocol. So Codex will connect and answer plain prompts, but its
agent loop (reading files, editing code, running commands) will not work.

Treat this as "ask Copilot a question from inside Codex", not "let Codex edit
your repo".

## 1. Start the proxy

```sh
cd m365-copilot-proxy
python -m m365_copilot_openai_proxy serve
```

Leave it running on `http://127.0.0.1:8000`.

## 2. Configure Codex (current Rust `codex` CLI)

Edit `~/.codex/config.toml`:

```toml
model = "m365-copilot"
model_provider = "m365copilot"

[model_providers.m365copilot]
name = "M365 Copilot (local proxy)"
base_url = "http://127.0.0.1:8000/v1"
env_key = "M365_PROXY_KEY"   # name of an env var; value is a dummy
wire_api = "chat"             # proxy supports chat completions (and /responses)
```

Codex requires the key env var to be present, so set a dummy value:

```sh
export M365_PROXY_KEY=dummy
```

Then run:

```sh
codex
```

Or override per-run without editing the file:

```sh
codex -c model_provider=m365copilot -c model=m365-copilot
```

## 3. Older Node `@openai/codex`

That version honors env vars directly:

```sh
export OPENAI_BASE_URL=http://127.0.0.1:8000/v1
export OPENAI_API_KEY=dummy
codex
```

## Optional: conversation memory

By default each request is stateless on the Copilot side. To keep one Copilot
conversation across turns, use the persistent model alias - just change the
model in the config:

```toml
model = "m365-copilot:persist"
```

The `:persist` suffix maps to a single server-side Copilot session. See the
[main README](README.md#persistent-sessions) for the header-based alternative.
