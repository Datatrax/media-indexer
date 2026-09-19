import os
import re
import secrets
import base64
from io import BytesIO
from typing import Dict, Any, Set

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto

from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import uvicorn
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
SESSION_STRING = os.getenv("SESSION_STRING")
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "cambia_esta_password")
ITEMS_PER_PAGE = 12

app = FastAPI(title="Media Indexer")
templates = Jinja2Templates(directory="templates")
security = HTTPBasic()

catalog = {"movies": [], "series": [], "music": [], "other": []}
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)


def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)):
    correct_user = secrets.compare_digest(credentials.username, ADMIN_USER)
    correct_pass = secrets.compare_digest(credentials.password, ADMIN_PASS)
    if not (correct_user and correct_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales incorrectas",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def normalize_title(title: str) -> str:
    if not title:
        return "sin título"
    t = title.lower().strip()
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\s*(heavens? lost property|heaven’s lost property).*$", "", t)
    return t.strip()


def parse_caption(caption: str) -> Dict[str, Any]:
    data = {
        "nombre": None, "secuela": None, "precuela": None, "tipo": None, "episodios": None,
        "estudio": None, "año": None, "genero": None, "resolucion": None,
        "calidad": None, "duracion": None, "audios": None, "subtitulos": None,
        "fansub": None, "formato": None, "peso": None, "sinopsis": None
    }
    if not caption:
        return data

    clean_caption = re.sub(r"[*_`~]", "", caption)

    sinopsis_match = re.search(r"Sinopsis:\s*(.+)", clean_caption, re.IGNORECASE | re.DOTALL)
    if sinopsis_match:
        data["sinopsis"] = sinopsis_match.group(1).strip()

    for line in clean_caption.split("\n"):
        line = line.strip()
        if ":" in line:
            key, value = line.split(":", 1)
            key = key.strip().lower()
            value = value.strip()

            mapping = {
                "nombre": "nombre", "title": "nombre",
                "secuela": "secuela", "sequel": "secuela",
                "precuela": "precuela", "prequel": "precuela",
                "tipo": "tipo", "type": "tipo",
                "episodios": "episodios", "episodes": "episodios", "eps": "episodios",
                "estudio": "estudio", "studio": "estudio",
                "año": "año", "year": "año", "ano": "año",
                "género": "genero", "genero": "genero", "genre": "genero",
                "resolución": "resolucion", "resolucion": "resolucion", "resolution": "resolucion",
                "calidad": "calidad", "quality": "calidad",
                "duración": "duracion", "duracion": "duracion", "duration": "duracion",
                "audios": "audios", "audio": "audios",
                "subtítulos": "subtitulos", "subtitulos": "subtitulos", "subtitles": "subtitulos", "subs": "subtitulos",
                "fansub": "fansub", "sub": "fansub",
                "formato": "formato", "format": "formato",
                "peso": "peso", "size": "peso"
            }
            if key in mapping:
                data[mapping[key]] = value
    return data


def extract_info(filename: str, caption: str = "") -> Dict[str, Any]:
    parsed = parse_caption(caption)

    if not parsed["nombre"]:
        clean = re.sub(r"\.(mkv|mp4|avi|mov)$", "", filename, flags=re.I)
        clean = re.sub(r"[\._]", " ", clean)
        clean = re.sub(r"\[.*?\]", "", clean)
        clean = re.sub(r"\s*[-–]\s*(ova|special|sp|especial)?\s*\d{0,3}.*$", "", clean, flags=re.I)
        parsed["nombre"] = re.sub(r"\s+", " ", clean).strip()

    episode = None
    is_ova = False
    special_type = None
    fname = filename.lower()

    if "ova" in fname:
        is_ova = True
        special_type = "OVA"
        match = re.search(r"ova\s*[-–]?\s*(\d{1,3})", fname)
        if match:
            episode = int(match.group(1))
        else:
            numbers = re.findall(r"\d{1,3}", fname)
            if numbers:
                episode = int(numbers[0])
    elif "special" in fname or "especial" in fname or re.search(r"\bsp\b", fname):
        is_ova = True
        special_type = "Special"
        match = re.search(r"(?:special|especial|sp)\s*[-–]?\s*(\d{1,3})", fname)
        if match:
            episode = int(match.group(1))
        else:
            numbers = re.findall(r"\d{1,3}", fname)
            if numbers:
                episode = int(numbers[0])
    else:
        match = re.search(r"[-–]\s*(\d{1,3})\b", fname)
        if match:
            episode = int(match.group(1))

    return {
        "title": parsed["nombre"],
        "secuela": parsed["secuela"],
        "precuela": parsed["precuela"],
        "tipo": parsed["tipo"],
        "episodios": parsed["episodios"],
        "estudio": parsed["estudio"],
        "year": parsed["año"],
        "genero": parsed["genero"],
        "resolucion": parsed["resolucion"],
        "quality": parsed["calidad"],
        "duracion": parsed["duracion"],
        "audios": parsed["audios"],
        "subtitulos": parsed["subtitulos"],
        "fansub": parsed["fansub"],
        "formato": parsed["formato"],
        "peso": parsed["peso"],
        "sinopsis": parsed["sinopsis"],
        "season": 1,
        "episode": episode,
        "is_ova": is_ova,
        "special_type": special_type
    }


def classify(filename: str, mime: str = "", caption: str = "", tipo: str = None) -> str:
    if tipo:
        t = tipo.lower()
        if any(x in t for x in ["serie", "ova", "anime"]):
            return "series"
        if any(x in t for x in ["película", "pelicula", "movie"]):
            return "movies"
        if any(x in t for x in ["música", "musica", "music"]):
            return "music"

    text = (filename + " " + caption).lower()
    if any(x in text for x in [".mp3", ".flac", ".m4a", "music", "ost"]) or "audio/" in mime:
        return "music"
    if re.search(r"(s\d{1,2}e\d{1,2}|season|temporada|[-–]\s*\d{1,3})", text):
        return "series"
    if any(x in text for x in [".mkv", ".mp4", "1080p", "720p", "bluray", "bd"]):
        return "movies"
    return "other"


async def process_message(msg):
    if not msg.media:
        return None

    filename = "Sin nombre"
    mime = ""
    size = 0
    caption = msg.message or ""

    if isinstance(msg.media, MessageMediaDocument):
        doc = msg.media.document
        for attr in doc.attributes:
            if hasattr(attr, "file_name") and attr.file_name:
                filename = attr.file_name
                break
        mime = doc.mime_type or ""
        size = doc.size or 0
    elif isinstance(msg.media, MessageMediaPhoto):
        filename = f"photo_{msg.id}.jpg"
        mime = "image/jpeg"

    info = extract_info(filename, caption)
    tipo_detectado = classify(filename, mime, caption, info.get("tipo"))

    thumbnail_b64 = None
    try:
        if msg.photo:
            thumb_bytes = await client.download_media(msg.photo, file=BytesIO())
            thumbnail_b64 = base64.b64encode(thumb_bytes.getvalue()).decode()
        elif isinstance(msg.media, MessageMediaDocument) and msg.media.document.thumbs:
            thumb_bytes = await client.download_media(msg.media, thumb=-1, file=BytesIO())
            thumbnail_b64 = base64.b64encode(thumb_bytes.getvalue()).decode()
    except Exception:
        pass

    item = {
        "id": msg.id,
        "filename": filename,
        "title": info["title"],
        "secuela": info["secuela"],
        "precuela": info["precuela"],
        "tipo": info["tipo"],
        "episodios": info["episodios"],
        "estudio": info["estudio"],
        "year": info["year"],
        "genero": info["genero"],
        "resolucion": info["resolucion"],
        "quality": info["quality"],
        "duracion": info["duracion"],
        "audios": info["audios"],
        "subtitulos": info["subtitulos"],
        "fansub": info["fansub"],
        "formato": info["formato"],
        "peso": info["peso"],
        "sinopsis": info["sinopsis"],
        "season": info["season"],
        "episode": info["episode"],
        "is_ova": info["is_ova"],
        "special_type": info["special_type"],
        "size_mb": round(size / (1024 * 1024), 2) if size else 0,
        "mime": mime,
        "caption": caption[:600],
        "date": msg.date.strftime("%Y-%m-%d") if msg.date else "",
        "link": f"https://t.me/c/{str(CHANNEL_ID)[4:]}/{msg.id}",
        "thumbnail": f"data:image/jpeg;base64,{thumbnail_b64}" if thumbnail_b64 else None,
        "is_photo": isinstance(msg.media, MessageMediaPhoto)
    }

    catalog[tipo_detectado].append(item)
    return item


async def reindex():
    global catalog
    catalog = {"movies": [], "series": [], "music": [], "other": []}
    print("Iniciando re-indexación completa...")

    try:
        entity = await client.get_entity(CHANNEL_ID)
        print(f"Canal encontrado: {entity.title}")

        count = 0
        async for msg in client.iter_messages(entity, limit=None):
            await process_message(msg)
            count += 1

        for key in catalog:
            catalog[key].sort(key=lambda x: x["date"], reverse=True)

        print(f"✅ Re-indexación completa: {count} archivos")

    except Exception as e:
        print(f"❌ Error: {e}")
        raise


def get_grouped_counts():
    counts = {"movies": 0, "series": 0, "music": 0, "other": 0}
    for cat_name, items in catalog.items():
        titles = set(normalize_title(item["title"]) for item in items)
        counts[cat_name] = len(titles)
    return counts


def get_all_genres() -> list:
    genres: Set[str] = set()
    for items in catalog.values():
        for item in items:
            if item.get("genero"):
                for g in item["genero"].split(","):
                    g = g.strip()
                    if g:
                        genres.add(g)
    return sorted(list(genres))


@app.on_event("startup")
async def startup_event():
    await client.start(bot_token=BOT_TOKEN)
    await reindex()

    @client.on(events.NewMessage(chats=CHANNEL_ID))
    async def handler(event):
        await process_message(event.message)


@app.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    category: str = "all",
    q: str = "",
    exact: str = "",
    page: int = 1,
    username: str = Depends(verify_credentials)
):
    if category == "all":
        raw_items = []
        for cat in catalog.values():
            raw_items.extend(cat)
    else:
        raw_items = catalog.get(category, [])

    # Agrupar primero
    grouped = {}
    for item in raw_items:
        key = normalize_title(item["title"])
        if key not in grouped:
            grouped[key] = {
                "title": item["title"],
                "year": item.get("year"),
                "genero": item.get("genero"),
                "sinopsis": item.get("sinopsis"),
                "thumbnail": item.get("thumbnail"),
                "quality": item.get("quality"),
                "estudio": item.get("estudio"),
                "episodios": item.get("episodios"),
                "secuela": item.get("secuela"),
                "precuela": item.get("precuela"),
                "resolucion": item.get("resolucion"),
                "duracion": item.get("duracion"),
                "audios": item.get("audios"),
                "subtitulos": item.get("subtitulos"),
                "fansub": item.get("fansub"),
                "formato": item.get("formato"),
                "peso": item.get("peso"),
                "episodes": []
            }

        g = grouped[key]

        if item.get("title") and len(item["title"]) > len(g["title"] or ""):
            g["title"] = item["title"]

        if item.get("thumbnail") and not g["thumbnail"]:
            g["thumbnail"] = item["thumbnail"]

        for field in ["year", "genero", "sinopsis", "quality", "estudio", "episodios",
                      "secuela", "precuela", "resolucion", "duracion", "audios", "subtitulos", "fansub", "formato", "peso"]:
            if item.get(field) and not g.get(field):
                g[field] = item[field]

        if not item.get("is_photo") and item.get("size_mb", 0) > 0:
            g["episodes"].append(item)

    for g in grouped.values():
        g["episodes"].sort(key=lambda x: (
            0 if x.get("is_ova") else 1,
            x.get("episode") or 999
        ))

    all_series = list(grouped.values())

    # ========== FILTRO ==========
    if q:
        q_lower = q.lower().strip()
        q_clean = re.sub(r"[^\w\s]", "", q_lower)
        q_clean = re.sub(r"\s+", " ", q_clean).strip()

        if exact == "1":
            # ===== MODO ESTRICTO (Precuela / Secuela) =====
            filtered = []
            for series in all_series:
                title = (series["title"] or "").lower().strip()
                title_clean = re.sub(r"[^\w\s]", "", title)
                title_clean = re.sub(r"\s+", " ", title_clean).strip()

                # Solo acepta si el título es casi idéntico
                if (q_lower == title or 
                    q_clean == title_clean or
                    (q_clean in title_clean and len(q_clean) >= len(title_clean) * 0.85) or
                    (title_clean in q_clean and len(title_clean) >= len(q_clean) * 0.85)):
                    filtered.append(series)
            all_series = filtered
        else:
            # ===== MODO NORMAL (parcial permitido) =====
            filtered = []
            for series in all_series:
                if (q_lower in (series["title"] or "").lower() or
                    q_lower in (series.get("genero") or "").lower() or
                    q_lower in (series.get("secuela") or "").lower() or
                    q_lower in (series.get("precuela") or "").lower() or
                    q_lower in (series.get("sinopsis") or "").lower()):
                    filtered.append(series)
            all_series = filtered

    total_series = len(all_series)

    start = (page - 1) * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    items = all_series[start:end]
    total_pages = max(1, (total_series + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "items": items,
            "total": sum(len(v) for v in catalog.values()),
            "total_series": total_series,
            "counts": get_grouped_counts(),
            "category": category,
            "q": q,
            "page": page,
            "total_pages": total_pages,
            "all_genres": get_all_genres()
        }
    )


@app.get("/health")
async def health():
    return {"status": "ok", "total_files": sum(len(v) for v in catalog.values())}


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
