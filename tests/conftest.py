import time

import pytest

from grocy_mcp.audit import AuditLog
from grocy_mcp.config import Settings
from grocy_mcp.crypto import SecretBox, generate_key, load_key
from grocy_mcp.store import Store
from tests.fake_grocy import FakeGrocy


class Clock:
    """Horloge du service, partie de l'heure réelle (le SDK compare aussi à time.time())."""

    def __init__(self) -> None:
        self.t = time.time()

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def fake():
    return FakeGrocy()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def settings(tmp_path):
    key = tmp_path / "mcp-key"
    key.write_text(generate_key())
    return Settings(
        grocy_base_url="https://grocy.example.test",
        public_url="https://mcp.example.test",
        db_path=tmp_path / "mcp.db",
        audit_path=tmp_path / "audit.jsonl",
        key_path=key,
    )


@pytest.fixture
def store(settings, clock):
    s = Store(settings.db_path, clock)
    yield s
    s.close()


@pytest.fixture
def audit(settings, clock):
    return AuditLog(settings.audit_path, clock)


@pytest.fixture
def box(settings):
    return SecretBox(load_key(settings.key_path))
