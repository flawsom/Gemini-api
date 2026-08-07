---
name: 🐛 Bug report
about: Report something that isn't working as expected
title: "[bug] "
labels: bug
assignees: ''
---

<!--
  Thanks for reporting! Please fill out every section that applies.
  Never paste real session cookies, cookie.txt contents, or live API keys.
-->

## Description

<!-- A clear, concise description of what the bug is. -->

## Steps to reproduce

1. Start the server with `...`
2. Send `...`
3. See error: `...`

## Expected behavior

<!-- What did you expect to happen? -->

## Actual behavior

<!-- What actually happened? Include error messages, 4xx/5xx statuses, retry output. -->

## Logs

<!-- Paste the relevant server.log / console output. Redact anything sensitive. -->
<details>
<summary>Server log</summary>

```text
<!-- paste logs here -->
```

</details>

## Environment

- **OS**: <!-- e.g. Windows 11 / macOS 14 / Ubuntu 22.04 -->
- **Python version**: <!-- e.g. 3.12 -->
- **How installed**: <!-- single file / package / Docker / Cloudflare Worker -->
- **Config**: <!-- paste config.json with secrets redacted, or the relevant keys -->
- **Browser & extension version**: <!-- e.g. Brave 1.6x, extension v1.6 -->
- **Optional deps**: <!-- httpx installed? (needed for streaming) -->

## Diagnostic checklist

- [ ] I searched existing issues (open + closed) first
- [ ] I tested against the latest version of `main`
- [ ] `python test_suite.py` passes (if applicable)
- [ ] No secrets / cookies / keys included in this report
