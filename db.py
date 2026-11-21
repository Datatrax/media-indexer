"""Caché del catálogo en SQLite. Guarda los datos CRUDOS de cada mensaje; la interpretación
(títulos, álbumes, géneros...) se recalcula al cargar, así un cambio en el parser no exige releer Telegram."""
import json
import sqlite3
import threading
from typing import Dict, Iterable, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id    INTEGER PRIMARY KEY,
    stamp TEXT NOT NULL,
    data  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class Database:
    def __init__(self, path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def load_all(self) -> List[Dict]:
        with self.lock:
            rows = self.conn.execute("SELECT data FROM items ORDER BY id").fetchall()
        return [json.loads(r[0]) for r in rows]

    def upsert(self, raw: Dict, commit: bool = True) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO items (id, stamp, data) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET stamp=excluded.stamp, data=excluded.data",
                (raw["id"], str(raw.get("stamp", "0")), json.dumps(raw, ensure_ascii=False)),
            )
            if commit:
                self.conn.commit()

    def delete(self, ids: Iterable[int], commit: bool = True) -> None:
        with self.lock:
            self.conn.executemany("DELETE FROM items WHERE id = ?", [(i,) for i in ids])
            if commit:
                self.conn.commit()

    def commit(self) -> None:
        with self.lock:
            self.conn.commit()

    def get_meta(self, key: str) -> Optional[str]:
        with self.lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
            self.conn.commit()
