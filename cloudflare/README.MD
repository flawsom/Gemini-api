# Gemini Web2API — Cloudflare Workers Deployment Guide

## 📖 Introduction

Gemini Web2API is a serverless proxy running on Cloudflare Workers that converts Google Gemini's web interface into an OpenAI-compatible API. No server, no API key required (optional), works out of the box.

### Core Features

- **Zero-cost deployment**: Runs on the Cloudflare Workers free tier (100k requests/day)
- **Global acceleration**: Automatically deployed to 300+ Cloudflare edge nodes worldwide
- **OpenAI compatible**: Fully supports the `/v1/chat/completions` and `/v1/models` endpoints
- **Typewriter streaming**: True SSE (Server-Sent Events) streaming responses
- **Multi-fingerprint rotation**: 8 browser fingerprints + 6 language preferences rotated randomly to lower detection rates
- **Multi-cookie rotation**: Supports multiple Google account cookies, selected at random
- **Concurrency safe**: Request-level config isolation eliminates config cross-talk under high concurrency
- **Tool calling**: Compatible with the OpenAI Function Calling format

### Use Cases

- Provide a free Gemini API for clients like NextChat, Cherry Studio, ChatBox
- Use as the Gemini backend inside tools like WorkBuddy
- Power AI features for personal projects, research, and small apps

---

## 🚀 Quick Deploy

### Step 1: Log in to Cloudflare

1. Open the [Cloudflare Dashboard](https://dash.cloudflare.com)
2. Log in to your Cloudflare account (free to register)
3. Open **Workers & Pages** in the left menu

### Step 2: Create a Worker

1. Click **Create application** → **Create Worker**
2. Name your Worker (e.g. `api`)
3. Click **Deploy**
4. Click **Edit code**
5. Clear the default code in the editor
6. Paste this project's full code into the editor
7. Click **Save and deploy** in the top right

### Step 3: Get Your Test URL

After deployment, your API endpoint is:

```
https://<worker-name>.<your-account>.workers.dev
```

Example: `https://api.geminai.workers.dev`

### Step 4: Verify the Deployment

Visit the following URL in your browser:

```
https://<worker-name>.workers.dev/health
```

If you see a JSON response similar to this, the deployment succeeded:

```json
{
  "status": "ok",
  "version": "1.5.0-cf-multifingerprint",
  "platform": "Cloudflare Workers",
  "models": ["gemini-3.6-flash", "gemini-3.5-flash", "..."],
  "hasCookie": false,
  "hasSapisid": false
}
```

---

## 🔧 Client Configuration

### NextChat (ChatGPT-Next-Web)

| Setting | Value |
|--------|-------|
| API Type | OpenAI |
| API Base URL | `https://<worker-name>.workers.dev/v1` |
| API Key | `sk-gemini` (default key) |
| Model | `gemini-3.6-flash` |

### Cherry Studio

| Setting | Value |
|--------|-------|
| API Base URL | `https://<worker-name>.workers.dev/v1` |
| API Key | `sk-gemini` |
| Model | `gemini-3.6-flash` |

### ChatBox

| Setting | Value |
|--------|-------|
| API Mode | OpenAI API |
| API Host | `https://<worker-name>.workers.dev` |
| API Path | `/v1/chat/completions` |
| API Key | `sk-gemini` |

### Test with curl

```bash
# Non-streaming request
curl https://<worker-name>.workers.dev/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-gemini" \
  -d '{
    "model": "gemini-3.6-flash",
    "messages": [{"role": "user", "content": "Hello"}],
    "stream": false
  }'

# Streaming request (typewriter effect)
curl -N https://<worker-name>.workers.dev/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-gemini" \
  -d '{
    "model": "gemini-3.6-flash",
    "messages": [{"role": "user", "content": "Tell me a story"}],
    "stream": true
  }'
```

---

## ⚙️ Environment Variables (Optional)

Configure them at Cloudflare Dashboard → Workers → your Worker → Settings → Variables → Environment variables:

### Authentication

| Variable | Description | Example |
|--------|------|--------|
| `COOKIE_STRING` | Gemini cookies, multiple separated by `\|` | `cookie1\| cookie2\| cookie3` |
| `SAPISID` | SAPISID values, multiple separated by `\|` | `sapisid1\| sapisid2\| sapisid3` |
| `API_KEYS` | API key whitelist (JSON array) | `["sk-gemini", "my-key"]` |

### Gemini Configuration

| Variable | Description | Example |
|--------|------|--------|
| `GEMINI_BL` | Gemini build label (update on 405) | `boq_assistant-bard-web-server_20260716.08_p0` |
| `DEFAULT_MODEL` | Default model | `gemini-3.6-flash` |
| `AUTH_USER` | Multi-account index | `0` |

### Performance Tuning

| Variable | Description | Default |
|--------|------|--------|
| `RETRY_ATTEMPTS` | Number of retries | `3` |
| `RETRY_DELAY_SEC` | Retry delay (seconds) | `2` |
| `REQUEST_TIMEOUT_SEC` | Request timeout (seconds) | `28` |
| `FINGERPRINT_JITTER_MS` | Max random delay (milliseconds) | `1500` |
| `RATE_LIMIT_MAX` | Max requests in the rate-limit window | `3000` |
| `RATE_LIMIT_WINDOW` | Rate-limit window (seconds) | `60` |

---

## 🍪 Getting a Gemini Cookie

### Why Do You Need a Cookie?

Anonymous requests are easily rate-limited by Gemini (HTTP 429). A valid cookie can:
- Drastically reduce the chance of being rate-limited
- Improve Pro model routing quality
- Provide a more stable experience

### How to Get One

1. Open Chrome/Edge
2. Visit https://gemini.google.com/app and log in with your Google account
3. Press **F12** to open Developer Tools
4. Open the **Application** tab
5. Select **Cookies** → `https://gemini.google.com` on the left
6. Find these cookies and copy their values:
   - `__Secure-1PSID`
   - `__Secure-3PSID`
   - `SAPISID`
7. Combine them into a full cookie string:
   ```
   __Secure-1PSID=<value>; __Secure-3PSID=<value>; SAPISID=<value>
   ```

### Multiple Accounts

If you have multiple Google accounts, separate multiple cookies with `|`:

```
COOKIE_STRING = "cookie_account1| cookie_account2| cookie_account3"
SAPISID = "sapisid_1| sapisid_2| sapisid_3"
```

Each request picks one cookie at random, drastically lowering the chance of a single account being rate-limited.

---

## 🔄 Updating the BL Version

If you see `HTTP 405: Method Not Allowed`, the Gemini frontend has been updated and you need to sync the build label:

1. Open https://gemini.google.com/app in your browser
2. Press **F12** → **Network** tab
3. Search for `boq_assistant` in any request URL
4. Copy the latest version, e.g.:
   ```
   boq_assistant-bard-web-server_20260730.02_p0
   ```
5. Update the `GEMINI_BL` environment variable or the `geminiBl` config value in the code

---

## 🎭 Multi-Fingerprint Rotation

This program has a built-in browser-fingerprint rotation system. Each request randomly picks a different browser identity:

| Fingerprint type | Pool size | Description |
|---------|--------|------|
| User-Agent | 8 | Weighted random, mimics real browser market share |
| Accept-Language | 6 | Uniform random, mimics users in different regions |
| Sec-Ch-Ua | 3 | Chrome version identity (added only for Chrome UAs) |
| Random delay | 0-1500ms | Random delay before a request, mimics human behavior |

---

## 🛡️ Security Recommendations

1. **Change the default API key**: replace `sk-gemini` in `apiKeys` with your own key
2. **Set rate limits**: tune `RATE_LIMIT_MAX` to your actual usage
3. **Refresh cookies regularly**: Google cookies expire and need periodic replacement
4. **Never share cookies**: a cookie is equivalent to your Google account credentials

---

## ❓ FAQ

### Q: Getting `empty response from server`

**Cause**: NextChat streaming parsing issue.
**Fix**: Make sure you are running the latest code (the SSE format is fixed).

### Q: Getting `HTTP 429: Too Many Requests`

**Cause**: Gemini rate limiting; anonymous requests are limited more strictly.
**Fix**: Configure a valid `COOKIE_STRING` and `SAPISID`.

### Q: Getting `HTTP 405: Method Not Allowed`

**Cause**: The BL version is outdated.
**Fix**: Update the `geminiBl` config (see the "Updating the BL Version" section above).

### Q: Getting `invalid api key`

**Cause**: The client's key is misconfigured.
**Fix**: Check that the client uses the correct API key (default `sk-gemini`).

### Q: Cross-talk in WorkBuddy

**Cause**: Multi-model concurrent requests share the global config.
**Fix**: The current version solves this via request-level config isolation.

---

## 📊 Supported Models

| Model ID | Type | Description |
|---------|------|------|
| `gemini-3.6-flash` | FAST | Latest all-around model |
| `gemini-3.5-flash` | FAST | Alias for 3.6 Flash |
| `gemini-3.5-flash-thinking` | THINKING | Deep thinking mode |
| `gemini-3.1-pro` | PRO | Pro model (needs a cookie) |
| `gemini-auto` | AUTO | Automatic model selection |
| `gemini-3.5-flash-thinking-lite` | DYNAMIC | Adaptive dynamic thinking |
| `gemini-flash-lite` | LITE | Lightweight fast model |

You can override the thinking mode with the `@think=` parameter:
- `gemini-3.6-flash@think=0` — Flash model + deep thinking
- `gemini-3.1-pro@think=4` — Pro model + automatic thinking

---

## 📝 Changelog

| Version | Date | Changes |
|------|------|---------|
| 1.5.0 | 2026-07-31 | Added multi-fingerprint rotation, multi-cookie rotation, random delay |
| 1.4.0 | 2026-07-30 | Fixed concurrent cross-talk, rate-limit memory safety |
| 1.3.0 | 2026-07-29 | Fixed SSE streaming format, NextChat compatibility |
| 1.0.0 | 2026-07-16 | Initial version, ported from gemini-web2api v1.1.0 |

---

## 📄 License

This project is ported from [gemini-web2api](https://github.com/your-repo/gemini-web2api) and follows the original project's open-source license.
