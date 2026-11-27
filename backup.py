"""Respaldo de lo que NO está en Telegram: favoritos, vistos, progreso de lectura/escucha y datos automáticos.
- Manual: descargar / restaurar un archivo JSON desde /admin.
- Automático (opcional, AUTO_BACKUP=1): se guarda en tus "Mensajes guardados" de Telegram y se restaura solo si el
  servidor arranca sin datos (por ejemplo, un hosting sin disco persistente que se reinicia)."""
import asyncio
import hashlib
import io
import json
import logging
import time
from typing import Any, Dict, Optional

log = logging.getLogger("biblioteca")
APP = "biblioteca-de-prozac"
FORMAT = 1
FILENAME = "biblioteca-respaldo.json"
CAPTION = "Respaldo automático de Biblioteca de Prozac (favoritos, vistos y progreso). Se reemplaza solo; no lo borres."
MAX_ROWS = 200_000

state: Dict[str, Any] = {"enabled": False, "last_upload": None, "last_restore": None, "error": None}


def export_data(db) -> Dict:
    progress = db.progress_map()
    return {
        "app": APP, "format": FORMAT, "exported_at": int(time.time()),
        "favorites": [{"key": k, "added": a} for k, a in sorted(db.favorites().items())],
        "seen": [{"id": i, "key": k, "at": at} for i, (k, at) in sorted(db.seen_map().items())],
        "progress": [{"id": i, "key": p["key"], "pos": p["pos"], "dur": p["dur"], "extra": p["extra"], "at": p["at"]}
                     for i, p in sorted(progress.items())],
        "meta_cache": [{"key": k, "data": v["data"], "fetched": v["fetched"]} for k, v in sorted(db.load_meta_cache().items())],
    }


def digest(data: Dict) -> str:
    body = {k: v for k, v in data.items() if k != "exported_at"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _need(cond: bool, what: str) -> None:
    if not cond:
        raise ValueError(f"Respaldo inválido: {what}")


def parse_backup(data: Any) -> Dict:
    """Valida el archivo y lo normaliza. Lanza ValueError con un mensaje claro si algo no cuadra."""
    _need(isinstance(data, dict) and data.get("app") == APP, "no es un respaldo de Biblioteca de Prozac")
    _need(isinstance(data.get("format"), int) and data["format"] <= FORMAT, "es de una versión más nueva")
    out = {}
    for name in ("favorites", "seen", "progress", "meta_cache"):
        rows = data.get(name, [])
        _need(isinstance(rows, list) and len(rows) <= MAX_ROWS, f"'{name}' mal formado")
        out[name] = rows
    s = lambda v, n=300: isinstance(v, str) and 0 < len(v) <= n
    i = lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 2 ** 53
    f = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < 1e12
    favorites, seen, progress, meta = [], [], [], []
    for r in out["favorites"]:
        _need(isinstance(r, dict) and s(r.get("key")) and i(r.get("added")), "favorito dañado")
        favorites.append((r["key"], r["added"]))
    for r in out["seen"]:
        _need(isinstance(r, dict) and i(r.get("id")) and s(r.get("key")) and i(r.get("at")), "'visto' dañado")
        seen.append((r["id"], r["key"], r["at"]))
    for r in out["progress"]:
        _need(isinstance(r, dict) and i(r.get("id")) and s(r.get("key")) and f(r.get("pos")) and f(r.get("dur"))
              and isinstance(r.get("extra", ""), str) and len(r.get("extra", "")) <= 400 and i(r.get("at")), "progreso dañado")
        progress.append((r["id"], r["key"], float(r["pos"]), float(r["dur"]), r.get("extra", ""), r["at"]))
    for r in out["meta_cache"]:
        _need(isinstance(r, dict) and s(r.get("key")) and isinstance(r.get("data"), dict) and i(r.get("fetched")), "dato automático dañado")
        meta.append((r["key"], r["data"], r["fetched"]))
    return {"favorites": favorites, "seen": seen, "progress": progress, "meta_cache": meta}


def import_data(db, data: Any) -> Dict[str, int]:
    p = parse_backup(data)
    return db.import_user_data(p["favorites"], p["seen"], p["progress"], p["meta_cache"])


class TelegramBackup:
    """Guarda y recupera el respaldo en 'Mensajes guardados'. `client` es el TelegramClient (o uno falso en pruebas)."""

    def __init__(self, client, db, interval: int = 600, keep: int = 2):
        self.client, self.db, self.interval, self.keep = client, db, interval, keep
        self.last_digest: Optional[str] = None
        state["enabled"] = True

    async def _backup_messages(self):
        found = []
        async for m in self.client.iter_messages("me", limit=200):
            f = getattr(m, "file", None)
            if f is not None and getattr(f, "name", None) == FILENAME:
                found.append(m)
        found.sort(key=lambda m: m.id, reverse=True)       # el más nuevo primero
        return found

    async def restore_if_empty(self) -> Optional[Dict[str, int]]:
        if not self.db.user_data_is_empty():
            self.last_digest = digest(export_data(self.db))
            return None
        for msg in await self._backup_messages():           # si el más nuevo estuviera dañado, prueba el anterior
            try:
                raw = await self.client.download_media(msg, file=bytes)
                counts = import_data(self.db, json.loads(raw.decode("utf-8")))
                self.last_digest = digest(export_data(self.db))
                state["last_restore"] = int(time.time())
                log.info("Respaldo restaurado desde Telegram: %s", counts)
                return counts
            except Exception as e:
                log.warning("Respaldo %s no se pudo restaurar: %s", msg.id, e)
        self.last_digest = digest(export_data(self.db))
        return None

    async def upload_if_changed(self) -> bool:
        data = export_data(self.db)
        d = digest(data)
        if d == self.last_digest:
            return False
        payload = io.BytesIO(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        payload.name = FILENAME
        await self.client.send_file("me", payload, caption=CAPTION, force_document=True)
        self.last_digest = d
        state["last_upload"] = int(time.time())
        old = (await self._backup_messages())[self.keep:]
        if old:
            await self.client.delete_messages("me", [m.id for m in old])
        return True

    async def run_forever(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            try:
                await self.upload_if_changed()
                state["error"] = None
            except Exception as e:
                state["error"] = str(e)
                log.warning("No se pudo subir el respaldo: %s", e)
