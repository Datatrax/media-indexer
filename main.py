import os
import re
import secrets
import base64
import zipfile
from io import BytesIO
from typing import Dict, Any, List, Optional
from collections import defaultdict
from urllib.parse import urlencode

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto

from fastapi import FastAPI, Request, Depends, HTTPException, status, Query
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
STREAM_CHUNK = 512 * 1024  # múltiplo de 4096 y divisor de 1 MB (requisito de Telegram)

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
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def clean_text(text: str) -> str:
    if not text:
        return ""
    return re.sub(r'\s+', ' ', str(text)).strip()


FIELD_MAP = {
    "tipo": "tipo", "type": "tipo",
    "artista": "artista", "artist": "artista", "banda": "artista", "band": "artista",
    "intérprete": "artista", "interprete": "artista",
    "álbum": "album", "album": "album", "disco": "album", "nombre del álbum": "album", "nombre del album": "album",
    "nombre": "nombre", "title": "nombre", "título": "nombre", "titulo": "nombre",
    "año": "year", "ano": "year", "year": "year", "fecha": "year", "lanzamiento": "year",
    "release": "year", "fecha de lanzamiento": "year", "año de lanzamiento": "year", "desde": "year",
    "género": "genero", "genero": "genero", "genre": "genero", "géneros": "genero", "generos": "genero",
    "genres": "genero", "estilo": "genero",
    "estudio": "estudio", "studio": "estudio",
    "sello": "label", "label": "label", "discográfica": "label", "discografica": "label", "disquera": "label",
    "productor": "productor", "producer": "productor",
    "precuela": "precuela", "prequel": "precuela",
    "secuela": "secuela", "sequel": "secuela",
}
BIO_KEYS = {"sinopsis", "descripción", "descripcion", "biografía", "biografia", "bio", "biography",
            "about", "reseña", "resena", "description"}


def norm_key(k: str) -> str:
    k = re.sub(r"[*_`~]", "", k.strip().lower())
    k = re.sub(r"^[^\w]+", "", k)   # emojis, viñetas, ">" de las citas de Telegram
    return re.sub(r"[^\w]+$", "", k)


def parse_caption(caption: str) -> Dict[str, str]:
    info: Dict[str, str] = {}
    if not caption:
        return info

    bio: List[str] = []
    free: List[str] = []
    capturing = False

    for raw in caption.replace("\r", "").split("\n"):
        line = re.sub(r"[*_`~]", "", raw).strip()
        if not line:
            continue

        key = None
        val = ""
        if ":" in line:
            k, v = line.split(":", 1)
            k = norm_key(k)
            if len(k) < 30 and (k in FIELD_MAP or k in BIO_KEYS):
                key, val = k, v.strip()

        if key:
            if key in BIO_KEYS:
                bio = [val] if val else []
                capturing = True
            else:
                capturing = False
                field = FIELD_MAP[key]
                if field == "year":
                    m = re.search(r"(?:19|20)\d{2}", val)
                    val = m.group(0) if m else val
                if val and field not in info:
                    info[field] = val.lower() if field == "tipo" else val
            continue

        text = re.sub(r"^[>\s]+", "", line)
        if capturing:
            bio.append(text)
        elif len(text) > 30:
            free.append(text)

    text = "\n".join(bio) if bio else "\n".join(free)
    if text:
        info["sinopsis"] = text

    if "year" not in info:
        m = re.search(r"\b(19|20)\d{2}\b", caption)
        if m:
            info["year"] = m.group(0)

    return info


def classify(filename: str, mime: str, size: int, caption: str, parsed: Dict) -> str:
    fname = (filename or "").lower()
    cap = (caption or "").lower()
    tipo = parsed.get('tipo', '')

    if any(x in tipo for x in ['película', 'pelicula', 'movie']):
        return 'movies'
    if any(x in tipo for x in ['serie', 'series', 'anime']):
        return 'series'
    if any(x in tipo for x in ['música', 'musica', 'music', 'álbum', 'album', 'artista', 'artist']):
        return 'music'

    if any(fname.endswith(ext) for ext in ['.mp3', '.flac', '.m4a', '.wav', '.ogg']) or (mime and mime.startswith('audio/')):
        return 'music'

    if mime.startswith('image/') and (parsed.get('album') or parsed.get('nombre') or parsed.get('artista')):
        return 'music'

    if re.search(r's\d{1,2}e\d{1,3}|temporada\s*\d+|season\s*\d+', fname + " " + cap, re.I):
        return 'series'

    if any(x in fname for x in ['.mkv', '.mp4', '.avi', '.rar']):
        return 'movies'
    return 'other'


def extract_info(msg) -> Optional[Dict[str, Any]]:
    if not msg.media:
        return None

    filename = ""
    mime = ""
    size = 0
    duration = 0
    is_photo = False

    if isinstance(msg.media, MessageMediaDocument):
        doc = msg.media.document
        for attr in doc.attributes:
            if hasattr(attr, 'file_name') and attr.file_name:
                filename = attr.file_name
            if hasattr(attr, 'duration'):
                duration = getattr(attr, 'duration', 0) or 0
        mime = getattr(doc, 'mime_type', '') or ""
        size = getattr(doc, 'size', 0) or 0
    elif isinstance(msg.media, MessageMediaPhoto):
        filename = f"photo_{msg.id}.jpg"
        mime = "image/jpeg"
        is_photo = True
    else:
        return None

    caption = msg.message or ""
    parsed = parse_caption(caption)
    category = classify(filename, mime, size, caption, parsed)

    is_artist = "artista" in (parsed.get("tipo") or "")
    nombre = parsed.pop("nombre", None)

    title = re.sub(r'\.[a-z0-9]{2,5}$', '', filename, flags=re.I)
    title = clean_text(title)

    if category == "music":
        if is_artist:
            if nombre and not parsed.get("artista"):
                parsed["artista"] = nombre
        elif is_photo:
            if nombre and not parsed.get("album"):
                parsed["album"] = nombre
        else:
            # pista de audio: "01 - Artista - Álbum - 02 - Canción", "Artista - Canción", "01. Canción"...
            parts = [p.strip() for p in re.split(r'\s+[–-]\s+', title) if p.strip()]
            parts = [re.sub(r'^\d{1,3}\s*[.)_-]\s*', '', p).strip() or p for p in parts]
            parts = [p for p in parts if not re.fullmatch(r'\d{1,3}', p)]
            if len(parts) >= 3:
                parsed.setdefault('artista', parts[0])
                parsed.setdefault('album', parts[1])
                title = parts[-1]
            elif len(parts) == 2:
                parsed.setdefault('artista', parts[0])
                title = parts[1]
            elif parts:
                title = parts[0]
            if nombre:
                title = nombre
    else:
        if nombre and not parsed.get("album"):
            parsed["album"] = nombre   # las series/películas se agrupan por "Nombre"

    info = {
        "id": msg.id,
        "title": title,
        "filename": filename,
        "category": category,
        "size_mb": round(size / (1024 * 1024), 2) if size else 0,
        "mime": mime,
        "date": msg.date.isoformat() if msg.date else "",
        "link": f"https://t.me/c/{str(CHANNEL_ID)[4:]}/{msg.id}" if str(CHANNEL_ID).startswith("-100") else "",
        "thumbnail": None,
        "duration": duration,
        "duration_str": f"{duration // 60}:{duration % 60:02d}" if duration else "",
        "caption": caption,
        "is_photo": is_photo,
        "is_artist": is_artist,
        **parsed
    }

    if category == "music":
        return info

    part_match = re.search(r'part[\s\._-]*(\d+)', filename + " " + caption, re.I)
    if part_match and category == "movies":
        info["episode"] = int(part_match.group(1))
        info["title"] = f"Part {info['episode']}"
    else:
        ep_match = re.search(r'(?:s|temporada|season)[\s\._-]*(\d{1,2})[\s\._-]*[ex][\s\._-]*(\d{1,3})', filename + " " + caption, re.I)
        if ep_match:
            info["season"] = int(ep_match.group(1))
            info["episode"] = int(ep_match.group(2))
            info["title"] = f"Cap {info['episode']}"
        else:
            ep_only = re.search(r'(?:ep|cap|capítulo|e)[\s\._-]*(\d{1,3})', filename + " " + caption, re.I)
            if ep_only:
                info["episode"] = int(ep_only.group(1))
                info["title"] = f"Cap {info['episode']}"
            else:
                num_match = re.search(r'[\s\._-]+(\d{1,2})(?:[\s\._-]|$)', filename, re.I)
                if num_match and category in ["series", "movies"]:
                    info["episode"] = int(num_match.group(1))
                    info["title"] = f"Cap {info['episode']}"

    ova_match = re.search(r'(\d{1,2})\s*ova|ova[\s\._-]*(\d{1,2})', filename + " " + caption, re.I)
    if ova_match:
        info["is_ova"] = True
        num = ova_match.group(1) or ova_match.group(2)
        info["episode"] = int(num) if num else None
        info["title"] = f"OVA {num}" if num else "OVA"
    elif re.search(r'\b(ova|ona|special)\b', filename + " " + caption, re.I):
        info["is_ova"] = True
        info["title"] = "OVA"

    return info


def clean_series_title(raw: str) -> str:
    if not raw:
        return ""
    t = re.sub(r'^\d{1,3}\s*', '', raw)
    t = re.sub(r'\s*\[.*?\]', '', t)
    t = re.sub(r'\s*\(.*?\)', '', t)
    t = re.sub(r'\s*[-–]?\s*(s\d{1,2}e\d{1,3}|temporada\s*\d+|season\s*\d+|ep\.?\s*\d+).*$', '', t, flags=re.I)
    t = clean_text(t)
    return t.strip(" -–_()[]")


def normalize_title(title: str) -> str:
    if not title:
        return ""
    t = title.lower().strip()
    t = clean_series_title(t)
    t = re.sub(r'[^\w\s]', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def slug(text: str) -> str:
    t = (text or "").lower()
    t = re.sub(r"[\(\[\{].*?[\)\]\}]", "", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def same_artist(a: str, b: str) -> bool:
    if not a or not b:
        return True
    na, nb = slug(a), slug(b)
    return na == nb or na in nb or nb in na


MUSIC_INHERIT = ["artista", "year", "genero", "label", "productor", "sinopsis"]


def link_music() -> None:
    """Enlaza pistas sueltas con su álbum: usa la portada (foto con Álbum/Artista)
    publicada justo antes y, si no hay, el único álbum de ese artista."""
    items = [i for cat in catalog.values() for i in cat]
    ordered = sorted(items, key=lambda x: x["id"])
    covers = [i for i in ordered if i.get("is_photo") and i["category"] == "music"
              and not i.get("is_artist") and i.get("album")]

    def inherit(track, cover):
        track["album"] = cover["album"]
        track["_cover"] = cover["id"]
        for f in MUSIC_INHERIT:
            if not track.get(f) and cover.get(f):
                track[f] = cover[f]

    ctx = None
    for it in ordered:
        if it.get("is_photo"):
            ctx = it if (it["category"] == "music" and not it.get("is_artist") and it.get("album")) else None
            continue
        if it["category"] != "music":
            ctx = None
            continue
        if it.get("album"):
            continue
        if ctx and same_artist(it.get("artista"), ctx.get("artista")):
            inherit(it, ctx)

    for it in ordered:
        if it.get("is_photo") or it["category"] != "music" or it.get("album") or not it.get("artista"):
            continue
        matches = {slug(c["album"]): c for c in covers
                   if c.get("artista") and same_artist(it["artista"], c["artista"])}
        if len(matches) == 1:
            inherit(it, next(iter(matches.values())))

    # portada sin artista -> toma el de sus pistas
    for c in covers:
        if not c.get("artista"):
            for it in ordered:
                if it.get("_cover") == c["id"] and it.get("artista"):
                    c["artista"] = it["artista"]
                    break


def build_groups():
    link_music()
    groups = defaultdict(lambda: {
        "title": "",
        "normalized": "",
        "final_category": "",
        "episodes": [],
        "seasons": defaultdict(list),
        "ovas": [],
        "parts": [],
        "playlist": [],
        "year": None,
        "genero": None,
        "estudio": None,
        "artista": None,
        "album": None,
        "label": None,
        "productor": None,
        "precuela": None,
        "secuela": None,
        "sinopsis": None,
        "thumbnail": None,
        "latest_date": "",
        "total_size": 0,
        "is_artist": False,
    })

    for cat in ["movies", "series", "music", "other"]:
        for item in catalog[cat]:
            if item.get("is_artist"):
                artist = clean_text(item.get("artista") or "")
                if not artist:
                    continue
                key = f"artist::{slug(artist)}"
                display = artist
            elif cat == "music":
                album = clean_text(item.get("album") or "")
                artist = clean_text(item.get("artista") or "")
                if album:
                    key = f"music::{slug(album)}"
                    display = album
                elif item.get("is_photo"):
                    continue
                elif artist:
                    key = f"music::{slug(artist)}::sencillos"
                    display = "Sencillos"
                else:
                    key = f"music::{slug(item.get('title', ''))}"
                    display = item.get("title")
            else:
                nombre = clean_text(item.get("album") or "")
                if nombre:
                    key = normalize_title(nombre)
                    display = nombre
                else:
                    key = f"single::{item['id']}"
                    display = item.get("title") or item.get("filename")

            g = groups[key]
            g["normalized"] = key

            if item.get("is_artist"):
                g["is_artist"] = True
                g["title"] = display
                for field in ["year", "genero", "sinopsis", "thumbnail"]:
                    if item.get(field):
                        g[field] = item[field]
                continue

            if not item.get("is_photo"):
                g["episodes"].append(item)
                g["total_size"] += item.get("size_mb", 0)

            if not g["title"]:
                g["title"] = display or item.get("title")

            for field in ["year", "genero", "estudio", "artista", "album", "label", "productor", "precuela", "secuela", "sinopsis"]:
                if item.get(field):
                    if item.get("is_photo") or not g.get(field):
                        g[field] = item[field]

            if item.get("thumbnail"):
                if item.get("is_photo") or not g["thumbnail"]:
                    g["thumbnail"] = item["thumbnail"]

            if item.get("date") and item["date"] > (g["latest_date"] or ""):
                g["latest_date"] = item["date"]

            g["final_category"] = cat if cat in ["music", "movies", "series"] else "other"

            if cat in ["series", "movies"] and not item.get("is_photo"):
                if item.get("is_ova"):
                    g["ovas"].append(item)
                elif item.get("season") is not None:
                    g["seasons"][item["season"]].append(item)
                else:
                    g["parts"].append(item)

    result = []
    for g in groups.values():
        if g.get("is_artist"):
            result.append(g)
            continue
        if not g["episodes"]:
            continue
        if g["final_category"] == "music":
            g["episodes"].sort(key=lambda x: x.get("id", 0))
            # lista compacta para el reproductor (sin miniaturas base64)
            g["playlist"] = [{"id": e["id"], "title": e["title"],
                              "artist": e.get("artista") or g.get("artista") or ""} for e in g["episodes"]]
        else:
            sorted_seasons = dict(sorted(g["seasons"].items()))
            g["seasons"] = sorted_seasons
            for sn in g["seasons"]:
                g["seasons"][sn].sort(key=lambda x: x.get("episode") or 0)
            g["ovas"].sort(key=lambda x: x.get("episode") or 0)
            g["parts"].sort(key=lambda x: x.get("episode") or 0)
            g["episodes"].sort(key=lambda x: (x.get("season") or 0, x.get("episode") or 0, x.get("id", 0)))
        result.append(g)

    return result


def add_synthetic_artists(groups: List[Dict]) -> List[Dict]:
    """Artistas que tienen álbumes pero no tienen mensaje de perfil: se crean al vuelo."""
    existing = {slug(g["title"]) for g in groups if g.get("is_artist")}
    extra: Dict[str, Dict] = {}
    for g in groups:
        if g.get("is_artist") or g["final_category"] != "music":
            continue
        name = clean_text(g.get("artista") or "")
        if not name or slug(name) in existing:
            continue
        e = extra.setdefault(slug(name), {
            "title": name, "is_artist": True, "synthetic": True, "final_category": "",
            "thumbnail": g.get("thumbnail"), "year": None, "genero": g.get("genero"),
            "sinopsis": None, "latest_date": g.get("latest_date") or "", "total_size": 0,
            "episodes": [], "playlist": [],
        })
        if not e["thumbnail"] and g.get("thumbnail"):
            e["thumbnail"] = g["thumbnail"]
        if (g.get("latest_date") or "") > e["latest_date"]:
            e["latest_date"] = g["latest_date"]
    return groups + list(extra.values())


def get_filters_from_groups(groups: List[Dict], section: str):
    years, genres, studios, artists, labels = set(), set(), set(), set(), set()
    for g in groups:
        if section == "music":
            if g.get("is_artist"):
                continue
            if g["final_category"] != "music":
                continue
        elif section == "movies" and g["final_category"] != "movies":
            continue
        elif section == "series" and g["final_category"] != "series":
            continue

        if g.get("year"):
            years.add(str(g["year"]))
        if g.get("genero"):
            for gen in re.split(r'[,;/]', g["genero"]):
                if gen.strip():
                    genres.add(gen.strip())
        if g.get("estudio"):
            studios.add(g["estudio"])
        if g.get("artista"):
            artists.add(g["artista"])
        if g.get("label"):
            labels.add(g["label"])
    return {
        "years": sorted(years, reverse=True),
        "genres": sorted(genres),
        "studios": sorted(studios),
        "artists": sorted(artists),
        "labels": sorted(labels),
    }


async def process_message(msg):
    info = extract_info(msg)
    if not info:
        return
    try:
        if msg.photo or (hasattr(msg.media, 'document') and getattr(msg.media.document, 'thumbs', None)):
            thumb_bytes = await client.download_media(msg, file=bytes, thumb=-1)
            if thumb_bytes:
                info["thumbnail"] = "data:image/jpeg;base64," + base64.b64encode(thumb_bytes).decode()
    except Exception:
        pass
    cat = info["category"]
    catalog[cat if cat in catalog else "other"].append(info)


async def reindex():
    global catalog
    catalog = {"movies": [], "series": [], "music": [], "other": []}
    print("Iniciando re-indexación completa...")
    try:
        entity = await client.get_entity(CHANNEL_ID)
        print(f"Canal encontrado: {getattr(entity, 'title', CHANNEL_ID)}")
        count = 0
        async for msg in client.iter_messages(entity, limit=None):
            await process_message(msg)
            count += 1
        print(f"✅ Re-indexación completa: {count} archivos")
    except Exception as e:
        print(f"❌ Error: {e}")


@app.on_event("startup")
async def startup_event():
    await client.start()
    await reindex()

    @client.on(events.NewMessage(chats=CHANNEL_ID))
    async def handler(event):
        await process_message(event.message)


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    section: str = "series",
    q: str = "",
    year: str = "",
    genero: str = "",
    estudio: str = "",
    artista: str = "",
    album: str = "",
    sort: str = "title",
    page: int = 1,
    view: str = "artists",
    username: str = Depends(verify_credentials)
):
    groups = add_synthetic_artists(build_groups())
    music_albums = [g for g in groups if g["final_category"] == "music" and not g.get("is_artist")]
    artist_profile = None

    if section == "music" and album:
        items = [g for g in music_albums if slug(g.get("title")) == slug(album)]
        if not items:
            items = [g for g in music_albums if album.lower() in (g.get("title") or "").lower()]
        if artista:
            narrowed = [g for g in items if same_artist(g.get("artista"), artista)]
            items = narrowed or items
    elif section == "music" and view == "artists" and not artista:
        items = [g for g in groups if g.get("is_artist")]
    elif section == "music":
        items = music_albums
        if artista:
            exact = [g for g in items if slug(g.get("artista")) == slug(artista)]
            items = exact or [g for g in items if artista.lower() in (g.get("artista") or "").lower()]
            artist_profile = next((g for g in groups if g.get("is_artist") and slug(g["title"]) == slug(artista)), None)
    elif section == "movies":
        items = [g for g in groups if g["final_category"] == "movies"]
    else:
        items = [g for g in groups if g["final_category"] == "series"]

    if q and not album:
        ql = q.lower()
        items = [g for g in items if ql in (g.get("title") or "").lower() or ql in (g.get("artista") or "").lower() or ql in (g.get("album") or "").lower()]

    if year:
        items = [g for g in items if str(g.get("year")) == year]
    if genero:
        items = [g for g in items if genero.lower() in (g.get("genero") or "").lower()]
    if estudio:
        items = [g for g in items if estudio.lower() in (g.get("estudio") or g.get("label") or "").lower()]

    if sort == "year":
        items.sort(key=lambda x: int(x["year"]) if x.get("year") and str(x["year"]).isdigit() else 0, reverse=True)
    elif sort == "date":
        items.sort(key=lambda x: x.get("latest_date") or "", reverse=True)
    elif sort == "size":
        items.sort(key=lambda x: x.get("total_size", 0), reverse=True)
    else:
        items.sort(key=lambda x: (x.get("title") or "").lower())

    filters = get_filters_from_groups(groups, section)
    total = len(items)

    # En la vista de artista / álbum se muestra todo junto (sin paginar)
    paginate = not (section == "music" and (artista or album))
    page = max(1, page)
    if paginate:
        start = (page - 1) * ITEMS_PER_PAGE
        page_items = items[start:start + ITEMS_PER_PAGE]
    else:
        page_items = items
    total_pages = max(1, (total + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE) if paginate else 1

    albums_list, eps_list = [], []
    if section == "music" and artista and not album:
        albums_list = [g for g in page_items if len(g["episodes"]) > 3]
        eps_list = [g for g in page_items if len(g["episodes"]) <= 3]

    counts = {
        "series": sum(1 for g in groups if g["final_category"] == "series"),
        "movies": sum(1 for g in groups if g["final_category"] == "movies"),
        "music": len(music_albums),
        "artists": sum(1 for g in groups if g.get("is_artist")),
    }

    qs = urlencode({k: v for k, v in {
        "section": section, "view": view, "q": q, "sort": sort, "year": year,
        "genero": genero, "estudio": estudio, "artista": artista}.items() if v})

    return templates.TemplateResponse("index.html", {
        "request": request,
        "items": page_items,
        "albums_list": albums_list,
        "eps_list": eps_list,
        "artist_profile": artist_profile,
        "total": sum(len(v) for v in catalog.values()),
        "counts": counts,
        "section": section,
        "q": q,
        "page": page,
        "total_pages": total_pages,
        "qs": qs,
        "all_years": filters["years"],
        "all_genres": filters["genres"],
        "all_studios": filters["labels"] if section == "music" else filters["studios"],
        "all_artists": filters["artists"],
        "sort": sort,
        "year": year,
        "genero": genero,
        "estudio": estudio,
        "artista": artista,
        "album": album,
        "view": view,
    })


def parse_range(header: str, size: int):
    m = re.match(r"bytes=(\d*)-(\d*)\s*$", header or "")
    if not m or (m.group(1) == "" and m.group(2) == ""):
        return None
    s_, e_ = m.groups()
    if s_ == "":
        start = max(0, size - int(e_))
        end = size - 1
    else:
        start = int(s_)
        end = int(e_) if e_ else size - 1
    end = min(end, size - 1)
    if start >= size or start > end:
        raise HTTPException(status_code=416, detail="Rango inválido",
                            headers={"Content-Range": f"bytes */{size}"})
    return start, end


@app.get("/stream/{msg_id}")
async def stream_file(msg_id: int, request: Request, username: str = Depends(verify_credentials)):
    msg = await client.get_messages(CHANNEL_ID, ids=msg_id)
    if not msg or not getattr(msg, "document", None):
        raise HTTPException(status_code=404, detail="Archivo no encontrado")

    doc = msg.document
    size = doc.size or 0
    mime = doc.mime_type or "application/octet-stream"
    filename = "file"
    for attr in doc.attributes:
        if hasattr(attr, "file_name") and attr.file_name:
            filename = attr.file_name
            break

    start, end, code = 0, max(size - 1, 0), 200
    rng = parse_range(request.headers.get("range"), size) if size else None
    if rng:
        start, end = rng
        code = 206
    length = end - start + 1 if size else 0

    async def body():
        remaining = length
        aligned = start - (start % STREAM_CHUNK)
        skip = start - aligned
        async for chunk in client.iter_download(msg.media, offset=aligned, request_size=STREAM_CHUNK):
            if skip:
                chunk = chunk[skip:]
                skip = 0
            if len(chunk) > remaining:
                chunk = chunk[:remaining]
            remaining -= len(chunk)
            if chunk:
                yield chunk
            if remaining <= 0:
                break

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Content-Disposition": f'inline; filename="{filename}"',
    }
    if code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(body(), status_code=code, media_type=mime, headers=headers)


@app.get("/download-album")
async def download_album(ids: str = Query(...), name: str = Query("Album"), username: str = Depends(verify_credentials)):
    try:
        id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()]
        buffer = BytesIO()
        safe_name = re.sub(r'[<>:"/\\|?*]', '', name).strip() or "Album"
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for mid in id_list:
                msg = await client.get_messages(CHANNEL_ID, ids=mid)
                if not msg or not msg.media:
                    continue
                if isinstance(msg.media, MessageMediaPhoto):
                    continue
                if not hasattr(msg.media, 'document'):
                    continue
                filename = f"{mid}.bin"
                for attr in getattr(msg.media.document, 'attributes', []):
                    if hasattr(attr, 'file_name') and attr.file_name:
                        filename = attr.file_name
                        break
                data = await client.download_media(msg, file=bytes)
                if data:
                    zf.writestr(f"{safe_name}/{filename}", data)
        buffer.seek(0)
        return StreamingResponse(buffer, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{safe_name}.zip"'})
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/download-season")
async def download_season(ids: str = Query(...), name: str = Query("Temporada"), username: str = Depends(verify_credentials)):
    try:
        id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()]
        buffer = BytesIO()
        safe_name = re.sub(r'[<>:"/\\|?*]', '', name).strip() or "Temporada"
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for mid in id_list:
                msg = await client.get_messages(CHANNEL_ID, ids=mid)
                if not msg or not msg.media:
                    continue
                if isinstance(msg.media, MessageMediaPhoto):
                    continue
                if not hasattr(msg.media, 'document'):
                    continue
                filename = f"{mid}.bin"
                for attr in getattr(msg.media.document, 'attributes', []):
                    if hasattr(attr, 'file_name') and attr.file_name:
                        filename = attr.file_name
                        break
                data = await client.download_media(msg, file=bytes)
                if data:
                    zf.writestr(f"{safe_name}/{filename}", data)
        buffer.seek(0)
        return StreamingResponse(buffer, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{safe_name}.zip"'})
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/health")
async def health():
    return {"status": "ok", "total_files": sum(len(v) for v in catalog.values())}


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
