"""Masquage des secrets dans tout texte journalisé (défense en profondeur : le code n'en journalise pas)."""

from __future__ import annotations

import logging
import re

_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)\b(authorization|grocy-api-key)(\s*[:=]\s*)(bearer\s+|basic\s+)?[^\s,;'\"]+"), r"\1\2[masqué]"),
    (
        re.compile(r"(?i)\b(code|refresh_token|access_token|code_verifier|client_secret|cle|req|token)=[^&\s'\"]+"),
        r"\1=[masqué]",
    ),
    (re.compile(r"\bgm[ar]_[A-Za-z0-9_-]{8,}"), "[jeton masqué]"),
    (re.compile(r"\b[A-Za-z0-9]{40,}\b"), "[clé masquée]"),
]


def redact_text(text: str) -> str:
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage())
        record.args = None
        return True


def install_redaction(logger: logging.Logger | None = None) -> None:
    target = logger or logging.getLogger()
    for handler in target.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())
