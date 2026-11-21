"""Conexión con Telegram: lee mensajes, guarda miniaturas en disco y mantiene la caché al día."""
import asyncio
import logging
import time
from typing import Dict, Optional

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto

from config import API_ID, API_HASH, CHANNEL_ID, SESSION_STRING, THUMB_DIR
from library import Library

log = logging.getLogger("biblioteca")
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

sync_state: Dict = {"running": False, "mode": None, "processed": 0, "scanned": 0,
                    "error": None, "finished_at": None}


def raw_from_message(msg) -> Optional[Dict]:
    """Datos mínimos y serializables de un mensaje con archivo o foto."""
    media = msg.media
    if not media:
        return None

    filename, mime, size, duration = "", "", 0, 0
    is_photo, thumb_src = False, False

    if isinstance(media, MessageMediaDocument) and getattr(media, "document", None):
        doc = media.document
        for attr in getattr(doc, "attributes", []):
            if getattr(attr, "file_name", None):
                filename = attr.file_name
            if hasattr(attr, "duration"):
                duration = getattr(attr, "duration", 0) or 0
        mime = getattr(doc, "mime_type", "") or ""
        size = getattr(doc, "size", 0) or 0
        thumb_src = bool(getattr(doc, "thumbs", None))
    elif isinstance(media, MessageMediaPhoto):
        filename, mime, is_photo, thumb_src = f"photo_{msg.id}.jpg", "image/jpeg", True, True
    else:
        return None

    when = msg.edit_date or msg.date
    link = f"https://t.me/c/{str(CHANNEL_ID)[4:]}/{msg.id}" if str(CHANNEL_ID).startswith("-100") else ""
    return {
        "id": msg.id, "filename": filename, "mime": mime, "size": size, "duration": int(duration),
        "is_photo": is_photo, "caption": msg.message or "",
        "date": msg.date.isoformat() if msg.date else "",
        "stamp": str(int(when.timestamp())) if when else "0",
        "link": link, "thumb_src": thumb_src, "thumb": False,
    }


async def _save_thumb(msg) -> bool:
    try:
        data = await client.download_media(msg, file=bytes, thumb=-1)
        if data:
            (THUMB_DIR / f"{msg.id}.jpg").write_bytes(data)
            return True
    except Exception as e:  # miniatura rota: no debe tumbar la indexación
        log.warning("Miniatura del mensaje %s no disponible: %s", msg.id, e)
    return False


async def process_message(lib: Library, msg, commit: bool = True) -> bool:
    """Guarda/actualiza un mensaje. Devuelve False si no había nada que hacer."""
    raw = raw_from_message(msg)
    if raw is None:
        return False
    prev = lib.raws.get(raw["id"])
    unchanged = prev is not None and prev.get("stamp") == raw["stamp"]
    if unchanged and (prev.get("thumb") or not raw["thumb_src"]):
        return False
    if raw["thumb_src"]:
        raw["thumb"] = await _save_thumb(msg)
    lib.upsert(raw, commit=commit)
    return True


async def sync(lib: Library, full: bool = False) -> None:
    """Incremental: solo mensajes nuevos. Completo: revisa todo (ediciones y borrados)
    pero solo descarga lo que cambió."""
    if sync_state["running"]:
        return
    sync_state.update(running=True, error=None, processed=0, scanned=0,
                      mode="completa" if full else "incremental")
    try:
        entity = await client.get_entity(CHANNEL_ID)
        last = int(lib.db.get_meta("last_id") or 0)
        full = full or last == 0
        sync_state["mode"] = "completa" if full else "incremental"
        seen, max_id = set(), last
        kwargs = {} if full else {"min_id": last}

        async for msg in client.iter_messages(entity, limit=None, **kwargs):
            max_id = max(max_id, msg.id)
            seen.add(msg.id)
            if await process_message(lib, msg, commit=False):
                sync_state["processed"] += 1
            sync_state["scanned"] += 1
            if sync_state["scanned"] % 100 == 0:
                lib.flush()
                log.info("Sincronizando... %s revisados", sync_state["scanned"])

        if full:
            gone = [i for i in list(lib.raws) if i not in seen]
            if gone:
                log.info("Eliminando %s mensajes que ya no existen en el canal", len(gone))
                lib.delete(gone)
        lib.bump_last_id(max_id)
        lib.flush()
        log.info("✅ Sincronización %s: %s revisados, %s actualizados",
                 sync_state["mode"], sync_state["scanned"], sync_state["processed"])
    except Exception as e:
        sync_state["error"] = str(e)
        log.exception("Error sincronizando")
    finally:
        sync_state["running"] = False
        sync_state["finished_at"] = int(time.time())


def register_handlers(lib: Library) -> None:
    @client.on(events.NewMessage(chats=CHANNEL_ID))
    @client.on(events.MessageEdited(chats=CHANNEL_ID))
    async def _changed(event):
        if await process_message(lib, event.message):
            pass
        lib.bump_last_id(event.message.id)

    @client.on(events.MessageDeleted())
    async def _deleted(event):
        # solo si Telegram indica que es de nuestro canal (los ids de otros chats pueden coincidir)
        if getattr(event, "chat_id", None) == CHANNEL_ID:
            lib.delete(event.deleted_ids)
