# Security Policy

## ⚠️ What this project holds

This project works with **live Gemini session cookies** (`cookie.txt`). Treat that file like a password:

- Never commit `cookie.txt`, `config.json`, or `cookie.json` (all gitignored).
- Never paste cookies, XSRF tokens, or API keys into issues, PRs, or chats.
- If you believe a session leaked, revoke it immediately in your Google account's security settings and re-sign-in.

## Reporting a vulnerability

Do **not** open a public issue for security problems. Report privately via GitHub's **Security Advisories**:

**[Report a vulnerability](https://github.com/flawsom/Gemini-api/security/advisories/new)**

Please include:

- The affected component (`gemini_web2api.py` / `gemini_web2api/` package / extension / Cloudflare Worker / Docker)
- A minimal reproduction (redact all secrets)
- Impact and suggested fix, if you have one

You'll get an acknowledgment within **72 hours**, and we'll coordinate a disclosure timeline with you.

## Supported versions

| Version | Supported |
|---|---|
| latest `main` | ✅ |
| latest tagged release | ✅ |
| older releases | ❌ — upgrade |

## Safe exposure checklist

Before exposing the server beyond `127.0.0.1`:

- [ ] Set `api_keys` in `config.json` (empty array = open access — not for the internet)
- [ ] Set a distinct `cookie_refresh_key`
- [ ] Prefer a reverse proxy or the [Cloudflare Worker](cloudflare/README.MD) for public deployments
