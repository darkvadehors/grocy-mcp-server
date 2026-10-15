import time

import pytest

from grocy_mcp.audit import AuditLog
from grocy_mcp.config import Settings
from grocy_mcp.crypto import SecretBox, generate_key, load_key
from grocy_mcp.store import Store
from grocy_mcp.grocy import GrocyClient
from grocy_mcp.tools import Caller, ToolService
from tests.fake_grocy import KEY_ELLI_IA, KEY_JOHNNY_IA, FakeGrocy


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


JOHNNY = Caller(grocy_user_id=3, family_id="f-johnny", client_id="c1")
ELLI = Caller(grocy_user_id=4, family_id="f-elli", client_id="c1")


@pytest.fixture
def service(settings, store, box, audit, fake):
    for uid, name, key in [(3, "johnny-ia", KEY_JOHNNY_IA), (4, "elli-ia", KEY_ELLI_IA)]:
        store.upsert_account(uid, name, box.seal(key, f"grocy-user:{uid}"))
    return ToolService(
        settings, store, box, audit, lambda key: GrocyClient(settings.grocy_base_url, key, transport=fake.transport())
    )


@pytest.fixture(autouse=True)
def _no_lookup_pause(monkeypatch):
    """Pas de pause entre requêtes Open Food Facts dans les tests (vérifiée dans test_enrich_job)."""
    from grocy_mcp import packaging_job

    monkeypatch.setattr(packaging_job, "LOOKUP_PAUSE", 0)
