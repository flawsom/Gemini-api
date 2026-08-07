// Node unit test for gemini-cookie-sync-extension/popup.js
// Stubs the DOM, chrome.storage, chrome.runtime, fetch, and timers, then
// drives the "Refresh now" flow end-to-end.
const fs = require("fs");
const vm = require("vm");
const path = require("path");

let PASS = 0, FAIL = 0;
function check(name, cond, extra = "") {
  if (cond) { PASS++; console.log("  PASS  " + name); }
  else { FAIL++; console.log("  FAIL  " + name + "  " + extra); }
}

// ─── DOM stub ───────────────────────────────────────────────────────────────
const elements = {};
function makeEl(id) {
  return {
    id,
    textContent: "",
    className: "",
    disabled: false,
    hidden: false,
    value: "",
    classList: {
      _set: new Set(),
      add(c) { this._set.add(c); },
      remove(c) { this._set.delete(c); },
      contains(c) { return this._set.has(c); }
    },
    _listeners: {},
    addEventListener(type, fn) { this._listeners[type] = fn; }
  };
}
for (const id of ["status", "export", "inspect", "open",
                  "lastRefresh", "lastFailure", "lastActivity",
                  "serverInfo", "refreshNow",
                  "serverInput", "keyInput", "testConn", "saveConn",
                  "healthHeading", "healthCookieAge", "healthUpdated",
                  "health405", "healthBl", "healthBridge", "healthRefresh"]) {
  elements[id] = makeEl(id);
}
elements.healthHeading.classList.add("live"); // mirrors the HTML class
global.document = { getElementById: (id) => elements[id] };

// Drain several layers of microtasks (setTimeout is stubbed to never fire, so
// we drive the test's own waits through queueMicrotask). Async/await chains in
// popup.js need a few layers before their DOM writes are visible.
const flush = () => new Promise((r) => {
  let depth = 0;
  const tick = () => {
    depth += 1;
    if (depth >= 5) r();
    else queueMicrotask(tick);
  };
  queueMicrotask(tick);
});
// Deeper settle for chains started by an unawaited listener (e.g. the health
// re-poll triggered from storage.onChanged) before the next test mutates state.
const settle = () => new Promise((r) => {
  let depth = 0;
  const tick = () => {
    depth += 1;
    if (depth >= 12) r();
    else queueMicrotask(tick);
  };
  queueMicrotask(tick);
});

// ─── chrome / fetch / timer stubs ───────────────────────────────────────────
const storageData = { refresh: null, lastStatus: "", lastFailure: 0, lastSuccess: 0 };
const calls = { posts: [], messages: [], health: [] };
let capturedInterval = null;

// Health payload served by the GET / stub; mutate per test.
const nowSec = Math.floor(Date.now() / 1000);
let healthPayload = {
  status: "ok",
  gemini_bl: "boq_assistant-bard-web-server_20260803.06_p0",
  bl_405_count: 0,
  cookie: {
    exists: true,
    age_sec: 8 * 3600,
    updated_at: nowSec - 8 * 3600,
    refresh_requested: false
  }
};
let healthStatus = 200;

const chrome = {
  storage: {
    local: {
      async get(defaults) {
        const out = {};
        for (const [k, v] of Object.entries(defaults)) out[k] = (k in storageData ? storageData[k] : v);
        return out;
      },
      async set(obj) { Object.assign(storageData, obj); },
    },
    onChanged: { addListener: (fn) => { chrome.__onChanged = fn; } },
  },
  runtime: {
    sendMessage: async (msg) => { calls.messages.push(msg); return { ok: true }; },
  },
};
global.chrome = chrome;
let verifyStatus = 200;
global.fetch = async (url, opts) => {
  // The health panel polls GET <base>/ - tracked separately so the existing
  // posts-based assertions stay unambiguous.
  if (url.endsWith("/")) {
    calls.health.push(url);
    return healthStatus === 200
      ? { ok: true, status: 200, json: async () => ({ ...healthPayload }) }
      : { ok: false, status: healthStatus, json: async () => ({}) };
  }
  calls.posts.push({ url, opts });
  if (url.includes("verify")) {
    return verifyStatus === 200
      ? { ok: true, status: 200, json: async () => ({ ok: true }) }
      : { ok: false, status: 401, json: async () => ({ error: "invalid api key" }) };
  }
  if (url.includes("config")) {
    return { ok: true, status: 200, json: async () => ({ base_url: "http://127.0.0.1:8081", api_key: "sk-gemini" }) };
  }
  return { ok: true, status: 200, json: async () => ({ requested: true }) };
};
global.setInterval = (fn) => { capturedInterval = fn; return 1; };
global.clearInterval = () => {};
global.setTimeout = () => 2; // never auto-fires in the test
global.clearTimeout = () => {};
global.Blob = class { static createObjectURL() { return "blob:x"; } static revokeObjectURL() {} };
// NOTE: do NOT shadow global.URL here - the popup uses new URL(...) for its
// loopback check, so the real URL constructor must stay available.

// ─── load popup.js ──────────────────────────────────────────────────────────
const src = fs.readFileSync(path.join(__dirname, "gemini-cookie-sync-extension", "popup.js"), "utf8");
const ctx = vm.createContext({ console, chrome, document, fetch, Date, JSON, RegExp, URL,
                               setInterval, clearInterval, setTimeout, clearTimeout, Blob });
vm.runInContext(src, ctx);

(async () => {
  console.log("popup unit tests");

  await flush(); // let loadStatus() microtasks flush

  // 1. initial load shows "never"/"none"/"idle" + the default server base
  check("initial last refresh shows 'never'", elements.lastRefresh.textContent === "never");
  check("initial last failure shows 'none'", elements.lastFailure.textContent === "none");
  check("server row shows the default base URL",
        elements.serverInfo.textContent === "http://127.0.0.1:8081",
        elements.serverInfo.textContent);

  // 2. seed a prior successful refresh, then click Refresh now
  storageData.lastSuccess = 1000;  // pre-existing success (the race case)
  await elements.refreshNow._listeners.click();

  check("Refresh now POSTs to the request endpoint",
        calls.posts.length === 1 && calls.posts[0].url.endsWith("/internal/cookie-refresh/request"),
        JSON.stringify(calls.posts));
  check("POST carries the API key header",
        calls.posts[0].opts.headers["X-API-Key"] === "sk-gemini" &&
        calls.posts[0].opts.body.includes('"reason":"popup"'),
        JSON.stringify(calls.posts[0].opts));
  check("wakes the background with poll-now (visible window for manual)",
        calls.messages.some((m) => m.type === "poll-now" && m.visible === true),
        JSON.stringify(calls.messages));
  check("button disabled while waiting",
        elements.refreshNow.disabled && elements.refreshNow.textContent === "Waiting…");

  // 3. false-complete prevention: storage unchanged (lastSuccess == snapshot)
  //    -> the watcher must NOT resolve yet
  capturedInterval();
  await flush();
  check("no false completion with unchanged storage (stale-snapshot race)",
        elements.refreshNow.disabled === true &&
        elements.status.textContent.includes("Refresh requested"),
        elements.status.textContent);

  // 4. background finishes: newer lastSuccess + refresh cleared -> completed
  storageData.refresh = { winId: 1 };
  storageData.lastSuccess = Date.now();
  storageData.refresh = null;
  capturedInterval();
  await flush();
  check("watcher resolves to 'Refresh completed'",
        elements.status.textContent.includes("Refresh completed"),
        elements.status.textContent);
  check("button re-enabled as 'Refresh now'",
        elements.refreshNow.disabled === false &&
        elements.refreshNow.textContent === "Refresh now");

  // 5. storage.onChanged path also updates the panel live
  storageData.lastFailure = storageData.lastSuccess + 1000; // strictly newer than lastSuccess
  chrome.__onChanged({ lastFailure: { newValue: storageData.lastFailure } }, "local");
  await flush();
  check("live panel shows last failure time",
        elements.lastFailure.textContent.includes("min ago") ||
        elements.lastFailure.textContent.includes("just now"),
        elements.lastFailure.textContent);
  check("failure row gets warn class",
        elements.lastFailure.className.includes("warn"),
        "class=" + JSON.stringify(elements.lastFailure.className) +
        " lastFailure=" + storageData.lastFailure +
        " lastSuccess=" + storageData.lastSuccess);

  // 6. unreachable server -> Server row warns (port moved / server down)
  storageData.serverReachable = false;
  chrome.__onChanged({ serverReachable: { newValue: false } }, "local");
  await flush();
  check("server row warns when unreachable",
        elements.serverInfo.textContent.startsWith("⚠") &&
        elements.serverInfo.className.includes("warn"),
        elements.serverInfo.textContent + " / " + elements.serverInfo.className);

  // 7. connection settings load from storage into the inputs
  check("settings inputs show stored server + key",
        elements.serverInput.value === "http://127.0.0.1:8081" &&
        elements.keyInput.value === "sk-gemini",
        elements.serverInput.value + " / " + elements.keyInput.value);

  // 8. Test connection with a matching key -> success
  await elements.testConn._listeners.click();
  await flush();
  check("test with matching key succeeds",
        elements.status.textContent.includes("Connected") &&
        !elements.keyInput.classList.contains("bad"),
        elements.status.textContent);

  // 9. Test connection with a wrong key -> visible mismatch error
  verifyStatus = 401;
  elements.keyInput.value = "WRONG";
  await elements.testConn._listeners.click();
  await flush();
  verifyStatus = 200;
  check("test with wrong key shows mismatch error",
        elements.status.textContent.includes("doesn't match") &&
        elements.status.textContent.includes("sk-gemini") &&
        elements.keyInput.classList.contains("bad"),
        elements.status.textContent);

  // 10. Save writes serverBase + apiKey to storage
  storageData.serverReachable = true; // simulated reachable state
  elements.serverInput.value = "http://127.0.0.1:9000/";
  elements.keyInput.value = "custom-key";
  await elements.saveConn._listeners.click();
  await flush();
  check("save writes serverBase + apiKey (trailing slash stripped)",
        storageData.serverBase === "http://127.0.0.1:9000" &&
        storageData.apiKey === "custom-key",
        storageData.serverBase + " / " + storageData.apiKey);
  check("save clears the key mismatch styling",
        !elements.keyInput.classList.contains("bad"));
  check("save re-renders the Server row",
        elements.serverInfo.textContent === "http://127.0.0.1:9000",
        elements.serverInfo.textContent);

  // 11. non-loopback base is rejected with a clear message (host_permissions
  //     would block it anyway - better to say so up front)
  elements.serverInput.value = "http://192.168.1.5:8081";
  await elements.testConn._listeners.click();
  await flush();
  check("non-loopback base rejected",
        elements.status.textContent.includes("loopback"),
        elements.status.textContent);

  // 12. empty key is rejected
  elements.serverInput.value = "http://127.0.0.1:8081";
  elements.keyInput.value = "";
  await elements.testConn._listeners.click();
  await flush();
  check("empty key rejected",
        elements.status.textContent.includes("can't be empty"),
        elements.status.textContent);

  // 13. IPv6 loopback base is accepted
  elements.serverInput.value = "http://[::1]:8081";
  elements.keyInput.value = "sk-gemini";
  await elements.testConn._listeners.click();
  await flush();
  check("IPv6 loopback base accepted",
        elements.status.textContent.includes("Connected"),
        elements.status.textContent);

  // ── Server health panel (GET / while the popup is open) ────────────────
  // Renders are driven deterministically via ctx.loadHealth(); the storage
  // onChanged hook is verified separately by the poll-count wiring check.
  check("health panel polls GET / on load",
        calls.health.length >= 1, JSON.stringify(calls.health));
  check("health shows cookie age", elements.healthCookieAge.textContent === "8.0h",
        elements.healthCookieAge.textContent);
  check("health shows 405 streak", elements.health405.textContent === "0",
        elements.health405.textContent);
  check("health shows build label",
        elements.healthBl.textContent.includes("boq_assistant"),
        elements.healthBl.textContent);
  check("health shows cookie updated time",
        (elements.healthUpdated.textContent.includes("min ago") ||
         elements.healthUpdated.textContent.includes("just now")),
        elements.healthUpdated.textContent);
  check("health cookie age ok-colored", elements.healthCookieAge.className.includes("ok"));
  check("health live dot not offline", !elements.healthHeading.classList.contains("offline"));
  check("fresh cookies -> no health Refresh link", elements.healthRefresh.hidden === true,
        "hidden=" + elements.healthRefresh.hidden);

  // a connection-settings change re-polls the health panel (wiring check)
  const healthPollsBefore = calls.health.length;
  chrome.__onChanged({ serverBase: { newValue: "http://127.0.0.1:8081" } }, "local");
  await flush();
  check("serverBase change re-polls health", calls.health.length > healthPollsBefore,
        JSON.stringify(calls.health));
  await settle(); // let the unawaited re-poll finish before mutating the payload

  // stale cookies -> warn color (30h renders as 1.3d and trips the 24h threshold)
  healthPayload.cookie.age_sec = 30 * 3600;
  healthPayload.cookie.updated_at = nowSec - 30 * 3600;
  healthPayload.cookie.refresh_requested = false;
  await vm.runInContext("loadHealth()", ctx);
  check("stale cookie age -> warn class", elements.healthCookieAge.className.includes("warn"),
        elements.healthCookieAge.className);
  check("stale cookie age renders as days", elements.healthCookieAge.textContent === "1.3d",
        elements.healthCookieAge.textContent);
  check("stale cookies -> health Refresh link shown", elements.healthRefresh.hidden === false,
        "hidden=" + elements.healthRefresh.hidden);

  // 405 storm -> warn color
  healthPayload.bl_405_count = 4;
  await vm.runInContext("loadHealth()", ctx);
  check("405 streak >= 3 -> warn class",
        elements.health405.className.includes("warn") &&
        elements.health405.textContent === "4",
        elements.health405.className + " / " + elements.health405.textContent);

  // refresh in flight -> suffix + warn + hide the link (already refreshing)
  healthPayload.cookie.refresh_requested = true;
  await vm.runInContext("loadHealth()", ctx);
  check("in-flight refresh noted",
        elements.healthUpdated.textContent.includes("in flight") &&
        elements.healthUpdated.className.includes("warn"),
        elements.healthUpdated.textContent);
  check("in-flight refresh -> health Refresh link hidden",
        elements.healthRefresh.hidden === true,
        "hidden=" + elements.healthRefresh.hidden);

  // unreachable server -> offline state
  healthStatus = 500;
  await vm.runInContext("loadHealth()", ctx);
  check("unreachable health -> 'unreachable' + warn + offline dot",
        elements.healthCookieAge.textContent === "unreachable" &&
        elements.healthCookieAge.className.includes("warn") &&
        elements.healthHeading.classList.contains("offline"),
        elements.healthCookieAge.textContent + " / " + elements.healthCookieAge.className);
  check("unreachable -> health Refresh link hidden",
        elements.healthRefresh.hidden === true,
        "hidden=" + elements.healthRefresh.hidden);

  // no cookie file on the server -> neutral (not green, not warn)
  healthStatus = 200;
  healthPayload.cookie.age_sec = null;
  await vm.runInContext("loadHealth()", ctx);
  check("no cookie file -> neutral 'no cookie file'",
        elements.healthCookieAge.textContent === "no cookie file" &&
        elements.healthCookieAge.className === "val",
        elements.healthCookieAge.textContent + " / " + elements.healthCookieAge.className);
  check("no cookie file -> health Refresh link hidden",
        elements.healthRefresh.hidden === true,
        "hidden=" + elements.healthRefresh.hidden);

  // ── Last bridge row (image_bridge.last_result from /health) ───────────
  // The /health fixture mirrors the real server: it reports the extension's
  // ON-DISK manifest version so the popup can spot an unreloaded build.
  healthPayload.extension_manifest_version = "1.15";
  // no bridge result yet -> neutral "never"
  healthPayload.image_bridge = {};
  await vm.runInContext("loadHealth()", ctx);
  check("no bridge result -> 'never' + neutral class",
        elements.healthBridge.textContent === "never" &&
        elements.healthBridge.className === "val",
        elements.healthBridge.textContent + " / " + elements.healthBridge.className);

  // successful attempt with a CURRENT version -> green ✓ + age + ext
  healthPayload.image_bridge = {
    last_result: { ok: true, text: "answer", ext_version: "1.15",
                   ts: Math.floor(Date.now() / 1000) - 120 }
  };
  await vm.runInContext("loadHealth()", ctx);
  check("ok bridge -> ✓ + age + ext version",
        elements.healthBridge.textContent.includes("✓ ok") &&
        elements.healthBridge.textContent.includes("2m ago") &&
        elements.healthBridge.textContent.includes("ext 1.15"),
        elements.healthBridge.textContent);
  check("ok bridge with current version -> ok class",
        elements.healthBridge.className.includes("ok") &&
        !elements.healthBridge.className.includes("warn"),
        elements.healthBridge.className);

  // successful attempt from an OLDER build -> warning color + reload note
  healthPayload.image_bridge = {
    last_result: { ok: true, text: "answer", ext_version: "1.13",
                   ts: Math.floor(Date.now() / 1000) - 120 }
  };
  await vm.runInContext("loadHealth()", ctx);
  check("ok bridge from older build -> warn class + reload note",
        elements.healthBridge.className.includes("warn") &&
        !elements.healthBridge.className.includes("ok") &&
        elements.healthBridge.textContent.includes("ext 1.13") &&
        elements.healthBridge.textContent.includes("reload needed") &&
        elements.healthBridge.textContent.includes("1.15 on disk"),
        elements.healthBridge.className + " / " + elements.healthBridge.textContent);

  // version-gap math: 1.9 must sort BEFORE 1.14 (numeric, not string)
  healthPayload.image_bridge = {
    last_result: { ok: true, text: "answer", ext_version: "1.9",
                   ts: Math.floor(Date.now() / 1000) - 60 }
  };
  await vm.runInContext("loadHealth()", ctx);
  check("older version detection is numeric (1.9 < 1.15)",
        elements.healthBridge.className.includes("warn"),
        elements.healthBridge.className);

  // no on-disk version exposed (older server) -> no false stale warning
  healthPayload.extension_manifest_version = null;
  healthPayload.image_bridge = {
    last_result: { ok: true, text: "answer", ext_version: "1.13",
                   ts: Math.floor(Date.now() / 1000) - 120 }
  };
  await vm.runInContext("loadHealth()", ctx);
  check("no on-disk version -> no false stale warning",
        !elements.healthBridge.className.includes("warn") &&
        elements.healthBridge.className.includes("ok"),
        elements.healthBridge.className);
  healthPayload.extension_manifest_version = "1.15";

  // failed attempt (CURRENT build) -> red ✗ with the error truncated to 90
  const longErr = "no model answer appeared before the timeout - " +
                  "composer still holds text; messages user=1 model=0; ".repeat(4);
  healthPayload.image_bridge = {
    last_result: { ok: false, error: longErr, ext_version: "1.15",
                   ts: Math.floor(Date.now() / 1000) - 300 }
  };
  await vm.runInContext("loadHealth()", ctx);
  check("failed bridge -> ✗ + age + truncated error",
        elements.healthBridge.textContent.includes("✗ failed") &&
        elements.healthBridge.textContent.includes("5m ago") &&
        elements.healthBridge.textContent.includes("no model answer") &&
        elements.healthBridge.textContent.includes("…") &&
        elements.healthBridge.textContent.length < 140 &&
        !elements.healthBridge.textContent.includes("reload needed"),
        elements.healthBridge.textContent);
  check("failed bridge -> warn class",
        elements.healthBridge.className.includes("warn"),
        elements.healthBridge.className);

  // failed attempt from an OLDER build -> ✗ stays, reload note appended
  healthPayload.image_bridge = {
    last_result: { ok: false, error: "boom", ext_version: "1.13",
                   ts: Math.floor(Date.now() / 1000) - 60 }
  };
  await vm.runInContext("loadHealth()", ctx);
  check("failed bridge from older build -> ✗ + reload note + warn",
        elements.healthBridge.textContent.includes("✗ failed") &&
        elements.healthBridge.textContent.includes("reload needed") &&
        elements.healthBridge.className.includes("warn"),
        elements.healthBridge.textContent + " / " + elements.healthBridge.className);

  // future ts (clock skew) -> never a negative age
  healthPayload.image_bridge = {
    last_result: { ok: true, text: "x", ext_version: "1.15",
                   ts: Math.floor(Date.now() / 1000) + 300 }
  };
  await vm.runInContext("loadHealth()", ctx);
  check("future ts -> no negative age",
        !elements.healthBridge.textContent.includes("-") &&
        elements.healthBridge.textContent.includes("0s ago"),
        elements.healthBridge.textContent);

  // missing ts -> age omitted (never "0s ago")
  healthPayload.image_bridge = {
    last_result: { ok: true, text: "x", ext_version: "1.15" }
  };
  await vm.runInContext("loadHealth()", ctx);
  check("missing ts -> age omitted",
        !elements.healthBridge.textContent.includes("ago") &&
        elements.healthBridge.textContent.includes("ext 1.15"),
        elements.healthBridge.textContent);

  // ancient success (>24h) -> neutral class, not green
  healthPayload.image_bridge = {
    last_result: { ok: true, text: "x", ext_version: "1.14",
                   ts: Math.floor(Date.now() / 1000) - 25 * 3600 }
  };
  await vm.runInContext("loadHealth()", ctx);
  check("ancient success -> neutral class (not green)",
        !elements.healthBridge.className.includes("ok") &&
        elements.healthBridge.textContent.includes("1.0d ago"),
        elements.healthBridge.className + " / " + elements.healthBridge.textContent);

  // unreachable server -> the bridge row joins the others as "—"
  healthStatus = 500;
  await vm.runInContext("loadHealth()", ctx);
  check("unreachable -> bridge row shows '—'",
        elements.healthBridge.textContent === "—" &&
        elements.healthBridge.className === "val",
        elements.healthBridge.textContent);
  healthStatus = 200;
  await vm.runInContext("loadHealth()", ctx);

  // clicking the health panel Refresh link fires the SAME flow as Refresh now
  healthPayload.cookie.age_sec = 30 * 3600;  // stale again -> link visible
  healthPayload.cookie.updated_at = nowSec - 30 * 3600;
  healthPayload.cookie.refresh_requested = false;
  await vm.runInContext("loadHealth()", ctx);
  const postsBefore = calls.posts.length;
  const messagesBefore = calls.messages.length;
  await elements.healthRefresh._listeners.click();
  await flush();
  check("health Refresh link POSTs to the request endpoint",
        calls.posts.length === postsBefore + 1 &&
        calls.posts[postsBefore].url.endsWith("/internal/cookie-refresh/request"),
        JSON.stringify(calls.posts.slice(postsBefore)));
  check("health Refresh link wakes the background with poll-now",
        calls.messages.length === messagesBefore + 1 &&
        calls.messages[messagesBefore].type === "poll-now" &&
        calls.messages[messagesBefore].visible === true,
        JSON.stringify(calls.messages.slice(messagesBefore)));
  check("health Refresh link enters the waiting state (button disabled)",
        elements.refreshNow.disabled === true &&
        elements.healthRefresh.disabled === true &&
        elements.refreshNow.textContent === "Waiting…",
        "refreshNow.disabled=" + elements.refreshNow.disabled +
        " healthRefresh.disabled=" + elements.healthRefresh.disabled);

  // a health poll landing mid-wait must NOT re-enable the trigger (race fix)
  await vm.runInContext("loadHealth()", ctx);
  check("health poll mid-wait keeps the link disabled",
        elements.healthRefresh.disabled === true,
        "healthRefresh.disabled=" + elements.healthRefresh.disabled);

  // double-click on either trigger must not double-fire the request
  const postsBefore2 = calls.posts.length;
  await elements.healthRefresh._listeners.click();
  await flush();
  check("second click while waiting does not double-fire",
        calls.posts.length === postsBefore2,
        "posts=" + JSON.stringify(calls.posts.slice(postsBefore2 - 2)));

  console.log(`\nRESULT: ${PASS} passed, ${FAIL} failed`);
  process.exit(FAIL ? 1 : 0);
})().catch((e) => { console.error("TEST CRASH:", e); process.exit(2); });
