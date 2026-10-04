---
name: jarvis-demo
description: Start the Jarvis demo, log in with the printed one-time password, screenshot every tab with Playwright and report console errors. Use for UI work and PR screenshots.
---
Run `python .claude/skills/jarvis-demo/demo_shots.py [--out DIR] [--port 8088]`.

It starts `python -m jarvis.demo` in the background, reads the one-time password from its output, logs in as
`owner`, opens each tab (Overview, Chat, Autonomy, Compute, Setup, Incidents), saves `DIR/<tab>.png` (default
`$TMPDIR/jarvis-shots`), prints console errors and page errors, then stops the demo. Exit 0 if there were no
console errors, 1 if there were, 2 if the `playwright` Python module is missing.

If it is missing, install it with the pinned version from the environment setup script (Chromium is already
preinstalled under `PLAYWRIGHT_BROWSERS_PATH`, so skip the download):

    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 pip install -q "playwright==1.56.0"

Report the screenshot paths and any console errors. Screenshots come from sample data only (no secrets).
