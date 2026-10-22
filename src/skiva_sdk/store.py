"""Optional durable bindings. Stored references never grant access."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

from .errors import LimitExceeded, ProtocolError, SnapshotBusy
from .protocol import SnapshotRef


class BindingStore(Protocol):
    """Implement atomic compare-and-set; never silently expire a binding."""

    def get(self, key: str) -> SnapshotRef | None: ...

    def put(
        self, key: str, ref: SnapshotRef, *, expected: SnapshotRef | None, limit: int
    ) -> None: ...

    def delete(self, key: str, *, expected: SnapshotRef | None) -> bool: ...


class SQLiteBindingStore:
    """Local-disk persistence with atomic publication across processes.

    Namespace must identify the application/profile. Use a private local directory,
    not a network filesystem. No credentials or Skill text are stored. A store
    coordinates publication, not execution: existing immutable views can overlap.
    """

    def __init__(self, path: str | Path, *, namespace: str):
        if not namespace or not isinstance(namespace, str):
            raise ValueError("A non-empty application/profile namespace is required")
        self.path = str(path)
        if self.path == ":memory:":
            raise ValueError("Use a file path for persistent bindings")
        self.namespace = namespace
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS bindings (namespace TEXT, key TEXT, ref TEXT NOT NULL, "
                "PRIMARY KEY(namespace,key))"
            )

    @contextmanager
    def _connect(self):
        # Short synchronous metadata transactions; no await/network under a DB lock.
        db = sqlite3.connect(self.path, timeout=0)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _read(db, namespace, key):
        row = db.execute(
            "SELECT ref FROM bindings WHERE namespace=? AND key=?", (namespace, key)
        ).fetchone()
        if row is None:
            return None
        try:
            ref = SnapshotRef(**json.loads(row[0]))
            if not all(isinstance(v, str) and v for v in asdict(ref).values()):
                raise ValueError()
            return ref
        except (ValueError, TypeError):
            raise ProtocolError("Invalid stored snapshot reference") from None

    def get(self, key):
        with self._connect() as db:
            return self._read(db, self.namespace, key)

    def put(self, key, ref, *, expected, limit):
        try:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                current = self._read(db, self.namespace, key)
                if current != expected:
                    raise SnapshotBusy("Stored binding changed during preparation; retry entry")
                if current is None:
                    count = db.execute(
                        "SELECT count(*) FROM bindings WHERE namespace=?", (self.namespace,)
                    ).fetchone()[0]
                    if count >= limit:
                        raise LimitExceeded(
                            "Persistent bindings are full; forget an inactive binding"
                        )
                db.execute(
                    "INSERT INTO bindings VALUES(?,?,?) ON CONFLICT(namespace,key) "
                    "DO UPDATE SET ref=excluded.ref",
                    (self.namespace, key, json.dumps(asdict(ref))),
                )
        except sqlite3.OperationalError:
            raise SnapshotBusy("Binding store is unavailable or busy") from None

    def delete(self, key, *, expected):
        try:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if self._read(db, self.namespace, key) != expected:
                    raise SnapshotBusy("Stored binding changed before cleanup")
                return (
                    db.execute(
                        "DELETE FROM bindings WHERE namespace=? AND key=?", (self.namespace, key)
                    ).rowcount
                    > 0
                )
        except sqlite3.OperationalError:
            raise SnapshotBusy("Binding store is unavailable or busy") from None
