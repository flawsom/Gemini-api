# 🤖 AionUI & Agentic Platforms

> Point **any** OpenAI-compatible client at the server and it works — no
> custom code. This guide covers **AionUI** specifically and every other
> agentic platform that speaks the OpenAI wire protocol.

---

## 1 · Start the server first

```bat
manage.bat start        & rem  silent background server + watchdog (Windows)
start_server.bat        & rem  double-click launcher (auto-closes itself)
python gemini_web2api.py & rem  or run it in a terminal
```

The OpenAI-compatible surface is **`http://127.0.0.1:8081/v1`** — `/v1/models`,
`/v1/chat/completions`, `/v1/responses` and `/v1beta` are all served there.

> 💡 The server listens on `0.0.0.0` by default, so `http://<your-lan-ip>:8081/v1`
> also works from another machine on your network. Set `api_keys` first if you
> do that (see `config.json`).

---

## 2 · AionUI provider settings

In AionUI, add a **custom OpenAI-compatible provider** with these values:

| Setting | Value |
|---|---|
| Provider type | OpenAI-compatible / Custom API |
| **API Base URL** | `http://127.0.0.1:8081/v1` |
| **API Key** | `sk-gemini` (whatever is first in `config.json` → `api_keys`) |
| Model (first one) | `gemini-3.6-flash` |
| Model list | See the model table below |
| Streaming | ✅ on (SSE is fully supported) |
| Function calling / Tools | ✅ supported |
| Vision / images | ✅ supported (`image_url` content parts) |

Hit **Test connection** — the server answers with `{"object":"list",...}`
from `/v1/models`. If the key is wrong you get `401 invalid api key`.

> 🖼 **Image requests use the browser bridge.** Google rejects uploaded images
> from exported-cookie sessions (`BardErrorInfo 1100`), so image requests are
> processed in your **real signed-in browser session** and the answer streams
> back to AionUI. With `image_bridge: "cdp"` (or `"auto"` and the browser
> closed) the server drives your browser profile itself — **no extension
> needed**. When the browser is open, `"auto"` uses the **Gemini Cookie Sync**
> extension (minimized window, closes only that window) — install it once
> from `chrome://extensions` → Developer mode → Load unpacked →
> `gemini-cookie-sync-extension/`. Keep `image_mode` on `"auto"`/`"browser"`
> and images just work — no extra setup in AionUI.

> The key is what **you** configure: edit `api_keys` in `config.json` and use
> that instead of `sk-gemini`. Empty `api_keys` = no auth (open access).

### Models

| ID | Notes |
|---|---|
| `gemini-3.6-flash` | default · fast all-rounder |
| `gemini-3.6-pro` | higher quality |
| `gemini-3.6-pro-thinking` | deep reasoning, ~20k-char output |
| `gemini-flash-lite` | lightest, fastest |
| `gemini-auto` | auto-selected |
| `gemini-3.5-flash` | alias → 3.6 backend |
| `gemini-3.6-flash@think=2` | append `@think=0..4` for thinking depth |

The full list (with descriptions) is served at `GET /v1/models`.

---

## 3 · Input compatibility matrix

Every row below is covered by the test suite and verified against a live server:

| Input | `/v1/chat/completions` | `/v1/responses` | `/v1beta …:generateContent` |
|---|---|---|---|
| Plain text | ✅ | ✅ | ✅ |
| SSE streaming | ✅ token-by-token + `[DONE]` | ✅ full event sequence | ✅ chunked + `STOP` |
| `stream_options.include_usage` | ✅ usage chunk before `[DONE]` | ✅ usage in `response.completed` | — |
| Tool calling (`tools`) | ✅ | ✅ | ✅ (`toolConfig`) |
| `tool_choice` auto/required/none/`{fn}` | ✅ (with retry) | ✅ | ✅ (`mode`) |
| Parallel tool calls | ✅ | ✅ | ✅ |
| Tool-result round-trip (`role:tool` / `function_call_output`) | ✅ | ✅ | ✅ (`functionResponse`) |
| System prompt / `instructions` | ✅ | ✅ | ✅ (`systemInstruction`) |
| `response_format: json_object` / `json_schema` | ✅ soft JSON mode | — | — |
| Images (`image_url` or base64 `data:`) | ✅ | — | ✅ (`inlineData`) |
| Model aliases + `@think=N` | ✅ | ✅ | ✅ |
| Auth: `Bearer` / `x-api-key` / `?key=` | ✅ | ✅ | ✅ |
| CORS preflight (`OPTIONS`) | ✅ `*` | ✅ | ✅ |

### Accepted-and-ignored inputs

These are read but not enforced (the Gemini web bridge can't control them —
ignoring them is standard and harmless):

- `max_tokens` / `max_completion_tokens` — the model may answer longer than
  requested; no error is raised.
- `temperature`, `top_p`, `presence_penalty`, `frequency_penalty`, `seed`,
  `stop`, `n`, `logprobs`, `user`, `metadata` — silently ignored.

---

## 4 · Other agentic platforms

Same two fields everywhere — **base URL** `http://127.0.0.1:8081/v1`,
**API key** `sk-gemini`:

- **OpenAI SDK / LangChain / LlamaIndex** — pass `base_url` + `api_key`.
- **Claude Code / Codex CLI** — `OPENAI_BASE_URL` (Codex: `OPENAI_API_BASE`)
  + `OPENAI_API_KEY`; Codex uses `/v1/responses`, which is implemented.
- **Gemini CLI** — `gemini --api-base http://127.0.0.1:8081` (native `/v1beta`).
- **Cursor / Windsurf / Continue** — "OpenAI-compatible" provider, same base URL.
- **Dify / n8n / Flowise** — OpenAI-compatible LLM node, same base URL.

---

## 5 · If something fails

| Symptom | Cause → fix |
|---|---|
| `405 Method Not Allowed` | Google rotated the build label → enable `auto_update_bl: true` in `config.json` (it self-heals), or run `manage.bat restart`. |
| `429 rate limited` | Start your proxy (e.g. Clash on `7890`) — the server auto-falls back to `proxy_fallbacks`. |
| `401 invalid api key` | `api_keys` mismatch — use the exact key from your `config.json`. |
| Empty/stale responses | Cookies expired → the extension or `manage.bat cookies` refreshes them; sign in once. |
| No model list in AionUI | Confirm `http://127.0.0.1:8081/v1/models` returns JSON with `Authorization: Bearer <key>` (or check the server is running). |

The server's `GET /` health endpoint shows cookie age, build label, 405 count
and the current proxy plan at a glance.
