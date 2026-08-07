"""Entry point: python -m gemini_web2api"""
import argparse
import os

from .config import CONFIG, load_config, find_config
from .models import MODELS
from .gemini import fetch_latest_bl, fetch_xsrf_token, log, probe_bl
from .server import GeminiHandler, ThreadedServer
from . import __version__


def main():
    parser = argparse.ArgumentParser(description="Gemini Web to OpenAI API")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--cookie-file", type=str, default=None)
    parser.add_argument("--proxy", type=str, default=None, help="HTTP proxy, e.g. http://127.0.0.1:7890")
    parser.add_argument("--version", action="version", version=f"gemini-web2api {__version__}")
    args = parser.parse_args()

    config_path = args.config or os.environ.get("GEMINI_WEB2API_CONFIG") or find_config()
    if config_path:
        load_config(config_path)

    if args.port:
        CONFIG["port"] = args.port
    if args.cookie_file:
        CONFIG["cookie_file"] = args.cookie_file
    if args.proxy:
        CONFIG["proxy"] = args.proxy

    # Self-heal: adopt the current BL / XSRF token from the live page at startup.
    # Only auto-update BL if explicitly enabled in config, and only if a probe
    # request with the candidate BL does not get rejected with 405.
    if CONFIG.get("auto_update_bl", False):
        new_bl = fetch_latest_bl()
        if new_bl and probe_bl(new_bl) is not False:
            CONFIG["gemini_bl"] = new_bl
    if not CONFIG.get("xsrf_token"):
        tok = fetch_xsrf_token()
        if tok:
            CONFIG["xsrf_token"] = tok
    log(f"BL: {CONFIG['gemini_bl']} | XSRF: {'configured' if CONFIG.get('xsrf_token') else 'none'} | Auto-BL: {'on' if CONFIG.get('auto_update_bl') else 'off'}")

    port = CONFIG["port"]
    server = ThreadedServer((CONFIG["host"], port), GeminiHandler)
    print(f"gemini-web2api v{__version__}")
    print(f"  Listening: http://0.0.0.0:{port}")
    print(f"  Base URL:  http://localhost:{port}/v1")
    print(f"  Models:    {', '.join(MODELS.keys())}")
    print(f"  Cookie:    {'yes (' + CONFIG['cookie_file'] + ')' if CONFIG.get('cookie_file') else 'none (anonymous)'}")
    print(f"  Proxy:     {CONFIG.get('proxy') or 'none (uses system env HTTP_PROXY/HTTPS_PROXY)'}")
    print(f"  Fallbacks: {', '.join(CONFIG.get('proxy_fallbacks') or []) or 'none (429 will still fail)'}")
    print(f"  Retry:     {CONFIG['retry_attempts']}x / {CONFIG['retry_delay_sec']}s")
    print(f"  BL:        {CONFIG['gemini_bl']}")
    print(f"  Temporary: {'yes' if CONFIG.get('temporary_chats', False) else 'no'}")
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
