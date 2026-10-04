"""Send a short notification to AUTONOMY_NOTIFY_URL (Slack/Discord-style JSON, or plain text for ntfy). Optional; never raises."""
import json
import os
import sys
import urllib.parse
import urllib.request

from redact import redact


def send(kind, title, body="", url=None, timeout=10):
    url = url or os.environ.get("AUTONOMY_NOTIFY_URL")
    if not url:
        return False
    parts = urllib.parse.urlparse(url)
    if parts.scheme not in ("https", "http") or (parts.scheme == "http" and parts.hostname not in ("127.0.0.1", "localhost")):
        print("notify: only https URLs (or http to localhost) are allowed", file=sys.stderr)
        return False
    text = redact(f"[{kind}] {title}\n{body}".strip())[:1800]
    if os.environ.get("AUTONOMY_NOTIFY_FORMAT") == "text":  # ntfy and similar
        req = urllib.request.Request(url, data=text.encode(), headers={"Title": redact(title)[:100], "Content-Type": "text/plain"})
    else:
        req = urllib.request.Request(url, data=json.dumps({"text": text, "content": text}).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except OSError as exc:
        print(f"notify: delivery failed: {exc}", file=sys.stderr)
        return False
