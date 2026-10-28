"""Хранилище SQLite в режиме WAL (ARCH-05, REPLAN-02, план §2.13).

Хранится: сессии, ревизии ``TripState`` (только добавляются), ``trip_heads``, корзины, чеки,
аудит и события сессии для SSE. Тексты пользователя в журналы и события не пишутся (SECURITY-01).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from odin.contracts import Cart, DemoReceipt, TripState

SCHEMA_VERSION = "1.0.0"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    llm_mode TEXT NOT NULL,
    data_mode TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS revisions (
    trip_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    request_id TEXT NOT NULL,
    status TEXT NOT NULL,
    state_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (trip_id, revision)
);

CREATE TABLE IF NOT EXISTS trip_heads (
    trip_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    current_revision INTEGER NOT NULL,
    last_agreed_revision INTEGER,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS carts (
    cart_id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL,
    trip_revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    cart_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS receipts (
    receipt_id TEXT PRIMARY KEY,
    cart_id TEXT NOT NULL,
    confirmation_id TEXT NOT NULL UNIQUE,
    receipt_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    at TEXT NOT NULL,
    event TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    event TEXT NOT NULL,
    request_id TEXT,
    trip_id TEXT,
    session_id TEXT,
    payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_session ON session_events (session_id, seq);
CREATE INDEX IF NOT EXISTS idx_carts_trip ON carts (trip_id, trip_revision);
CREATE INDEX IF NOT EXISTS idx_audit_trip ON audit (trip_id, audit_id);
"""


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _dump(model: TripState | Cart | DemoReceipt) -> str:
    return model.model_dump_json()


class ConflictError(RuntimeError):
    """Несовпадение ``trip_revision`` (API отвечает 409)."""


class NotFoundError(LookupError):
    """Запрошенная сущность отсутствует (API отвечает 404)."""


class Storage:
    """Единая точка записи состояния. Ревизии неизменяемы; голова обновляется в той же транзакции."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._memory = str(self.path) == ":memory:"
        if not self._memory:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_registry: dict[str, threading.Lock] = {}
        self._registry_guard = threading.Lock()
        self._memory_connection: sqlite3.Connection | None = None
        self._memory_guard = threading.Lock()
        self.initialize()

    # ------------------------------------------------------------------ соединение

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Соединение на операцию. Для ``:memory:`` — одно общее (иначе схема не видна)."""
        if not self._memory:
            connection = self._connect()
            try:
                yield connection
            finally:
                connection.close()
            return
        with self._memory_guard:
            if self._memory_connection is None:
                self._memory_connection = self._connect()
            yield self._memory_connection

    def _connect(self) -> sqlite3.Connection:
        if self._memory:
            connection = sqlite3.connect(
                ":memory:", timeout=10.0, isolation_level=None, check_same_thread=False
            )
        else:
            connection = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def initialize(self) -> None:
        with self._connection() as connection:
            if not self._memory:
                connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(_SCHEMA)
            connection.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,)
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.execute("ROLLBACK")
                raise
            else:
                connection.execute("COMMIT")

    def journal_mode(self) -> str:
        with self._connection() as connection:
            return str(connection.execute("PRAGMA journal_mode").fetchone()[0])

    @contextmanager
    def session_lock(self, session_id: str) -> Iterator[None]:
        """Одна сессия обрабатывает один ход за раз (план §2.13)."""
        with self._registry_guard:
            lock = self._lock_registry.setdefault(session_id, threading.Lock())
        with lock:
            yield

    # ------------------------------------------------------------------ сессии

    def create_session(self, session_id: str, trip_id: str, llm_mode: str, data_mode: str) -> dict[str, Any]:
        now = utcnow().isoformat()
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO sessions (session_id, trip_id, created_at, updated_at, llm_mode, data_mode)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, trip_id, now, now, llm_mode, data_mode),
            )
            connection.execute(
                "INSERT INTO trip_heads (trip_id, session_id, current_revision, last_agreed_revision, updated_at)"
                " VALUES (?, ?, 0, NULL, ?)",
                (trip_id, session_id, now),
            )
        return {
            "session_id": session_id,
            "trip_id": trip_id,
            "created_at": now,
            "updated_at": now,
            "llm_mode": llm_mode,
            "data_mode": data_mode,
        }

    def get_session(self, session_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"сессия не найдена: {session_id}")
        return dict(row)

    def get_session_by_trip(self, trip_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM sessions WHERE trip_id = ?", (trip_id,)).fetchone()
        return None if row is None else dict(row)

    # ------------------------------------------------------------------ ревизии

    def get_head(self, trip_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM trip_heads WHERE trip_id = ?", (trip_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"поездка не найдена: {trip_id}")
        return dict(row)

    def append_revision(self, state: TripState, request_id: str, *, agreed: bool) -> int:
        """Добавляет ревизию и атомарно обновляет голову. Возвращает номер ревизии."""
        now = utcnow().isoformat()
        with self.transaction() as connection:
            head = connection.execute(
                "SELECT current_revision FROM trip_heads WHERE trip_id = ?", (state.trip_id,)
            ).fetchone()
            if head is None:
                raise NotFoundError(f"поездка не найдена: {state.trip_id}")
            if state.revision != head["current_revision"] + 1:
                raise ConflictError(
                    f"ожидалась ревизия {head['current_revision'] + 1}, получена {state.revision}"
                )
            connection.execute(
                "INSERT INTO revisions (trip_id, revision, request_id, status, state_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (state.trip_id, state.revision, request_id, state.status, _dump(state), now),
            )
            if agreed:
                connection.execute(
                    "UPDATE trip_heads SET current_revision = ?, last_agreed_revision = ?, updated_at = ?"
                    " WHERE trip_id = ?",
                    (state.revision, state.revision, now, state.trip_id),
                )
            else:
                connection.execute(
                    "UPDATE trip_heads SET current_revision = ?, updated_at = ? WHERE trip_id = ?",
                    (state.revision, now, state.trip_id),
                )
            connection.execute("UPDATE sessions SET updated_at = ? WHERE trip_id = ?", (now, state.trip_id))
        return state.revision

    def get_state(self, trip_id: str, revision: int | None = None) -> TripState:
        if revision is None:
            revision = int(self.get_head(trip_id)["current_revision"])
        with self._connection() as connection:
            row = connection.execute(
                "SELECT state_json FROM revisions WHERE trip_id = ? AND revision = ?", (trip_id, revision)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"ревизия {revision} поездки {trip_id} не найдена")
        return TripState.model_validate_json(row["state_json"])

    def list_revisions(self, trip_id: str) -> list[int]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT revision FROM revisions WHERE trip_id = ? ORDER BY revision", (trip_id,)
            ).fetchall()
        return [int(row["revision"]) for row in rows]

    # ------------------------------------------------------------------ корзины и чеки

    def save_cart(self, cart: Cart) -> None:
        now = utcnow().isoformat()
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO carts (cart_id, trip_id, trip_revision, status, cart_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(cart_id) DO UPDATE SET status = excluded.status,"
                " cart_json = excluded.cart_json, updated_at = excluded.updated_at",
                (cart.cart_id, cart.trip_id, cart.trip_revision, cart.status, _dump(cart), now, now),
            )

    def get_cart(self, cart_id: str) -> Cart:
        with self._connection() as connection:
            row = connection.execute("SELECT cart_json FROM carts WHERE cart_id = ?", (cart_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"корзина не найдена: {cart_id}")
        return Cart.model_validate_json(row["cart_json"])

    def get_cart_by_trip(self, trip_id: str) -> Cart | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT cart_json FROM carts WHERE trip_id = ? ORDER BY updated_at DESC, rowid DESC LIMIT 1",
                (trip_id,),
            ).fetchone()
        return None if row is None else Cart.model_validate_json(row["cart_json"])

    def save_receipt(self, receipt: DemoReceipt, confirmation_id: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO receipts (receipt_id, cart_id, confirmation_id, receipt_json, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    receipt.receipt_id,
                    receipt.cart_id,
                    confirmation_id,
                    _dump(receipt),
                    receipt.created_at.isoformat(),
                ),
            )

    def get_receipt(self, receipt_id: str) -> DemoReceipt:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT receipt_json FROM receipts WHERE receipt_id = ?", (receipt_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"чек не найден: {receipt_id}")
        return DemoReceipt.model_validate_json(row["receipt_json"])

    def get_receipt_by_confirmation(self, confirmation_id: str) -> DemoReceipt | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT receipt_json FROM receipts WHERE confirmation_id = ?", (confirmation_id,)
            ).fetchone()
        return None if row is None else DemoReceipt.model_validate_json(row["receipt_json"])

    # ------------------------------------------------------------------ события и аудит

    def add_event(self, session_id: str, event: str, payload: dict[str, Any] | None = None) -> int:

        with self.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO session_events (session_id, at, event, payload_json) VALUES (?, ?, ?, ?)",
                (session_id, utcnow().isoformat(), event, json.dumps(payload or {}, ensure_ascii=False)),
            )
            return int(cursor.lastrowid or 0)

    def list_events(self, session_id: str, after_seq: int = 0) -> list[dict[str, Any]]:

        with self._connection() as connection:
            rows = connection.execute(
                "SELECT seq, at, event, payload_json FROM session_events WHERE session_id = ? AND seq > ?"
                " ORDER BY seq",
                (session_id, after_seq),
            ).fetchall()
        return [
            {
                "seq": row["seq"],
                "at": row["at"],
                "event": row["event"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    def audit(
        self,
        event: str,
        *,
        request_id: str | None = None,
        trip_id: str | None = None,
        session_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:

        with self.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO audit (at, event, request_id, trip_id, session_id, payload_json)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    utcnow().isoformat(),
                    event,
                    request_id,
                    trip_id,
                    session_id,
                    json.dumps(payload or {}, ensure_ascii=False),
                ),
            )
            return int(cursor.lastrowid or 0)

    def list_audit(self, trip_id: str | None = None) -> list[dict[str, Any]]:

        query = "SELECT * FROM audit"
        params: tuple[Any, ...] = ()
        if trip_id is not None:
            query += " WHERE trip_id = ?"
            params = (trip_id,)
        query += " ORDER BY audit_id"
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [
            {
                "audit_id": row["audit_id"],
                "at": row["at"],
                "event": row["event"],
                "request_id": row["request_id"],
                "trip_id": row["trip_id"],
                "session_id": row["session_id"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    def ping(self) -> bool:
        try:
            with self._connection() as connection:
                connection.execute("SELECT 1").fetchone()
        except sqlite3.Error:
            return False
        return True
