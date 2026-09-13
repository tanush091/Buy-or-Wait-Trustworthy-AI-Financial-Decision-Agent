"""Redact secrets before anything is written to logs, reports or evaluation artifacts."""
import os
import re

_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),          # Google API keys
    re.compile(r"AQ\.[0-9A-Za-z_\-.]{20,}"),         # Google AI Studio tokens
    re.compile(r"sk-(?:ant-)?[0-9A-Za-z_\-]{20,}"),  # Anthropic / OpenAI style keys
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|secret)\s*[:=]\s*\S+"),
]
_ENV_KEYS = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY")


def sanitize(text: str) -> str:
    for name in _ENV_KEYS:
        value = os.environ.get(name)
        if value and len(value) >= 8:
            text = text.replace(value, "[REDACTED]")
    for pattern in _PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text
