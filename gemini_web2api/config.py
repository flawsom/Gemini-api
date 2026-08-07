"""Configuration management."""
import json
import os

DEFAULT_CONFIG = {
    "port": 8081,
    "host": "0.0.0.0",
    "retry_attempts": 3,
    "retry_delay_sec": 2,
    "request_timeout_sec": 180,
    "gemini_bl": "boq_assistant-bard-web-server_20260802.16_p0",
    "auth_user": None,
    "xsrf_token": None,
    "default_model": "gemini-3.6-flash",
    "log_requests": True,
    "cookie_file": None,
    "proxy": None,
    "proxy_fallbacks": [],
    "api_keys": [],
    "cookie_refresh_key": None,
    "auto_update_bl": False,
    "temporary_chats": False,
    # Image input: "auto" tries the direct upload first and falls back to the
    # browser image bridge on BardErrorInfo 1100; "browser" skips the direct
    # attempt entirely (when the session is known to 1100); "direct" never
    # bridges (the original behaviour).
    "image_mode": "auto",
    # How a bridged image request is processed in the user's real browser:
    #   auto       - try image_bridge_cdp.py (real profile, no extension
    #                needed when the browser is closed); fall back to the
    #                Gemini Cookie Sync extension when the browser is open
    #                without a debug port
    #   cdp        - always use image_bridge_cdp.py (no extension)
    #   extension  - always park the request for the extension
    "image_bridge": "auto",
    # How long the server waits for the browser to answer a bridged image
    # request (extension round trip or the CDP script). The extension cycle -
    # poll (≤30s) + cold window load (~20-60s) + attach/send (~10s) + Gemini
    # answer - can legitimately take a few minutes, so the default is generous.
    "image_bridge_timeout": 300,
}

CONFIG = dict(DEFAULT_CONFIG)


def load_config(path: str = None):
    """Load config from JSON file."""
    if path and os.path.exists(path):
        with open(path) as f:
            CONFIG.update(json.load(f))
    return CONFIG


def find_config():
    """Search for config file in standard locations."""
    for p in ["./config.json", os.path.expanduser("~/.config/gemini-web2api/config.json")]:
        if os.path.exists(p):
            return p
    return None
