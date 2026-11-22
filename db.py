"""Caché del catálogo en SQLite. Guarda los datos CRUDOS de cada mensaje; la interpretación
(títulos, álbumes, géneros...) se recalcula al cargar, así un cambio en el parser no exige releer Telegram."""
import json
import sqlite3
import threading
import time
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
CREATE TABLE IF NOT EXISTS favorites (
    key   TEXT PRIMARY KEY,
    added INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS seen (
    item_id INTEGER PRIMARY KEY,
    key     TEXT NOT NULL,
    at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta_cache (
    key     TEXT PRIMARY KEY,
    data    TEXT NOT NULL,
    fetched INTEGER NOT NULL
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

    # ---- favoritos ----
    def toggle_favorite(self, key: str) -> bool:
        """Devuelve True si queda como favorito, False si se quitó."""
        with self.lock:
            row = self.conn.execute("SELECT 1 FROM favorites WHERE key = ?", (key,)).fetchone()
            if row:
                self.conn.execute("DELETE FROM favorites WHERE key = ?", (key,))
                state = False
            else:
                self.conn.execute("INSERT INTO favorites (key, added) VALUES (?, ?)", (key, int(time.time())))
                state = True
            self.conn.commit()
        return state

    def favorites(self) -> Dict[str, int]:
        with self.lock:
            return dict(self.conn.execute("SELECT key, added FROM favorites").fetchall())

    # ---- vistos / continuar ----
    def mark_seen(self, item_id: int, key: str) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO seen (item_id, key, at) VALUES (?, ?, ?) "
                "ON CONFLICT(item_id) DO UPDATE SET key=excluded.key, at=excluded.at",
                (int(item_id), key, int(time.time())))
            self.conn.commit()

    def unmark_seen(self, item_id: int) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM seen WHERE item_id = ?", (int(item_id),))
            self.conn.commit()

    def seen_map(self) -> Dict[int, tuple]:
        """{id de archivo: (clave del grupo, momento)}"""
        with self.lock:
            rows = self.conn.execute("SELECT item_id, key, at FROM seen").fetchall()
        return {r[0]: (r[1], r[2]) for r in rows}

    # ---- caché de metadatos automáticos ----
    def load_meta_cache(self) -> Dict[str, Dict]:
        with self.lock:
            rows = self.conn.execute("SELECT key, data, fetched FROM meta_cache").fetchall()
        return {k: {"data": json.loads(d), "fetched": f} for k, d, f in rows}

    def set_meta_cache(self, key: str, data: Dict) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO meta_cache (key, data, fetched) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET data=excluded.data, fetched=excluded.fetched",
                (key, json.dumps(data, ensure_ascii=False), int(time.time())))
            self.conn.commit()
