"""Drive the Jarvis demo with Playwright: log in, screenshot every tab, collect console errors."""
import argparse
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

TABS = ["Overview", "Chat", "Autonomy", "Compute", "Setup", "Incidents"]  # TABS in jarvis/static/app.js
ROOT = Path(__file__).resolve().parents[3]

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("playwright python module not installed. Install the pinned version (Chromium is preinstalled):\n"
          '  PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 pip install -q "playwright==<PINNED_VERSION>"\n'
          "(the pin is in the cloud environment setup script)", file=sys.stderr)
    sys.exit(2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(os.environ.get("TMPDIR", tempfile.gettempdir())) / "jarvis-shots"))
    ap.add_argument("--port", type=int, default=8088)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    proc = subprocess.Popen([sys.executable, "-m", "jarvis.demo", "--port", str(args.port)], cwd=ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines, password = [], []

    def pump():
        for line in proc.stdout:
            lines.append(line)
            m = re.search(r"password:\s*(\S+)", line)
            if m and not password:
                password.append(m.group(1))

    threading.Thread(target=pump, daemon=True).start()
    errors = []
    try:
        deadline = time.time() + 40
        while not password and time.time() < deadline and proc.poll() is None:
            time.sleep(0.2)
        if not password:
            print("demo did not print a password:\n" + "".join(lines), file=sys.stderr)
            return 1
        time.sleep(1)  # let uvicorn bind
        with sync_playwright() as p:
            browser = p.chromium.launch()
            def on_console(m):
                # The demo returns expected 4xx responses (e.g. GPU pods 400 "not set up"); the browser logs those
                # as "Failed to load resource". Ignore them; every other console error is a hard failure.
                if m.type == "error" and not m.text.startswith("Failed to load resource"):
                    errors.append(f"console.error: {m.text}")

            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.on("console", on_console)
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            page.goto(f"http://127.0.0.1:{args.port}/")
            page.wait_for_selector("#login:not(.hidden)", timeout=15000)
            page.fill("#u", "owner")
            page.fill("#p", password[0])
            page.click("#login-form button")
            page.wait_for_selector("#app:not(.hidden)", timeout=15000)
            for tab in TABS:
                page.click(f"#tabs button:text-is('{tab}')")
                page.wait_for_selector(f"#tab-{tab.lower()}:not(.hidden)", timeout=10000)
                page.wait_for_timeout(500)
                path = out / f"{tab.lower()}.png"
                page.screenshot(path=str(path), full_page=True)
                print("screenshot", path)
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    if errors:
        print("CONSOLE ERRORS:")
        for e in errors:
            print("  " + e)
        return 1
    print("no console errors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
