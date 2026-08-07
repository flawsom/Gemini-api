"""Gemini StreamGenerate protocol implementation with httpx streaming."""
import json
import time
import uuid
import re
import urllib.request
import urllib.parse
import urllib.error
import ssl
import os
import hashlib

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

from .config import CONFIG

_ssl_ctx = None
_cookie_cache = {"str": "", "sapisid": None, "mtime": 0}


def log(msg: str):
    if CONFIG["log_requests"]:
        import sys
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        sys.stderr.flush()


def _get_ssl_ctx():
    global _ssl_ctx
    if _ssl_ctx is None:
        _ssl_ctx = ssl.create_default_context()
    return _ssl_ctx


def _parse_netscape_cookie(content: str) -> tuple:
    """Parse a Netscape HTTP Cookie File export into (cookie_str, sapisid)."""
    pairs = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            pairs[parts[5]] = parts[6]
    cookie_str = "; ".join(f"{k}={v}" for k, v in pairs.items())
    return cookie_str, pairs.get("SAPISID", "")


def _parse_compass_cookie(content: str) -> tuple:
    """Parse Google COMPASS cookie format into (cookie_str, sapisid).

    Format: COMPASS=key1=val1;key2=val2;...;COMPASS=key1=val1;key2=val2;...
    """
    pairs = {}
    # Split by COMPASS= to get each cookie block
    for block in content.split("COMPASS="):
        if not block.strip():
            continue
        # Each block contains semicolon-separated key=value pairs
        for pair in block.split(";"):
            pair = pair.strip()
            if "=" in pair:
                k, v = pair.split("=", 1)
                pairs[k] = v
    cookie_str = "; ".join(f"{k}={v}" for k, v in pairs.items())
    return cookie_str, pairs.get("SAPISID", "")


def load_cookie() -> tuple:
    """Load cookie from file with mtime-based caching.

    Supports four formats:
      - JSON: {"cookie": "SID=...; HSID=...", "sapisid": "..."}
      - Netscape: # Netscape HTTP Cookie File (exported by browser extensions)
      - Single-line: "SID=...; HSID=...; SAPISID=..."
      - COMPASS: Google's COMPASS cookie format (multiple COMPASS= blocks)
    """
    cookie_file = CONFIG.get("cookie_file")
    if not cookie_file or not os.path.exists(cookie_file):
        return "", None
    try:
        mtime = os.path.getmtime(cookie_file)
        if mtime == _cookie_cache["mtime"] and _cookie_cache["str"]:
            return _cookie_cache["str"], _cookie_cache["sapisid"]
        with open(cookie_file, "r") as f:
            content = f.read().strip()
        if content.startswith("{"):
            data = json.loads(content)
            cookie_str = data.get("cookie", "")
            sapisid = data.get("sapisid", "")
        elif content.startswith("# Netscape") or "\n" in content or "\t" in content:
            cookie_str, sapisid = _parse_netscape_cookie(content)
        elif content.startswith("COMPASS="):
            cookie_str, sapisid = _parse_compass_cookie(content)
        else:
            cookie_str = content
            pairs = dict(p.split("=", 1) for p in cookie_str.split("; ") if "=" in p)
            sapisid = pairs.get("SAPISID", "")
        _cookie_cache.update({"str": cookie_str, "sapisid": sapisid or None, "mtime": mtime})
        return cookie_str, sapisid if sapisid else None
    except Exception as e:
        log(f"Cookie load error: {e}")
        return _cookie_cache["str"], _cookie_cache["sapisid"]


def make_sapisidhash(sapisid: str) -> str:
    ts = int(time.time())
    h = hashlib.sha1(f"{ts} {sapisid} https://gemini.google.com".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{h}"


# ─── BL (build label) and XSRF auto-update ──────────────────────────────────
# Google rotates the frontend build label every few days. A stale BL makes
# StreamGenerate return HTTP 405 "Method Not Allowed". These helpers fetch the
# current values from the gemini.google.com/app page so the server self-heals.

_page_cache = {"html": None, "ts": 0}
_bl_state = {"candidates": [], "fetched_ts": 0}
_bl_reject_cache = {}  # {bl: ts} - candidate build labels rejected with 405 by probe_bl
_proxy_state = {"working": None, "ts": 0}
# Consecutive live-request 405s since the last successful generation. Exposed
# via the / health payload so the watchdog can spot "the BL started 405-ing
# repeatedly" (a stale build label, often after cookies went stale) and kick a
# cookie refresh instead of letting the server sit broken.
_bl_405 = {"count": 0, "ts": 0.0}


def _mark_405() -> None:
    """Record one more consecutive HTTP 405 from a live StreamGenerate request."""
    _bl_405["count"] += 1
    _bl_405["ts"] = time.time()


def _mark_405_resolved() -> None:
    """A generation succeeded (or the BL was switched) - reset the 405 streak."""
    _bl_405["count"] = 0

# ─── Cookie auto-refresh flag (see server.py internal endpoints) ───────────
REFRESH_FLAG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookie-refresh.flag")
_refresh_throttle = {"ts": 0}


def refresh_key() -> str:
    """The API key the cookie-refresh tooling must use.

    Resolution: cookie_refresh_key from config.json, else api_keys[0], else the
    default "sk-gemini". The extension learns this via /internal/
    cookie-refresh/config and stores it, so a custom key works out of the box.
    """
    k = CONFIG.get("cookie_refresh_key")
    if k:
        return k
    keys = CONFIG.get("api_keys") or []
    return keys[0] if keys else "sk-gemini"


def cookie_refresh_requested() -> bool:
    """True while a refresh flag exists AND it is younger than 15 minutes.

    The age cap means a stale flag (extension uninstalled, upload failed,
    machine asleep) expires on its own instead of triggering refresh
    windows forever.
    """
    if not os.path.exists(REFRESH_FLAG):
        return False
    try:
        with open(REFRESH_FLAG) as f:
            data = json.load(f)
        if time.time() - float(data.get("requested_at", 0)) > 900:
            clear_cookie_refresh()
            return False
    except (OSError, ValueError, TypeError):
        # Unparseable/empty flag: expire it too, so a corrupt flag cannot
        # keep triggering refresh windows forever. Safe against the mid-write
        # race - Windows refuses to delete a file another process holds open
        # for writing, and the writer re-creates the flag anyway.
        clear_cookie_refresh()
        return False
    return True


def request_cookie_refresh(reason: str = "manual") -> bool:
    try:
        with open(REFRESH_FLAG, "w") as f:
            json.dump({"requested_at": time.time(), "reason": reason}, f)
        log(f"Cookie refresh requested ({reason})")
        return True
    except OSError:
        return False


def clear_cookie_refresh():
    try:
        os.remove(REFRESH_FLAG)
    except OSError:
        pass


def maybe_request_refresh_on_failure(err):
    """When Google rejects auth (401/403), flag a cookie refresh (throttled)."""
    code = getattr(err, "code", 0)
    if code in (401, 403) and CONFIG.get("cookie_file"):
        now = time.time()
        if now - _refresh_throttle["ts"] > 900:
            _refresh_throttle["ts"] = now
            request_cookie_refresh(f"google-http-{code}")


def _proxy_plan() -> list:
    """Ordered proxy choices: configured proxy, then a recently-working fallback, then direct, then the fallback list.

    A recently-working proxy is tried before direct so a rate-limited IP is not
    hammered again; once it expires (~30 min) direct is retried first so the
    server self-heals when Google's cooldown clears.
    """
    plan = []
    if CONFIG.get("proxy"):
        plan.append(CONFIG["proxy"])
    w = _proxy_state.get("working")
    if w and time.time() - _proxy_state.get("ts", 0) < 1800 and w not in plan:
        plan.append(w)
    if None not in plan:
        plan.append(None)  # direct connection
    for p in CONFIG.get("proxy_fallbacks") or []:
        if p and p not in plan:
            plan.append(p)
    return plan


def _proxy_for_attempt(attempt: int):
    """Pick the proxy for retry attempt N (None = direct). Cycles if more attempts than proxies."""
    plan = _proxy_plan()
    if not plan:
        return None
    return plan[attempt % len(plan)]


def _mark_proxy_working(proxy):
    """Remember a fallback proxy that got us past Google's rate limit (30 min)."""
    if proxy and proxy != CONFIG.get("proxy") and _proxy_state.get("working") != proxy:
        _proxy_state["working"] = proxy
        _proxy_state["ts"] = time.time()
        log(f"Proxy {proxy} answered - will prefer it while rate-limited")


def _fetch_page_html() -> str:
    """Fetch https://gemini.google.com/app HTML (cached 5 min, proxy-fallback aware)."""
    now = time.time()
    if _page_cache["html"] and now - _page_cache["ts"] < 300:
        return _page_cache["html"]
    req = urllib.request.Request(
        "https://gemini.google.com/app",
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
    ctx = _get_ssl_ctx()
    plan = _proxy_plan() or [None]
    last_err = None
    for proxy in plan:
        try:
            if proxy:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                    urllib.request.HTTPSHandler(context=ctx))
                resp = opener.open(req, timeout=15)
            else:
                resp = urllib.request.urlopen(req, context=ctx, timeout=15)
            html = resp.read().decode("utf-8", errors="replace")
            if proxy:
                _mark_proxy_working(proxy)
            _page_cache.update({"html": html, "ts": now})
            return html
        except Exception as e:
            last_err = e
            continue
    if last_err:
        raise last_err
    return ""


def _bl_candidates() -> list:
    """All build labels found on the page, newest first (deterministic)."""
    now = time.time()
    if now - _bl_state["fetched_ts"] > 600 or not _bl_state["candidates"]:
        try:
            html = _fetch_page_html()
            found = set(re.findall(r'boq_assistant-bard-web-server_\d+\.\d+_p\d+', html))
            cfb = re.search(r'"cfb2h":"(boq_assistant-bard-web-server_[^"]+)"', html)
            if cfb:
                found.add(cfb.group(1))
            _bl_state["candidates"] = sorted(
                found,
                key=lambda s: [int(x) for x in re.findall(r'\d+', s)],
                reverse=True,
            )
            _bl_state["fetched_ts"] = now
        except Exception as e:
            log(f"BL candidates fetch failed: {e}")
    return _bl_state["candidates"]


def fetch_latest_bl() -> str | None:
    """Return the newest build label found on the Gemini page."""
    cands = _bl_candidates()
    return cands[0] if cands else None


def probe_bl(candidate_bl: str) -> bool | None:
    """Probe a candidate build label with a minimal StreamGenerate request.

    Returns:
      True  - the candidate BL answered (HTTP 200) and is safe to adopt
      False - the candidate BL was rejected with HTTP 405 (stale build)
      None  - outcome ambiguous (429 rate limit, timeout, network error...);
              callers must not block on it

    Rejected candidates are remembered in _bl_reject_cache so a page that
    A/B-serves multiple builds can never make the server flip-flop between
    two stale build labels.
    """
    from .models import MODELS as _MODELS
    default = CONFIG.get("default_model", "gemini-3.6-flash")
    cfg = _MODELS.get(default) or _MODELS["gemini-3.6-flash"]
    body = _build_payload("ping", cfg["mode"], 0).encode()
    url = _get_url(bl=candidate_bl)
    headers = _build_headers()
    ctx = _get_ssl_ctx()
    for proxy in _proxy_plan() or [None]:
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            if proxy:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                    urllib.request.HTTPSHandler(context=ctx))
                resp = opener.open(req, timeout=12)
            else:
                resp = urllib.request.urlopen(req, context=ctx, timeout=12)
            resp.read()
            if proxy:
                _mark_proxy_working(proxy)
            return True
        except urllib.error.HTTPError as e:
            if e.code == 405:
                _bl_reject_cache[candidate_bl] = time.time()
                log(f"BL probe: {candidate_bl} rejected (405) - keeping {CONFIG['gemini_bl']}")
                return False
            return None  # 429 or other HTTP error - ambiguous
        except Exception:
            return None  # timeout / network error - ambiguous
    return None


def _advance_bl() -> bool:
    """On HTTP 405, probe the other BL candidates and switch to the first one
    that actually answers (no 405).

    Returns True if the BL was switched. Candidates rejected with 405 are
    remembered for 10 minutes, so a page that A/B-serves multiple builds can
    never make the server flip-flop between two stale build labels.
    """
    if not CONFIG.get("auto_update_bl", False):
        return False
    # A 405 can mean the page rotated to a brand-new build: force a fresh page
    # + candidate fetch so the newest BL is seen now, not after the cache TTL.
    _page_cache["ts"] = 0
    _bl_state["fetched_ts"] = 0
    cands = _bl_candidates()
    now = time.time()
    fresh = [c for c in cands if c != CONFIG["gemini_bl"]
             and _bl_reject_cache.get(c, 0) < now - 600]
    for cand in fresh:  # newest-first, deterministic
        if probe_bl(cand) is False:
            _bl_reject_cache[cand] = now  # remember the rejection
            continue
        log(f"BL switched: {CONFIG['gemini_bl']} -> {cand}")
        CONFIG["gemini_bl"] = cand
        return True
    return False


_xsrf_state = {"token": None, "ts": 0}


def fetch_xsrf_token() -> str | None:
    """Fetch the page XSRF token (sent as the `at` form field)."""
    try:
        html = _fetch_page_html()
        for pat in (r'"thykhd":"([^"]+)"', r'"SNlM0e":"([^"]+)"'):
            m = re.search(pat, html)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None


def ensure_xsrf_token():
    """Auto-populate CONFIG['xsrf_token'] from the page if not configured.

    Authenticated (cookie) requests to StreamGenerate are often rejected
    (HTTP 400/405) without the `at` token, so we fetch it lazily and cache
    it for 10 minutes.
    """
    if CONFIG.get("xsrf_token"):
        return
    now = time.time()
    if now - _xsrf_state["ts"] > 600:
        _xsrf_state["token"] = fetch_xsrf_token()
        _xsrf_state["ts"] = now
    if _xsrf_state["token"] and not CONFIG.get("xsrf_token"):
        log("XSRF token auto-fetched from page")
        CONFIG["xsrf_token"] = _xsrf_state["token"]


def _account_prefix() -> str:
    """Return the Gemini account path prefix for non-default Google accounts."""
    auth_user = CONFIG.get("auth_user")
    if auth_user is None or auth_user == "":
        return ""
    return f"/u/{auth_user}"


def _build_headers() -> dict:
    account_prefix = _account_prefix()
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://gemini.google.com",
        "Referer": f"https://gemini.google.com{account_prefix}/app",
        "X-Same-Domain": "1",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    if account_prefix:
        headers["X-Goog-AuthUser"] = str(CONFIG["auth_user"])
    cookie_str, sapisid = load_cookie()
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)
    return headers


def apply_chat_persistence_flags(inner: list) -> None:
    """Apply Gemini Web persistence flags to an outgoing request payload."""
    if CONFIG.get("temporary_chats", False):
        inner[41] = [1]
        inner[45] = 1
    else:
        inner[41] = [2]


def _build_payload(prompt: str, model_id: int, think_mode: int, file_refs: list = None, extra_fields: dict = None) -> str:
    inner = [None] * 102
    if file_refs:
        # Ground-truth format captured from the live web UI (Aug 2026): the
        # ref array carries the mime type and the file entry is the full
        # 9-element shape with a trailing [0] marker. Must match the current
        # UI exactly or Google rejects the ref.
        refs = []
        for item in file_refs:
            ref, name = item[0], item[1]
            mime_type = (item[2] if len(item) > 2 else None) or "image/png"
            refs.append([[ref, 1, None, mime_type], name,
                         None, None, None, None, None, None, [0]])
        inner[0] = [prompt, 0, None, refs, None, None, 0]
    else:
        inner[0] = [prompt, 0, None, None, None, None, 0]
    inner[1] = ["en"]
    inner[2] = ["", "", "", None, None, None, None, None, None, ""]
    inner[6] = [0]
    inner[7] = 1
    inner[10] = 1
    inner[11] = 0
    inner[17] = [[think_mode]]
    inner[18] = 0
    inner[27] = 1
    inner[30] = [4]
    apply_chat_persistence_flags(inner)
    inner[53] = 0
    inner[59] = str(uuid.uuid4())
    inner[61] = []
    inner[68] = 1
    inner[79] = model_id
    if extra_fields:
        for k, v in extra_fields.items():
            inner[k] = v
    ensure_xsrf_token()
    outer = [None, json.dumps(inner)]
    params = {"f.req": json.dumps(outer)}
    # The `at` XSRF token must NOT be sent on image requests: ground-truth
    # capture (live UI, Aug 2026) shows the real frontend sends no `at` for
    # uploaded images, and sending it makes Google reject the request with
    # BardErrorInfo 1100 even though text requests tolerate it.
    if CONFIG.get("xsrf_token") and not file_refs:
        params["at"] = CONFIG["xsrf_token"]
    return urllib.parse.urlencode(params)


def _get_url(bl: str = None) -> str:
    """StreamGenerate URL. Pass `bl` to probe a candidate build label without
    touching the live CONFIG['gemini_bl']."""
    reqid = int(time.time()) % 1000000
    account_prefix = _account_prefix()
    return (
        f"https://gemini.google.com{account_prefix}/_/BardChatUi/data/"
        "assistant.lamda.BardFrontendService/StreamGenerate"
        f"?bl={bl or CONFIG['gemini_bl']}&hl=en&_reqid={reqid}&rt=c"
    )


def clean_text(text: str, strip: bool = True) -> str:
    text = re.sub(
        r'```(?:python|javascript|text)\?code_(?:reference|stdout)&code_event_index=\d+\n.*?```\n?',
        '', text, flags=re.DOTALL
    )
    text = re.sub(r'http://googleusercontent\.com/card_content/\d+\n?', '', text)
    return text.strip() if strip else text


def _extract_texts_from_line(line: str) -> list:
    """Parse a single wrb.fr line and return list of text strings found."""
    if '"wrb.fr"' not in line or len(line) < 200:
        return []
    try:
        arr = json.loads(line)
        inner_str = arr[0][2]
        if not inner_str or len(inner_str) < 50:
            return []
        inner = json.loads(inner_str)
        if not (isinstance(inner, list) and len(inner) > 4 and inner[4]):
            return []
        texts = []
        for part in inner[4]:
            if isinstance(part, list) and len(part) > 1 and part[1] and isinstance(part[1], list):
                for t in part[1]:
                    if isinstance(t, str) and t:
                        texts.append(t)
        return texts
    except (json.JSONDecodeError, IndexError, TypeError):
        return []


def extract_response_text(raw: str) -> str:
    """Parse full response to get final text."""
    bard_err = re.search(r'BardErrorInfo\s*\[(\d+)\]', raw)
    if not bard_err:
        bard_err = re.search(r'BardErrorInfo",\[(\d+)\]', raw)
    if bard_err:
        code = bard_err.group(1)
        if code == "1100":
            raise RuntimeError(
                "Google rejected image input (BardErrorInfo 1100) - this account/session "
                "cannot process uploaded images. Try the same chat without an image."
            )
        raise RuntimeError(f"Gemini upstream rejected request: BardErrorInfo [{code}]")
    last_text = ""
    for line in raw.split("\n"):
        for t in _extract_texts_from_line(line):
            if len(t) > len(last_text):
                last_text = t
    return clean_text(last_text)


def generate(prompt: str, model_id: int, think_mode: int, file_refs: list = None, extra_fields: dict = None) -> str:
    """Non-streaming generation with retry."""
    body = _build_payload(prompt, model_id, think_mode, file_refs, extra_fields).encode()
    url = _get_url()
    headers = _build_headers()
    ctx = _get_ssl_ctx()

    plan = _proxy_plan()
    attempts = max(CONFIG["retry_attempts"], len(plan))
    last_err = None
    last_429 = None
    saw_proxy_err = False
    for attempt in range(attempts):
        proxy = _proxy_for_attempt(attempt)
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            if proxy:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                    urllib.request.HTTPSHandler(context=ctx)
                )
                resp = opener.open(req, timeout=CONFIG["request_timeout_sec"])
            else:
                resp = urllib.request.urlopen(req, context=ctx, timeout=CONFIG["request_timeout_sec"])
            raw = resp.read().decode("utf-8", errors="replace")
            _mark_405_resolved()  # any successful HTTP round-trip ends the 405 streak
            if proxy:
                _mark_proxy_working(proxy)
            else:
                _proxy_state["working"] = None  # direct works again - stop preferring proxy
            text = extract_response_text(raw)
            if file_refs and not text:
                # Upload succeeded but Google returned no content (stale tenant/
                # account-level rejection without an explicit error frame). Fail
                # loudly instead of returning a 200 with empty content.
                raise RuntimeError(
                    "Gemini returned an empty response for the image request - "
                    "image uploads appear blocked for this account/session. "
                    "Try the same chat without an image."
                )
            return text
        except urllib.error.HTTPError as e:
            if e.code == 405:
                _mark_405()
                if _advance_bl():
                    url = _get_url()  # rebuild URL with the updated BL
                    log("Retrying with updated BL...")
                    last_err = e
                    continue
            if e.code == 429:
                last_429 = e
                if proxy:
                    log(f"Rate limited (429) via proxy {proxy} - switching route")
                else:
                    log("Rate limited (429) on direct connection - switching route")
                last_err = e
                if attempt < attempts - 1 and _proxy_for_attempt(attempt + 1) != proxy:
                    continue  # different route next - switch right away
            last_err = e
            if attempt < attempts - 1:
                log(f"Retry {attempt+1}/{attempts}: {e}")
                time.sleep(CONFIG["retry_delay_sec"])
        except RuntimeError as e:
            # Deterministic upstream rejection (e.g. BardErrorInfo 1100 -
            # image input blocked for this account). Retrying can only mask
            # the real cause behind network noise - surface it as-is.
            raise
        except Exception as e:
            if proxy:
                saw_proxy_err = True  # proxy route failed (e.g. unreachable proxy)
            last_err = e
            if attempt < attempts - 1:
                log(f"Retry {attempt+1}/{attempts}: {e}")
                time.sleep(CONFIG["retry_delay_sec"])
    if last_429 is not None and saw_proxy_err:
        raise RuntimeError(
            "Google rate limited this IP (HTTP 429) and the fallback proxy(ies) were unreachable - "
            "start your proxy client (config: proxy_fallbacks). Last error: {0}".format(last_err)
        )
    maybe_request_refresh_on_failure(last_err)
    raise last_err


def generate_stream(prompt: str, model_id: int, think_mode: int, file_refs: list = None, extra_fields: dict = None):
    """Streaming generation via httpx with retry on connection failure."""
    if not HAS_HTTPX:
        text = generate(prompt, model_id, think_mode, file_refs, extra_fields)
        if text:
            yield text
        return

    body = _build_payload(prompt, model_id, think_mode, file_refs, extra_fields)
    url = _get_url()
    headers = _build_headers()

    plan = _proxy_plan()
    attempts = max(CONFIG["retry_attempts"], len(plan))
    last_err = None
    last_429 = None
    saw_proxy_err = False
    emitted_raw_text = ""
    for attempt in range(attempts):
        proxy = _proxy_for_attempt(attempt)
        transport = httpx.HTTPTransport(proxy=proxy) if proxy else None
        with httpx.Client(transport=transport, timeout=CONFIG["request_timeout_sec"], verify=True) as client:
            try:
                with client.stream("POST", url, content=body, headers=headers) as resp:
                    resp.raise_for_status()
                    buf = ""
                    for chunk in resp.iter_text():
                        buf += chunk
                        if "BardErrorInfo" in buf:
                            bard_err = re.search(r'BardErrorInfo\s*\[(\d+)\]', buf)
                            if bard_err:
                                raise RuntimeError(
                                    f"Gemini upstream rejected request: BardErrorInfo [{bard_err.group(1)}]"
                                )
                        while "\n" in buf:
                            line, buf = buf.split("\n", 1)
                            for t in _extract_texts_from_line(line):
                                if t == emitted_raw_text or emitted_raw_text.startswith(t):
                                    continue
                                if not t.startswith(emitted_raw_text):
                                    raise RuntimeError("Gemini stream content changed during retry")
                                delta = clean_text(t[len(emitted_raw_text):], strip=False)
                                emitted_raw_text = t
                                if delta:
                                    yield delta
                _mark_405_resolved()  # successful stream ends the 405 streak
                if proxy:
                    _mark_proxy_working(proxy)
                else:
                    _proxy_state["working"] = None  # direct works again - stop preferring proxy
                return
            except RuntimeError as e:
                # Deterministic upstream rejection (BardErrorInfo) - do not
                # retry or mask it; surface as-is.
                raise
            except Exception as e:
                status = getattr(getattr(e, "response", None), "status_code", 0)
                if status == 405:
                    _mark_405()
                    if _advance_bl():
                        log("BL updated, falling back to non-streaming for this request")
                    text = generate(prompt, model_id, think_mode, file_refs, extra_fields)
                    if text:
                        yield text
                    return
                if status == 429:
                    last_429 = e
                    if proxy:
                        log(f"Rate limited (429) via proxy {proxy} - switching route")
                    else:
                        log("Rate limited (429) on direct connection - switching route")
                    last_err = e
                    if attempt < attempts - 1 and _proxy_for_attempt(attempt + 1) != proxy:
                        continue  # different route next - switch right away
                elif proxy:
                    saw_proxy_err = True  # proxy route failed (e.g. unreachable proxy)
                last_err = e
                if attempt < attempts - 1:
                    log(f"Stream retry {attempt+1}/{attempts}: {e}")
                    time.sleep(CONFIG["retry_delay_sec"])
    if last_429 is not None and saw_proxy_err:
        raise RuntimeError(
            "Google rate limited this IP (HTTP 429) and the fallback proxy(ies) were unreachable - "
            "start your proxy client (config: proxy_fallbacks). Last error: {0}".format(last_err)
        )
    maybe_request_refresh_on_failure(last_err)
    raise last_err
