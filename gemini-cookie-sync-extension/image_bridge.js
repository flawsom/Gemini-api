// Gemini Cookie Sync - image bridge content script (v1.12)
//
// Runs on gemini.google.com. The background worker opens a REAL, fully
// authenticated Gemini window and messages this script with an image request
// ({prompt, images:[{name, mime, data_b64}]}). This script attaches the
// image(s) to the composer, types the prompt, sends the message, waits for
// the model's answer to stabilize, and returns the text.
//
// Why: image requests made from exported cookies are rejected with
// BardErrorInfo 1100 - Google only processes uploaded images in a
// fully-authenticated browser session. This content script runs inside that
// session, so the upload + send happen exactly as if the user typed them.

(() => {
  const TAG = "gemini-cookie-sync-image-bridge";
  if (window[TAG]) return; // one listener per page
  window[TAG] = true;

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function findComposer() {
    return document.querySelector('div[contenteditable="true"], textarea');
  }

  function findAttachButton() {
    const buttons = document.querySelectorAll("button");
    for (const b of buttons) {
      const label = (b.getAttribute("aria-label") || "") + " " + (b.textContent || "");
      if (/upload|attach|add photo/i.test(label) && label.length < 60) return b;
    }
    return null;
  }

  function findSendButton() {
    const buttons = document.querySelectorAll("button");
    for (const b of buttons) {
      const label = (b.getAttribute("aria-label") || "") + " " + (b.textContent || "");
      if (/send|submit message/i.test(label) && label.length < 40) return b;
    }
    return null;
  }

  function sendEnabled(btn) {
    return Boolean(btn && !btn.disabled
      && btn.getAttribute("aria-disabled") !== "true"
      && !btn.hasAttribute("data-disabled"));
  }

  // Attach image(s): click the attach button so the UI materializes its file
  // input, then assign our files to it and fire change - the same event the
  // page uses when a user picks files. Falls back to an input we create.
  async function attachImages(images) {
    const attachBtn = findAttachButton();
    if (attachBtn) {
      attachBtn.click();
      await sleep(2000);
    }
    let input = document.querySelector('input[type="file"]');
    if (!input) {
      input = document.createElement("input");
      input.type = "file";
      input.multiple = true;
      input.style.display = "none";
      document.body.appendChild(input);
    }
    const dataTransfer = new DataTransfer();
    for (const img of images) {
      const binary = atob(img.data_b64);
      const bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
      const file = new File([bytes], img.name || "image.png",
                            { type: img.mime || "image/png" });
      dataTransfer.items.add(file);
    }
    try {
      input.files = dataTransfer.files;
    } catch (e) {
      // Some engines reject the direct assignment; define it instead.
      Object.defineProperty(input, "files", { value: dataTransfer.files, configurable: true });
    }
    input.dispatchEvent(new Event("change", { bubbles: true }));
    await sleep(3000); // let the page upload the file(s)
  }

  async function typePrompt(text) {
    const composer = findComposer();
    if (!composer) throw new Error("composer not found");
    composer.focus();
    if (!text) return;
    document.execCommand("insertText", false, text);
    await sleep(600);
    if (composer.innerText.includes(text.slice(0, 24))) return;
    // insertText silently failed - fall back to clipboard paste.
    try {
      await navigator.clipboard.writeText(text);
      composer.focus();
      document.execCommand("paste");
    } catch (e) {
      composer.dispatchEvent(new InputEvent("beforeinput", {
        bubbles: true, inputType: "insertText", data: text
      }));
      composer.dispatchEvent(new InputEvent("input", {
        bubbles: true, inputType: "insertText", data: text
      }));
    }
    await sleep(600);
  }

  // Text of the newest model message, stripped of UI chrome.
  function readLastModelText() {
    const nodes = document.querySelectorAll('[data-message-author-role="model"]');
    if (!nodes.length) return "";
    const last = nodes[nodes.length - 1];
    // Prefer the inner markdown/rich-text node when the UI provides one - the
    // outer bubble can also hold action chips (Copy, Edit, ...) whose labels
    // would otherwise leak into the returned answer.
    const inner = last.querySelector(
      '.markdown, .rich-text, .model-response-text, [data-test-id*="response-text"]');
    const source = inner || last;
    return (source.innerText || "")
      .replace(/^Thinking…\s*\n?/, "")
      .split("\n")
      .map((line) => line.trim())
      // Drop short action-chip labels (Copy/Edit/...) - only when the whole
      // line is a bare label, so real one-word answers survive.
      .filter((line) => line && !(line.length <= 12
        && /^(copy|edit|regenerate|delete|show more|show less|dismiss)\b/i.test(line)))
      .join("\n")
      .trim();
  }

  // Why did no answer arrive? Snapshot the page state so the server log (and
  // the popup) say WHERE it stopped instead of a bare timeout - e.g. the send
  // never fired, an error banner appeared, or a sign-in wall blocked it.
  function diagnoseNoAnswer() {
    const parts = [];
    const composer = findComposer();
    if (composer && (composer.innerText || "").trim()) {
      parts.push("composer still holds text (send may not have fired)");
    }
    const users = document.querySelectorAll('[data-message-author-role="user"]').length;
    const models = document.querySelectorAll('[data-message-author-role="model"]').length;
    parts.push(`messages user=${users} model=${models}`);
    if (!users) parts.push("no user message was recorded");
    const err = document.querySelector('[role="alert"], .error, [data-test-id*="error"]');
    if (err && (err.innerText || "").trim()) {
      parts.push("page error: " + err.innerText.trim().slice(0, 160));
    }
    if (/sign in/i.test((document.body.innerText || "").slice(0, 3000))) {
      parts.push("page shows a sign-in prompt");
    }
    return "no model answer appeared before the timeout - " + parts.join("; ");
  }

  async function sendAndWait(beforeText, deadline) {
    // The send button is disabled while the image is still uploading - wait
    // for it to enable (up to 20s) rather than clicking a dead button.
    const sendStart = Date.now();
    let sendBtn = findSendButton();
    while (!sendEnabled(sendBtn) && Date.now() - sendStart < 20000) {
      await sleep(1000);
      sendBtn = findSendButton();
    }
    if (!sendEnabled(sendBtn)) throw new Error("send button never enabled (image upload stuck?)");
    sendBtn.click();
    let lastText = "";
    let stableRounds = 0;
    while (Date.now() < deadline) {
      await sleep(1500);
      const text = readLastModelText();
      if (text && text !== beforeText) {
        if (text === lastText) {
          stableRounds += 1;
          if (stableRounds >= 2) return text; // unchanged across ~3s -> done
        } else {
          stableRounds = 0;
          lastText = text;
        }
      }
    }
    if (lastText) return lastText; // timed out mid-stream - return what we have
    throw new Error(diagnoseNoAnswer());
  }

  async function run(request) {
    // Answer budget mirrors the server's wait (image_bridge_timeout): a cold
    // Gemini window can take 20-60s to load plus up to a few minutes for the
    // model's answer, and the server now holds the request open that long.
    const timeoutMs = Math.max(30000, Math.min(300000, request.timeout_ms || 240000));
    const deadline = Date.now() + timeoutMs;
    const waitFor = async (fn, label, ms = 30000) => {
      const start = Date.now();
      while (Date.now() - start < ms) {
        const value = fn();
        if (value) return value;
        await sleep(1000);
      }
      throw new Error(label + " not found");
    };

    await waitFor(() => findComposer(), "composer");
    await sleep(1500); // let the composer settle
    await attachImages(request.images || []);
    await typePrompt(request.prompt || "");
    const beforeText = readLastModelText();
    const text = await sendAndWait(beforeText, deadline);
    return { ok: true, text };
  }

  // Post the outcome straight to the server. This runs in the PAGE (not the
  // service worker), so it survives the worker being suspended mid-processing
  // - the background's sendMessage channel can die when the MV3 service
  // worker goes to sleep, but this fetch always lands.
  async function postResult(request, ok, text, error) {
    try {
      const settings = await chrome.storage.local.get({
        apiKey: "sk-gemini", serverBase: "http://127.0.0.1:8081"
      });
      await fetch(settings.serverBase + "/internal/image-bridge/result", {
        method: "POST",
        headers: { "Content-Type": "application/json",
                   "X-API-Key": settings.apiKey },
        body: JSON.stringify({ id: request.id, ok, text, error,
                               ext_version: chrome.runtime.getManifest().version })
      });
    } catch (e) {
      // The server may already have timed out and cancelled - 409 is fine.
      console.error("[ImageBridge] result post failed:", e);
    }
  }

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (!msg || msg.type !== "image-bridge") return;
    run(msg.request)
      .then(async (result) => {
        await postResult(msg.request, true, result.text || "", "");
        sendResponse(result); // best-effort: lets the worker close the window
      })
      .catch(async (err) => {
        const error = String((err && err.message) || err);
        await postResult(msg.request, false, "", error);
        sendResponse({ ok: false, error });
      });
    return true; // keep the channel open for the async response
  });
})();
