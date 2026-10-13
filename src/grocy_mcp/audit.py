"""Journal d'audit JSONL : une ligne par événement, chaînée par l'empreinte SHA-256 de la ligne précédente."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from grocy_mcp.redact import redact_text

GENESIS = "0" * 64


def _digest(line: str) -> str:
    return hashlib.sha256(line.encode()).hexdigest()


def _last_line(path: Path) -> str | None:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65_536))
            lines = [line for line in f.read().split(b"\n") if line.strip()]
    except FileNotFoundError:
        return None
    # seule la dernière ligne est décodée : la première du bloc lu peut commencer au milieu d'un caractère
    return lines[-1].decode("utf-8", errors="replace") if lines else None


def _clean(value: object) -> object:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_clean(v) for v in value]
    return value


class AuditLog:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._clock = clock
        self._lock = threading.Lock()
        last = _last_line(self.path)
        self._prev = _digest(last) if last else GENESIS

    def write(self, event: str, **fields: object) -> None:
        ts = datetime.fromtimestamp(self._clock(), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            record = {"ts": ts, "event": event, **{k: _clean(v) for k, v in fields.items()}, "prev": self._prev}
            line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
            try:
                os.write(fd, (line + "\n").encode())
            finally:
                os.close(fd)
            self._prev = _digest(line)


def verify_chain(path: Path) -> bool:
    prev = GENESIS
    for line in Path(path).read_text().splitlines():
        if json.loads(line).get("prev") != prev:
            return False
        prev = _digest(line)
    return True
