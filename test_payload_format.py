"""Unit test for the StreamGenerate payload image-refs format.

Regression guard for the fix that embeds uploaded-image refs in the EXACT
format the live Gemini web UI uses (captured via CDP, Aug 2026):

  [[["/contrib_service/...", 1, null, "image/png"],
    "name.png", null, null, null, null, null, null, [0]]]

The old 2-element form ([["/contrib_service/...", 1], "name.png"]) makes
Google reject the request with BardErrorInfo 1100. Also asserts the legacy
2-tuple input is still tolerated (defensive) and the `at` XSRF param is
appended when configured.

No network, no cookie, no config.json. Run:  python test_payload_format.py
"""
import json
import os
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import gemini_web2api.gemini as gem

_orig_token = gem.CONFIG.get("xsrf_token")
# Never hit the network for the XSRF token during a unit test.
_orig_ensure_xsrf = gem.ensure_xsrf_token
gem.ensure_xsrf_token = lambda: None


def build_refs(file_refs):
    """Decode _build_payload and return inner[0][3] (the refs array)."""
    body = gem._build_payload("hi", 0, 0, file_refs)
    q = urllib.parse.parse_qs(body)
    outer = json.loads(q["f.req"][0])
    inner = json.loads(outer[1])
    return inner[0][3]


try:
    # ── Test 1: (ref, name, mime) triple -> the full live-UI entry shape ──
    refs = build_refs([("/contrib_service/abc", "cat.png", "image/png")])
    assert len(refs) == 1, refs
    entry = refs[0]
    # ref array: [ref, 1, null, mime]
    assert entry[0] == ["/contrib_service/abc", 1, None, "image/png"], entry[0]
    # entry: [ref_array, name, None, None, None, None, None, None, [0]]
    assert len(entry) == 9, entry
    assert entry[1] == "cat.png", entry
    assert entry[2:] == [None, None, None, None, None, None, [0]], entry
    print("Test 1 OK: triple -> live-UI ref entry (mime in ref array, [0] marker)")

    # ── Test 2: two images -> both entries, each in the new format ─────────
    refs = build_refs([("/a", "a.png", "image/png"), ("/b", "b.jpg", "image/jpeg")])
    assert len(refs) == 2, refs
    assert refs[0][0] == ["/a", 1, None, "image/png"]
    assert refs[1][0] == ["/b", 1, None, "image/jpeg"]
    assert refs[1][1] == "b.jpg"
    print("Test 2 OK: multiple images keep per-file mime types")

    # ── Test 3: no images -> inner[0][3] stays None (text-only payload) ────
    refs = build_refs(None)
    assert refs is None, refs
    print("Test 3 OK: no refs -> inner[0][3] is None")

    # ── Test 4: legacy (ref, name) 2-tuple still tolerated (defensive) ─────
    refs = build_refs([("/old", "old.png")])
    assert refs[0][0] == ["/old", 1, None, "image/png"], refs[0][0]
    assert refs[0][1] == "old.png"
    print("Test 4 OK: legacy 2-tuple tolerated, still emits the new shape")

    # ── Test 4b: 3-tuple with None mime falls back to image/png (defensive) ─
    refs = build_refs([("/no-mime", "x.png", None)])
    assert refs[0][0] == ["/no-mime", 1, None, "image/png"], refs[0][0]
    print("Test 4b OK: None mime in a triple falls back to image/png")

    # ── Test 5: xsrf_token configured -> `at` param present in body ────────
    gem.CONFIG["xsrf_token"] = "tok123"
    body = gem._build_payload("hi", 0, 0, None)
    q = urllib.parse.parse_qs(body)
    assert q.get("at", [None])[0] == "tok123", q.keys()
    print("Test 5 OK: configured xsrf token sent as `at`")

    print("ALL TESTS PASSED")
finally:
    gem.ensure_xsrf_token = _orig_ensure_xsrf
    if _orig_token is None:
        gem.CONFIG.pop("xsrf_token", None)
    else:
        gem.CONFIG["xsrf_token"] = _orig_token
