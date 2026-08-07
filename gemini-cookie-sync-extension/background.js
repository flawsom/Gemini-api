// Gemini Cookie Sync - background worker (v1.6)
//
// Fully autonomous cookie refresh:
//   1. Polls the local server every 30s: GET /internal/cookie-refresh/request
//   2. When a refresh is requested (server detected 401/403, or you ran
//      "manage.bat cookies"), opens a NEW MINIMIZED WINDOW on
//      gemini.google.com/app - your existing windows are never touched.
//   3. Reads the session cookies + XSRF/BL from that window.
//   4. Uploads them to the server, which writes cookie.txt.
//   5. Closes ONLY the window it opened.
//
// Manual refreshes (the popup's Refresh now) open a NORMAL, focused window
// instead - the user asked for it - and if an automatic refresh finds the
// session incomplete (real sign-in needed), the window is revealed so the
// user can sign in rather than hunting for a minimized window.
//
// Implemented as an alarm-driven state machine so the service worker can be
// suspended between steps without losing the flow.

const DEFAULT_BASE = "http://127.0.0.1:8081";
const REQUEST_PATH = "/internal/cookie-refresh/request";
const UPLOAD_PATH = "/internal/cookie-refresh/upload";
const HEALTH_PATH = "/";
const POLL_ALARM = "gemini-cookie-poll";
const STEP_ALARM = "gemini-cookie-step";

// Image bridge: the server parks image requests here because direct requests
// from exported cookies get BardErrorInfo 1100 - only a fully-authenticated
// browser session can process images. The worker opens a real Gemini window
// and the image_bridge.js content script does the upload + send + read.
const BRIDGE_REQUEST_PATH = "/internal/image-bridge/request";
const BRIDGE_CLAIM_PATH = "/internal/image-bridge/claim";
const BRIDGE_RESULT_PATH = "/internal/image-bridge/result";
const BRIDGE_STEP_ALARM = "gemini-image-bridge-step";

// ─── desktop health alerts ───────────────────────────────────────────────────
// When the server's / health payload shows stale cookies or a 405 streak, the
// worker pings the user with a desktop notification so they learn about it
// even with the popup closed. Notifications are debounced per condition (and
// globally) so a broken server never spams the system tray.
const STALE_COOKIE_AGE_SEC = 24 * 60 * 60;   // warn when cookies are older than 24h
const BL_405_STREAK = 3;                     // warn after 3+ consecutive live 405s
const NOTIFY_COOLDOWN_MS = 6 * 60 * 60 * 1000; // re-notify at most every 6h

const NOTIFY_STALE_ID = "gemini-cookie-stale";
const NOTIFY_405_ID = "gemini-cookie-405";

const NOTIFY_STATE_DEFAULTS = {
  notifyStaleAt: 0,
  notify405At: 0,
};

// ─── toolbar badge ──────────────────────────────────────────────────────────
// The extension icon shows a compact health badge: cookie age in hours,
// RED once the cookies go stale (> 24h) - staleness visible at a glance even
// with the popup closed. Green while the session is healthy.
const BADGE_STALE_COLOR = "#dc2626";   // red - cookies older than 24h
const BADGE_OK_COLOR = "#16a34a";      // green - session healthy

// Compact badge text: "9h", "48h", then days ("4d") past 100h to stay
// within the icon's ~4 character limit. "" clears the badge.
// The hours->days switch keys off the RAW seconds (not the rounded hours) so
// a cookie at 99.5h can't flip units a few minutes before the threshold.
function fmtBadgeAge(sec) {
  if (sec === null || sec === undefined || sec < 0) return "";
  if (sec >= 100 * 3600) return Math.round(sec / 86400) + "d";
  return Math.round(sec / 3600) + "h";
}

// Paint the toolbar badge from a / health payload. A missing/unknown age
// (no cookie file yet, malformed payload) clears the badge so it never shows
// a misleading number. Never throws - badge failures are visual only.
async function updateBadge(health) {
  try {
    const cookie = (health && health.cookie) || {};
    const age = cookie.age_sec;
    const stale = cookie.exists && typeof age === "number" && age > STALE_COOKIE_AGE_SEC;
    await chrome.action.setBadgeText({ text: fmtBadgeAge(age) });
    await chrome.action.setBadgeBackgroundColor({ color: stale ? BADGE_STALE_COLOR : BADGE_OK_COLOR });
  } catch {
    // badge API failure (e.g. context invalidated) - cosmetic only
  }
}

// After a failed refresh, skip re-triggering for 5 minutes (and the server
// expires its own flag after 15 min), so a bad upload never opens a new
// minimized window every ~2 minutes forever.
const FAILURE_BACKOFF_MS = 5 * 60 * 1000;

const CORE_REQUIRED = ["SAPISID"];
const SESSION_ALTERNATIVES = ["__Secure-1PSID", "__Secure-3PSID", "SID"];

const EXPORT_ORDER = [
  "SID", "HSID", "SSID", "APISID", "SAPISID", "LSID", "OSID", "SIDCC",
  "AEC", "NID", "COMPASS",
  "__Secure-1PAPISID", "__Secure-1PSID", "__Secure-1PSIDTS",
  "__Secure-1PSIDCC", "__Secure-1PSIDRTS",
  "__Secure-3PAPISID", "__Secure-3PSID", "__Secure-3PSIDTS",
  "__Secure-3PSIDCC", "__Secure-3PSIDRTS",
  "__Secure-OSID", "__Host-1PLSID", "__Host-3PLSID",
];

const LOOKUP_URLS = [
  "https://gemini.google.com/app",
  "https://accounts.google.com/",
  "https://www.google.com/",
  "https://google.com/",
];

// ─── helpers ────────────────────────────────────────────────────────────────

function isGoogleCookie(cookie) {
  const domain = (cookie.domain || "").replace(/^\./, "").toLowerCase();
  return domain === "google.com" || domain.endsWith(".google.com");
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
  return score;
}

async function getSettings() {
  return chrome.storage.local.get({
    apiKey: "sk-gemini",
    serverBase: DEFAULT_BASE
  });
}

function fmtAge(sec) {
  if (sec === null || sec === undefined) return "?";
  if (sec < 3600) return Math.max(1, Math.round(sec / 60)) + " min";
  if (sec < 86400) return (sec / 3600).toFixed(1) + " h";
  return (sec / 86400).toFixed(1) + " d";
}

// Desktop notification. Icon required for the system tray on Windows; the
// bundled icons/ PNGs keep notifications consistent across platforms.
async function notify(id, title, message) {
  try {
    await chrome.notifications.create(id, {
      type: "basic",
      iconUrl: chrome.runtime.getURL("icons/icon128.png"),
      title,
      message
    });
  } catch (e) {
    // notifications permission missing/denied - never break the poll loop
    setStatus("Notification failed: " + e.message);
  }
}

// Health check fired after every poll. Fetches the server / payload and,
// when cookies are stale or the BL is 405-ing repeatedly, raises ONE debounced
// desktop notification per condition. Quiet when the server is unreachable
// (no payload), no cookie file is configured yet, or a refresh is in flight
// (the refresh itself already opens a window).
async function checkHealthAndNotify() {
  const { serverBase, apiKey } = await getSettings();
  let health;
  try {
    health = await fetchJson(serverBase + HEALTH_PATH, {
      headers: { "X-API-Key": apiKey }
    });
  } catch {
    updateBadge(null); // server unreachable - clear any stale-age badge
    return; // server unreachable - nothing to diagnose
  }
  if (!health || typeof health !== "object") {
    updateBadge(null);
    return;
  }

  // The badge updates on every poll (before the notification gating below) so
  // the icon always reflects the current session age, even mid-refresh.
  updateBadge(health);

  const now = Date.now();
  const state = await chrome.storage.local.get(NOTIFY_STATE_DEFAULTS);
  const { refresh } = await getState();
  if (refresh) return; // a refresh window is already open - no need to nag

  const cookie = health.cookie || {};
  const age = cookie.age_sec;
  const stale = cookie.exists && typeof age === "number" && age > STALE_COOKIE_AGE_SEC;
  if (stale && now - (state.notifyStaleAt || 0) > NOTIFY_COOLDOWN_MS) {
    await notify(
      NOTIFY_STALE_ID,
      "Gemini cookies are stale",
      `Cookie age ${fmtAge(age)} (over 24h). Click to refresh them now.`
    );
    await chrome.storage.local.set({ notifyStaleAt: now });
  }

  const streak = typeof health.bl_405_count === "number" ? health.bl_405_count : 0;
  if (streak >= BL_405_STREAK && now - (state.notify405At || 0) > NOTIFY_COOLDOWN_MS) {
    await notify(
      NOTIFY_405_ID,
      "Gemini is rejecting requests",
      `${streak} consecutive HTTP 405s - the session/build label may be stale.`
    );
    await chrome.storage.local.set({ notify405At: now });
  }
}

// Tapping a health notification opens the popup (a real, visible window) so
// the user can act on the warning immediately.
chrome.notifications.onClicked.addListener((id) => {
  if (id === NOTIFY_STALE_ID || id === NOTIFY_405_ID) {
    chrome.action.openPopup().catch(() => {});
  }
});

async function fetchJson(url, options) {
  const resp = await fetch(url, options);
  if (!resp.ok) throw new Error("HTTP " + resp.status);
  return resp.json();
}

// ─── cookie + page extraction (mirrors popup.js) ───────────────────────────

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
        const key = [cookie.storeId || "", cookie.name, cookie.domain,
                     cookie.path, JSON.stringify(cookie.partitionKey || null)].join("|");
        deduped.set(key, cookie);
      }
    }
  }
  return [...deduped.values()];
}

function selectBestCookies(cookies) {
  const selected = new Map();
  for (const name of EXPORT_ORDER) {
    const candidates = cookies
      .filter((c) => c.name === name && c.value)
      .sort((a, b) => scoreCookie(b) - scoreCookie(a));
    if (candidates.length > 0) selected.set(name, candidates[0]);
  }
  return selected;
}

function buildCookieString(selected) {
  return EXPORT_ORDER
    .filter((name) => selected.has(name))
    .map((name) => `${name}=${selected.get(name).value}`)
    .join("; ");
}

function getAuthUser(url) {
  try {
    const u = new URL(url);
    if (u.hostname !== "gemini.google.com") return null;
    const m = u.pathname.match(/^\/u\/(\d+)(?:\/|$)/);
    return m ? m[1] : null;
  } catch {
    return null;
  }
}

async function readPageMetadata(tabId) {
  try {
    const result = await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: () => {
        const wiz = globalThis.WIZ_global_data || {};
        const html = document.documentElement?.innerHTML || "";
        const decode = (value) => {
          if (!value) return null;
          try { return JSON.parse(`"${value.replace(/"/g, '\\"')}"`); }
          catch { return value.replace(/\\u003d/gi, "=").replace(/\\u0026/gi, "&"); }
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
          for (const entry of performance.getEntriesByType("resource")) {
            if (!entry?.name?.includes("gemini.google.com")) continue;
            const bl = new URL(entry.name).searchParams.get("bl");
            if (bl) { resourceBl = bl; break; }
          }
        } catch { /* ignore */ }
        return {
          xsrfToken: wiz.SNlM0e || regexValue("SNlM0e") || null,
          geminiBl: wiz.cfb2h || resourceBl || regexValue("cfb2h") || null,
          url: location.href
        };
      }
    });
    return result?.[0]?.result || {};
  } catch {
    return {};
  }
}

async function buildInspection(tabId) {
  const cookies = await readGoogleCookies();
  const selected = selectBestCookies(cookies);
  const sessionCookie = SESSION_ALTERNATIVES.find((n) => selected.has(n));
  const missingCore = CORE_REQUIRED.filter((n) => !selected.has(n));
  const metadata = await readPageMetadata(tabId);
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  return {
    valid: missingCore.length === 0 && Boolean(sessionCookie),
    cookieString: buildCookieString(selected),
    sapisid: selected.get("SAPISID")?.value || "",
    authUser: getAuthUser(tab?.url) || metadata.url ? getAuthUser(metadata.url) : null,
    xsrf: metadata.xsrfToken || null,
    geminiBl: metadata.geminiBl || null,
    sessionCookie
  };
}

// ─── state machine ──────────────────────────────────────────────────────────

function getState() {
  return chrome.storage.local.get({
    refresh: null, lastStatus: "", lastFailure: 0, lastSuccess: 0
  });
}

function setStatus(msg) {
  console.log("[Gemini Cookie Sync]", msg);
  chrome.storage.local.set({ lastStatus: msg });
}

async function startRefresh(visible = false) {
  setStatus(visible
    ? "Opening a Gemini window for sign-in (manual refresh)..."
    : "Opening a new minimized Gemini window to refresh cookies...");
  try {
    const win = await chrome.windows.create({
      url: "https://gemini.google.com/app",
      state: visible ? "normal" : "minimized",
      focused: visible
    });
    const tabs = await chrome.tabs.query({ windowId: win.id });
    const tab = tabs[0];
    if (!tab?.id) throw new Error("no tab in created window");
    await chrome.storage.local.set({
      refresh: { winId: win.id, tabId: tab.id, attempt: 0,
                 startedAt: Date.now(), visible }
    });
    chrome.alarms.create(STEP_ALARM, { delayInMinutes: 0.3 }); // ~18s first check
  } catch (e) {
    setStatus("startRefresh error: " + e.message);
    // If window creation itself fails there is no window to close and no
    // refresh state - record a backoff so the 30s poll stops retrying
    // (possibly forever) and waits out the 5 min window.
    await chrome.storage.local.set({ lastFailure: Date.now() });
  }
}

async function finishRefresh(state, message, failed = false) {
  setStatus(message);
  try { await chrome.windows.remove(state.winId); }
  catch { /* window already gone */ }
  const updates = { refresh: null };
  if (failed) updates.lastFailure = Date.now();
  else updates.lastSuccess = Date.now();  // shown in the popup as "last refresh"
  await chrome.storage.local.set(updates);
}

async function continueRefresh() {
  const { refresh } = await getState();
  if (!refresh) return;
  const elapsed = Date.now() - refresh.startedAt;

  try {
    const tab = await chrome.tabs.get(refresh.tabId).catch(() => null);
    if (!tab || tab.status !== "complete" || elapsed < 20000) {
      // A visible (manual/revealed-for-sign-in) refresh gets 5 min to load -
      // a human is involved; automatic refreshes stay on the tight 90s cap.
      if (elapsed > (refresh.visible ? 300000 : 90000)) {
        return finishRefresh(refresh, "Timed out waiting for Gemini to load.", true);
      }
      chrome.alarms.create(STEP_ALARM, { delayInMinutes: 0.15 }); // ~9s
      return;
    }

    const info = await buildInspection(refresh.tabId);
    if (info.valid) {
      const { apiKey, serverBase } = await getSettings();
      await fetchJson(serverBase + UPLOAD_PATH, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-API-Key": apiKey },
        body: JSON.stringify({
          cookie: info.cookieString,
          sapisid: info.sapisid,
          auth_user: info.authUser,
          xsrf_token: info.xsrf,
          gemini_bl: info.geminiBl
        })
      });
      return finishRefresh(refresh,
        "Cookies refreshed and saved. Window closed. " +
        `(${info.cookieString.split("; ").length} cookies, XSRF ${info.xsrf ? "yes" : "auto"})`);
    }

    // Real sign-in needed: reveal the window so the user can actually sign in
    // (a minimized window would just be missed).
    const wasVisible = refresh.visible;
    if (!wasVisible) {
      refresh.visible = true;
      try { await chrome.windows.update(refresh.winId, { state: "normal", focused: true }); }
      catch { /* window already gone */ }
    }
    refresh.attempt += 1;
    // Manual refreshes (visible from the start) get ~10 min to sign in;
    // automatic refreshes stay on the tight 3-attempt cap even after the
    // reveal - that is a nudge, not an invitation to linger.
    const maxAttempts = wasVisible ? 20 : 3;
    if (refresh.attempt >= maxAttempts) {
      return finishRefresh(refresh,
        "Session incomplete - are you signed in to Gemini in your browser? Window closed.", true);
    }
    setStatus(`Session not ready - keeping the Gemini window open for sign-in `
              + `(attempt ${refresh.attempt}/${maxAttempts})...`);
    await chrome.storage.local.set({ refresh });
    chrome.alarms.create(STEP_ALARM, { delayInMinutes: 0.5 }); // ~30s retry
  } catch (e) {
    finishRefresh(refresh, "Refresh failed: " + e.message, true);
  }
}

// ─── alarms ─────────────────────────────────────────────────────────────────

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create(POLL_ALARM, { periodInMinutes: 0.5 });
});
chrome.runtime.onStartup.addListener(() => {
  chrome.alarms.create(POLL_ALARM, { periodInMinutes: 0.5 });
});

// The server advertises its own base URL + refresh key at
// /internal/cookie-refresh/config (config.json port + cookie_refresh_key).
// Adopt it whenever we can reach the server, so a non-default port or custom
// api key works without manual extension setup.
async function adoptServerConfig() {
  const { serverBase, apiKey } = await getSettings();
  let cfg;
  try {
    cfg = await fetchJson(serverBase + "/internal/cookie-refresh/config");
  } catch { return; } // server unreachable - keep current settings
  if (!cfg || typeof cfg !== "object") return;
  const updates = {};
  if (cfg.base_url && cfg.base_url !== serverBase) updates.serverBase = cfg.base_url;
  if (cfg.api_key && cfg.api_key !== apiKey) updates.apiKey = cfg.api_key;
  if (Object.keys(updates).length) {
    await chrome.storage.local.set(updates);
    setStatus("Adopted server config: " + (updates.serverBase || serverBase));
  }
}

// ─── image bridge (browser-assisted image processing) ───────────────────────
// Direct image requests from exported cookies get BardErrorInfo 1100 because
// Google only processes uploaded images in a fully-authenticated browser
// session. When the server parks an image request, this worker opens a REAL
// gemini.google.com window, the image_bridge.js content script attaches the
// image + sends the prompt, and the answer is uploaded back - then only that
// window closes. Claims are atomic (server-side), so two polling instances
// can never process the same request twice.

function getBridgeState() {
  return chrome.storage.local.get({ bridge: null });
}

async function startImageBridge(request) {
  setStatus("Image request received - opening a real Gemini window to process it...");
  try {
    const win = await chrome.windows.create({
      url: "https://gemini.google.com/app",
      state: "minimized",
      focused: false
    });
    const tabs = await chrome.tabs.query({ windowId: win.id });
    const tab = tabs[0];
    if (!tab?.id) throw new Error("no tab in created window");
    await chrome.storage.local.set({
      bridge: { winId: win.id, tabId: tab.id, request,
                startedAt: Date.now(), attempt: 0 }
    });
    chrome.alarms.create(BRIDGE_STEP_ALARM, { delayInMinutes: 0.3 }); // ~18s first check
  } catch (e) {
    setStatus("startImageBridge error: " + e.message);
    // Resolve the waiting chat request on the server so it fails cleanly.
    await postBridgeResult(request.id, false, "",
      "could not open the browser window: " + e.message);
  }
}

async function finishBridge(state, message, failed = false) {
  setStatus(message);
  try { await chrome.windows.remove(state.winId); }
  catch { /* window already gone */ }
  await chrome.storage.local.set({ bridge: null });
}

async function postBridgeResult(id, ok, text, error) {
  try {
    const { apiKey, serverBase } = await getSettings();
    await fetchJson(serverBase + BRIDGE_RESULT_PATH, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-API-Key": apiKey },
      body: JSON.stringify({ id, ok, text, error,
                             ext_version: chrome.runtime.getManifest().version })
    });
  } catch (e) {
    setStatus("Could not upload the image-bridge result: " + e.message);
  }
}

// The content script posts the result DIRECTLY to the server (image_bridge.js
// has the storage + loopback host permissions), so this state machine never
// blocks on a sendMessage response - the MV3 service worker can be suspended
// mid-await and the flow must survive it. Every non-terminal path re-arms the
// step alarm so the worker is always woken until the job is done.
async function continueImageBridge() {
  const { bridge } = await getBridgeState();
  if (!bridge) return;
  const elapsed = Date.now() - bridge.startedAt;
  try {
    const tab = await chrome.tabs.get(bridge.tabId).catch(() => null);
    if (!tab || tab.status !== "complete" || elapsed < 20000) {
      if (elapsed > 150000) {
        await finishBridge(bridge, "Image bridge timed out waiting for Gemini to load.", true);
        await postBridgeResult(bridge.request.id, false, "",
          "the Gemini page did not load in time");
      } else {
        chrome.alarms.create(BRIDGE_STEP_ALARM, { delayInMinutes: 0.15 }); // ~9s
      }
      return;
    }
    // Dispatch the job exactly once, once the tab is ready. Fire-and-forget:
    // the content script posts its own result to the server, so the worker
    // never blocks on the response (MV3 suspension safe). The callback only
    // detects a missing receiver (content script not injected yet) and clears
    // sentAt so the next step re-dispatches.
    if (!bridge.sentAt) {
      const stamp = Date.now();
      chrome.tabs.sendMessage(
        bridge.tabId,
        { type: "image-bridge", request: bridge.request },
        () => {
          if (!chrome.runtime.lastError) return; // receiver exists - all good
          chrome.storage.local.get({ bridge: null }).then((s) => {
            if (s.bridge && s.bridge.sentAt === stamp) {
              s.bridge.sentAt = null; // allow a fresh dispatch next step
              chrome.storage.local.set({ bridge: s.bridge });
            }
          }).catch(() => {});
        }
      );
      bridge.sentAt = stamp;
      await chrome.storage.local.set({ bridge });
    }
    // The job is running in the page and will post its own result. Keep the
    // worker alive and close the (minimized) window once a budget has passed
    // so a stuck page can never leave a ghost window behind. Must exceed the
    // content script's answer budget (image_bridge.js caps at ~300s) - an
    // earlier cleanup would kill a legitimately slow answer mid-stream.
    if (Date.now() - bridge.sentAt > 330000) {
      await finishBridge(bridge, "Image bridge finished (window cleanup).");
      return;
    }
    chrome.alarms.create(BRIDGE_STEP_ALARM, { delayInMinutes: 0.15 });
  } catch (e) {
    await finishBridge(bridge, "Image bridge error: " + e.message, true);
    await postBridgeResult(bridge.request.id, false, "", e.message);
  }
}

async function pollImageBridge(serverBase, apiKey) {
  const { bridge } = await getBridgeState();
  if (bridge) return; // one image request at a time
  let request = null;
  try {
    const resp = await fetchJson(serverBase + BRIDGE_REQUEST_PATH, {
      headers: { "X-API-Key": apiKey }
    });
    if (resp.requested && resp.request) request = resp.request;
  } catch { return; } // server unreachable - the cookie poll already reports it
  if (!request) return;
  // Atomic claim: only the first poller wins; second instances see nothing.
  try {
    await fetchJson(serverBase + BRIDGE_CLAIM_PATH, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-API-Key": apiKey },
      body: JSON.stringify({ id: request.id })
    });
  } catch { return; } // lost the claim race (or server vanished) - drop it
  await startImageBridge(request);
}

async function runPoll(visible = false) {
  await adoptServerConfig();
  const { serverBase, apiKey } = await getSettings();
  let requested = false;
  try {
    const r = await fetchJson(serverBase + REQUEST_PATH, {
      headers: { "X-API-Key": apiKey }
    });
    requested = Boolean(r.requested);
    await chrome.storage.local.set({ serverReachable: true });
  } catch {
    // Server not up (or the port changed and adoption can't reach it yet) -
    // record it so the popup's Server row can warn instead of failing silently.
    await chrome.storage.local.set({ serverReachable: false });
    setStatus("Cannot reach the refresh server at " + serverBase);
  }
  // Desktop health alerts (stale cookies / 405 streak) - fire-and-forget so a
  // slow health endpoint can never delay the refresh flow below. The .catch
  // keeps a storage failure (quota, context invalidation) from becoming an
  // unhandled rejection that could terminate the service worker mid-poll.
  checkHealthAndNotify().catch(() => {});
  if (requested) {
    const { refresh, lastFailure } = await getState();
    if (!refresh) {
      const sinceFailure = Date.now() - (lastFailure || 0);
      if (sinceFailure < FAILURE_BACKOFF_MS) {
        setStatus(`Refresh requested but the last attempt failed ` +
          `${Math.round(sinceFailure / 1000)}s ago - waiting out the ` +
          `${Math.round(FAILURE_BACKOFF_MS / 60000)} min backoff.`);
      } else {
        await startRefresh(visible);
      }
    }
  }
  // Image bridge: pick up any parked image request (processed in a real
  // Gemini window - the only context where Google allows uploaded images).
  await pollImageBridge(serverBase, apiKey);
}

// The popup's "Refresh now" button posts to the server, then wakes us up so
// the refresh starts immediately instead of waiting for the next 30s alarm.
// Manual refreshes pass visible:true so the window opens NORMAL and focused
// (a user is waiting on it); automatic refreshes stay minimized.
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "poll-now") {
    runPoll(Boolean(msg.visible))
      .then(() => sendResponse({ ok: true }))
      .catch(() => sendResponse({ ok: false }));
    return true; // keep the channel open for the async response
  }
});

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === POLL_ALARM) {
    await runPoll();
  } else if (alarm.name === STEP_ALARM) {
    await continueRefresh();
  } else if (alarm.name === BRIDGE_STEP_ALARM) {
    await continueImageBridge();
  }
});
