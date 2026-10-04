"""Strip IPs, credentials and keys before text is stored, embedded, trained on, or sent to a cloud model."""
import re

_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "<private-key>"),
    (re.compile(r"(sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,}|xox[bap]-[A-Za-z0-9-]{10,})"), "<secret>"),
    (re.compile(r"(?i)\b(bearer)\s+([A-Za-z0-9._~+/=-]{8,})"), r"\1 <secret>"),
    (re.compile(r"(?i)\b(token|password|passwd|secret|api[_-]?key)(\s*[:=]\s*)([^\s\"']+)"), r"\1\2<secret>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<ip>"),
]


def redact(text):
    if not isinstance(text, str):
        return text
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
