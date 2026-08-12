#!/usr/bin/env python3
"""
Ensure the "Gemini Web2API (Google session)" provider exists in AionUi's backend.

Why: AionUi stores providers in its local SQLite database (aionui-backend.db).
A fresh install, an update that resets local data, or a DB wipe removes the
provider row that points at the local Gemini Web2API bridge (localhost:8081).
This script re-creates it, so setting up a fresh machine is one command:

    python ensure_gemini_provider.py

It is idempotent: it detects an existing provider whose base_url points at the
local bridge (127.0.0.1:8081 / localhost:8081) and does nothing in that case.

Notes:
  * Run it while AionUi is closed for the most reliable write (SQLite is
    WAL-mode, so a running app usually tolerates the write, but the app caches
    the provider list — restart AionUi afterwards so the model picker sees it).
  * The provider's API key is "sk-gemini" (the bridge's built-in key),
    encrypted with the same AES-256-GCM scheme AionUi uses for other providers
    (key = SHA-256("aionui-encryption-key:" + jwt_secret)).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys

MODELS = [
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-thinking",
    "gemini-3.1-pro",
    "gemini-3.1-pro-enhanced",
    "gemini-auto",
    "gemini-3.5-flash-thinking-lite",
    "gemini-flash-lite",
]

BRIDGE_API_KEY = "sk-gemini"
BRIDGE_PORT = 8081

DEFAULT_DB_CANDIDATES = [
    os.path.expandvars(r"%APPDATA%\AionUi\aionui\aionui-backend.db"),
    os.path.expanduser(r"~/AppData/Roaming/AionUi/aionui/aionui-backend.db"),
    "/c/Users/{user}/AppData/Roaming/AionUi/aionui/aionui-backend.db".format(
        user=os.environ.get("USERNAME", "")
    ),
]


def find_db(explicit: str | None = None) -> str | None:
    if explicit and os.path.exists(explicit):
        return explicit
    for cand in DEFAULT_DB_CANDIDATES:
        if cand and os.path.exists(cand):
            return cand
    # Fall back to a filesystem search of the AionUi data dirs.
    for root in (os.path.expandvars(r"%APPDATA%\AionUi"), os.path.expandvars(r"%LOCALAPPDATA%\AionUi")):
        if root and os.path.isdir(root):
            for dirpath, _, files in os.walk(root):
                for f in files:
                    if f == "aionui-backend.db":
                        return os.path.join(dirpath, f)
    return None


def encrypt_api_key(plaintext: str, jwt_secret: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # local import

    key = hashlib.sha256(("aionui-encryption-key:" + jwt_secret).encode("utf-8")).digest()
    nonce = secrets.token_bytes(12)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ct).decode("ascii")


def main() -> int:
    db = find_db(sys.argv[1] if len(sys.argv) > 1 else None)
    if not db:
        print("ERROR: aionui-backend.db not found.")
        print("  Start AionUi once so it initialises its data directory, then re-run this script.")
        return 1

    print(f"Backend DB: {db}")
    con = sqlite3.connect(db, timeout=15)
    try:
        # 1. Pick the user to own the provider (aionpro first, then local/system).
        users = con.execute(
            "SELECT id, user_type FROM users ORDER BY "
            "CASE user_type WHEN 'aionpro' THEN 0 WHEN 'local' THEN 1 ELSE 2 END LIMIT 1"
        ).fetchone()
        if not users:
            print("ERROR: no user row found in the backend DB.")
            return 1
        user_id, user_type = users
        print(f"Owner user: {user_id} ({user_type})")

        # 2. Already present?
        existing = con.execute(
            "SELECT id, name, base_url FROM providers "
            "WHERE base_url LIKE '%127.0.0.1:%8081%' OR base_url LIKE '%localhost:%8081%'"
        ).fetchall()
        if existing:
            pid, name, base = existing[0]
            print(f"OK: provider already exists -> id={pid} name={name!r} base_url={base!r}")
            return 0

        # 3. Recreate it.
        jwt_secret = con.execute(
            "SELECT jwt_secret FROM users WHERE id='system_default_user' OR user_type='local' LIMIT 1"
        ).fetchone()
        if not jwt_secret:
            print("ERROR: cannot locate the encryption secret (system user row).")
            return 1
        enc = encrypt_api_key(BRIDGE_API_KEY, jwt_secret[0])

        pid = secrets.token_hex(4)
        now = int(__import__("time").time() * 1000)
        con.execute(
            "INSERT INTO providers (id, platform, name, base_url, api_key_encrypted, models, enabled,"
            " capabilities, context_limit, model_protocols, model_enabled, model_health, bedrock_config,"
            " created_at, updated_at, is_full_url, model_settings, user_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                pid, "custom", "Gemini Web2API (Google session)", "http://localhost:8081/v1", enc,
                json.dumps(MODELS), 1, "[]", None, None, None, "{}", None,
                now, now, 0, "{}", user_id,
            ),
        )
        con.commit()
        print(f"CREATED provider id={pid} name='Gemini Web2API (Google session)'")
        print("  base_url : http://localhost:8081/v1")
        print("  api_key  : sk-gemini (encrypted)")
        print(f"  models   : {', '.join(MODELS)}")
        print()
        print("Next: make sure the Gemini Web2API bridge is running on :8081, then")
        print("restart AionUi so the model picker reloads the provider list.")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
