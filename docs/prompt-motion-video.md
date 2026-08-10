# 🎬 Motion Graphics Video Prompt Kit — Gemini Web2API (v2)

Ultra-hyperrealistic motion graphics for the launch: dark glassmorphism, cinematic lighting, premium SaaS energy (Apple × Vercel × Stripe). This kit is built for **per-scene generation + assembly** — the only route that survives AI video's real limits. Every clip is a full prompt; nothing is a one-liner.

**Workflow:**
1. Generate the **Style Anchor** (below) once.
2. Feed the anchor as an image/style reference to every clip (Veo 3 / Gemini video support image conditioning — if your tool has it, this is the single biggest consistency lever; if not, reuse the identical style block and a fixed seed).
3. Generate the 10 clips, 3-4s each.
4. Assemble in the **Hook-first cut** order, overlay text in post, add sound design. Done.

---

## 1 · STYLE ANCHOR (generate first, reuse as reference)

```
Ultra-hyperrealistic premium motion graphics keyframe, 16:9, dark glassmorphism aesthetic. Deep near-black indigo background (#05070F) with volumetric fog and slowly drifting dust particles. A large frosted-glass monolith floats center-frame with physically accurate reflections, refraction, and 1px luminous borders in a violet-to-cyan-to-emerald gradient. Soft cinematic key light from upper left, subtle violet rim light on the glass edges, gentle bloom on glows. On the glass, small clean sans-serif text "GEMINI WEB2API" with a soft white glow. Photorealistic materials, pure abstract HUD — no humans. 60fps still, premium product-film lighting.
```

**Negative block (append to every clip):** `No humans, no faces, no hands, no watermarks, no film grain, no lens flares, no misspelled words, no morphing text, no text clipping through glass, no jitter, no flicker, no flickering text, no abrupt color shifts between frames.`

---

## 2 · THE 10 CLIPS (full prompts, 3-4s each)

Each clip = Style Anchor reference + scene prompt. Text in quotes is a **lockup**: it must appear exactly as written, or the shot is unusable.

### Clip A — COLD OPEN: the 405 slam (the hook) ⭐
```
Ultra-hyperrealistic motion graphics, 3 seconds. A fast luminous particle stream races left-to-right through dark volumetric space, representing a live API request. At the 1s mark the stream SNAPS and shatters. A crimson siren pulse floods the scene from the center. A heavy frosted-glass slab slams in from depth with a low sub-bass thump, displaying the exact lockup "405" in huge letter-spaced type, with "METHOD NOT ALLOWED" below in smaller clean type. Red rim light on the glass, deep shadows, volumetric dust knocked loose by the impact. Cinematic easing: the slam is fast and sharp, the afterglow slow and ominous. Exact text: "405" then "METHOD NOT ALLOWED".
```

### Clip B — the monolith reveal (setup)
```
Ultra-hyperrealistic motion graphics, 3 seconds. Gentle dolly-in through drifting particles toward a frosted-glass monolith. The lockup "GEMINI WEB2API" materializes letter by letter on the glass with a soft white glow, then a smaller line fades in below: "OpenAI-compatible API on Gemini's web protocol". Slow cinematic push, violet-to-cyan rim light, subtle bloom. Exact text: "GEMINI WEB2API" and "OpenAI-compatible API on Gemini's web protocol".
```

### Clip C — models & endpoints
```
Ultra-hyperrealistic motion graphics, 3 seconds. Eight frosted-glass model chips slide into a row and lock with soft clicks, each with a small gradient icon. Behind them, fifteen endpoint nodes light up one by one across a glass network map, connected by thin glowing lines. A steady stream of luminous dashes flows out to the right. Cyan-to-violet lighting, gentle parallax, premium UI feel. No on-screen text.
```

### Clip D — probe-before-apply (the thesis) ⭐
```
Ultra-hyperrealistic motion graphics, 4 seconds. Three frosted-glass build-label chips float in a row, amber-lit. A thin luminous beam probes the first chip — it flashes red with an X and dims. The beam probes the second — red X, dims. The beam probes the third — the chip ignites with a bright green checkmark, glows, and locks into place with a soft mechanical click; the entire glass panel shifts from amber tension to emerald calm, and the stream resumes flowing behind it. At the bottom, small clean type fades in: "PROBE-BEFORE-APPLY". Exact text: "PROBE-BEFORE-APPLY".
```

### Clip E — the token war
```
Ultra-hyperrealistic motion graphics, 3 seconds. A single glass session-token chip is struck by a red "400" flash and shatters into glowing shards. A horizontal scan beam sweeps across the shards; they reassemble into the correct token, which slots into a glass socket with a green pulse. Glitch, then clarity. No on-screen text except a brief red "400" flash.
```

### Clip F — the watchdog
```
Ultra-hyperrealistic motion graphics, 4 seconds. A circular glass cookie-age gauge drains from "24h" toward "0h" as its rim shifts from emerald to amber. A glowing single-eye watchdog icon scans left to right. A minimized glass browser window (small Brave-style orange icon) slides in from the side, a cookie chip inside refreshes to bright green, and the gauge refills to full. The scene resolves from amber tension to green calm. Exact text: "24h" and "0h" on the gauge.
```

### Clip G — SSE discipline ⭐
```
Ultra-hyperrealistic motion graphics, 4 seconds. A frosted-glass terminal panel types out a clean data stream, letter by letter, perfectly legible: an amber error frame line, then a bright green lockup "[DONE]" that pulses once. Behind the panel, an oscilloscope-style ticker runs rapid byte-level checkmarks. Dark indigo void, violet rim light, soft bloom, no flicker. Exact text: "[DONE]".
```

### Clip H — the numbers
```
Ultra-hyperrealistic motion graphics, 3 seconds. A glass counter counts up rapidly to the lockup "515" in large glowing type, with "TESTS · 16 SUITES" beneath. A glowing self-healing loop ring (a circle made of light) completes a full circuit around the counter and pulses green with a satisfying low-end thump. Exact text: "515" and "TESTS · 16 SUITES".
```

### Clip I — the tagline
```
Ultra-hyperrealistic motion graphics, 3 seconds. Slow pull-back from the glass monolith. The lockup "Zero keys. Zero billing. Heals itself." fades in line by line, clean white type with a soft glow, centered. Minimal, premium, confident. Exact text: "Zero keys. Zero billing. Heals itself."
```

### Clip J — the repo & fade
```
Ultra-hyperrealistic motion graphics, 3 seconds. The lockup "github.com/flawsom/Gemini-api" renders in clean small type on the glass monolith, then the scene gently fades to black with a single emerald spark left behind. Exact text: "github.com/flawsom/Gemini-api".
```

---

## 3 · ASSEMBLY — HOOK-FIRST CUT (primary, 30s)

Retention rule: the first 3 seconds decide the watch. The 405 slam is the hook; the reveal explains it after.

| Time | Clip | Why here |
|---|---|---|
| 0:00–0:03 | **A — 405 slam** | Cold open. The most arresting visual wins the first 3 seconds. |
| 0:03–0:06 | B — monolith reveal | Rewind to setup: what is this thing? |
| 0:06–0:09 | C — models & endpoints | Proof of scale: 8 models, 15 endpoints. |
| 0:09–0:13 | D — probe-before-apply | The thesis: it heals itself. |
| 0:13–0:16 | E — token war | War 2: depth of the engineering. |
| 0:16–0:20 | F — watchdog | War 3: runs unattended. |
| 0:20–0:24 | G — SSE discipline | War 4: correctness under failure. |
| 0:24–0:27 | H — 515 tests | Credibility: numbers close the argument. |
| 0:27–0:30 | I — tagline | The emotional close. |
| 0:30–0:33 | J — repo + fade | The action: where to click. |

**Linear variant** (for YouTube, where viewers expect setup-first): B → A → C → D → E → F → G → H → I → J. Same clips, different order — no regeneration needed.

---

## 4 · POST-PRODUCTION (this is what separates "good" from "absolute best")

1. **Text goes in post, except the lockups that carry the scene.** Generate clips with ONLY the quoted lockups in-scene (405 slam, [DONE], PROBE-BEFORE-APPLY, the numbers, the repo). Everything else (taglines, subtitles) is overlay text added in CapCut/Premiere: Inter or SF Pro, 600–800 weight, letter-spacing +0.02em, white at 90% opacity with a 10% white glow.
2. **Grade every clip with one LUT pass** so the 10 generations share a consistent look: lift blacks to #0A0D1A, push violet/cyan/emerald saturation +5%, add 0.5px chromatic aberration only on cuts.
3. **Generate clips SILENT.** Lay in sound design in post — you want total control: deep whooshes on transitions, a sub-bass thump on the 405 slam and the 515 pulse, a soft click on chip lock-ins, a warm pad swell when scenes resolve to green. Music bed at −18 LUFS under the SFX.
4. **Check the lockups frame-by-frame.** AI text renders imperfectly — the quoted lockups are the only in-scene text, so a 20-second QA pass per clip is enough. If one garbles, regenerate that single clip; do not hand-fix in paint.

---

## 5 · DELIVERABLES PER PLATFORM

| Platform | Cut | Spec |
|---|---|---|
| Product Hunt video field | Hook-first 30s | 16:9, 3840×2160 → upload to YouTube/Loom, paste the link |
| YouTube (studio) | Hook-first 30s | 4K, title *"Gemini Web2API — the self-healing Gemini API"* |
| X / Shorts | Vertical cut | 9:16, re-time A → D → F → H → I → J (~18s) |
| dev.to cover | Loop cut | 6s loop of Clip H's self-heal ring, replaces docs/ph-selfheal.gif |
| GitHub README gallery | Still | Frame from Clip D as `docs/shot-video-still.png` |

---

## 6 · 15-SECOND VERTICAL VARIANT (paste when you need the short form)

```
Ultra-hyperrealistic vertical motion graphics, 15 seconds, 9:16, 60fps, dark glassmorphism, indigo void, volumetric fog, violet-to-cyan-to-emerald luminous glass edges, cinematic easing, no humans. (1) A request stream races across the frame and SNAPS — crimson pulse, heavy glass slab slams in with exact text "405" and "METHOD NOT ALLOWED". (2) A frosted-glass monolith: "GEMINI WEB2API" materializes, eight model chips lock, fifteen nodes light. (3) Three build-label chips probe: red X, red X, then a green checkmark locks in with a click and the stream resumes, small text "PROBE-BEFORE-APPLY". (4) A cookie gauge drains, a watchdog eye scans, a minimized browser window refreshes a green cookie chip, the gauge refills. (5) Logo lockup: "Zero keys. Zero billing. Heals itself." then "github.com/flawsom/Gemini-api". No watermarks, no misspellings.
```
