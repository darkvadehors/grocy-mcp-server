"""Stockage SQLite du service : tout secret y est haché (codes, jetons) ou chiffré (clés Grocy)."""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clients(
    client_id TEXT PRIMARY KEY, info TEXT NOT NULL, created_at REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS pending(
    id_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, params TEXT NOT NULL, csrf_hash TEXT,
    enc_key BLOB, grocy_user_id INTEGER, grocy_username TEXT, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS accounts(
    grocy_user_id INTEGER PRIMARY KEY, username TEXT NOT NULL, enc_key BLOB NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS families(
    id TEXT PRIMARY KEY, grocy_user_id INTEGER NOT NULL, client_id TEXT NOT NULL, client_name TEXT,
    loopback INTEGER NOT NULL, scopes TEXT NOT NULL, created_at REAL NOT NULL,
    absolute_expires_at REAL NOT NULL, last_used_at REAL, revoked_at REAL);
CREATE INDEX IF NOT EXISTS families_user ON families(grocy_user_id);
CREATE TABLE IF NOT EXISTS codes(
    hash TEXT PRIMARY KEY, family_id TEXT NOT NULL, client_id TEXT NOT NULL, data TEXT NOT NULL,
    expires_at REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS tokens(
    hash TEXT PRIMARY KEY, family_id TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('access', 'refresh')),
    client_id TEXT NOT NULL, expires_at REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS sessions(
    id_hash TEXT PRIMARY KEY, grocy_user_id INTEGER NOT NULL, csrf_hash TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS idempotency(
    grocy_user_id INTEGER NOT NULL, key TEXT NOT NULL, result TEXT NOT NULL, created_at REAL NOT NULL,
    PRIMARY KEY(grocy_user_id, key));
CREATE TABLE IF NOT EXISTS actions(grocy_user_id INTEGER NOT NULL, at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS actions_user ON actions(grocy_user_id, at);
CREATE TABLE IF NOT EXISTS undo(
    grocy_user_id INTEGER NOT NULL, transaction_id TEXT NOT NULL, tool TEXT NOT NULL, summary TEXT NOT NULL,
    at REAL NOT NULL, undone INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS confirmations(
    hash TEXT PRIMARY KEY, grocy_user_id INTEGER NOT NULL, tool TEXT NOT NULL, payload TEXT NOT NULL,
    expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS prices(
    id INTEGER PRIMARY KEY AUTOINCREMENT, grocy_user_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
    location_id INTEGER NOT NULL, price REAL NOT NULL, day TEXT NOT NULL, source TEXT NOT NULL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS prices_product ON prices(product_id);
CREATE TABLE IF NOT EXISTS receipt_labels(
    location_id INTEGER NOT NULL, label TEXT NOT NULL, product_id INTEGER NOT NULL, product_created TEXT,
    updated_at REAL NOT NULL, PRIMARY KEY(location_id, label));
CREATE TABLE IF NOT EXISTS receipt_label_changes(
    receipt_id INTEGER NOT NULL, location_id INTEGER NOT NULL, label TEXT NOT NULL, prev_product INTEGER,
    prev_created TEXT, PRIMARY KEY(receipt_id, location_id, label));
CREATE TABLE IF NOT EXISTS receipts(
    id INTEGER PRIMARY KEY AUTOINCREMENT, location_id INTEGER NOT NULL, day TEXT NOT NULL, total_cents INTEGER NOT NULL,
    created_at REAL NOT NULL, UNIQUE(location_id, day, total_cents));
"""

PENDING_TTL = 600.0


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class PendingRow:
    id_hash: str
    client_id: str
    params: str
    csrf_hash: str | None
    enc_key: bytes | None
    grocy_user_id: int | None
    grocy_username: str | None
    created_at: float


@dataclass(frozen=True)
class AccountRow:
    grocy_user_id: int
    username: str
    enc_key: bytes


@dataclass(frozen=True)
class FamilyRow:
    id: str
    grocy_user_id: int
    client_id: str
    client_name: str | None
    loopback: bool
    scopes: str
    created_at: float
    absolute_expires_at: float
    last_used_at: float | None
    revoked_at: float | None


@dataclass(frozen=True)
class CodeRow:
    hash: str
    family_id: str
    client_id: str
    data: str
    expires_at: float
    used: bool


@dataclass(frozen=True)
class TokenRow:
    hash: str
    family_id: str
    kind: str
    client_id: str
    expires_at: float
    used: bool


@dataclass(frozen=True)
class UndoRow:
    rowid: int
    transaction_id: str
    tool: str
    summary: str
    at: float
    undone: bool


class Store:
    def __init__(self, path: Path | str, clock: Callable[[], float] = time.time) -> None:
        self.now = clock
        path = Path(path)
        if not path.exists():
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        os.chmod(path, 0o600)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, params)

    def _one(self, sql: str, params: tuple = ()) -> tuple | None:
        with self._lock:
            return self._db.execute(sql, params).fetchone()

    # --- clients -------------------------------------------------------------------------------
    def save_client(self, client_id: str, info_json: str) -> None:
        self._exec(
            "INSERT INTO clients(client_id, info, created_at) VALUES(?, ?, ?) "
            "ON CONFLICT(client_id) DO UPDATE SET info = excluded.info, created_at = excluded.created_at",
            (client_id, info_json, self.now()),
        )

    def get_client(self, client_id: str) -> tuple[str, float] | None:
        row = self._one("SELECT info, created_at FROM clients WHERE client_id = ?", (client_id,))
        return (row[0], row[1]) if row else None

    def mark_client_used(self, client_id: str) -> None:
        self._exec("UPDATE clients SET used = 1 WHERE client_id = ?", (client_id,))

    def count_clients(self) -> int:
        return self._one("SELECT COUNT(*) FROM clients")[0]

    def purge_unused_clients(self, max_age: float) -> int:
        return self._exec(
            "DELETE FROM clients WHERE used = 0 AND created_at < ?", (self.now() - max_age,)
        ).rowcount

    # --- pré-autorisations ----------------------------------------------------------------------
    def create_pending(self, id_hash: str, client_id: str, params_json: str) -> None:
        self._exec(
            "INSERT INTO pending(id_hash, client_id, params, created_at) VALUES(?, ?, ?, ?)",
            (id_hash, client_id, params_json, self.now()),
        )

    def get_pending(self, id_hash: str, max_age: float = PENDING_TTL) -> PendingRow | None:
        row = self._one(
            "SELECT id_hash, client_id, params, csrf_hash, enc_key, grocy_user_id, grocy_username, created_at "
            "FROM pending WHERE id_hash = ? AND created_at >= ?",
            (id_hash, self.now() - max_age),
        )
        return PendingRow(*row) if row else None

    def set_pending_csrf(self, id_hash: str, csrf_hash: str) -> None:
        self._exec("UPDATE pending SET csrf_hash = ? WHERE id_hash = ?", (csrf_hash, id_hash))

    def set_pending_identity(
        self, id_hash: str, enc_key: bytes, grocy_user_id: int, username: str, csrf_hash: str
    ) -> None:
        self._exec(
            "UPDATE pending SET enc_key = ?, grocy_user_id = ?, grocy_username = ?, csrf_hash = ? WHERE id_hash = ?",
            (enc_key, grocy_user_id, username, csrf_hash, id_hash),
        )

    def delete_pending(self, id_hash: str) -> None:
        self._exec("DELETE FROM pending WHERE id_hash = ?", (id_hash,))

    # --- comptes --------------------------------------------------------------------------------
    def upsert_account(self, grocy_user_id: int, username: str, enc_key: bytes) -> None:
        self._exec(
            "INSERT INTO accounts(grocy_user_id, username, enc_key, updated_at) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(grocy_user_id) DO UPDATE SET username = excluded.username, "
            "enc_key = excluded.enc_key, updated_at = excluded.updated_at",
            (grocy_user_id, username, enc_key, self.now()),
        )

    def get_account(self, grocy_user_id: int) -> AccountRow | None:
        row = self._one("SELECT grocy_user_id, username, enc_key FROM accounts WHERE grocy_user_id = ?", (grocy_user_id,))
        return AccountRow(*row) if row else None

    # --- connexions (familles de jetons) --------------------------------------------------------
    _FAMILY_COLUMNS = (
        "id, grocy_user_id, client_id, client_name, loopback, scopes, created_at, "
        "absolute_expires_at, last_used_at, revoked_at"
    )

    @staticmethod
    def _family(row: tuple) -> FamilyRow:
        values = list(row)
        values[4] = bool(values[4])
        return FamilyRow(*values)

    def create_family(
        self,
        grocy_user_id: int,
        client_id: str,
        client_name: str | None,
        loopback: bool,
        scopes_json: str,
        absolute_expires_at: float,
        max_active: int,
    ) -> tuple[str, list[str]]:
        family_id = secrets.token_urlsafe(16)
        now = self.now()
        active = self.list_active_families(grocy_user_id)
        to_revoke = [f.id for f in sorted(active, key=lambda f: f.created_at)][: max(0, len(active) - max_active + 1)]
        for fid in to_revoke:
            self.revoke_family(fid)
        self._exec(
            f"INSERT INTO families({self._FAMILY_COLUMNS}) VALUES(?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
            (family_id, grocy_user_id, client_id, client_name, int(loopback), scopes_json, now, absolute_expires_at),
        )
        return family_id, to_revoke

    def get_family(self, family_id: str) -> FamilyRow | None:
        row = self._one(f"SELECT {self._FAMILY_COLUMNS} FROM families WHERE id = ?", (family_id,))
        return self._family(row) if row else None

    def touch_family(self, family_id: str) -> None:
        self._exec("UPDATE families SET last_used_at = ? WHERE id = ?", (self.now(), family_id))

    def revoke_family(self, family_id: str) -> bool:
        return (
            self._exec(
                "UPDATE families SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (self.now(), family_id)
            ).rowcount
            == 1
        )

    def revoke_account(self, grocy_user_id: int) -> int:
        return self._exec(
            "UPDATE families SET revoked_at = ? WHERE grocy_user_id = ? AND revoked_at IS NULL",
            (self.now(), grocy_user_id),
        ).rowcount

    def list_active_families(self, grocy_user_id: int) -> list[FamilyRow]:
        with self._lock:
            rows = self._db.execute(
                f"SELECT {self._FAMILY_COLUMNS} FROM families WHERE grocy_user_id = ? AND revoked_at IS NULL "
                "AND absolute_expires_at > ? ORDER BY created_at DESC",
                (grocy_user_id, self.now()),
            ).fetchall()
        return [self._family(r) for r in rows]

    # --- codes et jetons ------------------------------------------------------------------------
    def save_code(self, code_hash: str, family_id: str, client_id: str, data_json: str, expires_at: float) -> None:
        self._exec(
            "INSERT INTO codes(hash, family_id, client_id, data, expires_at) VALUES(?, ?, ?, ?, ?)",
            (code_hash, family_id, client_id, data_json, expires_at),
        )

    def get_code(self, code_hash: str) -> CodeRow | None:
        row = self._one("SELECT hash, family_id, client_id, data, expires_at, used FROM codes WHERE hash = ?", (code_hash,))
        return CodeRow(*row[:5], bool(row[5])) if row else None

    def mark_code_used(self, code_hash: str) -> bool:
        return self._exec("UPDATE codes SET used = 1 WHERE hash = ? AND used = 0", (code_hash,)).rowcount == 1

    def save_token(self, token_hash: str, family_id: str, kind: str, client_id: str, expires_at: float) -> None:
        self._exec(
            "INSERT INTO tokens(hash, family_id, kind, client_id, expires_at) VALUES(?, ?, ?, ?, ?)",
            (token_hash, family_id, kind, client_id, expires_at),
        )

    def get_token(self, token_hash: str) -> TokenRow | None:
        row = self._one(
            "SELECT hash, family_id, kind, client_id, expires_at, used FROM tokens WHERE hash = ?", (token_hash,)
        )
        return TokenRow(*row[:5], bool(row[5])) if row else None

    def mark_token_used(self, token_hash: str) -> bool:
        return self._exec("UPDATE tokens SET used = 1 WHERE hash = ? AND used = 0", (token_hash,)).rowcount == 1

    # --- sessions de la page /connexions -------------------------------------------------------
    def create_session(self, id_hash: str, grocy_user_id: int, csrf_hash: str, expires_at: float) -> None:
        self._exec(
            "INSERT INTO sessions(id_hash, grocy_user_id, csrf_hash, expires_at) VALUES(?, ?, ?, ?)",
            (id_hash, grocy_user_id, csrf_hash, expires_at),
        )

    def get_session(self, id_hash: str) -> tuple[int, str] | None:
        row = self._one(
            "SELECT grocy_user_id, csrf_hash FROM sessions WHERE id_hash = ? AND expires_at > ?", (id_hash, self.now())
        )
        return (row[0], row[1]) if row else None

    # --- outils ---------------------------------------------------------------------------------
    def get_idempotent(self, grocy_user_id: int, key: str, max_age: float) -> str | None:
        row = self._one(
            "SELECT result FROM idempotency WHERE grocy_user_id = ? AND key = ? AND created_at >= ?",
            (grocy_user_id, key, self.now() - max_age),
        )
        return row[0] if row else None

    def reserve_idempotent(self, grocy_user_id: int, key: str, max_age: float, marker: str) -> str | None:
        """Réserve la clé de façon atomique. Renvoie None si la réservation est acquise, sinon la valeur existante."""
        with self._lock:  # une seule connexion protégée par le verrou : la séquence est atomique dans le service
            self._db.execute(
                "DELETE FROM idempotency WHERE grocy_user_id = ? AND key = ? AND created_at < ?",
                (grocy_user_id, key, self.now() - max_age),
            )
            inserted = self._db.execute(
                "INSERT INTO idempotency(grocy_user_id, key, result, created_at) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(grocy_user_id, key) DO NOTHING",
                (grocy_user_id, key, marker, self.now()),
            ).rowcount
            if inserted == 1:
                return None
            return self._db.execute(
                "SELECT result FROM idempotency WHERE grocy_user_id = ? AND key = ?", (grocy_user_id, key)
            ).fetchone()[0]

    def release_idempotent(self, grocy_user_id: int, key: str) -> None:
        self._exec("DELETE FROM idempotency WHERE grocy_user_id = ? AND key = ?", (grocy_user_id, key))

    def release_idempotent_prefix(self, grocy_user_id: int, prefix: str) -> None:
        self._exec(
            "DELETE FROM idempotency WHERE grocy_user_id = ? AND substr(key, 1, ?) = ?", (grocy_user_id, len(prefix), prefix)
        )

    def save_idempotent(self, grocy_user_id: int, key: str, result_json: str) -> None:
        self._exec(
            "INSERT INTO idempotency(grocy_user_id, key, result, created_at) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(grocy_user_id, key) DO UPDATE SET result = excluded.result, created_at = excluded.created_at",
            (grocy_user_id, key, result_json, self.now()),
        )

    def count_actions(self, grocy_user_id: int, since: float) -> int:
        return self._one(
            "SELECT COUNT(*) FROM actions WHERE grocy_user_id = ? AND at > ?", (grocy_user_id, since)
        )[0]

    def record_action(self, grocy_user_id: int) -> None:
        self._exec("INSERT INTO actions(grocy_user_id, at) VALUES(?, ?)", (grocy_user_id, self.now()))

    def record_undo(self, grocy_user_id: int, transaction_id: str, tool: str, summary: str) -> None:
        self._exec(
            "INSERT INTO undo(grocy_user_id, transaction_id, tool, summary, at) VALUES(?, ?, ?, ?, ?)",
            (grocy_user_id, transaction_id, tool, summary, self.now()),
        )

    def last_undo(self, grocy_user_id: int) -> UndoRow | None:
        row = self._one(
            "SELECT rowid, transaction_id, tool, summary, at, undone FROM undo WHERE grocy_user_id = ? "
            "ORDER BY at DESC, rowid DESC LIMIT 1",
            (grocy_user_id,),
        )
        return UndoRow(*row[:5], bool(row[5])) if row else None

    def update_undo(self, rowid: int, transaction_id: str) -> None:
        self._exec("UPDATE undo SET transaction_id = ? WHERE rowid = ?", (transaction_id, rowid))

    def mark_undone(self, rowid: int) -> None:
        self._exec("UPDATE undo SET undone = 1 WHERE rowid = ?", (rowid,))

    def save_confirmation(
        self, conf_hash: str, grocy_user_id: int, tool: str, payload_json: str, expires_at: float
    ) -> None:
        self._exec(
            "INSERT INTO confirmations(hash, grocy_user_id, tool, payload, expires_at) VALUES(?, ?, ?, ?, ?)",
            (conf_hash, grocy_user_id, tool, payload_json, expires_at),
        )

    def peek_confirmation(self, token: str, grocy_user_id: int, tool: str) -> str | None:
        """Contenu d'un jeton encore valable, sans le consommer (contrôle des choix avant confirmation)."""
        row = self._one(
            "SELECT payload FROM confirmations WHERE hash = ? AND grocy_user_id = ? AND tool = ? AND expires_at > ?",
            (sha256_hex(token), grocy_user_id, tool, self.now()),
        )
        return row[0] if row else None

    def pop_confirmation(self, conf_hash: str, grocy_user_id: int, tool: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "DELETE FROM confirmations WHERE hash = ? AND grocy_user_id = ? AND tool = ? AND expires_at > ? "
                "RETURNING payload",
                (conf_hash, grocy_user_id, tool, self.now()),
            ).fetchone()
        return row[0] if row else None

    # --- prix des courses (spec prix P6) : partagés par le foyer, supprimés seulement par leur auteur ---------
    def add_price(self, grocy_user_id: int, *, product_id: int, location_id: int, price: float, day: str, source: str) -> int:
        cursor = self._exec(
            "INSERT INTO prices(grocy_user_id, product_id, location_id, price, day, source, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (grocy_user_id, product_id, location_id, price, day, source, self.now()),
        )
        return int(cursor.lastrowid)

    def delete_prices(self, ids: list[int], grocy_user_id: int) -> int:
        count = 0
        for price_id in ids:
            count += self._exec("DELETE FROM prices WHERE id = ? AND grocy_user_id = ?", (price_id, grocy_user_id)).rowcount
        return count

    def prices_for(self, product_ids: list[int] | None) -> list[dict]:
        sql = "SELECT id, product_id, location_id, price, day, source FROM prices"
        params: tuple = ()
        if product_ids is not None:
            sql += f" WHERE product_id IN ({','.join('?' * len(product_ids))})" if product_ids else " WHERE 0"
            params = tuple(product_ids)
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY day, id", params).fetchall()
        return [{"id": r[0], "product_id": r[1], "location_id": r[2], "prix": r[3], "date": r[4], "source": r[5]} for r in rows]

    def label_product(self, location_id: int, label: str, created: str | None = None) -> int | None:
        """Produit associé à ce libellé dans ce magasin ; ignoré si le numéro désigne un produit recréé depuis."""
        row = self._one(
            "SELECT product_id, product_created FROM receipt_labels WHERE location_id = ? AND label = ?", (location_id, label)
        )
        if row is None:
            return None
        return int(row[0]) if created is None or row[1] is None or row[1] == created else None

    def save_label(
        self, location_id: int, label: str, product_id: int, created: str | None = None, receipt_id: int | None = None
    ) -> None:
        """Associe un libellé à un produit ; avec receipt_id, l'association d'avant est gardée pour l'annulation."""
        if receipt_id is not None:
            previous = self._one(
                "SELECT product_id, product_created FROM receipt_labels WHERE location_id = ? AND label = ?", (location_id, label)
            )
            self._exec(
                "INSERT OR IGNORE INTO receipt_label_changes(receipt_id, location_id, label, prev_product, prev_created) "
                "VALUES(?, ?, ?, ?, ?)",
                (receipt_id, location_id, label, previous[0] if previous else None, previous[1] if previous else None),
            )
        self._exec(
            "INSERT INTO receipt_labels(location_id, label, product_id, product_created, updated_at) VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(location_id, label) DO UPDATE SET product_id = excluded.product_id, "
            "product_created = excluded.product_created, updated_at = excluded.updated_at",
            (location_id, label, product_id, created, self.now()),
        )

    def receipt_exists(self, location_id: int, day: str, total_cents: int) -> bool:
        return self._one(
            "SELECT 1 FROM receipts WHERE location_id = ? AND day = ? AND total_cents = ?", (location_id, day, total_cents)
        ) is not None

    def reserve_receipt(self, location_id: int, day: str, total_cents: int) -> int | None:
        """Réserve un ticket (magasin, date, total) ; None s'il a déjà été importé."""
        cursor = self._exec(
            "INSERT OR IGNORE INTO receipts(location_id, day, total_cents, created_at) VALUES(?, ?, ?, ?)",
            (location_id, day, total_cents, self.now()),
        )
        return int(cursor.lastrowid) if cursor.rowcount else None

    def delete_receipt(self, receipt_id: int) -> None:
        """Oublie un ticket (annulation) et remet les associations de libellés d'avant ce ticket."""
        with self._lock:
            changes = self._db.execute(
                "SELECT location_id, label, prev_product, prev_created FROM receipt_label_changes WHERE receipt_id = ?",
                (receipt_id,),
            ).fetchall()
        for location_id, label, prev_product, prev_created in changes:
            if prev_product is None:
                self._exec("DELETE FROM receipt_labels WHERE location_id = ? AND label = ?", (location_id, label))
            else:
                self.save_label(location_id, label, prev_product, prev_created)
        self._exec("DELETE FROM receipt_label_changes WHERE receipt_id = ?", (receipt_id,))
        self._exec("DELETE FROM receipts WHERE id = ?", (receipt_id,))

    # --- maintenance ----------------------------------------------------------------------------
    def purge_expired(self) -> None:
        now = self.now()
        with self._lock:
            self._db.execute("DELETE FROM codes WHERE expires_at < ?", (now - 3600,))
            self._db.execute("DELETE FROM tokens WHERE expires_at < ?", (now,))
            self._db.execute("DELETE FROM pending WHERE created_at < ?", (now - PENDING_TTL,))
            self._db.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
            self._db.execute("DELETE FROM idempotency WHERE created_at < ?", (now - 86_400,))
            self._db.execute("DELETE FROM actions WHERE at < ?", (now - 3600,))
            self._db.execute("DELETE FROM confirmations WHERE expires_at < ?", (now,))
            self._db.execute("DELETE FROM undo WHERE at < ?", (now - 86_400,))
            self._db.execute("DELETE FROM families WHERE absolute_expires_at < ?", (now - 86_400,))

    def rekey(self, reseal: Callable[[bytes, str], bytes]) -> int:
        """Re-chiffre toutes les clés Grocy (rotation de la clé maître) dans une seule transaction."""
        count = 0
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                for uid, blob in self._db.execute("SELECT grocy_user_id, enc_key FROM accounts").fetchall():
                    new = reseal(blob, f"grocy-user:{uid}")
                    self._db.execute("UPDATE accounts SET enc_key = ? WHERE grocy_user_id = ?", (new, uid))
                    count += 1
                rows = self._db.execute("SELECT id_hash, enc_key FROM pending WHERE enc_key IS NOT NULL").fetchall()
                for id_hash, blob in rows:
                    new = reseal(blob, f"pending:{id_hash}")
                    self._db.execute("UPDATE pending SET enc_key = ? WHERE id_hash = ?", (new, id_hash))
                    count += 1
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return count
