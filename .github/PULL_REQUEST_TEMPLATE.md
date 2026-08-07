---
name: Pull request
about: Propose changes to the project
title: ""
labels: ""
assignees: ""
---

<!-- Thanks for contributing! Please complete every applicable section.
     Check the boxes that apply — they keep reviews fast and friendly. -->

## Summary

<!-- What does this PR do, and why? One or two sentences is fine. -->

## Type of change

<!-- Check one box that best describes the change (use `x`). -->

- [ ] 🆕 feat — new capability
- [ ] 🐛 fix — bug fix
- [ ] 📝 docs — documentation only
- [ ] ⚡ perf — performance improvement
- [ ] 🧹 chore — housekeeping / refactor / tooling

## Related issue

<!-- Link the issue this resolves, e.g. "Closes #12" or "N/A". -->

Closes #

## Changes

<!-- Bullet-list the notable changes. -->

- 

## Test plan

<!-- How did you verify this works? Paste the commands you ran. -->

```bash
python test_suite.py
python test_proxy_fallback.py
python test_cookie_refresh.py
node test_extension.js
node test_popup.js
```

<details>
<summary>Test results</summary>

```text
<!-- Paste the output here -->
```

</details>

## Checklist

- [ ] Branch name follows `type/description` (e.g. `feat/auto-model-routing`)
- [ ] Commits follow [Conventional Commits](https://www.conventionalcommits.org)
- [ ] **Both server copies updated** (single-file `gemini_web2api.py` + `gemini_web2api/` package) when server behavior changed
- [ ] `manifest.json` version bumped when the extension changed
- [ ] New tests added for behavior changes / bug fixes
- [ ] All test suites pass locally (Python + Node)
- [ ] No `cookie.txt`, real `config.json`, or secrets committed
- [ ] README updated when user-facing behavior changed (features, config keys, endpoints)
- [ ] Ran syntax checks: `python -m py_compile gemini_web2api.py gemini_web2api/*.py` and `node --check` on changed JS

## Screenshots / GIFs

<!-- Optional but appreciated for UI changes (extension popup, etc.). -->
