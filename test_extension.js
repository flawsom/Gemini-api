// Node unit test for gemini-cookie-sync-extension/background.js
// Mocks the chrome.* API and drives the alarm state machine end-to-end.
const fs = require("fs");
const vm = require("vm");
const path = require("path");

let PASS = 0, FAIL = 0;
function check(name, cond, extra = "") {
  if (cond) { PASS++; console.log("  PASS  " + name); }
  else { FAIL++; console.log("  FAIL  " + name + "  " + extra); }
}

// ─── chrome mock ────────────────────────────────────────────────────────────
const COOKIES = [
  { storeId: "0", name: "SID", domain: ".google.com", path: "/", value: "sid123", secure: true, httpOnly: true },
  { storeId: "0", name: "SAPISID", domain: ".google.com", path: "/", value: "sap123", secure: true, httpOnly: true },
  { storeId: "0", name: "__Secure-1PSID", domain: ".google.com", path: "/", value: "ps1", secure: true, httpOnly: true },
  { storeId: "0", name: "NID", domain: ".google.com", path: "/", value: "nid9", secure: false, httpOnly: true },
  { storeId: "0", name: "EVIL", domain: "evil.com", path: "/", value: "x", secure: true, httpOnly: true },
];
const calls = { windowsCreated: [], windowsRemoved: [], windowsUpdated: [], alarms: [], uploads: [], notified: [], badges: [], sendMessages: [] };
let storage = { refresh: null, lastStatus: "" };
// Health payload served for the / health endpoint (default: healthy, so the
// poll-time health check never fires a stray notification in older tests).
let healthPayload = {
  status: "ok",
  gemini_bl: "boq_bl_1",
  bl_405_count: 0,
  bl_405_last_ts: null,
  cookie: { exists: true, age_sec: 100, refresh_requested: false },
};

const chrome = {
  cookies: {
    getAllCookieStores: async () => [{ id: "0" }],
    getAll: async (opts) => (opts.url ? COOKIES : COOKIES),
  },
  tabs: {
    query: async () => [{ id: 7, url: "https://gemini.google.com/u/1/app" }],
    get: async () => ({ id: 7, status: "complete" }),
    // The callback fires asynchronously (IPC round-trip), AFTER the worker has
    // stored sentAt - mirroring the real MV3 extension. In the failure case
    // the callback carries a lastError and no response object.
    sendMessage: async (tabId, msg, cb) => {
      calls.sendMessages.push({ tabId, msg });
      if (cb) {
        setTimeout(() => {
          if (sendMessageFail) {
            chrome.runtime.lastError = { message: "Receiving end does not exist" };
            cb();
            delete chrome.runtime.lastError;
          } else {
            cb({ ok: true });
          }
        }, 0);
      }
    },
  },
  scripting: {
    executeScript: async () => [{
      result: { xsrfToken: "tok_x", geminiBl: "boq_bl_1", url: "https://gemini.google.com/u/1/app" }
    }],
  },
  windows: {
    create: async (opts) => { calls.windowsCreated.push(opts); return { id: 100 }; },
    remove: async (id) => { calls.windowsRemoved.push(id); },
    update: async (id, opts) => { calls.windowsUpdated.push({ id, opts }); },
  },
  storage: {
    local: {
      async get(defaults) {
        const out = {};
        for (const [k, v] of Object.entries(defaults)) out[k] = (k in storage ? storage[k] : v);
        return out;
      },
      async set(obj) { Object.assign(storage, obj); },
    },
  },
  alarms: {
    create: async (name, opts) => calls.alarms.push([name, opts]),
    clear: async () => {},
    onAlarm: { addListener: (fn) => { chrome.__onAlarm = fn; } },
  },
  runtime: {
    onInstalled: { addListener: () => {} },
    onStartup: { addListener: () => {} },
    onMessage: { addListener: (fn) => { chrome.__onMessage = fn; } },
    sendMessage: async () => ({}),
    getURL: (p) => "chrome-extension://test/" + p,
    getManifest: () => ({ version: "1.15" }),
  },
  action: {
    openPopup: async () => { calls.popupOpened = (calls.popupOpened || 0) + 1; },
    setBadgeText: async (o) => { calls.badges.push({ text: o.text || "" }); },
    setBadgeBackgroundColor: async (o) => {
      if (!calls.badges.length) calls.badges.push({}); // robust to a bare color call
      calls.badges[calls.badges.length - 1].color = o.color;
    },
  },
  notifications: {
    create: async (id, opts) => { calls.notified.push({ id, opts }); },
    onClicked: { addListener: (fn) => { chrome.__notifClicked = fn; } },
  },
};
let fetchCalls = [];
let configOverride = null;
// When set, the /internal/image-bridge/request endpoint offers this request.
let bridgeRequest = null;
// When set, chrome.tabs.sendMessage's callback fires with a lastError (as if
// the content script was not injected yet) - the bridge must clear sentAt so
// the next step re-dispatches instead of parking forever.
let sendMessageFail = false;
global.chrome = chrome;
global.fetch = async (url, opts) => {
  fetchCalls.push({ url, opts, body: opts && opts.body ? JSON.parse(opts.body) : null });
  if (configOverride && url.includes("config")) return { ok: true, json: async () => configOverride };
  if (url.endsWith("/internal/cookie-refresh/request") || url.includes("/internal/cookie-refresh/request"))
    return { ok: true, json: async () => ({ requested: true }) };
  if (url.includes("/internal/image-bridge/request"))
    return { ok: true, json: async () => bridgeRequest || { requested: false } };
  const u = new URL(url);
  if (u.pathname === "/") return { ok: true, json: async () => healthPayload };
  return { ok: true, json: async () => ({ ok: true }) };
};

// load the service worker script
const src = fs.readFileSync(path.join(__dirname, "gemini-cookie-sync-extension", "background.js"), "utf8");
const ctx = vm.createContext({ chrome, console, fetch, URL, Date, JSON, setTimeout, clearTimeout, RegExp, Promise });
vm.runInContext(src, ctx);

// expose the internal functions from the context
const selectBest = vm.runInContext("selectBestCookies", ctx);
const buildStr = vm.runInContext("buildCookieString", ctx);
const getAuth = vm.runInContext("getAuthUser", ctx);
const buildInspection = vm.runInContext("buildInspection", ctx);
const startRefresh = vm.runInContext("startRefresh", ctx);
const continueRefresh = vm.runInContext("continueRefresh", ctx);
const checkHealth = vm.runInContext("checkHealthAndNotify", ctx);
const fmtAge = vm.runInContext("fmtAge", ctx);
const fmtBadgeAge = vm.runInContext("fmtBadgeAge", ctx);
const updateBadge = vm.runInContext("updateBadge", ctx);
const BADGE_STALE = vm.runInContext("BADGE_STALE_COLOR", ctx);
const BADGE_OK = vm.runInContext("BADGE_OK_COLOR", ctx);

(async () => {
  console.log("extension unit tests");

  // 1. cookie selection mirrors the popup
  const selected = selectBest(COOKIES);
  const str = buildStr(selected);
  check("selects SID/SAPISID/1PSID/NID", ["SID", "SAPISID", "__Secure-1PSID", "NID"].every(n => selected.has(n)));
  check("excludes non-google EVIL", !selected.has("EVIL"));
  check("cookie string order", str.startsWith("SID=sid123;") && str.includes("SAPISID=sap123"), str);

  // 2. auth_user from URL
  check("auth_user parsed from /u/1", getAuth("https://gemini.google.com/u/1/app") === "1");
  check("auth_user null on other host", getAuth("https://www.google.com/") === null);

  // 3. inspection builds a valid payload
  const info = await buildInspection(7);
  check("inspection valid (SAPISID + session cookie)", info.valid === true);
  check("xsrf + bl read from page", info.xsrf === "tok_x" && info.geminiBl === "boq_bl_1");
  check("authUser from tab", info.authUser === "1");

  // 4. poll alarm -> startRefresh opens a window, stores state, arms step alarm
  const req = vm.runInContext("REQUEST_PATH", ctx);
  chrome.__onAlarm({ name: "gemini-cookie-poll" });
  await new Promise(r => setTimeout(r, 50));
  check("windows.create called with gemini URL",
        calls.windowsCreated.length === 1 && calls.windowsCreated[0].url.includes("gemini.google.com"));
  check("created window is minimized (no focus steal)",
        calls.windowsCreated[0].state === "minimized" && calls.windowsCreated[0].focused === false);
  check("refresh state stored with winId 100", storage.refresh && storage.refresh.winId === 100);
  check("step alarm armed", calls.alarms.some(a => a[0] === "gemini-cookie-step"));

  // 5. step alarm -> continueRefresh: extraction, upload, close ONLY our window
  storage.refresh.startedAt = Date.now() - 60000; // pretend 60s elapsed (page "complete")
  const before = fetchCalls.length;
  await continueRefresh();
  await new Promise(r => setTimeout(r, 50));
  const upload = fetchCalls.filter(c => c.url.includes("upload"));
  check("uploaded cookie payload", upload.length === 1 &&
        upload[0].body.cookie.includes("SID=sid123") &&
        upload[0].body.sapisid === "sap123" &&
        upload[0].body.auth_user === "1" &&
        upload[0].body.xsrf_token === "tok_x",
        JSON.stringify(upload.map(u => u.body)));
  check("upload carried the API key", upload.length === 1 && upload[0].opts.headers["X-API-Key"] === "sk-gemini");
  check("closed ONLY the window it opened (100)", calls.windowsRemoved.includes(100));
  check("refresh state cleared", storage.refresh === null);
  check("did NOT create extra windows", calls.windowsCreated.length === 1);
  check("success records lastSuccess (popup 'last refresh')",
        typeof storage.lastSuccess === "number" && storage.lastSuccess > 0,
        "lastSuccess=" + storage.lastSuccess);

  // 6. failure path: invalid session keeps the window open (for sign-in), then closes
  //    after attempts - simulate by making cookies empty and attempts already at 2
  const realCookies = chrome.cookies.getAll;
  chrome.cookies.getAll = async () => [];
  storage.refresh = { winId: 101, tabId: 7, attempt: 2, startedAt: Date.now() - 60000 };
  await continueRefresh();
  await new Promise(r => setTimeout(r, 50));
  chrome.cookies.getAll = realCookies;
  check("failure path still closes its window", calls.windowsRemoved.includes(101));
  check("failure clears state", storage.refresh === null);
  check("failure records lastFailure (backoff seed)",
        typeof storage.lastFailure === "number" && storage.lastFailure > 0,
        "lastFailure=" + storage.lastFailure);

  // 7. backoff: a poll with a recent lastFailure must NOT open another window
  const windowsBefore = calls.windowsCreated.length;
  chrome.__onAlarm({ name: "gemini-cookie-poll" });
  await new Promise(r => setTimeout(r, 50));
  check("poll during 5-min backoff opens NO new window",
        calls.windowsCreated.length === windowsBefore,
        "created=" + calls.windowsCreated.length);

  // 8. backoff expired (lastFailure cleared) -> poll opens a window again
  storage.lastFailure = 0;
  chrome.__onAlarm({ name: "gemini-cookie-poll" });
  await new Promise(r => setTimeout(r, 50));
  check("poll after backoff expiry opens a window again",
        calls.windowsCreated.length === windowsBefore + 1);

  // 9. windows.create failure records a backoff so the poll does NOT retry
  //    every 30s forever (startRefresh catch path)
  const realCreate = chrome.windows.create;
  chrome.windows.create = async () => { throw new Error("no permission"); };
  storage.refresh = null;   // no in-flight refresh (else the poll skips startRefresh)
  storage.lastFailure = 0;
  chrome.__onAlarm({ name: "gemini-cookie-poll" });
  await new Promise(r => setTimeout(r, 50));
  chrome.windows.create = realCreate;
  check("startRefresh failure records lastFailure backoff",
        typeof storage.lastFailure === "number" && storage.lastFailure > 0,
        "lastFailure=" + storage.lastFailure);
  // and the following poll (still in backoff) must NOT try again
  const winsNow = calls.windowsCreated.length;
  chrome.__onAlarm({ name: "gemini-cookie-poll" });
  await new Promise(r => setTimeout(r, 50));
  check("poll after failed startRefresh stays quiet (backoff)",
        calls.windowsCreated.length === winsNow);

  // 10. popup "Refresh now" path: poll-now (visible:true) wakes the worker,
  //     starts an immediate refresh, and opens a NORMAL focused window
  storage.refresh = null;
  storage.lastFailure = 0;
  storage.lastSuccess = 0;
  const winsBefore = calls.windowsCreated.length;
  let msgResp = null;
  chrome.__onMessage({ type: "poll-now", visible: true }, {}, (r) => { msgResp = r; });
  await new Promise(r => setTimeout(r, 50));
  check("poll-now message starts an immediate refresh",
        calls.windowsCreated.length === winsBefore + 1,
        "created=" + calls.windowsCreated.length);
  check("manual refresh opens a NORMAL focused window",
        calls.windowsCreated[winsBefore].state === "normal" &&
        calls.windowsCreated[winsBefore].focused === true,
        JSON.stringify(calls.windowsCreated[winsBefore]));
  check("poll-now ack sent back to the popup",
        msgResp && msgResp.ok === true, JSON.stringify(msgResp));

  // complete that refresh: success updates lastSuccess so the popup's
  // "Waiting…" state resolves to "completed"
  storage.refresh.startedAt = Date.now() - 60000;
  await continueRefresh();
  await new Promise(r => setTimeout(r, 50));
  check("completed poll-now refresh records lastSuccess",
        typeof storage.lastSuccess === "number" && storage.lastSuccess > 0,
        "lastSuccess=" + storage.lastSuccess);
  check("completed poll-now refresh closed its window",
        calls.windowsRemoved.includes(100));

  // 11. auto-adopt the server's advertised config (non-default port / custom
  //     api key from config.json) - the poll fetches /internal/
  //     cookie-refresh/config and stores base_url + api_key
  configOverride = { base_url: "http://127.0.0.1:9000", api_key: "custom-key" };
  storage.serverBase = "http://127.0.0.1:8081";
  storage.apiKey = "sk-gemini";
  storage.refresh = null;
  storage.lastFailure = 0;
  const fetchedConfig = fetchCalls.length;
  chrome.__onAlarm({ name: "gemini-cookie-poll" });
  await new Promise(r => setTimeout(r, 50));
  configOverride = null;
  check("adopts server base_url + api_key from config endpoint",
        storage.serverBase === "http://127.0.0.1:9000" && storage.apiKey === "custom-key",
        "serverBase=" + storage.serverBase + " apiKey=" + storage.apiKey);
  check("poll actually fetched the config endpoint",
        fetchCalls.slice(fetchedConfig).some(c => c.url.includes("config")),
        fetchCalls.slice(fetchedConfig).map(c => c.url).join(","));

  // 12. automatic refresh with an incomplete session REVEALS the window
  //     (real sign-in needed) instead of staying minimized forever
  const realCookies2 = chrome.cookies.getAll;
  chrome.cookies.getAll = async () => [];
  storage.refresh = { winId: 102, tabId: 7, attempt: 0,
                      startedAt: Date.now() - 60000, visible: false };
  await continueRefresh();
  await new Promise(r => setTimeout(r, 50));
  chrome.cookies.getAll = realCookies2;
  check("incomplete session reveals the window for sign-in",
        calls.windowsUpdated.some(u => u.id === 102 &&
          u.opts.state === "normal" && u.opts.focused === true),
        JSON.stringify(calls.windowsUpdated));
  check("refresh state marked visible",
        storage.refresh && storage.refresh.visible === true,
        JSON.stringify(storage.refresh));

  // 13. desktop health notifications - stale cookies raise ONE debounced
  //     notification; fresh cookies stay quiet; cooldown blocks re-notify
  const resetNotif = async () => {
    calls.notified.length = 0;
    storage.notifyStaleAt = 0;
    storage.notify405At = 0;
    storage.refresh = null;
  };
  const lastNotif = (id) => calls.notified.filter(n => n.id === id).pop();

  await resetNotif();
  healthPayload = { status: "ok", bl_405_count: 0,
                     cookie: { exists: true, age_sec: 30 * 3600, refresh_requested: false } };
  await checkHealth();
  check("stale cookies (30h) raise a desktop notification",
        calls.notified.some(n => n.id === "gemini-cookie-stale"),
        JSON.stringify(calls.notified.map(n => n.id)));
  const staleMsg = lastNotif("gemini-cookie-stale")?.opts?.message || "";
  check("stale notification carries the cookie age", staleMsg.includes("1.2 d") || staleMsg.includes("1.3 d"), staleMsg);
  check("notification uses the extension icon",
        lastNotif("gemini-cookie-stale")?.opts?.iconUrl === "chrome-extension://test/icons/icon128.png",
        lastNotif("gemini-cookie-stale")?.opts?.iconUrl);

  // cooldown: WITHOUT resetting the timestamps, an immediate re-check must
  // stay quiet (the first check above already stored notifyStaleAt=now)
  const notifCountBefore = calls.notified.length;
  healthPayload = { status: "ok", bl_405_count: 0,
                     cookie: { exists: true, age_sec: 30 * 3600, refresh_requested: false } };
  await checkHealth();
  check("immediate re-check does NOT re-notify (cooldown)",
        calls.notified.length === notifCountBefore,
        "new=" + (calls.notified.length - notifCountBefore));

  // cooldown expiry (simulate 6h later) re-notifies
  await resetNotif();
  storage.notifyStaleAt = Date.now() - (6 * 60 * 60 * 1000 + 1000);
  await checkHealth();
  check("cooldown expiry re-notifies once",
        calls.notified.some(n => n.id === "gemini-cookie-stale"));

  await resetNotif();
  healthPayload = { status: "ok", bl_405_count: 5,
                     cookie: { exists: true, age_sec: 100, refresh_requested: false } };
  await checkHealth();
  check("405 streak (5) raises a desktop notification",
        calls.notified.some(n => n.id === "gemini-cookie-405"),
        JSON.stringify(calls.notified.map(n => n.id)));
  const msg405 = lastNotif("gemini-cookie-405")?.opts?.message || "";
  check("405 notification names the streak count", msg405.includes("5"), msg405);

  await resetNotif();
  healthPayload = { status: "ok", bl_405_count: 0,
                     cookie: { exists: true, age_sec: 100, refresh_requested: false } };
  await checkHealth();
  check("fresh cookies + no 405s stay quiet", calls.notified.length === 0,
        "notified=" + calls.notified.length);

  await resetNotif();
  healthPayload = { status: "ok", bl_405_count: 0,
                     cookie: { exists: false, age_sec: null, refresh_requested: false } };
  await checkHealth();
  check("no cookie file configured stays quiet", calls.notified.length === 0,
        "notified=" + calls.notified.length);

  // malformed / empty health payloads stay quiet (typeof-object guard)
  await resetNotif();
  healthPayload = {};
  await checkHealth();
  check("empty health payload stays quiet", calls.notified.length === 0,
        "notified=" + calls.notified.length);
  await resetNotif();
  healthPayload = null;
  await checkHealth();
  check("null health payload stays quiet", calls.notified.length === 0,
        "notified=" + calls.notified.length);

  // both conditions at once fire exactly ONE notification per condition
  await resetNotif();
  healthPayload = { status: "ok", bl_405_count: 7,
                     cookie: { exists: true, age_sec: 3 * 86400, refresh_requested: false } };
  await checkHealth();
  const ids = calls.notified.map(n => n.id).sort();
  check("stale + 405 together fire one notification each",
        ids.length === 2 &&
        ids[0] === "gemini-cookie-405" && ids[1] === "gemini-cookie-stale",
        JSON.stringify(ids));

  // notifications permission denied: create() throws -> poll loop survives,
  // no notification, no unhandled rejection
  const realNotifCreate = chrome.notifications.create;
  chrome.notifications.create = async () => { throw new Error("Not allowed") ; };
  await resetNotif();
  healthPayload = { status: "ok", bl_405_count: 0,
                     cookie: { exists: true, age_sec: 30 * 3600, refresh_requested: false } };
  await checkHealth();
  chrome.notifications.create = realNotifCreate;
  check("notifications permission denied survives silently", calls.notified.length === 0,
        "notified=" + calls.notified.length);

  await resetNotif();
  healthPayload = { status: "ok", bl_405_count: 0,
                     cookie: { exists: true, age_sec: 30 * 3600, refresh_requested: false } };
  storage.refresh = { winId: 500, tabId: 7, attempt: 0, startedAt: Date.now(), visible: false };
  await checkHealth();
  check("refresh in flight suppresses the nag", calls.notified.length === 0,
        "notified=" + calls.notified.length);
  storage.refresh = null;

  // unreachable server: fetch throws -> quiet, and the poll loop must survive
  // (timestamps pre-set so any successful health fetch WOULD be silenced by
  // the cooldown - proving the silence comes from the fetch failure)
  const realFetch = global.fetch;
  global.fetch = async (url) => {
    if (new URL(url).pathname === "/") throw new Error("ECONNREFUSED");
    return realFetch(url);
  };
  await resetNotif();
  storage.notifyStaleAt = Date.now() - 1000; // within cooldown - would suppress
  storage.notify405At = Date.now() - 1000;
  healthPayload = { status: "ok", bl_405_count: 9,
                     cookie: { exists: true, age_sec: 99 * 3600, refresh_requested: false } };
  await checkHealth();
  global.fetch = realFetch;
  check("unreachable server stays quiet", calls.notified.length === 0,
        "notified=" + calls.notified.length);

  // clicking a health notification opens the popup
  const popsBefore = calls.popupOpened || 0;
  chrome.__notifClicked("gemini-cookie-stale");
  chrome.__notifClicked("gemini-cookie-405");
  chrome.__notifClicked("unrelated-id");
  check("clicking a health notification opens the popup",
        (calls.popupOpened || 0) === popsBefore + 2,
        "popupOpened=" + (calls.popupOpened || 0));

  // fmtAge formatting helper
  check("fmtAge formats hours", fmtAge(7200) === "2.0 h", fmtAge(7200));
  check("fmtAge formats days", fmtAge(2.5 * 86400) === "2.5 d", fmtAge(2.5 * 86400));
  check("fmtAge null-safe", fmtAge(null) === "?");

  // poll alarm with a healthy server never fires a stray notification
  await resetNotif();
  healthPayload = { status: "ok", bl_405_count: 0,
                     cookie: { exists: true, age_sec: 100, refresh_requested: false } };
  storage.lastFailure = 0;
  chrome.__onAlarm({ name: "gemini-cookie-poll" });
  await new Promise(r => setTimeout(r, 50));
  check("poll with healthy server fires NO notifications", calls.notified.length === 0,
        "notified=" + calls.notified.length);

  // 14. toolbar badge: cookie age in hours, RED when stale, cleared when
  //     there is no cookie file or the server is unreachable
  const lastBadge = () => calls.badges[calls.badges.length - 1];

  check("fmtBadgeAge hours", fmtBadgeAge(9 * 3600) === "9h", fmtBadgeAge(9 * 3600));
  check("fmtBadgeAge rounds hours", fmtBadgeAge(5.5 * 3600) === "6h", fmtBadgeAge(5.5 * 3600));
  check("fmtBadgeAge days past 100h", fmtBadgeAge(5 * 86400) === "5d", fmtBadgeAge(5 * 86400));
  check("fmtBadgeAge exactly 24h stays hours", fmtBadgeAge(24 * 3600) === "24h", fmtBadgeAge(24 * 3600));
  check("fmtBadgeAge no unit flip near 100h", fmtBadgeAge(99.9 * 3600) === "100h", fmtBadgeAge(99.9 * 3600));
  check("fmtBadgeAge empty on unknown age", fmtBadgeAge(null) === "", fmtBadgeAge(null));
  check("fmtBadgeAge empty on negative age", fmtBadgeAge(-500) === "", fmtBadgeAge(-500));

  await resetNotif();
  calls.badges.length = 0;
  healthPayload = { status: "ok", bl_405_count: 0,
                     cookie: { exists: true, age_sec: 9 * 3600, refresh_requested: false } };
  await checkHealth();
  check("healthy cookies paint the badge with hours",
        lastBadge().text === "9h", JSON.stringify(lastBadge()));
  check("healthy badge is green", lastBadge().color === BADGE_OK, lastBadge().color);

  await resetNotif();
  calls.badges.length = 0;
  healthPayload = { status: "ok", bl_405_count: 0,
                     cookie: { exists: true, age_sec: 30 * 3600, refresh_requested: false } };
  await checkHealth();
  check("stale cookies paint the badge with hours",
        lastBadge().text === "30h", JSON.stringify(lastBadge()));
  check("stale badge is RED", lastBadge().color === BADGE_STALE, lastBadge().color);

  // exactly 24h is NOT stale (the check is strict >) - badge shows 24h green
  await resetNotif();
  calls.badges.length = 0;
  healthPayload = { status: "ok", bl_405_count: 0,
                     cookie: { exists: true, age_sec: 24 * 3600, refresh_requested: false } };
  await checkHealth();
  check("exactly 24h stays green (strict threshold)",
        lastBadge().text === "24h" && lastBadge().color === BADGE_OK, JSON.stringify(lastBadge()));

  // 405 streak alone does NOT turn the badge red - only stale cookies do
  await resetNotif();
  calls.badges.length = 0;
  healthPayload = { status: "ok", bl_405_count: 9,
                     cookie: { exists: true, age_sec: 2 * 3600, refresh_requested: false } };
  await checkHealth();
  check("405 streak alone keeps the badge green (stale is cookie-only)",
        lastBadge().color === BADGE_OK, lastBadge().color);

  // no cookie file configured -> badge cleared (no misleading number)
  await resetNotif();
  calls.badges.length = 0;
  healthPayload = { status: "ok", bl_405_count: 0,
                     cookie: { exists: false, age_sec: null, refresh_requested: false } };
  await checkHealth();
  check("no cookie file clears the badge", lastBadge().text === "", JSON.stringify(lastBadge()));

  // unreachable server -> badge cleared (the poll's health check clears it)
  await resetNotif();
  calls.badges.length = 0;
  const realFetch2 = global.fetch;
  global.fetch = async (url) => {
    if (new URL(url).pathname === "/") throw new Error("ECONNREFUSED");
    return realFetch2(url);
  };
  await checkHealth();
  global.fetch = realFetch2;
  check("unreachable server clears the badge",
        calls.badges.length >= 1 && lastBadge().text === "",
        JSON.stringify(calls.badges));

  // the badge updates even while a refresh is in flight (info, not a nag)
  await resetNotif();
  calls.badges.length = 0;
  healthPayload = { status: "ok", bl_405_count: 0,
                     cookie: { exists: true, age_sec: 20 * 3600, refresh_requested: false } };
  storage.refresh = { winId: 600, tabId: 7, attempt: 0, startedAt: Date.now(), visible: false };
  await checkHealth();
  storage.refresh = null;
  check("badge updates even mid-refresh (notification suppressed)",
        lastBadge().text === "20h", JSON.stringify(lastBadge()));

  // 15. image bridge pickup: the poll sees a parked request, CLAIMS it
  //     atomically, opens a MINIMIZED Gemini window, and stores bridge state.
  //     A dummy in-flight cookie refresh keeps the cookie branch of runPoll
  //     quiet so only the bridge window is created.
  storage.refresh = { winId: 999, tabId: 7, attempt: 0, startedAt: Date.now(), visible: false };
  storage.bridge = null;
  bridgeRequest = {
    requested: true,
    request: { id: "img1", prompt: "what is this?", timeout_ms: 60000,
               images: [{ name: "a.png", mime: "image/png", data_b64: "aGk=" }] }
  };
  calls.sendMessages.length = 0;
  const winsBeforeBridge = calls.windowsCreated.length;
  chrome.__onAlarm({ name: "gemini-cookie-poll" });
  await new Promise(r => setTimeout(r, 50));
  check("bridge poll atomically claims the request",
        fetchCalls.some(c => c.url.includes("/internal/image-bridge/claim")
          && c.body && c.body.id === "img1"),
        JSON.stringify(fetchCalls.filter(c => c.url.includes("image-bridge")).map(c => c.url)));
  check("bridge opens a MINIMIZED Gemini window (no focus steal)",
        calls.windowsCreated.length === winsBeforeBridge + 1 &&
        calls.windowsCreated[winsBeforeBridge].url.includes("gemini.google.com") &&
        calls.windowsCreated[winsBeforeBridge].state === "minimized" &&
        calls.windowsCreated[winsBeforeBridge].focused === false,
        JSON.stringify(calls.windowsCreated.slice(winsBeforeBridge)));
  check("bridge state stored with the request id + win id",
        storage.bridge && storage.bridge.request.id === "img1"
          && storage.bridge.winId === 100,
        JSON.stringify(storage.bridge));
  check("bridge step alarm armed",
        calls.alarms.some(a => a[0] === "gemini-image-bridge-step"));

  // 16. the step alarm dispatches the job to the content script EXACTLY once
  storage.bridge.startedAt = Date.now() - 60000; // pretend the page is loaded
  chrome.__onAlarm({ name: "gemini-image-bridge-step" });
  await new Promise(r => setTimeout(r, 50));
  check("step dispatches the image-bridge message once",
        calls.sendMessages.length === 1 &&
        calls.sendMessages[0].msg.type === "image-bridge" &&
        calls.sendMessages[0].msg.request.id === "img1",
        JSON.stringify(calls.sendMessages));
  check("sentAt recorded to gate re-dispatch",
        typeof storage.bridge.sentAt === "number",
        "sentAt=" + storage.bridge.sentAt);

  // 17. sentAt gating: a second step must NOT dispatch again (the content
  //     script owns result delivery; no double window, no double send)
  chrome.__onAlarm({ name: "gemini-image-bridge-step" });
  await new Promise(r => setTimeout(r, 50));
  check("sentAt gating prevents double dispatch",
        calls.sendMessages.length === 1,
        "dispatches=" + calls.sendMessages.length);

  // 18. missing receiver (content script not injected yet): the callback sees
  //     lastError and clears sentAt so the NEXT step re-dispatches - the MV3
  //     suspension-proof path
  sendMessageFail = true;
  storage.bridge.sentAt = null;
  chrome.__onAlarm({ name: "gemini-image-bridge-step" });
  await new Promise(r => setTimeout(r, 50));
  sendMessageFail = false;
  check("missing receiver clears sentAt for re-dispatch",
        storage.bridge && storage.bridge.sentAt === null,
        JSON.stringify(storage.bridge));
  const dispatchesAfterMiss = calls.sendMessages.length; // 2 (1 initial + 1 miss)
  chrome.__onAlarm({ name: "gemini-image-bridge-step" });
  await new Promise(r => setTimeout(r, 50));
  check("re-dispatch succeeds once the receiver is back",
        calls.sendMessages.length === dispatchesAfterMiss + 1,
        "dispatches=" + calls.sendMessages.length);

  // 19. budget cleanup: a stale sentAt (stuck page) closes ONLY the bridge
  //     window and clears the state - no ghost minimized window. Must exceed
  //     the cleanup budget (330s) while staying under the content script's
  //     answer budget so a legit slow answer is never killed mid-stream.
  storage.bridge.sentAt = Date.now() - 340000;
  const removedBeforeBridge = calls.windowsRemoved.length;
  chrome.__onAlarm({ name: "gemini-image-bridge-step" });
  await new Promise(r => setTimeout(r, 50));
  check("stale budget closes the bridge window",
        calls.windowsRemoved.slice(removedBeforeBridge).includes(100),
        JSON.stringify(calls.windowsRemoved.slice(removedBeforeBridge)));
  check("bridge state cleared after cleanup",
        storage.bridge === null, JSON.stringify(storage.bridge));
  bridgeRequest = null;
  storage.refresh = null;

  // 20. any result POST carries the extension's manifest version so the
  //     server/watchdog can flag a stale (unreloaded) build at a glance.
  //     Drive the real postBridgeResult via the step alarm: tabs.get throws
  //     (page never became available) and elapsed > 150s -> the load-timeout
  //     path posts a failure result whose body must include ext_version.
  const callsBeforeErr = fetchCalls.length;
  const realTabsGet = chrome.tabs.get;
  chrome.tabs.get = async () => { throw new Error("tab gone"); };
  storage.bridge = { request: { id: "img_ver", prompt: "p" }, winId: 101,
                     tabId: 7, startedAt: Date.now() - 160000, sentAt: null };
  chrome.__onAlarm({ name: "gemini-image-bridge-step" });
  await new Promise(r => setTimeout(r, 50));
  chrome.tabs.get = realTabsGet;
  const errPosts = fetchCalls.slice(callsBeforeErr)
    .filter(c => c.url.includes("/internal/image-bridge/result"));
  check("result POST sent after the load-timeout path",
        errPosts.length >= 1, "len=" + errPosts.length);
  if (errPosts.length >= 1) {
    check("result POST includes ext_version",
          errPosts[errPosts.length - 1].body &&
          errPosts[errPosts.length - 1].body.ext_version === "1.15",
          JSON.stringify(errPosts[errPosts.length - 1].body));
  }
  storage.bridge = null;
  storage.refresh = null;

  console.log(`\nRESULT: ${PASS} passed, ${FAIL} failed`);
  process.exit(FAIL ? 1 : 0);
})().catch(e => { console.error("TEST CRASH:", e); process.exit(2); });
