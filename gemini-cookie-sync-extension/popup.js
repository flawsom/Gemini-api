const CORE_REQUIRED = ["SAPISID"];
const SESSION_ALTERNATIVES = ["__Secure-1PSID", "__Secure-3PSID", "SID"];
const LEGACY_COOKIES = ["SID", "HSID", "SSID", "APISID", "SAPISID"];

const EXPORT_ORDER = [
  "SID",
  "HSID",
  "SSID",
  "APISID",
  "SAPISID",
  "LSID",
  "OSID",
  "SIDCC",
  "AEC",
  "NID",
  "COMPASS",
  "__Secure-1PAPISID",
  "__Secure-1PSID",
  "__Secure-1PSIDTS",
  "__Secure-1PSIDCC",
  "__Secure-1PSIDRTS",
  "__Secure-3PAPISID",
  "__Secure-3PSID",
  "__Secure-3PSIDTS",
  "__Secure-3PSIDCC",
  "__Secure-3PSIDRTS",
  "__Secure-OSID",
  "__Host-1PLSID",
  "__Host-3PLSID"
];

const LOOKUP_URLS = [
  "https://gemini.google.com/app",
  "https://accounts.google.com/",
  "https://www.google.com/",
  "https://google.com/"
];

const DEFAULT_BASE = "http://127.0.0.1:8081";
const REQUEST_PATH = "/internal/cookie-refresh/request";
const HEALTH_PATH = "/";
const HEALTH_POLL_MS = 10000;
const STALE_COOKIE_H = 24;   // warn when cookies are older than this (matches watchdog --cookie-age-h)
const BL_405_WARN = 3;       // warn when the 405 streak reaches this

const statusEl = document.getElementById("status");
const exportButton = document.getElementById("export");
const inspectButton = document.getElementById("inspect");
const openButton = document.getElementById("open");
const lastRefreshEl = document.getElementById("lastRefresh");
const lastFailureEl = document.getElementById("lastFailure");
const lastActivityEl = document.getElementById("lastActivity");
const serverInfoEl = document.getElementById("serverInfo");
const refreshNowButton = document.getElementById("refreshNow");
const serverInput = document.getElementById("serverInput");
const keyInput = document.getElementById("keyInput");
const testConnButton = document.getElementById("testConn");
const saveConnButton = document.getElementById("saveConn");
const healthHeading = document.getElementById("healthHeading");
const healthCookieAgeEl = document.getElementById("healthCookieAge");
const healthUpdatedEl = document.getElementById("healthUpdated");
const health405El = document.getElementById("health405");
const healthBlEl = document.getElementById("healthBl");
const healthBridgeEl = document.getElementById("healthBridge");
const healthRefreshButton = document.getElementById("healthRefresh");

function setStatus(message, kind = "") {
  statusEl.textContent = message;
  statusEl.className = kind;
}

function getSettings() {
  return chrome.storage.local.get({ apiKey: "sk-gemini", serverBase: DEFAULT_BASE });
}

function fmtTime(ts) {
  if (!ts) return "never";
  const now = Date.now();
  if (now - ts < 45000) return "just now";
  const mins = Math.max(1, Math.round((now - ts) / 60000));
  return `${new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} (${mins} min ago)`;
}

async function loadStatus() {
  const { refresh, lastStatus, lastFailure, lastSuccess, serverBase,
          serverReachable } = await chrome.storage.local.get({
      refresh: null, lastStatus: "", lastFailure: 0, lastSuccess: 0,
      serverBase: DEFAULT_BASE, serverReachable: true
    });
  const unreachable = serverReachable === false;
  serverInfoEl.textContent = (unreachable ? "⚠ " : "") + serverBase;
  serverInfoEl.className = "val" + (unreachable ? " warn" : "");
  lastRefreshEl.textContent = fmtTime(lastSuccess);
  lastFailureEl.textContent = lastFailure ? fmtTime(lastFailure) : "none";
  lastFailureEl.className = "val" + (lastFailure && lastFailure > (lastSuccess || 0) ? " warn" : "");
  lastActivityEl.textContent = refresh ? "refresh in progress…" : (lastStatus || "idle");
}

// Live-update the panel while it is open (the background writes these keys
// whenever a refresh finishes or its status changes).
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  if (["refresh", "lastStatus", "lastFailure", "lastSuccess", "serverBase", "apiKey", "serverReachable"].some((k) => k in changes)) {
    loadStatus();
    maybeFinishWaiting();
  }
  // Re-poll the health panel when the connection settings change.
  if ("serverBase" in changes || "apiKey" in changes) {
    loadHealth();
  }
});

// ─── Server health panel (GET / every 10s while the popup is open) ─────────

function fmtDuration(sec) {
  if (typeof sec !== "number" || !isFinite(sec)) return "n/a";
  if (sec < 60) return sec + "s";
  if (sec < 3600) return Math.round(sec / 60) + "m";
  if (sec < 86400) return (sec / 3600).toFixed(1) + "h";
  return (sec / 86400).toFixed(1) + "d";
}

// "1.14" -> [1, 14] for numeric comparison ("1.9" must sort before "1.14");
// unparseable -> null (no version to compare - never a false stale warning).
// Strict per-part: only pure-digit segments count, so junk like "1.15x" can
// never silently compare as if it were "1.15".
function versionTuple(v) {
  if (typeof v !== "string" || !/^\d+(\.\d+)*$/.test(v)) return null;
  return v.split(".").map((p) => parseInt(p, 10));
}

function isOlderVersion(reported, onDisk) {
  const r = versionTuple(reported);
  const d = versionTuple(onDisk);
  if (!r || !d) return false;
  for (let i = 0; i < Math.max(r.length, d.length); i++) {
    const a = r[i] || 0;
    const b = d[i] || 0;
    if (a !== b) return a < b;
  }
  return false; // equal
}

// The last image-bridge attempt (from /health image_bridge.last_result): a
// one-glance ✓/✗ with how long ago it finished, the extension build that ran
// it, and the failure reason (truncated - the server caps the text too).
// `onDiskVersion` is the extension version on the server's disk: when the
// result came from an OLDER build, the extension was updated but never
// reloaded - that deserves a warning color even when the attempt succeeded.
function renderLastBridge(last, onDiskVersion) {
  if (!last || typeof last !== "object") {
    healthBridgeEl.textContent = "never";
    healthBridgeEl.className = "val";
    return;
  }
  // ts is epoch SECONDS (server time.time()); a missing ts is just "recently",
  // and a future ts (clock skew) must never render as "-Xs ago".
  const nowSec = Date.now() / 1000;
  const ageSec = typeof last.ts === "number" ? Math.max(0, nowSec - last.ts) : null;
  const ago = ageSec === null ? "" : ` · ${fmtDuration(ageSec)} ago`;
  const ext = last.ext_version ? ` (ext ${last.ext_version})` : "";
  const staleBuild = Boolean(last.ext_version) &&
    isOlderVersion(last.ext_version, onDiskVersion);
  const icon = last.ok ? "✓ ok" : "✗ failed";
  let text = `${icon}${ago}${ext}`;
  if (!last.ok) {
    const err = String(last.error || "bridge failed").slice(0, 90);
    const suffix = String(last.error || "").length > 90 ? "…" : "";
    text += ` — ${err}${suffix}`;
  }
  // Updated on disk but never reloaded: the running code is old, so warn even
  // for a successful attempt - the fix shipped in the newer build is not live.
  // staleBuild already implies onDiskVersion parsed, so no extra guard needed.
  if (staleBuild) {
    text += ` — reload needed (${onDiskVersion} on disk)`;
  }
  let cls = "val";
  if (staleBuild || !last.ok) cls += " warn";
  else if (!(ageSec !== null && ageSec > STALE_COOKIE_H * 3600)) cls += " ok";
  healthBridgeEl.textContent = text;
  healthBridgeEl.className = cls;
}

async function loadHealth() {
  let payload = null;
  try {
    const { serverBase } = await getSettings();
    const resp = await fetch(serverBase + HEALTH_PATH);
    if (resp.ok) payload = await resp.json();
  } catch {
    payload = null;
  }

  if (!payload || typeof payload !== "object") {
    healthHeading.classList.add("offline");
    healthCookieAgeEl.textContent = "unreachable";
    healthCookieAgeEl.className = "val warn";
    healthRefreshButton.hidden = true;  // nothing reachable to refresh from
    for (const el of [healthUpdatedEl, health405El, healthBlEl, healthBridgeEl]) {
      el.textContent = "—";
      el.className = "val";
    }
    return;
  }

  healthHeading.classList.remove("offline");
  const cookie = payload.cookie || {};
  const age = cookie.age_sec;
  const stale = typeof age === "number" && age > STALE_COOKIE_H * 3600;
  // One-tap "Refresh" next to the age when the panel is warning about stale
  // cookies AND no refresh is already in flight - it fires the same flow as
  // the Refresh now button. Respect the wait state so a 10s poll that lands
  // mid-refresh cannot re-enable the trigger before the server confirms the
  // in-flight flag.
  healthRefreshButton.hidden = !(stale && !cookie.refresh_requested);
  healthRefreshButton.disabled = waiting;
  if (typeof age === "number") {
    healthCookieAgeEl.textContent = fmtDuration(age);
    healthCookieAgeEl.className = "val " + (stale ? "warn" : "ok");
  } else {
    // No cookie file configured on the server - neutral, not green: there is
    // nowhere for a refresh to land, so "all good" would be misleading.
    healthCookieAgeEl.textContent = "no cookie file";
    healthCookieAgeEl.className = "val";
  }

  const bl405 = payload.bl_405_count || 0;
  const storm = bl405 >= BL_405_WARN;
  health405El.textContent = String(bl405);
  health405El.className = "val " + (storm ? "warn" : "ok");

  // cookie.updated_at is the file mtime in epoch SECONDS (vs fmtTime's ms)
  const updatedMs = cookie.updated_at ? cookie.updated_at * 1000 : 0;
  healthUpdatedEl.textContent =
    fmtTime(updatedMs) + (cookie.refresh_requested ? " (in flight)" : "");
  healthUpdatedEl.className = "val" + (cookie.refresh_requested ? " warn" : " ok");

  healthBlEl.textContent = payload.gemini_bl || "n/a";
  healthBlEl.className = "val";

  renderLastBridge((payload.image_bridge || {}).last_result,
                   payload.extension_manifest_version);
}

// Poll while the popup is open; popup JS contexts are torn down on close.
setInterval(loadHealth, HEALTH_POLL_MS);

// ─── Refresh now (manual trigger via the same internal endpoints) ──────────

let waiting = false;
let waitingSince = 0;
let baseSuccess = 0;
let baseFailure = 0;
let waitTimer = null;

async function beginWaiting() {
  // Snapshot the CURRENT values so a resolve requires a result NEWER than
  // this click - otherwise a pre-existing lastSuccess/lastFailure would
  // falsely complete the first watcher tick.
  const { lastSuccess, lastFailure } = await chrome.storage.local.get({
    lastSuccess: 0, lastFailure: 0
  });
  baseSuccess = lastSuccess;
  baseFailure = lastFailure;
  waiting = true;
  waitingSince = Date.now();
  refreshNowButton.disabled = true;
  refreshNowButton.textContent = "Waiting…";
  healthRefreshButton.disabled = true;
}

async function finishWaiting(message, kind = "") {
  if (!waiting) return;
  waiting = false;
  clearInterval(waitTimer);
  waitTimer = null;
  refreshNowButton.disabled = false;
  refreshNowButton.textContent = "Refresh now";
  healthRefreshButton.disabled = false;
  if (message) setStatus(message, kind);
}

async function maybeFinishWaiting() {
  if (!waiting) return;
  const { refresh, lastSuccess, lastFailure } = await chrome.storage.local.get({
    refresh: null, lastSuccess: 0, lastFailure: 0
  });
  // True hard stop: even if a refresh is still in flight, stop waiting after
  // 3 minutes (the background itself caps a refresh at ~3-4 min).
  if (Date.now() - waitingSince > 180000) {
    return finishWaiting("Timed out waiting for the refresh to complete - is the local server running?", "warn");
  }
  if (refresh) return; // still in flight
  if (lastSuccess > baseSuccess) {
    return finishWaiting("Refresh completed - cookies were updated and the window was closed.", "ok");
  }
  if (lastFailure > baseFailure) {
    return finishWaiting("Refresh failed - see the Activity line. The extension is now in its 5-minute backoff.", "warn");
  }
}

// ─── Connection settings (Server URL + API key) ─────────────────────────────

const VERIFY_PATH = "/internal/cookie-refresh/verify";
const CONFIG_PATH = "/internal/cookie-refresh/config";

async function loadConnectionSettings() {
  const { serverBase, apiKey } = await chrome.storage.local.get({
    serverBase: DEFAULT_BASE, apiKey: "sk-gemini"
  });
  serverInput.value = serverBase;
  keyInput.value = apiKey;
}

function normalizeBase(value) {
  return value.trim().replace(/\/+$/, "");
}

// The extension's host_permissions only cover loopback hosts, so any other
// base would be blocked by the browser and silently break polling/upload.
function isLoopbackBase(base) {
  try {
    const host = new URL(base).hostname;
    return host === "127.0.0.1" || host === "localhost" || host === "[::1]";
  } catch {
    return false;
  }
}

function baseError(base) {
  if (!/^https?:\/\//.test(base)) {
    return "Server URL must start with http:// or https://";
  }
  if (!isLoopbackBase(base)) {
    return "The extension can only reach loopback hosts (127.0.0.1, localhost, ::1).";
  }
  return null;
}

// Side-effect-free key check: POST /internal/cookie-refresh/verify -> 200 if
// the key matches what the server requires, 401 otherwise. The config
// endpoint (loopback-only) tells us the advertised key for a helpful hint.
async function testConnection() {
  const base = normalizeBase(serverInput.value);
  const key = keyInput.value.trim();
  const err = baseError(base);
  if (err) {
    setStatus(err, "warn");
    return;
  }
  if (!key) {
    setStatus("The API key can't be empty.", "warn");
    return;
  }
  testConnButton.disabled = true;
  setStatus("Testing connection…");
  let advertised = null;
  try {
    const cfgResp = await fetch(base + CONFIG_PATH);
    if (cfgResp.ok) {
      advertised = (await cfgResp.json().catch(() => ({}))).api_key || null;
    }
    const resp = await fetch(base + VERIFY_PATH, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-API-Key": key },
      body: JSON.stringify({})
    });
    if (resp.status === 401) {
      keyInput.classList.add("bad");
      const hint = advertised ? ` The server expects \"${advertised}\".` : "";
      setStatus("Key doesn't match the server." + hint, "warn");
      return;
    }
    if (!resp.ok) {
      setStatus("Server responded with HTTP " + resp.status + " - is this the right URL?", "warn");
      return;
    }
    keyInput.classList.remove("bad");
    setStatus("Connected - the key works. Click Save to apply.", "ok");
  } catch (e) {
    setStatus("Could not reach the server at " + base + " - is it running?", "warn");
  } finally {
    testConnButton.disabled = false;
  }
}

saveConnButton.addEventListener("click", async () => {
  const serverBase = normalizeBase(serverInput.value);
  const apiKey = keyInput.value.trim();
  const err = baseError(serverBase);
  if (err) {
    setStatus(err, "warn");
    return;
  }
  if (!apiKey) {
    setStatus("The API key can't be empty.", "warn");
    return;
  }
  await chrome.storage.local.set({ serverBase, apiKey });
  keyInput.classList.remove("bad");
  setStatus("Saved. The extension will use " + serverBase +
            " on its next poll - run Test connection if you haven't.", "ok");
  loadStatus();
});

testConnButton.addEventListener("click", testConnection);

// Shared refresh trigger: used by the Refresh now button AND the health
// panel's stale-cookie "Refresh" link, so both fire the exact same flow.
async function requestRefresh() {
  if (waiting) return;  // double-click on either trigger must not double-fire
  await beginWaiting();
  setStatus("Requesting a cookie refresh from the local server…");
  try {
    const { apiKey, serverBase } = await getSettings();
    const resp = await fetch(serverBase + REQUEST_PATH, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-API-Key": apiKey },
      body: JSON.stringify({ reason: "popup" })
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.error || "HTTP " + resp.status);
    }
    // Wake the background worker so it polls now instead of waiting 30s.
    // visible:true opens the window NORMAL and focused (a manual refresh
    // means the user is waiting - sign-in needs to be visible).
    chrome.runtime.sendMessage({ type: "poll-now", visible: true }).catch(() => {});
    setStatus("Refresh requested - a Gemini window will open for sign-in if needed, then close.", "ok");
    waitTimer = setInterval(maybeFinishWaiting, 2000);
    setTimeout(() => maybeFinishWaiting(), 180000); // hard stop after 3 min
  } catch (e) {
    setStatus("Could not request a refresh: " + e.message, "warn");
    finishWaiting();
  }
}

refreshNowButton.addEventListener("click", requestRefresh);
healthRefreshButton.addEventListener("click", requestRefresh);

loadStatus();
loadHealth();

function normalizeDomain(domain = "") {
  return domain.replace(/^\./, "").toLowerCase();
}

function isGoogleCookie(cookie) {
  const domain = normalizeDomain(cookie.domain);
  return domain === "google.com" || domain.endsWith(".google.com");
}

function cookieKey(cookie) {
  const partition = cookie.partitionKey ? JSON.stringify(cookie.partitionKey) : "";
  return [
    cookie.storeId || "",
    cookie.name,
    cookie.domain,
    cookie.path,
    partition
  ].join("|");
}

function scoreCookie(cookie) {
  const domain = (cookie.domain || "").toLowerCase();
  let score = 0;

  if (domain === ".google.com") score += 120;
  else if (domain === "google.com") score += 110;
  else if (domain === ".gemini.google.com") score += 100;
  else if (domain === "gemini.google.com") score += 95;
  else if (domain === ".accounts.google.com") score += 80;
  else if (domain === "accounts.google.com") score += 75;
  else if (domain.endsWith(".google.com")) score += 40;

  if (cookie.path === "/") score += 10;
  if (cookie.secure) score += 3;
  if (cookie.httpOnly) score += 2;
  if (!cookie.partitionKey) score += 2;
  if (!cookie.session) score += 1;

  return score;
}

async function readGoogleCookies() {
  const stores = await chrome.cookies.getAllCookieStores();
  const deduped = new Map();

  for (const store of stores) {
    const queries = [
      chrome.cookies.getAll({ storeId: store.id }),
      ...LOOKUP_URLS.map((url) => chrome.cookies.getAll({ storeId: store.id, url }))
    ];

    const results = await Promise.allSettled(queries);

    for (const result of results) {
      if (result.status !== "fulfilled") continue;
      for (const cookie of result.value) {
        if (!isGoogleCookie(cookie) || !cookie.value) continue;
        deduped.set(cookieKey(cookie), cookie);
      }
    }
  }

  return [...deduped.values()];
}

function selectBestCookies(cookies) {
  const selected = new Map();

  for (const name of EXPORT_ORDER) {
    const candidates = cookies
      .filter((cookie) => cookie.name === name && cookie.value)
      .sort((a, b) => scoreCookie(b) - scoreCookie(a));

    if (candidates.length > 0) selected.set(name, candidates[0]);
  }

  return selected;
}

function validateSelection(selected) {
  const missingCore = CORE_REQUIRED.filter((name) => !selected.has(name));
  const sessionCookie = SESSION_ALTERNATIVES.find((name) => selected.has(name));
  return {
    missingCore,
    sessionCookie,
    valid: missingCore.length === 0 && Boolean(sessionCookie)
  };
}

function listPresent(selected, names) {
  return names.filter((name) => selected.has(name));
}

function summarizeAvailable(cookies) {
  return [...new Set(cookies
    .map((cookie) => cookie.name)
    .filter((name) => /SID|APISID|LSID|OSID|COMPASS/.test(name)))]
    .sort();
}

function getActiveGeminiAccount(tabs) {
  for (const tab of tabs) {
    try {
      const url = new URL(tab.url || "");
      if (url.hostname !== "gemini.google.com") continue;
      const match = url.pathname.match(/^\/u\/(\d+)(?:\/|$)/);
      return match ? match[1] : null;
    } catch {
      // Ignore malformed or unavailable URLs.
    }
  }
  return null;
}

function chooseGeminiTab(tabs) {
  return tabs.find((tab) => tab.active) || tabs[0] || null;
}

async function readGeminiPageMetadata(tabs) {
  const tab = chooseGeminiTab(tabs);
  if (!tab?.id) {
    return {
      xsrfToken: null,
      geminiBl: null,
      source: null,
      error: "Open gemini.google.com in a tab, sign in, then refresh the page."
    };
  }

  try {
    const result = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      world: "MAIN",
      func: () => {
        const wiz = globalThis.WIZ_global_data || {};
        const html = document.documentElement?.innerHTML || "";

        const decode = (value) => {
          if (!value) return null;
          try {
            return JSON.parse(`"${value.replace(/"/g, '\\"')}"`);
          } catch {
            return value
              .replace(/\\u003d/gi, "=")
              .replace(/\\u0026/gi, "&")
              .replace(/\\u003c/gi, "<")
              .replace(/\\u003e/gi, ">");
          }
        };

        const regexValue = (name) => {
          const patterns = [
            new RegExp(`"${name}"\\s*:\\s*"([^"\\n]+)"`),
            new RegExp(`\\\\"${name}\\\\"\\s*:\\s*\\\\"([^"\\n]+)\\\\"`)
          ];
          for (const pattern of patterns) {
            const match = html.match(pattern);
            if (match?.[1]) return decode(match[1]);
          }
          return null;
        };

        let resourceBl = null;
        try {
          const resources = performance.getEntriesByType("resource");
          for (const entry of resources) {
            if (!entry?.name?.includes("gemini.google.com")) continue;
            const url = new URL(entry.name);
            const bl = url.searchParams.get("bl");
            if (bl) {
              resourceBl = bl;
              break;
            }
          }
        } catch {
          // Ignore performance API failures.
        }

        const xsrfToken = wiz.SNlM0e || regexValue("SNlM0e");
        const geminiBl = wiz.cfb2h || resourceBl || regexValue("cfb2h");

        let source = null;
        if (wiz.SNlM0e || wiz.cfb2h) source = "WIZ_global_data";
        else if (resourceBl) source = "performance";
        else if (xsrfToken || geminiBl) source = "page-html";

        return {
          xsrfToken: xsrfToken || null,
          geminiBl: geminiBl || null,
          source,
          url: location.href
        };
      }
    });

    return result?.[0]?.result || {
      xsrfToken: null,
      geminiBl: null,
      source: null,
      error: "Failed to read Gemini page data."
    };
  } catch (error) {
    return {
      xsrfToken: null,
      geminiBl: null,
      source: null,
      error: error?.message || String(error)
    };
  }
}

async function buildInspection() {
  const [cookies, tabs] = await Promise.all([
    readGoogleCookies(),
    chrome.tabs.query({ url: "https://gemini.google.com/*" })
  ]);

  const selected = selectBestCookies(cookies);
  const validation = validateSelection(selected);
  const legacyPresent = listPresent(selected, LEGACY_COOKIES);
  const legacyMissing = LEGACY_COOKIES.filter((name) => !selected.has(name));
  const available = summarizeAvailable(cookies);
  const authUser = getActiveGeminiAccount(tabs);
  const pageMetadata = await readGeminiPageMetadata(tabs);

  return {
    cookies,
    selected,
    validation,
    legacyPresent,
    legacyMissing,
    available,
    authUser,
    pageMetadata
  };
}

function inspectionMessage(info) {
  const {
    cookies,
    selected,
    validation,
    legacyPresent,
    legacyMissing,
    available,
    authUser,
    pageMetadata
  } = info;

  const lines = [
    `Found ${cookies.length} cookie(s) across Google domains.`,
    "",
    `SAPISID: ${selected.has("SAPISID") ? "present" : "missing"}`,
    `Session cookie: ${validation.sessionCookie || "missing"}`,
    `XSRF / SNlM0e: ${pageMetadata.xsrfToken ? "present" : "missing"}`,
    `gemini_bl / cfb2h: ${pageMetadata.geminiBl ? "present" : "missing"}`,
    `auth_user: ${authUser ?? "default account"}`,
    `Page data source: ${pageMetadata.source || "unavailable"}`,
    "",
    `Legacy present: ${legacyPresent.join(", ") || "none"}`,
    `Legacy not visible to the extension: ${legacyMissing.join(", ") || "none"}`,
    "",
    `Available for export: ${available.join(", ") || "none"}`
  ];

  if (pageMetadata.error) {
    lines.push("", `Page note: ${pageMetadata.error}`);
  }

  if (validation.valid && pageMetadata.xsrfToken) {
    lines.push("", "Session and XSRF are ready for export.");
  } else if (validation.valid) {
    lines.push("", "Cookies are ready, but XSRF is missing. Open Gemini, refresh the page, then inspect again.");
  } else {
    lines.push("", "Session is incomplete: SAPISID and one of __Secure-1PSID, __Secure-3PSID, or SID are required.");
  }

  return lines.join("\n");
}

function buildCookieString(info) {
  return EXPORT_ORDER
    .filter((name) => info.selected.has(name))
    .map((name) => `${name}=${info.selected.get(name).value}`)
    .join("; ");
}

async function downloadJson(filename, payload) {
  const blob = new Blob([JSON.stringify(payload, null, 2) + "\n"], {
    type: "application/json;charset=utf-8"
  });
  const url = URL.createObjectURL(blob);

  await chrome.downloads.download({
    url,
    filename,
    saveAs: true,
    conflictAction: "uniquify"
  });

  setTimeout(() => URL.revokeObjectURL(url), 30000);
}

openButton.addEventListener("click", async () => {
  await chrome.tabs.create({ url: "https://gemini.google.com/app" });
});

inspectButton.addEventListener("click", async () => {
  inspectButton.disabled = true;
  setStatus("Inspecting session and page data…");

  try {
    const info = await buildInspection();
    const ready = info.validation.valid && Boolean(info.pageMetadata.xsrfToken);
    setStatus(inspectionMessage(info), ready ? "ok" : "warn");
  } catch (error) {
    setStatus(error?.message || String(error), "warn");
  } finally {
    inspectButton.disabled = false;
  }
});

exportButton.addEventListener("click", async () => {
  exportButton.disabled = true;
  setStatus("Reading cookies and XSRF…");

  try {
    const info = await buildInspection();

    if (!info.validation.valid || !info.pageMetadata.xsrfToken) {
      throw new Error(inspectionMessage(info));
    }

    const cookieString = buildCookieString(info);
    const payload = {
      cookie: cookieString,
      sapisid: info.selected.get("SAPISID").value,
      auth_user: info.authUser,
      xsrf_token: info.pageMetadata.xsrfToken,
      gemini_bl: info.pageMetadata.geminiBl
    };

    await downloadJson("gemini-auth.json", payload);

    const exportedNames = EXPORT_ORDER.filter((name) => info.selected.has(name));
    setStatus(
      `Created gemini-auth.json with ${exportedNames.length} cookie(s) and XSRF.\n\n` +
      `Session cookie: ${info.validation.sessionCookie}\n` +
      `XSRF: present\n` +
      `gemini_bl: ${info.pageMetadata.geminiBl ? "present" : "not present — current server setting will remain"}\n` +
      `auth_user: ${info.authUser ?? "null"}\n\n` +
      "Move the file into gemini-web2api and do not share it or commit it to Git.",
      "ok"
    );
  } catch (error) {
    setStatus(error?.message || String(error), "warn");
  } finally {
    exportButton.disabled = false;
  }
});

loadStatus();
loadConnectionSettings();
