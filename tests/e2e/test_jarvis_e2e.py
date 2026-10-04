"""Browser end-to-end test of the Jarvis dashboard against `python -m jarvis.demo`.

Needs Playwright and a browser (`pip install playwright && python -m playwright install chromium`); skipped otherwise.
"""
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
TABS = ("Overview", "Chat", "Autonomy", "Compute", "Setup", "Incidents")
WAIT = 10_000


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def demo():
    port = free_port()
    proc = subprocess.Popen([sys.executable, "-m", "jarvis.demo", "--port", str(port)], cwd=ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        password = None
        for line in proc.stdout:
            m = re.search(r"password:\s*(\S+)", line)
            if m:
                password = m.group(1)
                break
        assert password, "jarvis.demo did not print a password"
        url = f"http://127.0.0.1:{port}"
        for _ in range(100):  # server starts listening just after printing; poll up to ~10 s
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise AssertionError("jarvis.demo did not start listening")
        yield url, password
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        proc.stdout.close()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser, demo):
    ctx = browser.new_context()
    pg = ctx.new_page()
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(f"pageerror: {e}"))
    pg.on("console", lambda m: pg.errors.append(f"console: {m.text}")
          if m.type == "error" and not m.text.startswith("Failed to load resource") else None)
    yield pg
    ctx.close()


def login(page, demo):
    url, password = demo
    page.goto(url)
    page.wait_for_selector("#login-form", state="visible", timeout=WAIT)
    page.fill("#u", "owner")
    page.fill("#p", password)
    page.click("#login-form button")
    page.wait_for_selector("#app", state="visible", timeout=WAIT)


def open_tab(page, name):
    page.click(f"#tabs button:text-is('{name}')")
    page.wait_for_selector(f"#tab-{name.lower()}", state="visible", timeout=WAIT)


def test_login_and_every_tab_without_errors(page, demo):
    login(page, demo)
    for name in TABS:
        open_tab(page, name)
    page.wait_for_timeout(500)  # let late async renders surface errors
    assert page.errors == []


def test_discard_pending_approval(page, demo):
    login(page, demo)
    open_tab(page, "Autonomy")
    row = page.locator("#tab-autonomy tr", has=page.locator("button:text-is('Confirm')"))
    row.first.wait_for(state="visible", timeout=WAIT)
    assert row.first.locator("button:text-is('Discard')").is_visible()
    before = row.count()
    row.first.locator("button:text-is('Discard')").click()
    if before == 1:
        page.wait_for_selector("text=Nothing is waiting", timeout=WAIT)
    else:
        page.wait_for_function("n => document.querySelectorAll('#tab-autonomy tr button').length < n", arg=before * 2, timeout=WAIT)
    assert page.errors == []


def test_logout_returns_to_login(page, demo):
    login(page, demo)
    page.click("#logout")
    page.wait_for_selector("#login-form", state="visible", timeout=WAIT)
    assert not page.is_visible("#app")
