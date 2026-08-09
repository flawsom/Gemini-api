/**
 * Gemini Web2API - Cloudflare Workers concurrency-safe edition
 * Multi-fingerprint rotation + multi-cookie rotation + typewriter streaming + random delay
 * 
 * ============================================================================
 * Project description
 * ============================================================================
 * This program converts Google Gemini's web interface into an OpenAI-compatible API.
 * It runs on the Cloudflare Workers edge platform - no server required.
 * Supports streaming output (SSE typewriter effect), non-streaming output, and tool calling (Function Calling).
 * 
 * ============================================================================
 * Core features:
 * ============================================================================
 * 
 * 1. [Concurrency safety] Completely eliminates the risk of the global CONFIG being mutated or crossed over by concurrent async requests.
 *    Root cause: when a CF Workers Isolate is hot-started (reused), top-level code does not re-run.
 *    When clients like WorkBuddy fire several concurrent requests within a very short window,
 *    they share the same global CONFIG object (because the same Isolate is reused).
 *    Request A sets CONFIG.cookieString = "cookie_a",
 *    Request B then sets CONFIG.cookieString = "cookie_b",
 *    but Request A later uses cookie_b - auth cross-talk.
 *    This is especially severe with WorkBuddy's multi-model concurrent calls.
 *    
 *    Solution:
 *    Each request builds a fresh, isolated config copy via getRequestConfig(env),
 *    and every function receives the config as a parameter - no dependence on global mutable state.
 * 
 * 2. [Request-level config isolation] Implements a per-request config copy mechanism.
 *    - DEFAULT_CONFIG is a read-only template and is never modified
 *    - getRequestConfig(env) creates an independent config copy per request
 *    - Custom config is loaded from env (environment variables injected per request by the CF platform)
 *    - Every function signature carries the config parameter, removing global-state dependence
 *    - Explicit assignment (env.X || null) prevents stale values from a reused Isolate
 * 
 * 3. [Rate-limit memory safety] Fixes the implicit memory leak of the global rateLimitStore in a serverless environment.
 *    - In serverless environments an Isolate may live a long time (hot-start reuse)
 *    - Without pruning expired records, the Map grows unbounded and leaks memory
 *    - A random-probability cleanup is used (5% chance of triggering a global sweep)
 *    - Each sweep iterates all keys and removes expired or empty entries
 *    - Keeps memory usage stable over long runtimes
 * 
 * 4. [SAPISID auto-extraction] Adds defensive logic that auto-extracts SAPISID from COOKIE_STRING.
 *    - Users typically copy the full cookie string from the browser
 *    - Cookie format: "__Secure-1PSID=xxx; SAPISID=yyy; ..."
 *    - If the user set COOKIE_STRING but forgot to set SAPISID separately
 *    - the program auto-extracts the SAPISID value from the cookie string via regex
 *    - Regex: /SAPISID=([^;]+)/
 *    - Better UX, fewer config mistakes
 * 
 * 5. [Multi-fingerprint rotation] Adds browser-fingerprint rotation to lower the odds of being flagged by Gemini.
 *    - User-Agent pool (8 real browser UAs across Windows/macOS/Linux)
 *    - Accept-Language pool (6 language preferences)
 *    - Sec-Ch-Ua pool (3 Chrome version markers)
 *    - Sec-Ch-Ua-Platform pool (3 OS platforms)
 *    - Weighted random selection mimicking real browser market share
 *    - Chrome ~72% (Windows/macOS/Linux), Firefox ~8%, Safari ~8%
 * 
 * 6. [Multi-cookie rotation] Supports cookies for multiple Google accounts, picked at random.
 *    - Env var separates multiple cookies with |: "cookie1| cookie2| cookie3"
 *    - Env var separates multiple SAPISIDs with |: "sapisid1| sapisid2| sapisid3"
 *    - Each request picks one cookie and its matching SAPISID at random
 *    - If the SAPISID count matches the cookie count, the indexed SAPISID is used
 *    - Drastically lowers the chance of a single Google account being rate-limited (429)
 * 
 * 7. [Random delay] Adds a small random delay before requests to mimic human timing.
 *    - Delay is random between 0 and fingerprintJitterMs (default 1500ms)
 *    - Retries also get a fresh random delay
 *    - Configurable: set FINGERPRINT_JITTER_MS=0 to disable
 *    - Works best combined with fingerprint rotation
 * 
 * 8. [SSE typewriter effect] OPTIONS preflight handled first, live incremental output, heartbeat keep-alive.
 *    The SSE format strictly follows the OpenAI spec:
 *    - First chunk: delta: { role: 'assistant' } (role only, no content)
 *    - Content chunk: delta: { content: '<incremental text>' } (delta computed and pushed live)
 *    - Final chunk: delta: { content: "" }, finish_reason: 'stop'
 *    - Heartbeat: sends ": heartbeat\n\n" SSE comment every 2 seconds
 * 
 * 9. [Full feature set] Tool calling (Function Calling), rate limiting, API auth,
 *    native Google API (Gemini CLI compatible), Responses API (Codex CLI compatible).
 * 
 * ============================================================================
 * Deployment notes:
 * ============================================================================
 * 1. Log in to the Cloudflare Dashboard -> Workers & Pages
 * 2. Create a Worker -> paste this code -> save and deploy
 * 3. Configure environment variables (optional):
 * 
 *    [Authentication]
 *    - COOKIE_STRING: Cookie string, multiple separated by |
 *      Format: "cookie_account1| cookie_account2| cookie_account3"
 *      Copy the full cookie from browser F12 -> Application -> Cookies
 *      Includes __Secure-1PSID, __Secure-3PSID, SAPISID, etc.
 *    - SAPISID: SAPISID value, multiple separated by |
 *      Format: "sapisid_1| sapisid_2| sapisid_3"
 *      If unset, it is auto-extracted from COOKIE_STRING
 * 
 *    [API security]
 *    - API_KEYS: API key JSON array, e.g. ["sk-gemini", "sk-my-key"]
 *      Leave empty or set to [] to skip key validation
 * 
 *    [Gemini config]
 *    - GEMINI_BL: Gemini build label
 *      Update this when you hit 405 errors
 *      How to get it: open gemini.google.com -> F12 -> Network -> search "boq_assistant"
 *    - DEFAULT_MODEL: default model name, e.g. "gemini-3.6-flash"
 *    - AUTH_USER: multi-account index, 0=first account, 1=second account
 * 
 *    [Performance tuning]
 *    - RETRY_ATTEMPTS: retry count, default 3
 *    - RETRY_DELAY_SEC: retry delay (seconds), default 2
 *    - REQUEST_TIMEOUT_SEC: request timeout (seconds), default 28
 *    - FINGERPRINT_JITTER_MS: max random pre-request delay (ms), default 1500
 *      Set to 0 to disable the random delay
 *    - RATE_LIMIT_MAX: max requests in the rate-limit window, default 3000
 *    - RATE_LIMIT_WINDOW: rate-limit window (seconds), default 60
 * 
 * Client configuration:
 *   Base URL: https://<your-worker>.workers.dev/v1
 *   API key: sk-gemini (or whatever key you configured)
 *   Model: gemini-3.6-flash
 * 
 * ============================================================================
 * Technical architecture:
 * ============================================================================
 * 
 * [Isolate model]
 * Cloudflare Workers handles each request with an Isolate (isolated environment):
 * - Cold start: top-level code re-runs and all variables re-initialize
 * - Hot start: an existing Isolate is reused, top-level code does not run, variables keep their last state
 * - env: injected per request by the CF platform and always holds the latest environment variables
 * 
 * [Config isolation principle]
 * 1. DEFAULT_CONFIG is an immutable template (read-only)
 * 2. getRequestConfig(env) creates a brand-new copy each time
 * 3. Config is read from env, with every field explicitly overridden via || null
 * 4. Every function receives config through its parameters
 * 5. No dependency on any global mutable state
 * 
 * [Why explicit override?]
 * With the if (env.X) CONFIG.X = env.X pattern:
 * - When env.X exists, CONFIG.X is updated OK
 * - When env.X is missing, the if is skipped and CONFIG.X keeps its old value
 * Using CONFIG.X = env.X || null guarantees an explicit assignment every time.
 * 
 * Ported from the original gemini-web2api v1.1.0 project
 * Original project: https://github.com/your-repo/gemini-web2api
 */

// ============================================================================
// Locked default config - read-only template only
// ============================================================================
// This is the "blueprint" for all request configs, used to generate an independent copy per request.
// This object is never modified; all changes happen on the per-request config copy.
// Object.freeze() guarantees immutability and prevents accidental global side effects.

var DEFAULT_CONFIG = {
  // ---- Retry config ----
  // How many times to retry automatically when a request fails
  // Each retry uses exponential backoff: delay = retryDelaySec * 2^attempt
  // e.g. first retry waits 2s, second 4s, third 8s
  retryAttempts: 3,
  // Base retry delay (seconds)
  // Actual delay = retryDelaySec * 2^attempt (exponential backoff)
  retryDelaySec: 2,

  // ---- Request timeout ----
  // Timeout for a single HTTP request (seconds)
  // Note: the CF Workers free tier has a 30s CPU time limit
  // Streaming requests reset CPU time as data arrives, so they are not bound by this
  // But the initial connection and first data chunk must arrive within the timeout
  requestTimeoutSec: 28,

  // ---- Gemini build label ----
  // Version marker of the Gemini frontend, used as a URL parameter for API requests
  // If you hit 405 Method Not Allowed, this value is stale
  // How to update it:
  //   1. Open https://gemini.google.com/app in a browser
  //   2. Press F12 to open DevTools
  //   3. Switch to the Network tab
  //   4. Search "boq_assistant" in any request URL
  //   5. Copy the newest version, e.g. "boq_assistant-bard-web-server_20260730.02_p0"
  geminiBl: 'boq_assistant-bard-web-server_20260716.08_p0',

  // ---- Multi-account support ----
  // Google supports signing into multiple accounts in one browser
  // null or "" means the default account (the first one signed in)
  // "0" is the first account, "1" the second, and so on
  // With a non-default account the Gemini URL gains a /u/1-style prefix
  authUser: null,

  // ---- XSRF token ----
  // Cross-site request forgery protection token
  // The Gemini web frontend uses it, but API calls usually do not need it
  // On 403 errors you can try extracting this from the browser
  xsrfToken: null,

  // ---- Default model ----
  // Model used when a client request does not specify the model parameter
  // Valid values are the keys of the MODELS dict
  defaultModel: 'gemini-3.6-flash',

  // ---- API key whitelist ----
  // List of keys used to validate client requests
  // Empty array [] disables validation, allowing all requests (not recommended for production)
  // Once set, clients must supply a valid key in the request headers
  // Supports Bearer Token, x-api-key, x-goog-api-key, and the ?key= URL parameter
  // Example: ["sk-gemini", "sk-my-custom-key"]
  apiKeys: ['sk-gemini'],

  // ---- Cookie auth ----
  // Gemini rate-limits anonymous requests aggressively (easy to hit 429 Too Many Requests)
  // A valid cookie greatly improves stability and lowers the odds of being rate-limited
  // cookieString: the full cookie string copied from the browser
  //   Format: "__Secure-1PSID=xxx; __Secure-3PSID=xxx; SAPISID=xxx; ..."
  //   Multiple cookies are supported (| separated), one picked at random per request
  //   Example: "cookie1| cookie2| cookie3"
  cookieString: null,
  // sapisid: the SAPISID value extracted from the cookie
  //   Used to build the SAPISIDHASH auth header required by Google APIs
  //   Format: "abc123/def456"
  //   If cookieString is set but sapisid is not, the program auto-extracts it
  //   Multiple supported (| separated), matching the cookies
  //   Example: "sapisid1| sapisid2| sapisid3"
  sapisid: null,

  // ---- Logging switch ----
  // Whether to print request logs to the console
  // Keep on in production for easier debugging
  // Log format: [HH:MM:SS] [LEVEL] message
  logRequests: true,

  // ---- Rate limiting ----
  // Request-frequency control at the Cloudflare Workers level
  // Protects against abuse and shields the upstream Gemini API
  rateLimit: {
    // Whether rate limiting is enabled
    enabled: true,
    // Max requests within the time window
    // Default 3000; set higher to avoid limiting normal usage
    // On abuse, lower it (e.g. 30-100)
    maxRequests: 3000,
    // Time window size (seconds)
    // 60 means at most maxRequests requests per minute
    windowSec: 60,
  },

  // ---- Fingerprint rotation config ----
  // Max random pre-request delay (milliseconds)
  // Mimics human timing to lower the odds of being flagged as automated
  // Default 1500ms (1.5s); set 0 to disable
  // Works best combined with User-Agent rotation
  fingerprintJitterMs: 1500,
};

// ============================================================================
// Multi-fingerprint rotation pool
// ============================================================================
// These fingerprint pools pick a different browser identity for every request.
// The goal is to make each request look like it comes from a different browser and device,
// lowering the odds of being detected as an automated script by Gemini.

/**
 * User-Agent rotation pool
 * 
 * Contains 8 real-browser User-Agent strings.
 * Covers Windows, macOS, and Linux.
 * Covers mainstream versions: Chrome 125-127, Firefox 128, Safari 17.4.
 * 
 * Each UA has a matching weight (UA_WEIGHTS) for weighted random selection.
 * Weights mimic real browser market share:
 *   - Chrome Windows: ~65% (two versions combined)
 *   - Safari macOS: ~8%
 *   - Chrome macOS: ~10%
 *   - Chrome Linux: ~7%
 *   - Firefox all platforms: ~10% (three versions combined)
 * 
 * The weights array maps 1:1 to USER_AGENTS and sums to 100.
 */

var USER_AGENTS = [
  // Chrome 127 on Windows 10/11 (highest share, ~35%)
  // The most mainstream browser config today
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36',
  // Chrome 126 on Windows 10/11 (~30%)
  // Previous Chrome version, still widely used
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
  // Safari 17.4 on macOS 14.5 (~8%)
  // Mac users use the system browser
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15',
  // Chrome 127 on macOS 14.5 (~10%)
  // Mac users who install Chrome
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36',
  // Chrome 127 on Linux (~7%)
  // Linux desktop users (developers)
  'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36',
  // Firefox 128 on Windows (~5%)
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0',
  // Firefox 128 on macOS (~3%)
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 14.5; rv:128.0) Gecko/20100101 Firefox/128.0',
  // Firefox 128 on Linux (~2%)
  'Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0',
];

// Weighted array, 1:1 with USER_AGENTS
// Sum = 35 + 30 + 8 + 10 + 7 + 5 + 3 + 2 = 100
// Mimics real market share: Chrome ~72%, Safari ~8%, Firefox ~10%
var UA_WEIGHTS = [35, 30, 8, 10, 7, 5, 3, 2];

/**
 * Weighted random User-Agent selection
 * 
 * Algorithm:
 * 1. Sum all weights (totalWeight = 100)
 * 2. Generate a random float between 0 and totalWeight
 * 3. Accumulate weights from the start; once the sum passes the random number
 * 4. return the User-Agent at that index
 * 
 * This ensures higher-weight UAs are picked more often.
 * 
 * @returns {string} the randomly selected User-Agent string
 */
function getRandomUserAgent() {
  // Step 1: compute the total weight
  var totalWeight = 0;
  for (var i = 0; i < UA_WEIGHTS.length; i++) {
    totalWeight += UA_WEIGHTS[i];
  }
  // Step 2: generate a random number in [0, totalWeight)
  var random = Math.random() * totalWeight;
  // Step 3: accumulate weights to find the winning bucket
  var cumulative = 0;
  for (var j = 0; j < USER_AGENTS.length; j++) {
    cumulative += UA_WEIGHTS[j];
    // Return the current UA once the sum passes the random number
    if (random < cumulative) {
      return USER_AGENTS[j];
    }
  }
  // Fallback: if floating-point precision missed, return the first one
  return USER_AGENTS[0];
}

/**
 * Accept-Language rotation pool
 * 
 * Contains 6 different browser language preferences.
 * Mimics browsers of users in different regions.
 * English-first (US/UK); some include Chinese, Japanese, Korean, or Spanish as a second language.
 * 
 * The q value is the priority weight:
 *   - q=0.9 means high priority for the second language
 *   - The first language omits q (defaults to 1.0)
 */
var ACCEPT_LANGUAGES = [
  'en-US,en;q=0.9',              // Pure English (US), the most common config
  'en-US,en;q=0.9,zh-CN;q=0.8',  // English-first with Chinese (heritage or students)
  'en-GB,en;q=0.9',              // British English (UK/Commonwealth)
  'en-US,en;q=0.9,ja;q=0.8',     // English-first with Japanese
  'en-US,en;q=0.9,ko;q=0.8',     // English-first with Korean
  'en-US,en;q=0.9,es;q=0.8',     // English-first with Spanish
];

/**
 * Random Accept-Language selection
 * Uniform random (every language preference is equally likely)
 * @returns {string} the randomly selected Accept-Language string
 */
function getRandomAcceptLanguage() {
  var idx = Math.floor(Math.random() * ACCEPT_LANGUAGES.length);
  return ACCEPT_LANGUAGES[idx];
}

/**
 * Sec-Ch-Ua rotation pool (Chrome User-Agent Client Hints)
 * 
 * Sec-Ch-Ua is an extra request header sent by Chromium-based browsers,
 * holding brand and version info. Only Chromium-family browsers send it.
 * Firefox and Safari do not send it.
 * 
 * Format: "Brand";v="MajorVersion"
 * - "Not)A;Brand";v="99" is Chromium's fixed marker
 * - "Google Chrome";v="127" is the Chrome major version
 * - "Chromium";v="127" is the Chromium engine version
 */
var SEC_CH_UA_POOLS = [
  // Chrome 127
  '"Not)A;Brand";v="99", "Google Chrome";v="127", "Chromium";v="127"',
  // Chrome 126
  '"Not)A;Brand";v="99", "Google Chrome";v="126", "Chromium";v="126"',
  // Chrome 125
  '"Not)A;Brand";v="99", "Google Chrome";v="125", "Chromium";v="125"',
];

/**
 * Sec-Ch-Ua-Platform rotation pool (OS platform marker)
 * 
 * Used with Sec-Ch-Ua to mark the browser's OS platform.
 * Should match the platform in the User-Agent.
 */
var SEC_CH_UA_PLATFORMS = [
  '"Windows"',   // Windows platform
  '"macOS"',     // macOS platform (case-sensitive)
  '"Linux"',     // Linux platform
];

/**
 * Random Sec-Ch-Ua selection (Chrome version marker)
 * @returns {string} the randomly selected Sec-Ch-Ua string
 */
function getRandomSecChUa() {
  var idx = Math.floor(Math.random() * SEC_CH_UA_POOLS.length);
  return SEC_CH_UA_POOLS[idx];
}

/**
 * Random Sec-Ch-Ua-Platform selection (OS platform marker)
 * @returns {string} the randomly selected platform string
 */
function getRandomSecChUaPlatform() {
  var idx = Math.floor(Math.random() * SEC_CH_UA_PLATFORMS.length);
  return SEC_CH_UA_PLATFORMS[idx];
}

// ============================================================================
// Model definitions
// ============================================================================
// Mapped from the MODE_CATEGORY enum in Gemini's web frontend JS
// 
// mode field meanings (MODE_CATEGORY enum values):
//   1 = FAST - Gemini Flash family, fastest
//   2 = THINKING - deep reasoning, higher output quality
//   3 = PRO - strongest model, needs a valid cookie to route correctly
//   4 = AUTO - Gemini picks the most suitable model
//   5 = FAST_DYNAMIC_THINKING - adaptive thinking depth
//   6 = FLASH_LITE - lightest model, fastest but lower quality
// 
// think field meanings (thinking mode):
//   0 = deep thinking enabled (the model reasons longer)
//   4 = AUTO (Gemini decides the thinking depth)

var MODELS = {
  'gemini-3.6-flash': {
    mode: 1,        // FAST
    think: 4,       // AUTO thinking depth
    desc: 'Latest all-around model (Gemini 3.6 Flash)',
  },
  'gemini-3.5-flash': {
    mode: 1,        // FAST
    think: 4,       // AUTO
    desc: 'Alias for gemini-3.6-flash (backend upgraded)',
  },
  'gemini-3.5-flash-thinking': {
    mode: 2,        // THINKING mode
    think: 0,       // deep thinking on
    desc: 'Deep thinking mode, longest output (~20k chars)',
  },
  'gemini-3.1-pro': {
    mode: 3,        // PRO
    think: 4,       // AUTO
    desc: 'Pro model (requires cookie for real routing)',
  },
  'gemini-auto': {
    mode: 4,        // AUTO model selection
    think: 4,       // AUTO
    desc: 'Auto model selection',
  },
  'gemini-3.5-flash-thinking-lite': {
    mode: 5,        // FAST_DYNAMIC_THINKING
    think: 0,       // thinking on
    desc: 'Dynamic thinking with adaptive depth',
  },
  'gemini-flash-lite': {
    mode: 6,        // FLASH_LITE
    think: 4,       // AUTO
    desc: 'Lightweight fast model',
  },
};

// ============================================================================
// Core: per-request config generator (concurrency cross-talk + multi-cookie + fingerprint rotation)
// ============================================================================

/**
 * Creates an independent config copy for the current request
 * 
 * [Why this function? - concurrent cross-talk]
 * Cloudflare Workers processes requests with an Isolate (isolated environment).
 * On cold start, top-level code re-runs and variables reset.
 * On hot start (Isolate reuse) top-level code does not re-run,
 * and globals keep whatever the previous request left behind.
 * 
 * When clients like WorkBuddy send 5-20 concurrent requests in a tiny window,
 * they may land on the same Isolate and share globals.
 * 
 * Example of cross-talk:
 *   Request A arrives -> CONFIG.cookieString = "cookie_a"
 *   Request B arrives -> CONFIG.cookieString = "cookie_b"  <- overwrites A!
 *   Request A continues -> uses "cookie_b" <- cross-talk!
 * 
 * [How to fix it? - request-level config isolation]
 * 1. Each request calls this function to build a fresh config from the DEFAULT_CONFIG template
 * 2. Custom config is read from env (injected per request by the CF platform)
 * 3. Every downstream function gets config via parameters - no global state
 * 4. Explicit assignment (env.X || null) prevents stale values on Isolate reuse
 * 
 * [Multi-cookie rotation]
 * If the env vars separate multiple cookies and SAPISIDs with |,
 * each request picks one pair at random.
 * This drastically lowers the chance of one account being rate-limited.
 * 
 * [Env var format]
 * COOKIE_STRING = "cookie_account1| cookie_account2| cookie_account3"
 * SAPISID = "sapisid_1| sapisid_2| sapisid_3"
 * 
 * @param {Object} env - Cloudflare Worker environment variables (per-request)
 * @returns {Object} the config copy belonging to this request
 */
function getRequestConfig(env) {
  // Build a brand-new config object from the default template
  // Copy field by field so every value is an independent primitive
  // No spread (...DEFAULT_CONFIG) to avoid shared references
  var config = {
    // ---- Basic config fields ----
    retryAttempts: DEFAULT_CONFIG.retryAttempts,
    retryDelaySec: DEFAULT_CONFIG.retryDelaySec,
    requestTimeoutSec: DEFAULT_CONFIG.requestTimeoutSec,
    geminiBl: DEFAULT_CONFIG.geminiBl,
    authUser: DEFAULT_CONFIG.authUser,
    xsrfToken: DEFAULT_CONFIG.xsrfToken,
    defaultModel: DEFAULT_CONFIG.defaultModel,
    apiKeys: DEFAULT_CONFIG.apiKeys,
    cookieString: DEFAULT_CONFIG.cookieString,
    sapisid: DEFAULT_CONFIG.sapisid,
    logRequests: DEFAULT_CONFIG.logRequests,
    fingerprintJitterMs: DEFAULT_CONFIG.fingerprintJitterMs,

    // ---- Nested object: rateLimit needs a deep copy ----
    // rateLimit is an object, so direct assignment would share the reference
    // Create a new object and copy each field
    rateLimit: {
      enabled: DEFAULT_CONFIG.rateLimit.enabled,
      maxRequests: DEFAULT_CONFIG.rateLimit.maxRequests,
      windowSec: DEFAULT_CONFIG.rateLimit.windowSec,
    },
  };

  // ================================================================
  // Environment variable overrides
  // env is the per-request environment object provided by Cloudflare
  // These are configured in the CF Dashboard and take effect automatically
  // ================================================================

  // ---- String fields: override only when set (defaults as fallback) ----
  if (env.GEMINI_BL) {
    config.geminiBl = env.GEMINI_BL;
  }
  if (env.DEFAULT_MODEL) {
    config.defaultModel = env.DEFAULT_MODEL;
  }

  // ---- Auth fields: use || to guarantee explicit override ----
  // These fields may be null or empty strings
  // Using || null ensures that even when env is undefined or empty,
  // the value is explicitly null - no stale values on Isolate reuse
  config.authUser = env.AUTH_USER || null;
  config.xsrfToken = env.XSRF_TOKEN || null;

  // ================================================================
  // Multi-cookie rotation support
  // ================================================================
  // Split the env string into an array on |
  // Filter empty strings (handles consecutive or leading/trailing |)
  // 
  // Env var format example:
  //   COOKIE_STRING = "cookie_account1| cookie_account2| cookie_account3"
  //   SAPISID = "sapisid_1| sapisid_2| sapisid_3"
  // 
  // A single element after splitting behaves like one cookie

  var cookieStrings = (env.COOKIE_STRING || '').split('|').filter(function (c) {
    return c.trim();  // drop empty strings
  });
  var sapisids = (env.SAPISID || '').split('|').filter(function (s) {
    return s.trim();  // drop empty strings
  });

  // Case 1: multiple cookies available
  if (cookieStrings.length > 0) {
    // Pick a random cookie index
    var cookieIdx = Math.floor(Math.random() * cookieStrings.length);
    config.cookieString = cookieStrings[cookieIdx].trim();

    // If SAPISID count matches cookie count, use the indexed SAPISID
    // Keeps cookies and SAPISIDs paired
    if (sapisids.length === cookieStrings.length) {
      config.sapisid = sapisids[cookieIdx].trim();
    } else if (sapisids.length > 0) {
      // If counts differ, pick a SAPISID at random
      var sapisidIdx = Math.floor(Math.random() * sapisids.length);
      config.sapisid = sapisids[sapisidIdx].trim();
    }
    // With one or zero SAPISIDs, leave it to the auto-extraction logic below
  }
  // Case 2: SAPISID only, no cookie
  else if (sapisids.length > 0) {
    // Pick a SAPISID at random
    var sapisidIdx = Math.floor(Math.random() * sapisids.length);
    config.sapisid = sapisids[sapisidIdx].trim();
  }
  // Case 3: neither cookie nor SAPISID
  // config.cookieString and config.sapisid stay at their default null

  // ================================================================
  // Smart fallback: auto-extract SAPISID from COOKIE_STRING
  // ================================================================
  // If SAPISID is empty but a cookie is present,
  // try extracting the SAPISID value from the cookie string with a regex
  // 
  // Cookie format example:
  //   "__Secure-1PSID=AJDrVf...; __Secure-3PSID=AJDrVf...; SAPISID=abc123/def456; ..."
  // 
  // What /SAPISID=([^;]+)/ does:
  //   SAPISID=   matches the literal "SAPISID="
  //   ([^;]+)    captures one or more non-semicolon chars (the SAPISID value)
  if (!config.sapisid && config.cookieString) {
    var match = config.cookieString.match(/SAPISID=([^;]+)/);
    if (match) {
      // match[1] is the first capture group, i.e. the SAPISID value
      // trim() removes any surrounding whitespace
      config.sapisid = match[1].trim();
    }
  }

  // ---- API keys: JSON array format, needs special parsing ----
  // env.API_KEYS is a string, e.g. '["sk-gemini", "sk-my-key"]'
  // Needs JSON.parse to become a real array
  if (env.API_KEYS) {
    try {
      config.apiKeys = JSON.parse(env.API_KEYS);
    } catch (e) {
      // On parse failure keep the defaults
      // Log the error but keep running
      console.error('[ERROR] Failed to parse API_KEYS: ' + e.message + ', using defaults');
    }
  }

  // ---- Numeric fields: need parseInt ----
  // Env vars are all strings
  // parseInt(value, 10) converts to a base-10 integer
  // isNaN() guards against invalid values
  if (env.RETRY_ATTEMPTS) {
    var ra = parseInt(env.RETRY_ATTEMPTS, 10);
    if (!isNaN(ra)) config.retryAttempts = ra;
  }
  if (env.RETRY_DELAY_SEC) {
    var rd = parseInt(env.RETRY_DELAY_SEC, 10);
    if (!isNaN(rd)) config.retryDelaySec = rd;
  }
  if (env.REQUEST_TIMEOUT_SEC) {
    var rt = parseInt(env.REQUEST_TIMEOUT_SEC, 10);
    if (!isNaN(rt)) config.requestTimeoutSec = rt;
  }
  // Fingerprint rotation random delay config
  if (env.FINGERPRINT_JITTER_MS) {
    var fj = parseInt(env.FINGERPRINT_JITTER_MS, 10);
    if (!isNaN(fj)) config.fingerprintJitterMs = fj;
  }

  // ---- Rate limit config ----
  if (env.RATE_LIMIT_MAX) {
    var rlmax = parseInt(env.RATE_LIMIT_MAX, 10);
    if (!isNaN(rlmax)) config.rateLimit.maxRequests = rlmax;
  }
  if (env.RATE_LIMIT_WINDOW) {
    var rlwin = parseInt(env.RATE_LIMIT_WINDOW, 10);
    if (!isNaN(rlwin)) config.rateLimit.windowSec = rlwin;
  }

  // Return the request-scoped config copy
  // This object is reclaimed with the Isolate after the request
  return config;
}

// ============================================================================
// Utility functions
// ============================================================================

/**
 * Logging function
 * 
 * Uses the logRequests switch in the request config to decide whether to log.
 * If no config is passed (e.g. when called inside getRequestConfig),
 * the DEFAULT_CONFIG logRequests setting is used.
 * 
 * Log format: [HH:MM:SS] [LEVEL] message
 * Example: [14:30:25] [INFO] Chat: model=gemini-3.6-flash, stream=true
 * 
 * @param {string} msg - the message to log
 * @param {string} [level] - log level, default 'INFO'. Options: INFO / WARN / ERROR
 * @param {Object} [config] - request config (optional, for concurrency safety)
 */
function log(msg, level, config) {
  // Default the level to INFO
  level = level || 'INFO';
  // Decide based on the config parameter
  // Use config.logRequests when present, else the default config
  var shouldLog = config ? config.logRequests : DEFAULT_CONFIG.logRequests;
  if (shouldLog) {
    // Build a timestamp, format: HH:MM:SS
    // toISOString() returns "2026-07-30T14:30:25.123Z"
    // split('T')[1] gives "14:30:25.123Z"
    // split('.')[0] gives "14:30:25"
    var ts = new Date().toISOString().split('T')[1].split('.')[0];
    console.log('[' + ts + '] [' + level + '] ' + msg);
  }
}

/**
 * Generates a UUID v4 (universally unique identifier)
 * 
 * UUID v4 format: xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx
 * 4 is the fixed version, and y's high bits are 10xx (the variant)
 * 
 * Prefers the built-in crypto.randomUUID() in Cloudflare Workers.
 * Falls back to Math.random() when unavailable (older runtimes).
 * The fallback is weaker randomness - not for security-sensitive use.
 * 
 * @returns {string} a UUID v4 string, e.g. "550e8400-e29b-41d4-a716-446655440000"
 */
function generateUUID() {
  // Prefer the CF Workers built-in (faster, stronger randomness)
  if (typeof crypto !== 'undefined' && crypto.randomUUID) {
    return crypto.randomUUID();
  }
  // Fallback: hand-build a UUID v4 string
  // Use Math.random() for pseudo-random numbers
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
    // Generate a random integer 0-15
    var r = Math.random() * 16 | 0;
    // x positions use the random value directly
    // y positions force high bits to 10xx (UUID v4 variant: 8,9,a,b)
    var v = c === 'x' ? r : (r & 0x3 | 0x8);
    return v.toString(16);
  });
}

/**
 * Generates a short ID
 * 
 * Takes the first length hex chars of a UUID (dashes removed).
 * For chat-completion IDs, tool-call IDs, request IDs - anywhere a full UUID is overkill.
 * 
 * @param {number} [length] - desired ID length, default 12 chars
 * @returns {string} a short ID string, e.g. "a1b2c3d4e5f6"
 */
function generateShortId(length) {
  var len = length || 12;
  // Strip dashes from the UUID and take the first len chars
  return generateUUID().replace(/-/g, '').substring(0, len);
}

/**
 * Gets the current Unix timestamp (seconds)
 * 
 * A Unix timestamp is seconds since 1970-01-01 00:00:00 UTC.
 * Commonly used in the created field of API responses.
 * 
 * @returns {number} Unix timestamp (seconds), e.g. 1753872000
 */
function timestamp() {
  return Math.floor(Date.now() / 1000);
}

/**
 * Estimates the token count of text
 * 
 * Uses a simple heuristic for a rough estimate:
 * - Roughly 4 English chars = 1 token
 * - Not exact, but enough for rough resource estimates and logs
 * - Not precise, reference only
 * 
 * @param {string} text - the text to estimate
 * @returns {number} the estimated token count, at least 1
 */
function estimateTokens(text) {
  if (!text) return 0;
  // At least 1, to avoid division by zero
  return Math.max(1, Math.ceil(text.length / 4));
}

/**
 * Builds the SAPISID auth hash
 * 
 * Google APIs authenticate with a time-based SHA-1 hash.
 * The hash proves the request comes from a user with a valid Google session.
 * 
 * Algorithm steps:
 * 1. Get the current Unix timestamp (seconds)
 * 2. Build the input string: "{timestamp} {sapisid} https://gemini.google.com"
 * 3. Hash the input with SHA-1
 * 4. Convert the hash to a hex string
 * 5. Return the formatted string: "SAPISIDHASH {timestamp}_{hex_hash}"
 * 
 * Example: SAPISIDHASH 1753872000_a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0
 * 
 * @param {string} sapisid - the SAPISID value from the Google cookie
 * @returns {Promise<string>} the auth hash string
 */
async function makeSapisidHash(sapisid) {
  // Get the current timestamp
  var ts = timestamp();
  // Build the hash input (identical to the Google web frontend)
  var input = ts + ' ' + sapisid + ' https://gemini.google.com';

  // Encode the input string as UTF-8 bytes
  var encoder = new TextEncoder();
  var data = encoder.encode(input);

  // Hash with the Web Crypto API (SHA-1)
  var hashBuffer = await crypto.subtle.digest('SHA-1', data);

  // Convert the hash (ArrayBuffer) to a hex string
  var hashArray = Array.from(new Uint8Array(hashBuffer));
  var hashHex = hashArray.map(function (b) {
    // Each byte becomes two hex digits
    return b.toString(16).padStart(2, '0');
  }).join('');

  // Return the formatted auth string
  return 'SAPISIDHASH ' + ts + '_' + hashHex;
}

/**
 * Gets the multi-account URL prefix
 * 
 * Google supports multiple accounts signed in to one browser.
 * Non-default accounts add an index to the Gemini URL path:
 * - Default account: https://gemini.google.com/app
 * - Second account: https://gemini.google.com/u/1/app
 * - Third account: https://gemini.google.com/u/2/app
 * 
 * @param {Object} config - the request config object
 * @returns {string} URL prefix like "/u/1"; empty string "" for the default account
 */
function getAccountPrefix(config) {
  var authUser = config.authUser;
  // null, undefined, or "" means the default account
  if (authUser === null || authUser === undefined || authUser === '') {
    return '';
  }
  // Return the prefix with a leading slash
  return '/u/' + authUser;
}

// ============================================================================
// Gemini API request building
// ============================================================================
// Gemini's internal API uses complex nested array structures.
// These functions build request bodies and headers identical to Gemini's web frontend.
// This is the foundation of the whole program.

/**
 * Builds the Gemini API request body
 * 
 * Gemini internally uses a nested 80-element array as the request body.
 * This structure was reverse-engineered from Gemini's web frontend JS.
 * 
 * Key fields:
 *   inner[0]: user message and metadata
 *     [prompt, message index, image data, attachments, metadata, context ID, is-new-conversation]
 *   inner[1]: language ["en"]
 *   inner[2]: conversation context (empty = new conversation)
 *   inner[6]: continuing-conversation flag [0]
 *   inner[7]: streaming flag 1
 *   inner[10]: streaming flag 1
 *   inner[11]: safety filter level (0=basic, 1=strict, 2=strictest)
 *   inner[17]: thinking mode [[thinkMode]]
 *     thinkMode=0: deep thinking on
 *     thinkMode=4: automatic
 *   inner[18]: extended-thinking flag 0
 *   inner[30]: output format [4]
 *   inner[41]: response type [2]
 *   inner[59]: unique request ID (UUID v4)
 *   inner[61]: attachments []
 *   inner[79]: model selection (MODE_CATEGORY enum) - the most important field
 *     1=FAST, 2=THINKING, 3=PRO, 4=AUTO, 5=FAST_DYNAMIC_THINKING, 6=FLASH_LITE
 * 
 * Other indexes are null, meaning defaults.
 * 
 * Outer wrapper:
 *   outer = [null, json.dumps(inner)]
 *   then URL-encoded as the value of the f.req parameter
 * 
 * @param {string} prompt - the user's prompt text
 * @param {number} modelId - model category ID (MODE_CATEGORY enum: 1-6)
 * @param {number} thinkMode - thinking mode (0=deep, 4=auto)
 * @param {Object} config - the request config object
 * @returns {string} URL-encoded body string in the form "f.req=..."
 */
function buildPayload(prompt, modelId, thinkMode, config) {
  // Create an 80-element array, all initialized to null
  // This is the exact structure Gemini's web frontend uses
  var inner = new Array(80).fill(null);

  // --- User message ---
  // [prompt, message index, image, attachments, metadata, context ID, new-conversation flag]
  inner[0] = [prompt, 0, null, null, null, null, 0];

  // --- Language set to English ---
  inner[1] = ['en'];

  // --- Conversation context ---
  // All empty means a new conversation with no history
  inner[2] = ['', '', '', null, null, null, null, null, null, ''];

  // --- Continuing-conversation flag ---
  inner[6] = [0];

  // --- Streaming flags ---
  inner[7] = 1;    // enable streaming
  inner[10] = 1;   // streaming output

  // --- Safety filter level ---
  // 0 = basic filtering (recommended; won't over-block normal content)
  // 1 = strict (may false-positive)
  // 2 = strictest (very conservative)
  inner[11] = 0;

  // --- Thinking mode config ---
  // Doubly nested array: [[thinkMode]]
  // The outer array holds an inner array that holds the thinkMode value
  inner[17] = [[thinkMode]];

  // --- Extended-thinking flag ---
  inner[18] = 0;

  // --- Internal parameters ---
  // Exact meanings unknown, but kept identical to Gemini's web frontend
  inner[27] = 1;   // unknown flag
  inner[30] = [4]; // output format
  inner[41] = [2]; // response type
  inner[53] = 0;   // unknown flag

  // --- Unique request ID ---
  // A UUID v4 guarantees a globally unique id per request
  inner[59] = generateUUID();

  // --- Attachments ---
  // An empty array means no attachments
  inner[61] = [];

  // --- Other settings ---
  inner[68] = 1;   // unknown flag

  // Model selection (the most critical field)
  // MODE_CATEGORY enum values:
  //   1=FAST, 2=THINKING, 3=PRO
  //   4=AUTO, 5=FAST_DYNAMIC_THINKING, 6=FLASH_LITE
  inner[79] = modelId;

  // --- Outer wrapper ---
  // The Gemini body is doubly nested JSON:
  // Outer: [null, inner_json_string]
  var outer = [null, JSON.stringify(inner)];

  // --- Build URL-encoded params ---
  var params = new URLSearchParams();
  // Main data goes in the f.req param
  params.append('f.req', JSON.stringify(outer));

  // Optional: add the XSRF token
  // Usually unnecessary, but Gemini may require it in rare cases
  if (config.xsrfToken) {
    params.append('at', config.xsrfToken);
  }

  // Return the URL-encoded string
  // Example: f.req=%5Bnull%2C%22%5B%5B...%5D%5D%22%5D
  return params.toString();
}

/**
 * Builds the Gemini API request URL
 * 
 * URL format:
 * https://gemini.google.com{prefix}/_/BardChatUi/data/
 *   assistant.lamda.BardFrontendService/StreamGenerate
 *   ?bl={build_label}&hl=en&_reqid={request_id}&rt=c
 * 
 * Parameters:
 * - bl (build label): Gemini frontend build version, used for API versioning
 * - hl (host language): UI language, fixed to en
 * - _reqid: request ID, last 6 digits of the timestamp
 * - rt: request type; c is a normal chat request
 * 
 * @param {Object} config - the request config object
 * @returns {string} the full request URL
 */
function buildUrl(config) {
  // Get the multi-account URL prefix
  var prefix = getAccountPrefix(config);
  // Build the request ID (last 6 digits of the timestamp)
  // e.g. timestamp() = 1753872000 -> reqid = 872000
  var reqid = timestamp() % 1000000;
  // Assemble the full URL
  return 'https://gemini.google.com' + prefix +
    '/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate' +
    '?bl=' + config.geminiBl +
    '&hl=en' +
    '&_reqid=' + reqid +
    '&rt=c';
}

/**
 * Builds the Gemini API request headers (with multi-fingerprint rotation)
 * 
 * Multi-fingerprint rotation:
 * Each call picks a random combination of browser fingerprints:
 * - User-Agent: weighted random from 8 real browser UAs
 * - Accept-Language: uniform random from 6 preferences
 * - Sec-Ch-Ua: when a Chrome UA was picked, random Chrome version marker
 * - Sec-Ch-Ua-Platform: random OS platform
 * 
 * This makes each request look like a different browser and device,
 * lowering the odds of being flagged as an automated script.
 * 
 * Note: Firefox and Safari do not send Sec-Ch-Ua headers,
 * so they are only added when the UA is Chrome.
 * 
 * @param {Object} config - the request config object
 * @returns {Promise<Object>} the HTTP headers object
 */
async function buildHeaders(config) {
  // Get the multi-account URL prefix
  var prefix = getAccountPrefix(config);

  // Step 1: pick random browser fingerprints
  var selectedUA = getRandomUserAgent();           // weighted random UA
  var selectedLanguage = getRandomAcceptLanguage(); // uniform random language

  // Step 2: build the base headers
  var headers = {
    // Standard form-encoded content type (like a browser form)
    'Content-Type': 'application/x-www-form-urlencoded',
    // Declare the origin (must be gemini.google.com)
    'Origin': 'https://gemini.google.com',
    // Declare the referer page
    'Referer': 'https://gemini.google.com' + prefix + '/app',
    // Same-domain flag (makes Gemini treat it as internal)
    'X-Same-Domain': '1',
    // Use the randomly selected User-Agent
    'User-Agent': selectedUA,
    // Accept any response type
    'Accept': '*/*',
    // Use the randomly selected Accept-Language
    'Accept-Language': selectedLanguage,
    // Browser security-policy headers (standard modern-browser behavior)
    'Sec-Fetch-Dest': 'empty',
    'Sec-Fetch-Mode': 'cors',
    'Sec-Fetch-Site': 'same-origin',
  };

  // Step 3: add Sec-Ch-Ua headers when the UA is Chrome
  // Detected by checking if the User-Agent contains "Chrome"
  // Firefox UAs contain "Gecko" and "Firefox", not "Chrome"
  // Safari UAs contain "Safari" but not "Chrome"
  if (selectedUA.indexOf('Chrome') !== -1) {
    headers['Sec-Ch-Ua'] = getRandomSecChUa();              // Chrome version marker
    headers['Sec-Ch-Ua-Mobile'] = '?0';                      // desktop (not mobile)
    headers['Sec-Ch-Ua-Platform'] = getRandomSecChUaPlatform(); // OS platform
  }

  // Step 4: multi-account support
  // When a non-default account is used (authUser set), add the auth-user header
  if (prefix) {
    headers['X-Goog-AuthUser'] = String(config.authUser);
  }

  // Step 5: cookie auth (if any)
  // A valid cookie greatly improves request stability
  // and reduces 429 (rate-limit) and 403 (forbidden) errors
  if (config.cookieString) {
    headers['Cookie'] = config.cookieString;
  }

  // Step 6: SAPISID auth hash (if any)
  // A time-based SHA-1 hash proves the request comes from a valid Google session
  // Format: SAPISIDHASH {timestamp}_{sha1_hex_hash}
  if (config.sapisid) {
    headers['Authorization'] = await makeSapisidHash(config.sapisid);
  }

  return headers;
}

// ============================================================================
// Non-streaming API call
// ============================================================================

/**
 * Calls the Gemini API without streaming
 * 
 * Sends a request to the Gemini StreamGenerate endpoint and waits for the full response.
 * With automatic retries, exponential backoff, and detailed error handling.
 * 
 * [Retry policy]
 * Uses exponential backoff:
 * - First retry: wait retryDelaySec * 2^0 = 2s
 * - Second retry: wait retryDelaySec * 2^1 = 4s
 * - Third retry: wait retryDelaySec * 2^2 = 8s
 * 
 * [Error handling]
 * - 405: BL version stale; update the geminiBl config
 * - 429: rate-limited; wait Retry-After seconds and retry
 * - 403: a valid cookie is required
 * - Other: log the error and retry
 * 
 * [Fingerprint rotation]
 * Each retry rebuilds the headers with different browser fingerprints.
 * More chances of success, since different fingerprints may dodge different limits.
 * 
 * [Random delay]
 * A random delay between 0 and fingerprintJitterMs is added before each request.
 * Mimics human timing, lowering the odds of being flagged as a script.
 * 
 * @param {string} prompt - the user's prompt text
 * @param {number} modelId - model category ID (MODE_CATEGORY enum: 1-6)
 * @param {number} thinkMode - thinking mode (0=deep, 4=auto)
 * @param {Object} config - the request config object
 * @returns {Promise<string>} the raw API response text (with nested JSON)
 * @throws {Error} the last error after all retries fail
 */
async function geminiStreamGenerate(prompt, modelId, thinkMode, config) {
  // Add a small random delay before the request (mimic human timing)
  // Delay is uniform-random between 0 and fingerprintJitterMs ms
  // e.g. with fingerprintJitterMs=1500, delay is 0 to 1.5s
  if (config.fingerprintJitterMs > 0) {
    var jitter = Math.random() * config.fingerprintJitterMs;
    await new Promise(function (resolve) { setTimeout(resolve, jitter); });
  }

  // Build the body, headers, and URL
  var body = buildPayload(prompt, modelId, thinkMode, config);
  var headers = await buildHeaders(config);
  var url = buildUrl(config);

  // Keep the last error to throw after all retries fail
  var lastError;

  // Retry loop
  for (var attempt = 0; attempt < config.retryAttempts; attempt++) {
    try {
      // Rebuild headers on retry (fresh fingerprints)
      // Improves the odds of success
      if (attempt > 0) {
        headers = await buildHeaders(config);
        // Retries also get a fresh random delay
        // Avoid retrying at the exact same instant
        if (config.fingerprintJitterMs > 0) {
          var retryJitter = Math.random() * config.fingerprintJitterMs;
          await new Promise(function (resolve) { setTimeout(resolve, retryJitter); });
        }
      }

      // Create an AbortController for timeout control
      var controller = new AbortController();
      var timeout = setTimeout(function () {
        controller.abort();  // abort after the timeout
      }, config.requestTimeoutSec * 1000);

      // Send the HTTP POST request
      var response = await fetch(url, {
        method: 'POST',
        headers: headers,
        body: body,
        signal: controller.signal,  // wire up the abort signal
      });

      // Success - clear the timeout timer
      clearTimeout(timeout);

      // ============================================================
      // Error status handling
      // ============================================================

      // 405 Method Not Allowed: BL version stale
      // Gemini updated its frontend; sync the geminiBl config
      if (response.status === 405) {
        throw new Error('HTTP 405: Method Not Allowed - BL version may be stale, update geminiBl');
      }

      // 429 Too Many Requests: rate limited
      // Wait the server-specified Retry-After before retrying
      if (response.status === 429) {
        var retryAfter = parseInt(response.headers.get('Retry-After') || '5', 10);
        log('Got 429 rate limit, retrying in ' + retryAfter + 's...', 'WARN', config);
        if (attempt < config.retryAttempts - 1) {
          await new Promise(function (resolve) { setTimeout(resolve, retryAfter * 1000); });
          continue;  // skip to the next retry
        }
        throw new Error('HTTP 429: Too Many Requests - add a valid cookie or lower the request rate');
      }

      // 403 Forbidden: auth required
      if (response.status === 403) {
        throw new Error('HTTP 403: Forbidden - a valid cookie may be required');
      }

      // Other HTTP errors
      if (!response.ok) {
        var errorText = '';
        try {
          errorText = await response.text();
        } catch (e) {
          errorText = 'could not read the error body';
        }
        throw new Error('HTTP ' + response.status + ': ' + errorText.substring(0, 200));
      }

      // Success - return the response text
      return await response.text();

    } catch (error) {
      // Save the error
      lastError = error;

      // If retries remain, wait and retry
      if (attempt < config.retryAttempts - 1) {
        log('Retry ' + (attempt + 1) + '/' + config.retryAttempts + ': ' + error.message, 'WARN', config);
        // Exponential backoff: delay = base * 2^attempt
        var delay = config.retryDelaySec * Math.pow(2, attempt) * 1000;
        await new Promise(function (resolve) { setTimeout(resolve, delay); });
      }
    }
  }

  // All retries failed - throw the last error
  throw lastError;
}

// ============================================================================
// Text processing
// ============================================================================

/**
 * Removes code-execution traces from Gemini responses
 * 
 * Gemini sometimes includes code-execution references and output blocks like:
 * ```python?code_reference&code_event_index=0
 * ...code content...
 * ```
 * ```javascript?code_stdout&code_event_index=1
 * ...output content...
 * ```
 * 
 * These blocks are meaningless to end users and are removed for clean text.
 * 
 * Regex breakdown:
 * - ```(?:python|javascript|text): matches the opening triple backticks and language tag
 * - \?code_(?:reference|stdout): matches the code-execution parameters
 * - &code_event_index=\d+: matches the event index
 * - \n[\s\S]*?```: matches the block content (non-greedy) up to the closing backticks
 * - \n?: matches an optional trailing newline
 * 
 * @param {string} text - the raw response text
 * @param {boolean} [strip] - whether to trim whitespace, default true
 * @returns {string} the cleaned text
 */
function cleanGeminiText(text, strip) {
  // Default strip to true when not specified
  if (strip === undefined) strip = true;

  // Remove code-execution blocks
  // Global (g) and dotAll (s, so . matches newlines) replace
  text = text.replace(
    /```(?:python|javascript|text)\?code_(?:reference|stdout)&code_event_index=\d+\n[\s\S]*?```\n?/g,
    ''
  );

  // Trim based on the strip parameter
  return strip ? text.trim() : text;
}

// Agent-engine diagnostic filter: agent engines (e.g. AionUI's aioncore)
// surface internal diagnostics ("Token watermark override: ...",
// "Microcompact: ...", "Autocompact: ...") as visible text in the model
// stream. Once such a line lands in a conversation, clients echo it back
// and the model keeps reproducing it, ending its turn early. Strip these
// whole lines so they never reach any client conversation.
var AGENT_DIAG_LINE_RE = /^(?:Token watermark override: provider=\d+, local_estimate=\d+, using=\d+|Microcompact: cleared \d+ tool results \(~\d+ tokens freed\)|Autocompact threshold: \d+ tokens \(\d+% of \d+\)|Autocompact: summarized \d+ messages \(\d+ tokens . compact\)|Autocompact: skipped \(.*\)|Autocompact: disabled \(.*\)|Cache full miss: .*)$/;

// Same bounded patterns may also be echoed inline, glued to surrounding text
// on one line; strip the diagnostic text itself wherever it occurs.
var AGENT_DIAG_INLINE_RE = /(?:Token watermark override: provider=\d+, local_estimate=\d+, using=\d+|Microcompact: cleared \d+ tool results \(~\d+ tokens freed\)|Autocompact threshold: \d+ tokens \(\d+% of \d+\)|Autocompact: summarized \d+ messages \(\d+ tokens . compact\))[ \t]*/g;

function stripAgentDiagnostics(text) {
  var lines = text.split('\n');
  var kept = [];
  for (var i = 0; i < lines.length; i++) {
    var body = lines[i].replace(/\r$/, '');
    if (AGENT_DIAG_LINE_RE.test(body)) continue;
    kept.push(body.replace(AGENT_DIAG_INLINE_RE, ''));
  }
  return kept.join('\n');
}

function makeDiagnosticLineFilter() {
  var buf = '';
  return {
    feed: function (chunk) {
      buf += chunk;
      var idx = buf.lastIndexOf('\n');
      if (idx < 0) return '';
      var complete = buf.slice(0, idx + 1);
      buf = buf.slice(idx + 1);
      return stripAgentDiagnostics(complete);
    },
    flush: function () {
      var out = stripAgentDiagnostics(buf);
      buf = '';
      return out;
    }
  };
}

/**
 * Extracts the final text from a raw Gemini API response
 * 
 * The Gemini API returns multi-line nested JSON, one entry per line:
 * [["wrb.fr", "[[...]]", ...], ...]
 * 
 * Parsing logic:
 * 1. Check for a BardErrorInfo error
 * 2. Split the raw response by line
 * 3. Skip lines without the "wrb.fr" marker (non-data lines)
 * 4. Skip lines shorter than 200 chars (too short to be data)
 * 5. Parse each line's JSON (doubly nested)
 * 6. Extract text from inner[4]
 * 7. Return the last non-empty text (usually the full final response)
 * 
 * Data structure:
 * Outer JSON array:
 *   [0]: "wrb.fr" (data marker)
 *   [1]: reserved
 *   [2]: inner JSON string
 * Inner JSON array:
 *   [4]: conversation content array
 *     [*][0]: content type
 *     [*][1]: text array
 * 
 * @param {string} raw - the raw API response text
 * @returns {string} the extracted and cleaned final text
 * @throws {Error} if a BardErrorInfo error is detected
 */
function extractResponseText(raw) {
  // Step 1: check for BardErrorInfo
  // Format: BardErrorInfo [error code]
  // e.g. BardErrorInfo [10] means the request was rejected
  var bardErr = raw.match(/BardErrorInfo\s*\[(\d+)\]/);
  if (bardErr) {
    throw new Error('Gemini upstream rejected request: BardErrorInfo [' + bardErr[1] + ']');
  }

  // Step 2: collect all extracted text fragments
  var texts = [];

  // Step 3: split the raw response by line
  var lines = raw.split('\n');
  for (var i = 0; i < lines.length; i++) {
    var line = lines[i];

    // Skip lines without "wrb.fr" (not data lines)
    // Skip lines shorter than 200 chars (too short)
    if (line.indexOf('"wrb.fr"') === -1 || line.length < 200) continue;

    try {
      // Step 4: parse the outer JSON
      var arr = JSON.parse(line);
      // Extract the inner JSON string (arr[0][2])
      var innerStr = arr[0][2];

      // Skip empty or too-short inner JSON
      if (!innerStr || innerStr.length < 50) continue;

      // Step 5: parse the inner JSON
      var inner = JSON.parse(innerStr);

      // Step 6: check inner[4] exists and has content
      if (Array.isArray(inner) && inner.length > 4 && inner[4]) {
        var parts = inner[4];
        // Iterate each part of inner[4]
        for (var j = 0; j < parts.length; j++) {
          var part = parts[j];
          // part[1] holds the text data
          if (Array.isArray(part) && part.length > 1 && part[1]) {
            if (Array.isArray(part[1])) {
              var textItems = part[1];
              // Iterate the text items
              for (var k = 0; k < textItems.length; k++) {
                var t = textItems[k];
                // Collect non-empty strings
                if (typeof t === 'string' && t.length > 0) {
                  texts.push(t);
                }
              }
            }
          }
        }
      }
    } catch (e) {
      // JSON parse error - the response may be truncated
      // Keep going; don't abort the whole parse
    }
  }

  // Step 7: take the last non-empty text
  // Gemini responses accumulate; the last text usually holds the full content
  var text = '';
  for (var m = texts.length - 1; m >= 0; m--) {
    if (texts[m].trim()) {
      text = texts[m];
      break;
    }
  }

  // Step 8: clean code traces and return
  return stripAgentDiagnostics(cleanGeminiText(text));
}

// ============================================================================
// OpenAI format conversion
// ============================================================================

/**
 * Converts an OpenAI message list into a Gemini prompt
 * 
 * This is the program's "translation layer": it turns the OpenAI Chat Completions format
 * into plain text Gemini can understand.
 * 
 * Conversion rules:
 * ┌──────────────┬──────────────────────────────────────────┐
 * | OpenAI Role | Gemini format                            |
 * ├──────────────┼──────────────────────────────────────────┤
 * │ system       │ [System instruction]: {content}           │
 * │ assistant    │ [Assistant]: {content}                    │
 * │ tool         │ [Tool result for {name}]: {content}       │
 * | user        | {content} (used as-is)                   |
 * | tool call   | ```tool_call\n{json}\n``` code block    |
 * └──────────────┴──────────────────────────────────────────┘
 * 
 * Messages are separated by double newlines (\n\n).
 * 
 * @param {Array} messages - messages in OpenAI format
 *   Each message: { role: string, content: string | array }
 * @param {Array} [tools] - available tool/function definitions (optional)
 *   Each tool: { type: "function", function: { name, description, parameters } }
 * @returns {string} the converted prompt text
 */
function messagesToPrompt(messages, tools) {
  // Array holding the message segments
  var parts = [];

  // ================================================================
  // Step 1: prepend tool-usage instructions when tools are given
  // ================================================================
  if (tools && tools.length > 0) {
    // Normalize the tool definition format
    // Supports both formats:
    //   1. { type: "function", function: { name, description, parameters } }
    //   2. { name, description, parameters } (shorthand)
    var toolDefs = [];
    for (var ti = 0; ti < tools.length; ti++) {
      var tool = tools[ti];
      var fn = (tool.type === 'function') ? (tool.function || tool) : tool;
      toolDefs.push({
        name: fn.name || tool.name || '',
        description: fn.description || tool.description || '',
        parameters: fn.parameters || tool.parameters || {},
      });
    }

    // Build the tool-usage instructions text
    // Including:
    //   1. how to format tool calls
    //   2. JSON definitions of all tools
    parts.push(
      '[System instruction]: You have access to tools. ' +
      'To call a tool, respond with:\n' +
      '```tool_call\n{"name": "func_name", "arguments": {...}}\n```\n' +
      'Only use tool_call blocks when needed.\n\n' +
      'Available tools:\n' + JSON.stringify(toolDefs, null, 2)
    );
  }

  // ================================================================
  // Step 2: process each message
  // ================================================================
  for (var mi = 0; mi < messages.length; mi++) {
    var msg = messages[mi];
    var role = msg.role || 'user';     // role, default user
    var content = msg.content || '';    // message content

    // If content is an array (multimodal), extract the text parts
    // e.g. [{ type: "text", text: "Hello" }, { type: "image_url", ... }]
    // Keep only parts with type "text" or "input_text"
    if (Array.isArray(content)) {
      var textParts = [];
      for (var ci = 0; ci < content.length; ci++) {
        var c = content[ci];
        if (c.type === 'text' || c.type === 'input_text') {
          textParts.push(c.text || '');
        }
      }
      content = textParts.join(' ');
    }

    // Format differently per role
    if (role === 'system') {
      // System message: add an instruction prefix
      parts.push('[System instruction]: ' + content);
    } else if (role === 'assistant') {
      // Assistant message: check for tool calls
      if (msg.tool_calls && msg.tool_calls.length > 0) {
        // Convert tool calls to code-block format
        var tcStrs = [];
        for (var tci = 0; tci < msg.tool_calls.length; tci++) {
          var tc = msg.tool_calls[tci];
          var fn = tc.function || {};
          tcStrs.push(
            '```tool_call\n' +
            '{"name": "' + fn.name + '", "arguments": ' + (fn.arguments || '{}') + '}\n' +
            '```'
          );
        }
        parts.push('[Assistant]: ' + (content || '') + '\n' + tcStrs.join('\n'));
      } else {
        parts.push('[Assistant]: ' + content);
      }
    } else if (role === 'tool') {
      // Tool response: prefix with the result marker and tool name
      parts.push('[Tool result for ' + (msg.name || 'unknown') + ']: ' + content);
    } else {
      // User message: use the content as-is
      parts.push(content || '');
    }
  }

  // Step 3: join parts with double newlines, dropping empties
  return parts.filter(function (p) { return p; }).join('\n\n');
}

/**
 * Parses tool calls from the response text
 * 
 * Tool-call format (inside the response text):
 * ```tool_call
 * {"name": "get_weather", "arguments": {"city": "Beijing"}}
 * ```
 * 
 * After parsing, converts to OpenAI-format tool-call objects:
 * {
 *   id: "call_xxxxxxxxxxxx",
 *   type: "function",
 *   function: {
 *     name: "get_weather",
 *     arguments: '{"city":"Beijing"}'
 *   }
 * }
 * 
 * @param {string} text - response text that may contain tool calls
 * @returns {Object} { cleanText: cleaned plain text, toolCalls: tool-call array }
 */
function parseToolCalls(text) {
  var toolCalls = [];

  // Regex to find tool_call code blocks
  // /```tool_call\s*\n(.*?)\n```/gs
  // g: global (all matches, not just the first)
  // s: dotAll (allows . to match newlines)
  var pattern = /```tool_call\s*\n(.*?)\n```/gs;
  var match;

  // Loop extracting every tool call
  while ((match = pattern.exec(text)) !== null) {
    try {
      // match[1] is the first capture group - the JSON inside the tool_call block
      var data = JSON.parse(match[1].trim());

      // Build the OpenAI-format tool-call object
      toolCalls.push({
        id: 'call_' + generateShortId(8),       // unique call ID
        type: 'function',
        function: {
          name: data.name,                       // function name
          arguments: JSON.stringify(data.arguments || {}),  // args (must be a JSON string)
        },
      });
    } catch (e) {
      // JSON parse failed - skip the malformed block
      // Do not abort the whole parse
    }
  }

  // Remove all tool_call blocks from the text
  var cleanText = text.replace(pattern, '').trim();

  return {
    cleanText: cleanText,    // cleaned plain text
    toolCalls: toolCalls     // tool-call array
  };
}

/**
 * Converts Google native API format to a prompt
 * 
 * Supports the native API format of the Google Gemini CLI (generateContent).
 * Format example:
 * {
 *   "systemInstruction": {
 *     "parts": [{"text": "You are a helpful assistant"}]
 *   },
 *   "contents": [
 *     {"role": "user", "parts": [{"text": "Hello"}]},
 *     {"role": "model", "parts": [{"text": "Hello! How can I help you?"}]}
 *   ]
 * }
 * 
 * Conversion rules:
 * - systemInstruction.parts[].text → "[System instruction]: {text}"
 * - contents[].role="model" → "[Assistant]: {text}"
 * - contents[].role="user" -> {text} (used as-is)
 * 
 * @param {Object} req - the request object in Google API format
 * @returns {string} the converted prompt text
 */
function googleContentsToPrompt(req) {
  var parts = [];

  // Handle the system instruction (systemInstruction)
  var sysInst = req.systemInstruction;
  if (sysInst && sysInst.parts) {
    var sysTextParts = [];
    for (var si = 0; si < sysInst.parts.length; si++) {
      var sp = sysInst.parts[si];
      if (sp.text) sysTextParts.push(sp.text);
    }
    var sysText = sysTextParts.join(' ');
    if (sysText) {
      parts.push('[System instruction]: ' + sysText);
    }
  }

  // Handle the conversation contents
  var contents = req.contents || [];
  for (var ci = 0; ci < contents.length; ci++) {
    var content = contents[ci];
    var role = content.role || 'user';
    var textParts = [];
    var partsArr = content.parts || [];
    for (var pi = 0; pi < partsArr.length; pi++) {
      if (partsArr[pi].text) textParts.push(partsArr[pi].text);
    }
    var text = textParts.join(' ');

    // model role -> Assistant prefix
    if (role === 'model') {
      parts.push('[Assistant]: ' + text);
    } else {
      parts.push(text);
    }
  }

  return parts.filter(function (p) { return p; }).join('\n\n');
}

// ============================================================================
// Rate limiting (serverless-safe in-memory store)
// ============================================================================

// A Map stores each IP's request history
// Map advantages over a plain Object:
// 1. Any key type (strings here)
// 2. Built-in size property
// 3. Better iteration performance
var rateLimitStore = new Map();

/**
 * Checks whether a request exceeds the rate limit (sliding window)
 * 
 * Algorithm steps:
 * 1. Get the current time and the IP's history
 * 2. Filter to requests inside the window
 * 3. If the valid count reaches the threshold -> reject (false)
 * 4. Otherwise record and allow (true)
 * 
 * [Memory management]
 * Since a Workers Isolate can live long (hot-start reuse),
 * rateLimitStore entries grow forever without cleanup, leaking memory.
 * 
 * Cleanup strategy:
 * - 5% chance of a global sweep per check (Math.random() < 0.05)
 * - Iterate all IPs, dropping expired or empty entries
 * - 5% keeps sweeps infrequent enough not to hurt performance
 * 
 * @param {string} clientIP - the client IP
 * @param {Object} config - the request config object
 * @returns {boolean} true = allow, false = rate-limited
 */
function checkRateLimit(clientIP, config) {
  // Bypass when rate limiting is off
  if (!config.rateLimit || !config.rateLimit.enabled) return true;

  var now = Date.now();
  // Window in ms (config stores seconds)
  var windowMs = config.rateLimit.windowSec * 1000;
  // Storage key (prefixed to avoid collisions)
  var key = 'rl:' + clientIP;

  // Get the IP's history, filtered to the current window
  var timestamps = (rateLimitStore.get(key) || []).filter(function (t) {
    return now - t < windowMs;
  });

  // Check against the threshold
  if (timestamps.length >= config.rateLimit.maxRequests) {
    return false;  // reject the request
  }

  // Record this request's timestamp
  timestamps.push(now);
  rateLimitStore.set(key, timestamps);

  // ================================================================
  // Random-probability cleanup of stale keys (5%)
  // ================================================================
  // Prevents stale cold-IP entries piling up under sustained load
  // ~once per 20 checks, so it never runs too often
  if (Math.random() < 0.05) {
    // forEach over every Map entry
    rateLimitStore.forEach(function (v, k) {
      // Keep only valid (unexpired) records
      var valid = v.filter(function (t) {
        return now - t < windowMs;
      });
      if (valid.length === 0) {
        // No valid records left - delete the whole entry
        rateLimitStore.delete(k);
      } else {
        // Update to the array of valid records
        rateLimitStore.set(k, valid);
      }
    });
  }

  return true;  // allow the request
}

// ============================================================================
// API key validation
// ============================================================================

/**
 * Validates the API key (multiple auth methods)
 * 
 * Auth methods in priority order:
 * 1. Authorization: Bearer <key> (standard Bearer token, recommended)
 * 2. x-api-key: <key> (custom header, common in OpenAI SDKs)
 * 3. x-goog-api-key: <key> (Google-style header)
 * 4. URL query param ?key=<key> (least recommended; key leaks in the URL)
 * 
 * An empty apiKeys [] disables validation and allows every request.
 * For private networks or when other security is in place.
 * 
 * @param {Request} request - the HTTP request
 * @param {Object} config - the request config object
 * @returns {boolean} true = authenticated, false = failed
 */
function checkApiKey(request, config) {
  // Get the API key whitelist
  var keys = config.apiKeys || [];

  // No keys configured - allow everything (no-validation mode)
  if (keys.length === 0) return true;

  // ================================================================
  // Method 1: Authorization: Bearer <key>
  // ================================================================
  var auth = request.headers.get('Authorization') || '';
  // Check for the "Bearer " prefix
  if (auth.indexOf('Bearer ') === 0) {
    // Extract the token after "Bearer " (7 chars)
    var token = auth.slice(7);
    // indexOf checks the token against the whitelist
    if (keys.indexOf(token) !== -1) return true;
  }

  // ================================================================
  // Methods 2 & 3: x-api-key / x-goog-api-key
  // ================================================================
  var headerNames = ['x-api-key', 'x-goog-api-key'];
  for (var i = 0; i < headerNames.length; i++) {
    var value = request.headers.get(headerNames[i]) || '';
    if (keys.indexOf(value) !== -1) return true;
  }

  // ================================================================
  // Method 4: URL query param ?key=<key>
  // ================================================================
  var url = new URL(request.url);
  var keyParam = url.searchParams.get('key');
  if (keyParam && keys.indexOf(keyParam) !== -1) return true;

  // Every auth method failed
  return false;
}

// ============================================================================
// HTTP response building
// ============================================================================

/**
 * Sends a JSON HTTP response
 * 
 * Sets CORS headers so any origin can call it.
 * 
 * @param {Object} data - response data (JSON.stringify'd)
 * @param {number} [status] - HTTP status, default 200
 * @returns {Response} the HTTP response object
 */
function sendJSON(data, status) {
  if (status === undefined) status = 200;
  // Serialize the data as JSON
  var body = JSON.stringify(data);
  // Build and return the Response
  return new Response(body, {
    status: status,
    headers: {
      'Content-Type': 'application/json; charset=utf-8',
      'Access-Control-Allow-Origin': '*',           // allow all origins
      'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',  // allowed methods
      'Access-Control-Allow-Headers': '*',           // allow all headers
    },
  });
}

/**
 * Sends an SSE (Server-Sent Events) streaming response
 * 
 * SSE is a protocol for pushing real-time data to the client.
 * Compared to WebSocket, SSE is simpler:
 * - one-way (server -> client)
 * - over plain HTTP
 * - automatic reconnection
 * 
 * Data format:
 * data: {json}\n\n
 * 
 * Special formats:
 * data: [DONE]\n\n  -> end of stream
 * : heartbeat\n\n   -> SSE comment (ignored by clients) to keep the connection alive
 * 
 * @param {ReadableStream} stream - the readable stream
 * @returns {Response} the streaming response
 */
function sendSSE(stream) {
  return new Response(stream, {
    headers: {
      'Content-Type': 'text/event-stream; charset=utf-8',  // required for SSE
      'Cache-Control': 'no-cache',                          // disable caching
      'Connection': 'keep-alive',                           // keep the connection open
      'X-Accel-Buffering': 'no',                            // disable nginx buffering
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
      'Access-Control-Allow-Headers': '*',
    },
  });
}

// ============================================================================
// Model parsing
// ============================================================================

/**
 * Parses a model name into its config parameters
 * 
 * Supports the @think= parameter to override the default thinking mode.
 * Example: "gemini-3.6-flash@think=0"
 * meaning the Flash model with deep thinking on (think=0).
 * 
 * @param {string} modelName - the model name
 *   Format: "model" or "model@think=<number>"
 * @returns {Object} 
 *   - modelName: the real model name without the @think= suffix
 *   - modelId: MODE_CATEGORY enum value (1-6)
 *   - thinkMode: thinking mode (0=deep, 4=auto)
 *   - error: error message, null when OK
 */
function resolveModel(modelName) {
  var thinkOverride = null;
  var actualModelName = modelName;

  // Check for the @think= parameter
  if (modelName.indexOf('@think=') !== -1) {
    var parts = modelName.split('@think=');
    actualModelName = parts[0];           // the real model name
    thinkOverride = parseInt(parts[1], 10);  // the thinking override
    if (isNaN(thinkOverride)) {
      return { error: 'invalid think parameter: ' + parts[1] };
    }
  }

  // Look up the model config
  var cfg = MODELS[actualModelName];
  if (!cfg) {
    return { error: 'unknown model: ' + actualModelName };
  }

  // Return the parse result
  return {
    modelName: actualModelName,
    modelId: cfg.mode,                                            // model category ID
    thinkMode: thinkOverride !== null ? thinkOverride : cfg.think,  // override or default
    error: null,
  };
}

// ============================================================================
// Core request handler - /v1/chat/completions
// ============================================================================

/**
 * Handles /v1/chat/completions requests
 * 
 * The core OpenAI-compatible endpoint and the most critical function in the program.
 * Converts OpenAI-format chat requests to Gemini format and returns the response.
 * 
 * [Two modes supported]
 * 1. Non-streaming (stream=false):
 *    - Wait for Gemini's full response
 *    - Parse once and return a JSON response
 *    - For tool calls (the full response is needed to parse tool_call blocks)
 * 
 * 2. Streaming (stream=true):
 *    - Read Gemini's stream in real time
 *    - Compute the delta (current total - previous total)
 *    - Push each delta to the client immediately (typewriter effect)
 *    - With a heartbeat keep-alive (SSE comment every 2s)
 * 
 * [SSE format strictly follows the OpenAI spec]
 * First:  { delta: { role: 'assistant' }, finish_reason: null }
 * Content:{ delta: { content: '<incremental text>' }, finish_reason: null }
 * Final:  { delta: { content: "" }, finish_reason: 'stop' }
 * 
 * @param {Request} request - the HTTP request
 * @param {Object} body - the parsed request body (OpenAI Chat Completions format)
 * @param {Object} config - the request config object
 * @returns {Promise<Response>} the HTTP response object
 */
async function handleChatCompletions(request, body, config) {
  // ---- Step 1: parse the model ----
  var resolved = resolveModel(body.model || config.defaultModel);
  if (resolved.error) {
    return sendJSON({ error: { message: resolved.error } }, 400);
  }

  var modelName = resolved.modelName;
  var modelId = resolved.modelId;
  var thinkMode = resolved.thinkMode;
  var tools = body.tools || null;

  // ---- Step 2: convert messages to a prompt ----
  var prompt = messagesToPrompt(body.messages || [], tools);
  if (!prompt.trim()) {
    return sendJSON({ error: { message: 'empty prompt' } }, 400);
  }

  var stream = body.stream === true;
  var chatId = 'chatcmpl-' + generateShortId(12);

  log('Chat: model=' + modelName + ', stream=' + stream + ', tokens≈' + estimateTokens(prompt), 'INFO', config);

  // ================================================================
  // Case A: non-streaming or with tools
  // ================================================================
  // Tool calls need the full response to parse tool_call blocks
  // So stream=true is forced to non-streaming when tools are present
  if (!stream || tools) {
    try {
      // Call the Gemini API for the full response
      var raw = await geminiStreamGenerate(prompt, modelId, thinkMode, config);

      // Extract and clean the response text
      var text = extractResponseText(raw);
      var toolCalls = null;

      // If tools are enabled, parse tool calls
      if (tools && text) {
        var parsed = parseToolCalls(text);
        text = parsed.cleanText;
        toolCalls = parsed.toolCalls.length > 0 ? parsed.toolCalls : null;
      }

      // Build the response message
      var msg = { role: 'assistant', content: text || null };
      if (toolCalls) {
        msg.tool_calls = toolCalls;
      }

      var finishReason = toolCalls ? 'tool_calls' : 'stop';

      // Streaming requested but tools found - return as a single SSE chunk
      if (stream) {
        var encoder = new TextEncoder();
        var nonStreamSSE = new ReadableStream({
          start: function (controller) {
            var chunk = {
              id: chatId,
              object: 'chat.completion.chunk',
              created: timestamp(),
              model: modelName,
              choices: [{ index: 0, delta: msg, finish_reason: finishReason }],
            };
            controller.enqueue(encoder.encode('data: ' + JSON.stringify(chunk) + '\n\n'));
            controller.enqueue(encoder.encode('data: [DONE]\n\n'));
            controller.close();
          },
        });
        return sendSSE(nonStreamSSE);
      }

      // Standard non-streaming JSON response
      return sendJSON({
        id: chatId,
        object: 'chat.completion',
        created: timestamp(),
        model: modelName,
        choices: [{ index: 0, message: msg, finish_reason: finishReason }],
        usage: {
          prompt_tokens: estimateTokens(prompt),
          completion_tokens: estimateTokens(text),
          total_tokens: estimateTokens(prompt + text),
        },
      });

    } catch (error) {
      log('Upstream error: ' + error.message, 'ERROR', config);
      return sendJSON({ error: { message: 'upstream error: ' + error.message } }, 502);
    }
  }

  // ================================================================
  // Case B: true streaming SSE (typewriter effect)
  // ================================================================
  var streamEncoder = new TextEncoder();

  var streamBody = new ReadableStream({
    start: function (controller) {
      // ---- State variables ----
      var heartbeatTimer = null;  // heartbeat timer id
      var isFinished = false;      // stream finished (prevents double close)

      /**
       * Clears the heartbeat timer
       * Called on end or error to make sure the timer is cleared
       */
      var clearHeartbeat = function () {
        if (heartbeatTimer) {
          clearInterval(heartbeatTimer);
          heartbeatTimer = null;
        }
      };

      /**
       * Ends the stream safely
       * Ensures the final chunk and [DONE] are sent before closing
       * Prevents double-close errors
       * 
       * @param {string} reason - 'stop' for normal end, 'error' for abnormal
       */
      var finishStream = function (reason) {
        // Prevent double-end (error and close can both fire)
        if (isFinished) return;
        clearHeartbeat();
        isFinished = true;
        try {
          // Send the OpenAI-standard final chunk
          // IMPORTANT: delta.content must be "" (empty string), not {}
          // Clients like NextChat check that delta.content exists
          controller.enqueue(streamEncoder.encode('data: ' + JSON.stringify({
            id: chatId,
            object: 'chat.completion.chunk',
            created: timestamp(),
            model: modelName,
            choices: [{
              index: 0,
              delta: { content: "" },
              finish_reason: reason || 'stop'
            }],
          }) + '\n\n'));
          // Send the [DONE] marker (the SSE end-of-stream signal)
          controller.enqueue(streamEncoder.encode('data: [DONE]\n\n'));
          controller.close();
        } catch (e) {
          log('Failed to finish stream: ' + e.message, 'ERROR', config);
        }
      };

      // Use an async IIFE for the streaming logic
      // because ReadableStream's start cannot be async
      (async function () {
        try {
          // ---- Step 1: send the role-declaration chunk ----
          // Per the OpenAI spec, the first chunk has role only
          // Tells the client: "an assistant-role message is coming"
          controller.enqueue(streamEncoder.encode('data: ' + JSON.stringify({
            id: chatId,
            object: 'chat.completion.chunk',
            created: timestamp(),
            model: modelName,
            choices: [{
              index: 0,
              delta: { role: 'assistant' },
              finish_reason: null
            }],
          }) + '\n\n'));

          // ---- Step 2: start the heartbeat timer ----
          // Send an SSE comment (colon-prefixed line) every 2s
          // Clients ignore comments, but the connection stays alive
          // Prevents intermediaries from dropping an idle connection
          heartbeatTimer = setInterval(function () {
            if (!isFinished) {
              try {
                // SSE comment: colon-prefixed, ignored by clients
                controller.enqueue(streamEncoder.encode(': heartbeat\n\n'));
              } catch (e) {
                clearHeartbeat();  // write failed, stop the heartbeat
              }
            } else {
              clearHeartbeat();
            }
          }, 2000);

          // ---- Step 3: build and send the Gemini request ----
          var reqBody = buildPayload(prompt, modelId, thinkMode, config);
          var headers = await buildHeaders(config);
          var url = buildUrl(config);

          // Separate AbortController for timeout control
          var fetchController = new AbortController();
          var fetchTimeout = setTimeout(function () {
            fetchController.abort();  // abort the fetch on timeout
          }, (config.requestTimeoutSec - 2) * 1000);

          try {
            // POST to Gemini
            var response = await fetch(url, {
              method: 'POST',
              headers: headers,
              body: reqBody,
              signal: fetchController.signal,
            });
            clearTimeout(fetchTimeout);

            // Check the status code
            if (!response.ok) {
              var errorText = '';
              try {
                errorText = await response.text();
              } catch (e) {
                errorText = 'could not read the error body';
              }
              throw new Error('HTTP ' + response.status + ': ' + errorText.substring(0, 200));
            }

            // ---- Step 4: read the stream and forward deltas live ----
            var reader = response.body.getReader();
            var decoder = new TextDecoder();
            var buffer = '';      // line buffer (for partial lines)
            var prevText = '';    // tracks the text already sent
            var diagFilter = makeDiagnosticLineFilter();

            while (true) {
              var readResult = await reader.read();
              if (readResult.done) break;  // stream ended

              // Decode new data into the buffer
              buffer += decoder.decode(readResult.value, { stream: true });

              // Check for Gemini errors
              if (buffer.indexOf('BardErrorInfo') !== -1) {
                var match = buffer.match(/BardErrorInfo\s*\[(\d+)\]/);
                if (match) {
                  throw new Error('Gemini upstream rejected request: BardErrorInfo [' + match[1] + ']');
                }
              }

              // Split by line (Gemini sends one JSON per line)
              var lines = buffer.split('\n');
              // The last line may be partial; keep it in the buffer
              buffer = lines.pop() || '';

              // Iterate the complete lines
              for (var li = 0; li < lines.length; li++) {
                var line = lines[li];
                // Skip non-data or too-short lines
                if (line.indexOf('"wrb.fr"') === -1 || line.length < 200) continue;

                try {
                  // Parse Gemini's nested JSON
                  var arr = JSON.parse(line);
                  var innerStr = arr[0][2];
                  if (!innerStr || innerStr.length < 50) continue;

                  var inner2 = JSON.parse(innerStr);

                  // Extract the text
                  if (Array.isArray(inner2) && inner2.length > 4 && inner2[4]) {
                    var parts = inner2[4];
                    for (var pi = 0; pi < parts.length; pi++) {
                      var part = parts[pi];
                      if (Array.isArray(part) && part.length > 1 && part[1] && Array.isArray(part[1])) {
                        var textItems = part[1];
                        for (var ti = 0; ti < textItems.length; ti++) {
                          var t = textItems[ti];
                          // New content? (text length grew)
                          if (typeof t === 'string' && t.length > prevText.length) {
                            // Compute the delta text
                            // delta = current total - already-sent total
                            var delta = t.slice(prevText.length);
                            // Clean code traces (no trim; keep whitespace)
                            var cleaned = cleanGeminiText(delta, false);
                            cleaned = diagFilter.feed(cleaned);
                            if (cleaned) {
                              // Push the delta chunk immediately (typewriter)
                              controller.enqueue(streamEncoder.encode('data: ' + JSON.stringify({
                                id: chatId,
                                object: 'chat.completion.chunk',
                                created: timestamp(),
                                model: modelName,
                                choices: [{
                                  index: 0,
                                  delta: { content: cleaned },
                                  finish_reason: null
                                }],
                              }) + '\n\n'));
                            }
                            // Update the sent-text tracker
                            prevText = t;
                          }
                        }
                      }
                    }
                  }
                } catch (e) {
                  // JSON parse error; continue to the next line
                  // The response may have been truncated in transit
                }
              }
            }
          } finally {
            // Clear the timeout timer on success or failure
            clearTimeout(fetchTimeout);
          }

          // ---- Step 5: end the stream normally ----
          var diagTail = diagFilter.flush();
          if (diagTail) {
            controller.enqueue(streamEncoder.encode('data: ' + JSON.stringify({
              id: chatId,
              object: 'chat.completion.chunk',
              created: timestamp(),
              model: modelName,
              choices: [{
                index: 0,
                delta: { content: diagTail },
                finish_reason: null
              }],
            }) + '\n\n'));
          }
          finishStream('stop');

        } catch (error) {
          // Error handling: log and try to notify the client
          log('Stream error: ' + error.message, 'ERROR', config);
          try {
            if (!isFinished) {
              controller.enqueue(streamEncoder.encode('data: ' + JSON.stringify({
                error: { message: error.message, type: 'upstream_error' }
              }) + '\n\n'));
            }
          } catch (e) {
            // Failed to send the error - the client may be gone
          }
          finishStream('error');
        }
      })();  // run the async IIFE now
    },

    /**
     * Client-disconnect callback
     * Fires when the user closes the page or the network drops
     * Clean up resources and stop the heartbeat
     */
    cancel: function () {
      log('Client disconnected from stream', 'INFO', config);
    },
  });

  return sendSSE(streamBody);
}

/**
 * Handles /v1/responses requests (OpenAI Responses API)
 * 
 * OpenAI's newer Responses API (used by Codex CLI and similar tools).
 * Similar to Chat Completions, but with a slightly different message format.
 * 
 * Responses API format:
 * {
 *   "model": "gpt-4o",
 *   "input": [
 *     {"role": "user", "content": "Hello"},
 *     {"type": "function_call_output", "call_id": "...", "output": "..."}
 *   ],
 *   "instructions": "system instructions (optional)",
 *   "tools": [...]
 * }
 * 
 * This function converts Responses API format to Chat Completions format,
 * then reuses handleChatCompletions' logic.
 * 
 * @param {Request} request - the HTTP request
 * @param {Object} body - the parsed request body
 * @param {Object} config - the request config object
 * @returns {Promise<Response>} the HTTP response object
 */
async function handleResponses(request, body, config) {
  // Parse the model
  var resolved = resolveModel(body.model || config.defaultModel);
  if (resolved.error) {
    return sendJSON({ error: { message: resolved.error } }, 400);
  }

  var modelName = resolved.modelName;
  var modelId = resolved.modelId;
  var thinkMode = resolved.thinkMode;
  var messages = [];

  // Add the system instructions (instructions field)
  if (body.instructions) {
    messages.push({ role: 'system', content: body.instructions });
  }

  // Handle the input items
  var inputs = body.input || [];
  // Accept string-form input
  if (typeof inputs === 'string') {
    inputs = [inputs];
  }
  for (var i = 0; i < inputs.length; i++) {
    var item = inputs[i];
    if (typeof item === 'string') {
      // plain string -> user message
      messages.push({ role: 'user', content: item });
    } else if (item.type === 'function_call_output') {
      // function-call output -> tool message
      messages.push({
        role: 'tool',
        tool_call_id: item.call_id,
        name: item.name,
        content: item.output,
      });
    } else {
      // other message formats
      var content = item.content;
      if (Array.isArray(content)) {
        var textParts = [];
        for (var j = 0; j < content.length; j++) {
          var c = content[j];
          if (c.type === 'output_text') textParts.push(c.text || '');
        }
        content = textParts.join(' ');
      }
      messages.push({ role: item.role || 'user', content: content });
    }
  }

  // Normalize tool definitions
  var tools = body.tools;
  if (tools) {
    var normalizedTools = [];
    for (var ti = 0; ti < tools.length; ti++) {
      var t = tools[ti];
      if (t.type === 'function' && !t.function) {
        // shorthand -> full format
        normalizedTools.push({
          type: 'function',
          function: { name: t.name, description: t.description || '', parameters: t.parameters || {} },
        });
      } else {
        normalizedTools.push(t);
      }
    }
    tools = normalizedTools;
  }

  // Convert messages to a prompt
  var prompt = messagesToPrompt(messages, tools);
  if (!prompt.trim()) {
    return sendJSON({ error: { message: 'empty input' } }, 400);
  }

  try {
    // Call the Gemini API
    var raw = await geminiStreamGenerate(prompt, modelId, thinkMode, config);
    var text = extractResponseText(raw);
    var toolCalls = null;

    // Parse tool calls
    if (tools && text) {
      var parsed = parseToolCalls(text);
      text = parsed.cleanText;
      toolCalls = parsed.toolCalls.length > 0 ? parsed.toolCalls : null;
    }

    // Build Responses-API-format output
    var responseId = 'resp_' + generateShortId(16);
    var messageId = 'msg_' + generateShortId(12);
    var output = [];

    // Add tool-call output
    if (toolCalls) {
      for (var tci = 0; tci < toolCalls.length; tci++) {
        var tc = toolCalls[tci];
        output.push({
          type: 'function_call',
          id: tc.id,
          call_id: tc.id,
          name: tc.function.name,
          arguments: tc.function.arguments,
          status: 'completed',
        });
      }
    }

    // Add text output
    if (text || !toolCalls) {
      output.push({
        type: 'message',
        id: messageId,
        role: 'assistant',
        status: 'completed',
        content: [{ type: 'output_text', text: text || '', annotations: [] }],
      });
    }

    return sendJSON({
      id: responseId,
      object: 'response',
      created_at: timestamp(),
      status: 'completed',
      model: modelName,
      output: output,
      usage: {
        input_tokens: estimateTokens(prompt),
        output_tokens: estimateTokens(text),
        total_tokens: estimateTokens(prompt + text),
      },
    });
  } catch (error) {
    return sendJSON({ error: { message: 'upstream error: ' + error.message } }, 502);
  }
}

/**
 * Handles the Google native API (Gemini CLI compatible)
 * 
 * Supports the Gemini CLI's native generateContent and streamGenerateContent formats.
 * URL format: /v1beta/models/{model}:generateContent
 * 
 * @param {Request} request - the HTTP request
 * @param {Object} body - the parsed request body (Google format)
 * @param {boolean} stream - whether to stream
 * @param {Object} config - the request config object
 * @returns {Promise<Response>} the HTTP response object
 */
async function handleGoogleAPI(request, body, stream, config) {
  // Extract the model name from the URL path
  // e.g. /v1beta/models/gemini-3.6-flash:generateContent -> "gemini-3.6-flash"
  var requestUrl = new URL(request.url);
  var match = requestUrl.pathname.match(/\/v1beta\/models\/([^:]+)/);
  var modelName = match ? match[1] : null;

  if (!modelName) {
    return sendJSON({ error: { message: 'model not specified in path' } }, 400);
  }

  var resolved = resolveModel(modelName);
  if (resolved.error) {
    return sendJSON({ error: { message: resolved.error } }, 400);
  }

  var modelId = resolved.modelId;
  var thinkMode = resolved.thinkMode;

  // Convert Google format to a prompt
  var prompt = googleContentsToPrompt(body);
  if (!prompt.trim()) {
    return sendJSON({ error: { message: 'empty content' } }, 400);
  }

  try {
    var raw = await geminiStreamGenerate(prompt, modelId, thinkMode, config);
    var text = extractResponseText(raw);

    // Build the Google-format response
    var response = {
      candidates: [{
        content: { parts: [{ text: text || '' }], role: 'model' },
        finishReason: 'STOP',
        index: 0,
      }],
      usageMetadata: {
        promptTokenCount: estimateTokens(prompt),
        candidatesTokenCount: estimateTokens(text),
        totalTokenCount: estimateTokens(prompt + text),
      },
      modelVersion: modelName,
    };

    if (stream) {
      return new Response('data: ' + JSON.stringify(response) + '\n\n', {
        headers: {
          'Content-Type': 'text/event-stream',
          'Cache-Control': 'no-cache',
          'Access-Control-Allow-Origin': '*',
        },
      });
    }

    return sendJSON(response);
  } catch (error) {
    return sendJSON({ error: { message: 'upstream error: ' + error.message } }, 502);
  }
}

// ============================================================================
// Main entry - the Cloudflare Workers fetch event handler
// ============================================================================

export default {
  /**
   * The core entry point of the Cloudflare Worker
   * 
   * Called for every HTTP request reaching the Worker.
   * Processing order is strict:
   * 
   * 1. OPTIONS preflight -> CORS headers (required for browser CORS)
   * 2. Request config -> getRequestConfig(env) (concurrency safety)
   * 3. Rate limit -> checkRateLimit() (anti-abuse)
   * 4. API key -> checkApiKey() (auth)
   * 5. Routing:
   *    GET  /health              -> health check
   *    GET  /v1/models           -> model list (OpenAI format)
   *    GET  /v1beta/models       -> model list (Google format)
   *    POST /v1/chat/completions -> chat completions (OpenAI format)
   *    POST /v1/responses        → Responses API（Codex CLI）
   *    POST ...:generateContent  -> generate content (Google format)
   *    POST /v1/*                -> catch-all (auto-converted to chat)
   * 
   * @param {Request} request - the HTTP request
   * @param {Object} env - environment variables (per-request)
   * @param {Object} ctx - the execution context
   * @returns {Promise<Response>} the HTTP response object
   */
  async fetch(request, env, ctx) {
    // ================================================================
    // Step 1: handle OPTIONS CORS preflight first
    // ================================================================
    // Browsers send an OPTIONS preflight before cross-origin POSTs.
    // Without correct CORS headers the browser blocks the real request.
    // This must run before any other logic.
    if (request.method === 'OPTIONS') {
      return new Response(null, {
        status: 204,  // No Content
        headers: {
          'Access-Control-Allow-Origin': '*',              // allow all origins
          'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',  // allowed methods
          'Access-Control-Allow-Headers': '*',             // allow all headers
          'Access-Control-Max-Age': '86400',               // cache preflight 24h
        },
      });
    }

    // ================================================================
    // Step 2: build an independent config for this request
    // ================================================================
    // The core step that solves concurrent cross-talk.
    // No globals are mutated; every request has its own config.
    // env holds the per-request environment variables from Cloudflare.
    var config = getRequestConfig(env);

    // Parse the URL and method
    var requestUrl = new URL(request.url);
    var path = requestUrl.pathname;
    var method = request.method;

    // ================================================================
    // Step 3: rate-limit check
    // ================================================================
    // Uses the real client IP from Cloudflare (CF-Connecting-IP header)
    // Falls back to 0.0.0.0 when unavailable (non-CF proxy)
    var clientIP = request.headers.get('CF-Connecting-IP') || '0.0.0.0';
    if (!checkRateLimit(clientIP, config)) {
      log('Rate limit exceeded: ' + clientIP, 'WARN', config);
      return sendJSON({
        error: {
          message: 'Too many requests, please try again later',
          type: 'rate_limit_exceeded',
        },
      }, 429);  // HTTP 429 Too Many Requests
    }

    // ================================================================
    // Step 4: API key validation
    // ================================================================
    // Only /v1 paths are key-validated
    // Public endpoints like /health skip it
    if (path.indexOf('/v1') === 0 && !checkApiKey(request, config)) {
      return sendJSON({
        error: { message: 'invalid api key' },
      }, 401);  // HTTP 401 Unauthorized
    }

    // ================================================================
    // Step 5: GET requests
    // ================================================================
    if (method === 'GET') {
      // ---- Health-check endpoint ----
      // Useful for monitoring the Worker
      // Returns version, model list, and config status
      if (path === '/' || path === '/health') {
        return sendJSON({
          status: 'ok',
          version: '1.5.0-cf-multifingerprint',
          platform: 'Cloudflare Workers',
          models: Object.keys(MODELS),
          defaultModel: config.defaultModel,
        });
      }

      // ---- OpenAI-format model list ----
      // Returns all available models
      // Clients (NextChat, Cherry Studio, etc.) call this for the model list
      if (path === '/v1/models') {
        var modelList = [];
        var modelKeys = Object.keys(MODELS);
        for (var i = 0; i < modelKeys.length; i++) {
          var id = modelKeys[i];
          var cfg = MODELS[id];
          modelList.push({
            id: id,
            object: 'model',
            created: 1700000000,
            owned_by: 'google',
            description: cfg.desc,
          });
        }
        return sendJSON({ object: 'list', data: modelList });
      }

      // ---- Google-native-format model list ----
      // Model discovery for tools like the Gemini CLI
      if (path === '/v1beta/models') {
        var googleModels = [];
        var gKeys = Object.keys(MODELS);
        for (var j = 0; j < gKeys.length; j++) {
          var name = gKeys[j];
          var gCfg = MODELS[name];
          googleModels.push({
            name: 'models/' + name,
            displayName: name,
            description: gCfg.desc,
            supportedGenerationMethods: ['generateContent', 'streamGenerateContent'],
          });
        }
        return sendJSON({ models: googleModels });
      }

      // Unmatched GET request
      return sendJSON({ error: { message: 'not found' } }, 404);
    }

    // ================================================================
    // Step 6: POST requests
    // ================================================================
    if (method === 'POST') {
      var body;
      try {
        body = await request.json();
      } catch (e) {
        return sendJSON({ error: { message: 'invalid JSON' } }, 400);
      }

      // ---- OpenAI Chat Completions API ----
      // The most-used endpoint: chat completions
      if (path === '/v1/chat/completions') {
        return handleChatCompletions(request, body, config);
      }

      // ---- OpenAI Responses API (Codex CLI compatible) ----
      if (path === '/v1/responses') {
        return handleResponses(request, body, config);
      }

      // ---- Google native streamGenerateContent (streaming) ----
      if (path.indexOf(':streamGenerateContent') !== -1) {
        return handleGoogleAPI(request, body, true, config);
      }

      // ---- Google native generateContent (non-streaming) ----
      if (path.indexOf(':generateContent') !== -1) {
        return handleGoogleAPI(request, body, false, config);
      }

      // ---- Catch-all route ----
      // Unmatched /v1/ POSTs are auto-converted to chat
      // Handles path differences across clients
      if (path.indexOf('/v1/') === 0) {
        return handleChatCompletions(request, body, config);
      }

      // Unmatched POST request
      return sendJSON({ error: { message: 'not found' } }, 404);
    }

    // ================================================================
    // Step 7: unsupported HTTP methods
    // ================================================================
    return sendJSON({ error: { message: 'method not allowed' } }, 405);
  },
};
