import os
import sqlite3
import stat

import pytest

from grocy_mcp.store import Store, sha256_hex


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    s = Store(tmp_path / "mcp.db", clock)
    yield s
    s.close()


def test_file_permissions_and_wal(tmp_path, clock):
    s = Store(tmp_path / "x.db", clock)
    assert stat.S_IMODE(os.stat(tmp_path / "x.db").st_mode) == 0o600
    assert s._db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    s.close()


def test_sha256_hex():
    assert sha256_hex("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_clients(store, clock):
    store.save_client("c1", '{"a":1}')
    assert store.get_client("c1") == ('{"a":1}', clock.t)
    assert store.count_clients() == 1
    clock.t += 90_000
    store.save_client("c2", "{}")
    store.mark_client_used("c2")
    store.save_client("c3", "{}")
    assert store.purge_unused_clients(86_400) == 1  # c1 seulement
    assert store.get_client("c1") is None
    assert store.count_clients() == 2


def test_pending_lifecycle(store, clock):
    store.create_pending("p", "c1", "{}")
    store.set_pending_csrf("p", "h1")
    assert store.get_pending("p").csrf_hash == "h1"
    store.set_pending_identity("p", b"enc", 3, "johnny-ia", "h2")
    row = store.get_pending("p")
    assert (row.enc_key, row.grocy_user_id, row.grocy_username, row.csrf_hash) == (b"enc", 3, "johnny-ia", "h2")
    clock.t += 601
    assert store.get_pending("p") is None
    store.delete_pending("p")


def test_accounts(store):
    store.upsert_account(3, "johnny-ia", b"a")
    store.upsert_account(3, "johnny-ia", b"b")
    assert store.get_account(3).enc_key == b"b"
    assert store.get_account(4) is None


def test_families_limit_and_revocation(store, clock):
    ids = []
    for _ in range(3):
        clock.t += 1
        fid, revoked = store.create_family(3, "c1", "Claude", False, "[]", clock.t + 100, max_active=2)
        ids.append(fid)
    assert revoked == [ids[0]]  # la plus ancienne est révoquée à la 3e connexion
    assert [f.id for f in store.list_active_families(3)] == [ids[2], ids[1]]
    assert store.revoke_family(ids[1]) is True
    assert store.revoke_family(ids[1]) is False
    assert store.get_family(ids[1]).revoked_at == clock.t
    store.create_family(4, "c1", "Claude", False, "[]", clock.t + 100, max_active=2)
    assert store.revoke_account(3) == 1
    assert store.list_active_families(3) == []
    assert len(store.list_active_families(4)) == 1


def test_family_absolute_expiry_hides_it(store, clock):
    fid, _ = store.create_family(3, "c1", None, True, "[]", clock.t + 10, max_active=5)
    assert store.get_family(fid).loopback is True
    clock.t += 11
    assert store.list_active_families(3) == []


def test_codes_single_use(store, clock):
    store.save_code("h", "f", "c1", "{}", clock.t + 60)
    assert store.get_code("h").used is False
    assert store.mark_code_used("h") is True
    assert store.mark_code_used("h") is False
    assert store.get_code("h").used is True


def test_tokens_single_use(store, clock):
    store.save_token("r", "f", "refresh", "c1", clock.t + 60)
    assert store.get_token("r").kind == "refresh"
    assert store.mark_token_used("r") is True
    assert store.mark_token_used("r") is False
    with pytest.raises(sqlite3.IntegrityError):
        store.save_token("x", "f", "autre", "c1", 0)


def test_sessions(store, clock):
    store.create_session("s", 3, "csrf", clock.t + 600)
    assert store.get_session("s") == (3, "csrf")
    clock.t += 601
    assert store.get_session("s") is None


def test_idempotency(store, clock):
    store.save_idempotent(3, "k", '{"ok":true}')
    assert store.get_idempotent(3, "k", 600) == '{"ok":true}'
    assert store.get_idempotent(4, "k", 600) is None
    clock.t += 601
    assert store.get_idempotent(3, "k", 600) is None
    store.save_idempotent(3, "k", '{"ok":2}')  # remplace l'ancienne
    assert store.get_idempotent(3, "k", 600) == '{"ok":2}'


def test_actions_count(store, clock):
    for _ in range(3):
        store.record_action(3)
    store.record_action(4)
    assert store.count_actions(3, clock.t - 60) == 3
    clock.t += 61
    assert store.count_actions(3, clock.t - 60) == 0


def test_undo(store, clock):
    assert store.last_undo(3) is None
    store.record_undo(3, "t1", "consommer", "1 lait")
    clock.t += 1
    store.record_undo(3, "t2", "enregistrer_achat", "2 yaourts")
    store.record_undo(4, "t9", "consommer", "x")
    row = store.last_undo(3)
    assert (row.transaction_id, row.undone) == ("t2", False)
    store.mark_undone(row.rowid)
    assert store.last_undo(3).undone is True


def test_confirmations(store, clock):
    store.save_confirmation("h", 3, "corriger_inventaire", "{}", clock.t + 300)
    assert store.pop_confirmation("h", 4, "corriger_inventaire") is None
    assert store.pop_confirmation("h", 3, "consommer") is None
    assert store.pop_confirmation("h", 3, "corriger_inventaire") == "{}"
    assert store.pop_confirmation("h", 3, "corriger_inventaire") is None
    store.save_confirmation("h2", 3, "t", "{}", clock.t + 1)
    clock.t += 2
    assert store.pop_confirmation("h2", 3, "t") is None


def test_purge_expired(store, clock):
    store.save_code("c", "f", "c1", "{}", clock.t + 60)
    store.save_token("t", "f", "access", "c1", clock.t + 60)
    store.create_pending("p", "c1", "{}")
    clock.t += 7200
    store.purge_expired()
    assert store.get_code("c") is None
    assert store.get_token("t") is None
    assert store._db.execute("SELECT COUNT(*) FROM pending").fetchone()[0] == 0


def test_rekey(store):
    store.upsert_account(3, "a", b"old3")
    store.create_pending("p", "c1", "{}")
    store.set_pending_identity("p", b"oldp", 3, "a", "h")
    n = store.rekey(lambda blob, aad: blob.replace(b"old", b"new") + aad.encode())
    assert n == 2
    assert store.get_account(3).enc_key == b"new3grocy-user:3"
    assert store.get_pending("p").enc_key == b"newppending:p"
