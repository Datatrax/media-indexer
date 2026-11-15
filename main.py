import os
import re
import secrets
import base64
import zipfile
import io
from io import BytesIO
from typing import Dict, Any, List
from collections import defaultdict

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto, DocumentAttributeAudio

from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import uvicorn
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
SESSION_STRING = os.getenv("SESSION_STRING")
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "cambia_esta_password")
ITEMS_PER_PAGE = 12

app = FastAPI(title="Biblioteca de Prozac")
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
    if "tokeijikake" in t or "angeloid of clockwork" in t:
        return "sora no otoshimono tokeijikake movie"
    if "final" in t and ("eternal" in t or "my master" in t):
        return "sora no otoshimono final eternal my master"
    if "forte" in t:
        return "sora no otoshimono forte"
    if "ribbon hero" in t or "heroína del moño" in t or "heroina del mono" in t:
        return "the ribbon hero"
    t = re.sub(r"[\(\[\{].*?[\)\]\}]", "", t)
    if "," in t:
        t = t.split(",")[0]
    if ":" in t:
        parts = t.split(":", 1)
        if len(parts[1].strip()) > 10:
            t = parts[0]
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s+(part|parte|cd|disc|pt|ova|special|sp|especial)\s*\d*.*$", "", t)
    t = re.sub(r"\s+(the movie|movie|edit|final|complete|uncensored|bluray|bd).*$", "", t)
    return t.strip()


def parse_caption(caption: str) -> Dict[str, Any]:
    data = {
        "nombre": None, "secuela": None, "precuela": None, "tipo": None, "episodios": None,
        "estudio": None, "año": None, "genero": None, "resolucion": None,
        "calidad": None, "duracion": None, "audios": None, "subtitulos": None,
        "fansub": None, "formato": None, "peso": None, "sinopsis": None,
        "artista": None, "album": None, "label": None, "sello": None, "productor": None
    }
    if not caption:
        return data

    clean_caption = re.sub(r"[*_`~]", "", caption)

    sinopsis_match = re.search(r"(?:Sinopsis|Descripción|Description):\s*(.+)", clean_caption, re.IGNORECASE | re.DOTALL)
    if sinopsis_match:
        data["sinopsis"] = sinopsis_match.group(1).strip()
    else:
        lines = [l.strip() for l in clean_caption.split("\n") if l.strip()]
        long_text = " ".join([l for l in lines if len(l) > 60])
        if long_text:
            data["sinopsis"] = long_text

    for line in clean_caption.split("\n"):
        line = line.strip()
        if ":" in line:
            key, value = line.split(":", 1)
            key = key.strip().lower()
            value = value.strip()
            mapping = {
                "nombre": "nombre", "title": "nombre", "pista": "nombre", "track": "nombre",
                "canción": "nombre", "cancion": "nombre",
                "secuela": "secuela", "sequel": "secuela",
                "precuela": "precuela", "prequel": "precuela",
                "tipo": "tipo", "type": "tipo",
                "episodios": "episodios", "episodes": "episodios", "eps": "episodios",
                "estudio": "estudio", "studio": "estudio",
                "año": "año", "year": "año", "ano": "año", "fecha": "año",
                "género": "genero", "genero": "genero", "genre": "genero", "generos": "genero",
                "resolución": "resolucion", "resolucion": "resolucion", "resolution": "resolucion",
                "calidad": "calidad", "quality": "calidad",
                "duración": "duracion", "duracion": "duracion", "duration": "duracion",
                "audios": "audios", "audio": "audios",
                "subtítulos": "subtitulos", "subtitulos": "subtitulos", "subtitles": "subtitulos", "subs": "subtitulos",
                "fansub": "fansub", "sub": "fansub",
                "formato": "formato", "format": "formato",
                "peso": "peso", "size": "peso",
                "artista": "artista", "artist": "artista", "intérprete": "artista", "interprete": "artista",
                "álbum": "album", "album": "album",
                "label": "label", "sello": "sello", "sello discográfico": "sello", "sello discografico": "sello",
                "productor": "productor", "producer": "productor"
            }
            if key in mapping:
                data[mapping[key]] = value
    return data


def extract_info(filename: str, caption: str = "") -> Dict[str, Any]:
    parsed = parse_caption(caption)
    fname_lower = filename.lower()
    is_music_file = any(ext in fname_lower for ext in [".mp3", ".flac", ".m4a", ".wav", ".aac", ".ogg"])

    if not parsed["nombre"]:
        clean = re.sub(r"\.(mkv|mp4|avi|mov|rar|zip|mp3|flac|m4a|wav|aac|ogg)$", "", filename, flags=re.I)
        clean = re.sub(r"[\._]", " ", clean)
        clean = re.sub(r"\[.*?\]", "", clean).strip()

        if is_music_file:
            if " - " in clean or " – " in clean:
                parts = re.split(r"\s*[-–]\s*", clean, maxsplit=1)
                if len(parts) == 2:
                    if not parsed.get("artista"):
                        parsed["artista"] = parts[0].strip()
                    parsed["nombre"] = parts[1].strip()
                else:
                    parsed["nombre"] = clean
            else:
                parsed["nombre"] = clean
            parsed["nombre"] = re.sub(r"[\(\)]", "", parsed["nombre"]).strip()
        else:
            clean = re.sub(r"(?i)\s*[-–.]?\s*s\d{1,2}e\d{1,3}.*$", "", clean)
            clean = re.sub(r"(?i)\s*[-–.]?\s*\d{1,2}x\d{1,3}.*$", "", clean)
            clean = re.sub(r"\s*[-–]\s*(ova|special|sp|especial|part|parte|cd|disc)?\s*\d{0,3}.*$", "", clean, flags=re.I)
            parsed["nombre"] = re.sub(r"\s+", " ", clean).strip()

    if "forte" in fname_lower:
        parsed["nombre"] = "Sora no Otoshimono Forte, Heaven’s Lost Property 2"
    elif "tokeijikake" in fname_lower or "angeloid of clockwork" in fname_lower:
        parsed["nombre"] = "Sora no Otoshimono: Tokeijikake no Angeloid, Heaven’s Lost Property the Movie: The Angeloid of Clockwork"
    elif "final" in fname_lower and ("eternal" in fname_lower or "my master" in fname_lower):
        parsed["nombre"] = "Sora no Otoshimono Final: Eternal My Master, Heaven’s Lost Property: My Eternal Master"
    elif "ribbon hero" in fname_lower:
        parsed["nombre"] = "The Ribbon Hero, La Heroína del Moño"

    episode = None
    season = 1
    is_ova = False
    is_part = False
    special_type = None
    fname = filename.lower()

    part_match = re.search(r"(?:part|parte|cd|disc|pt)\s*[-–.]?\s*(\d{1,2})", fname)
    if part_match:
        is_part = True
        special_type = "Part"
        episode = int(part_match.group(1))
    elif re.search(r"\.(part\d+|r\d{2})\.", fname):
        is_part = True
        special_type = "Part"
        num = re.search(r"(?:part|r)(\d+)", fname)
        if num:
            episode = int(num.group(1))
    elif season_ep_match := re.search(r"[sS](\d{1,2})[eE](\d{1,3})", fname):
        season = int(season_ep_match.group(1))
        episode = int(season_ep_match.group(2))
    elif x_match := re.search(r"(\d{1,2})[xX](\d{1,3})", fname):
        season = int(x_match.group(1))
        episode = int(x_match.group(2))
    else:
        if "ova" in fname:
            is_ova = True
            special_type = "OVA"
            match = re.search(r"ova\s*[-–]?\s*(\d{1,3})", fname)
            if match:
                episode = int(match.group(1))
        elif "special" in fname or "especial" in fname or re.search(r"\bsp\b", fname):
            is_ova = True
            special_type = "Special"
            match = re.search(r"(?:special|especial|sp)\s*[-–]?\s*(\d{1,3})", fname)
            if match:
                episode = int(match.group(1))
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
        "artista": parsed.get("artista"),
        "album": parsed.get("album"),
        "label": parsed.get("label") or parsed.get("sello"),
        "productor": parsed.get("productor"),
        "season": season,
        "episode": episode,
        "is_ova": is_ova,
        "is_part": is_part,
        "special_type": special_type
    }


def classify(filename: str, mime: str = "", caption: str = "", tipo: str = None) -> str:
    text = (filename + " " + (caption or "")).lower()
    mime = (mime or "").lower()
    tipo_lower = (tipo or "").lower()

    # === PRIORIDAD MÁXIMA: Tipo del caption ===
    if "película" in tipo_lower or "pelicula" in tipo_lower or "movie" in tipo_lower:
        return "movies"
    if "serie" in tipo_lower or "ova" in tipo_lower or "anime" in tipo_lower:
        return "series"
    if "música" in tipo_lower or "musica" in tipo_lower or "music" in tipo_lower:
        return "music"

    # También buscar en el caption completo
    if re.search(r"tipo\s*:\s*(película|pelicula|movie)", text):
        return "movies"
    if re.search(r"tipo\s*:\s*(serie|ova|anime)", text):
        return "series"

    video_extensions = [".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v", ".webm"]
    audio_extensions = [".mp3", ".flac", ".m4a", ".wav", ".aac", ".ogg", ".opus"]
    archive_extensions = [".rar", ".zip", ".7z"]

    is_video = any(ext in text for ext in video_extensions) or mime.startswith("video/")
    is_audio = any(ext in text for ext in audio_extensions) or mime.startswith("audio/")
    is_archive = any(ext in text for ext in archive_extensions)

    # Casos especiales conocidos
    if "forte" in text and ("sora" in text or "otoshimono" in text):
        return "series"
    if any(x in text for x in ["tokeijikake", "angeloid of clockwork", "final eternal", "eternal my master", "my master"]):
        return "movies"
    if "ribbon hero" in text:
        return "movies"

    if is_audio:
        return "music"

    if is_video or is_archive:
        if any(x in text for x in ["movie", "pelicula", "película", "film", "the movie"]):
            return "movies"
        if ("part" in text or "parte" in text) and not re.search(r"s\d{1,2}e\d{1,2}", text):
            return "movies"
        if re.search(r"s\d{1,2}e\d{1,2}|season|temporada", text):
            return "series"
        return "series"  # por defecto videos/archivos van a series

    return "other"


def format_duration(seconds: int) -> str:
    if not seconds:
        return ""
    m = seconds // 60
    s = seconds % 60
    return f"{m}:{s:02d}"


async def process_message(msg):
    if not msg.media:
        return None

    filename = "Sin nombre"
    mime = ""
    size = 0
    caption = msg.message or ""
    duration_sec = None

    if isinstance(msg.media, MessageMediaDocument):
        doc = msg.media.document
        for attr in doc.attributes:
            if hasattr(attr, "file_name") and attr.file_name:
                filename = attr.file_name
            if isinstance(attr, DocumentAttributeAudio):
                duration_sec = attr.duration
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
        "duration_sec": duration_sec,
        "duration_str": format_duration(duration_sec) if duration_sec else "",
        "audios": info["audios"],
        "subtitulos": info["subtitulos"],
        "fansub": info["fansub"],
        "formato": info["formato"],
        "peso": info["peso"],
        "sinopsis": info["sinopsis"],
        "artista": info.get("artista"),
        "album": info.get("album"),
        "label": info.get("label"),
        "productor": info.get("productor"),
        "season": info["season"],
        "episode": info["episode"],
        "is_ova": info["is_ova"],
        "is_part": info["is_part"],
        "special_type": info["special_type"],
        "size_mb": round(size / (1024 * 1024), 2) if size else 0,
        "mime": mime,
        "caption": caption[:1000],
        "date": msg.date.strftime("%Y-%m-%d") if msg.date else "",
        "link": f"https://t.me/c/{str(CHANNEL_ID)[4:]}/{msg.id}",
        "thumbnail": f"data:image/jpeg;base64,{thumbnail_b64}" if thumbnail_b64 else None,
        "is_photo": isinstance(msg.media, MessageMediaPhoto),
        "original_category": tipo_detectado
    }

    if item["is_photo"] and (info.get("artista") or info.get("album") or info.get("sinopsis") or info.get("label")):
        if any(x in (caption or "").lower() for x in ["artista", "album", "álbum", "label", "sello", "género", "genero", "fecha"]):
            tipo_detectado = "music"
            item["original_category"] = "music"

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
        print(f"   Series: {len(catalog['series'])} | Películas: {len(catalog['movies'])} | Música: {len(catalog['music'])}")
    except Exception as e:
        print(f"❌ Error: {e}")
        raise


def get_all_items() -> List[Dict]:
    items = []
    for cat in catalog.values():
        items.extend(cat)
    return items


def build_groups(all_items: List[Dict]) -> Dict[str, Dict]:
    grouped = {}

    for item in all_items:
        if item.get("original_category") == "music":
            if item.get("album"):
                key = normalize_title(item["album"])
            elif item.get("artista"):
                key = normalize_title(item["artista"])
            else:
                key = normalize_title(item["title"])
        else:
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
                "artista": item.get("artista"),
                "album": item.get("album"),
                "label": item.get("label"),
                "productor": item.get("productor"),
                "episodes": [],
                "latest_date": item.get("date", ""),
                "categories": []
            }

        g = grouped[key]

        if item.get("title") and len(str(item["title"])) > len(str(g["title"] or "")):
            g["title"] = item["title"]

        if item.get("thumbnail"):
            g["thumbnail"] = item["thumbnail"]

        if item.get("date") and item["date"] > (g.get("latest_date") or ""):
            g["latest_date"] = item["date"]

        for field in ["year", "genero", "sinopsis", "quality", "estudio", "episodios",
                      "secuela", "precuela", "resolucion", "duracion", "audios",
                      "subtitulos", "fansub", "formato", "peso", "artista", "album", "label", "productor"]:
            if item.get(field):
                if not g.get(field) or len(str(item[field])) > len(str(g.get(field) or "")):
                    g[field] = item[field]

        g["categories"].append(item.get("original_category", "other"))

        if not item.get("is_photo") and item.get("size_mb", 0) > 0:
            g["episodes"].append(item)

    for key, g in grouped.items():
        if "forte" in key:
            g["title"] = "Sora no Otoshimono Forte, Heaven’s Lost Property 2"
        elif "tokeijikake" in key:
            g["title"] = "Sora no Otoshimono: Tokeijikake no Angeloid, Heaven’s Lost Property the Movie: The Angeloid of Clockwork"
        elif "final eternal" in key or "my master" in key:
            g["title"] = "Sora no Otoshimono Final: Eternal My Master, Heaven’s Lost Property: My Eternal Master"
        elif "ribbon hero" in key:
            g["title"] = "The Ribbon Hero, La Heroína del Moño"

        num_episodes = len(g["episodes"])
        cats = g["categories"]

        music_count = cats.count("music")
        series_count = cats.count("series")
        movies_count = cats.count("movies")

        if music_count > 0 and music_count >= series_count and music_count >= movies_count:
            g["final_category"] = "music"
        elif "tokeijikake" in key or "final eternal" in key or "my master" in key or "ribbon hero" in key:
            g["final_category"] = "movies"
        elif "forte" in key:
            g["final_category"] = "series"
        elif num_episodes >= 4:
            g["final_category"] = "series"
        elif num_episodes <= 3 and any(ep.get("is_part") for ep in g["episodes"]):
            g["final_category"] = "movies"
        elif movies_count > 0:
            g["final_category"] = "movies"
        elif series_count > 0:
            g["final_category"] = "series"
        elif music_count > 0:
            g["final_category"] = "music"
        else:
            g["final_category"] = "other"

        if g.get("final_category") == "music":
            g["episodes"].sort(key=lambda x: x.get("id", 0))
        else:
            g["episodes"].sort(key=lambda x: (
                0 if x.get("is_ova") or x.get("is_part") else 1,
                x.get("season") or 0,
                x.get("episode") or 999
            ))

        seasons = defaultdict(list)
        ovas = []
        parts = []
        for ep in g["episodes"]:
            if ep.get("is_part"):
                parts.append(ep)
            elif ep.get("is_ova"):
                ovas.append(ep)
            else:
                seasons[ep.get("season") or 1].append(ep)

        g["seasons"] = dict(sorted(seasons.items()))
        g["ovas"] = ovas
        g["parts"] = parts

    return grouped


def get_grouped_counts():
    all_items = get_all_items()
    groups = build_groups(all_items)
    counts = {"movies": 0, "series": 0, "music": 0, "other": 0}
    for g in groups.values():
        cat = g.get("final_category", "other")
        if cat in counts:
            counts[cat] += 1
    return counts


def get_music_genres() -> list:
    genres = set()
    for item in catalog.get("music", []):
        if item.get("genero"):
            for g in re.split(r"[,/|]", item["genero"]):
                g = g.strip()
                if g and len(g) > 1 and g.lower() not in ["acción", "accion", "aventura", "aventuras", "comedia", "drama", "ecchi", "fantasía", "fantastico", "harem", "intriga", "romance", "sobrenatural", "ciencia ficción"]:
                    genres.add(g)
    return sorted(genres)


def get_cine_genres() -> list:
    genres = set()
    for cat in ["series", "movies"]:
        for item in catalog.get(cat, []):
            if item.get("genero"):
                for g in re.split(r"[,/|]", item["genero"]):
                    g = g.strip()
                    if g and len(g) > 1:
                        genres.add(g)
    return sorted(genres)


def get_all_artists() -> list:
    artists = set()
    for item in catalog.get("music", []):
        if item.get("artista"):
            artists.add(item["artista"].strip())
    return sorted(artists)


def get_music_years() -> list:
    years = set()
    for item in catalog.get("music", []):
        mime = (item.get("mime") or "").lower()
        filename = (item.get("filename") or "").lower()
        if mime.startswith("audio/") or any(ext in filename for ext in [".mp3", ".flac", ".m4a", ".wav", ".aac", ".ogg"]):
            year = item.get("year")
            if year:
                try:
                    y = int(str(year).strip()[:4])
                    if 1950 <= y <= 2030:
                        years.add(y)
                except:
                    pass
    return sorted(years, reverse=True)


def get_cine_years() -> list:
    years = set()
    for cat in ["series", "movies"]:
        for item in catalog.get(cat, []):
            year = item.get("year")
            if year:
                try:
                    y = int(str(year).strip()[:4])
                    if 1950 <= y <= 2030:
                        years.add(y)
                except:
                    pass
    return sorted(years, reverse=True)


def get_music_labels() -> list:
    labels = set()
    for item in catalog.get("music", []):
        if item.get("label"):
            labels.add(item["label"].strip())
    return sorted(labels)


def get_cine_studios() -> list:
    studios = set()
    for cat in ["series", "movies"]:
        for item in catalog.get(cat, []):
            if item.get("estudio"):
                for s in re.split(r"[,/|]", item["estudio"]):
                    s = s.strip()
                    if s:
                        studios.add(s)
    return sorted(studios)


@app.on_event("startup")
async def startup_event():
    await client.start()
    await reindex()

    @client.on(events.NewMessage(chats=CHANNEL_ID))
    async def handler(event):
        await process_message(event.message)


@app.get("/stream/{msg_id}")
async def stream_audio(msg_id: int, request: Request, username: str = Depends(verify_credentials)):
    try:
        msg = await client.get_messages(CHANNEL_ID, ids=int(msg_id))
        if not msg or not msg.document:
            raise HTTPException(status_code=404, detail="Archivo no encontrado")

        file_size = msg.document.size
        mime = msg.document.mime_type or "audio/mpeg"
        filename = "track.mp3"
        for attr in msg.document.attributes:
            if hasattr(attr, "file_name") and attr.file_name:
                filename = attr.file_name
                break

        range_header = request.headers.get("range")

        if range_header:
            range_match = re.match(r"bytes=(\d+)-(\d*)", range_header)
            if range_match:
                start = int(range_match.group(1))
                end = int(range_match.group(2)) if range_match.group(2) else file_size - 1
                end = min(end, file_size - 1)
                length = end - start + 1

                async def ranged_stream():
                    async for chunk in client.iter_download(msg.media, offset=start, request_size=min(length, 1024*1024)):
                        yield chunk

                headers = {
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(length),
                    "Content-Type": mime,
                    "Content-Disposition": f'inline; filename="{filename}"'
                }
                return StreamingResponse(ranged_stream(), status_code=206, headers=headers, media_type=mime)

        async def full_stream():
            async for chunk in client.iter_download(msg.media):
                yield chunk

        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(file_size),
            "Content-Type": mime,
            "Content-Disposition": f'inline; filename="{filename}"'
        }
        return StreamingResponse(full_stream(), media_type=mime, headers=headers)

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/download-album")
async def download_album(ids: str, username: str = Depends(verify_credentials)):
    try:
        id_list = [int(x.strip()) for x in ids.split(",") if x.strip().isdigit()]
        if not id_list:
            raise HTTPException(status_code=400, detail="No se proporcionaron IDs válidos")
        if len(id_list) > 30:
            raise HTTPException(status_code=400, detail="Máximo 30 pistas por descarga")

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for msg_id in id_list:
                try:
                    msg = await client.get_messages(CHANNEL_ID, ids=msg_id)
                    if not msg or not msg.document:
                        continue
                    filename = f"track_{msg_id}.mp3"
                    for attr in msg.document.attributes:
                        if hasattr(attr, "file_name") and attr.file_name:
                            filename = attr.file_name
                            break
                    file_bytes = await client.download_media(msg.media, file=bytes)
                    zip_file.writestr(filename, file_bytes)
                except Exception as e:
                    print(f"Error descargando {msg_id}: {e}")
                    continue

        zip_buffer.seek(0)
        return StreamingResponse(
            zip_buffer,
            media_type="application/zip",
            headers={"Content-Disposition": "attachment; filename=album.zip"}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    q: str = "",
    page: int = 1,
    sort: str = "title",
    year: str = "",
    genero: str = "",
    estudio: str = "",
    artista: str = "",
    section: str = "series",
    username: str = Depends(verify_credentials)
):
    all_items = get_all_items()
    groups = build_groups(all_items)

    if section == "music":
        all_series = []
        for g in groups.values():
            if g.get("final_category") == "music":
                big_files = [ep for ep in g["episodes"] if ep.get("size_mb", 0) > 80]
                if len(big_files) == 0:
                    all_series.append(g)
    elif section == "movies":
        all_series = [g for g in groups.values() if g.get("final_category") == "movies"]
    else:
        all_series = [g for g in groups.values() if g.get("final_category") == "series"]

    if year:
        all_series = [s for s in all_series if s.get("year") and str(s["year"])[:4] == str(year)]
    if genero:
        genero_lower = genero.lower().strip()
        all_series = [s for s in all_series if s.get("genero") and genero_lower in s["genero"].lower()]
    if estudio:
        estudio_lower = estudio.lower().strip()
        all_series = [s for s in all_series if 
                      (s.get("estudio") and estudio_lower in s["estudio"].lower()) or
                      (s.get("label") and estudio_lower in s["label"].lower())]
    if artista and section == "music":
        artista_lower = artista.lower().strip()
        all_series = [s for s in all_series if s.get("artista") and artista_lower in s["artista"].lower()]

    if q:
        q_lower = q.lower().strip()
        filtered = []
        for series in all_series:
            if (q_lower in (series["title"] or "").lower() or
                q_lower in (series.get("genero") or "").lower() or
                q_lower in (series.get("sinopsis") or "").lower() or
                q_lower in (series.get("estudio") or "").lower() or
                q_lower in (series.get("artista") or "").lower() or
                q_lower in (series.get("album") or "").lower() or
                q_lower in (series.get("label") or "").lower()):
                filtered.append(series)
        all_series = filtered

    if sort == "year":
        all_series.sort(key=lambda x: int(str(x.get("year") or "0")[:4]) if str(x.get("year") or "").isdigit() else 0, reverse=True)
    elif sort == "date":
        all_series.sort(key=lambda x: x.get("latest_date") or "", reverse=True)
    else:
        all_series.sort(key=lambda x: (x.get("title") or "").lower())

    total_series = len(all_series)
    counts = get_grouped_counts()
    start = (page - 1) * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    items = all_series[start:end]
    total_pages = max(1, (total_series + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "items": items,
            "total": sum(len(v) for v in catalog.values()),
            "counts": counts,
            "q": q,
            "page": page,
            "total_pages": total_pages,
            "all_genres": get_music_genres() if section == "music" else get_cine_genres(),
            "all_artists": get_all_artists() if section == "music" else [],
            "all_years": get_music_years() if section == "music" else get_cine_years(),
            "all_studios": get_music_labels() if section == "music" else get_cine_studios(),
            "sort": sort,
            "year": year,
            "genero": genero,
            "estudio": estudio,
            "artista": artista,
            "section": section,
        }
    )


@app.get("/health")
async def health():
    return {"status": "ok", "total_files": sum(len(v) for v in catalog.values())}


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
