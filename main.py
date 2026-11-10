import os
import re
import secrets
import base64
from io import BytesIO
from typing import Dict, Any, Set, List
from collections import Counter, defaultdict

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto

from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, StreamingResponse
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

    # Casos especiales Sora no Otoshimono
    if "tokeijikake" in t or "angeloid of clockwork" in t:
        return "sora no otoshimono tokeijikake movie"
    if "final" in t and ("eternal" in t or "my master" in t):
        return "sora no otoshimono final eternal my master"
    if "forte" in t:
        return "sora no otoshimono forte"

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
        # Nuevos campos para música
        "artista": None, "album": None, "label": None, "sello": None, "productor": None
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
                "nombre": "nombre", "title": "nombre", "pista": "nombre", "track": "nombre", "canción": "nombre", "cancion": "nombre",
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
                # Música
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

    if not parsed["nombre"]:
        clean = re.sub(r"\.(mkv|mp4|avi|mov|rar|zip|mp3|flac|m4a|wav|aac|ogg)$", "", filename, flags=re.I)
        clean = re.sub(r"[\._]", " ", clean)
        clean = re.sub(r"\[.*?\]", "", clean)

        # Eliminar patrones de temporada/episodio
        clean = re.sub(r"(?i)\s*[-–.]?\s*s\d{1,2}e\d{1,3}.*$", "", clean)
        clean = re.sub(r"(?i)\s*[-–.]?\s*\d{1,2}x\d{1,3}.*$", "", clean)

        # Eliminar partes, OVAs, etc.
        clean = re.sub(r"\s*[-–]\s*(ova|special|sp|especial|part|parte|cd|disc)?\s*\d{0,3}.*$", "", clean, flags=re.I)
        parsed["nombre"] = re.sub(r"\s+", " ", clean).strip()

    fname_lower = filename.lower()

    # Títulos forzados Sora
    if "forte" in fname_lower:
        parsed["nombre"] = "Sora no Otoshimono Forte, Heaven’s Lost Property 2"
    elif "tokeijikake" in fname_lower or "angeloid of clockwork" in fname_lower:
        parsed["nombre"] = "Sora no Otoshimono: Tokeijikake no Angeloid, Heaven’s Lost Property the Movie: The Angeloid of Clockwork"
    elif "final" in fname_lower and ("eternal" in fname_lower or "my master" in fname_lower):
        parsed["nombre"] = "Sora no Otoshimono Final: Eternal My Master, Heaven’s Lost Property: My Eternal Master"

    episode = None
    season = 1
    is_ova = False
    is_part = False
    special_type = None
    fname = filename.lower()

    # 1. Partes
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

    # 2. Patrones de temporada + episodio
    elif season_ep_match := re.search(r"[sS](\d{1,2})[eE](\d{1,3})", fname):
        season = int(season_ep_match.group(1))
        episode = int(season_ep_match.group(2))
    elif x_match := re.search(r"(\d{1,2})[xX](\d{1,3})", fname):
        season = int(x_match.group(1))
        episode = int(x_match.group(2))

    # 3. Películas conocidas
    elif any(x in fname for x in ["tokeijikake", "angeloid of clockwork", "final eternal", "eternal my master"]):
        pass

    # 4. OVAs / Specials / episodio simple
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
        "artista": parsed["artista"],
        "album": parsed["album"],
        "label": parsed["label"] or parsed["sello"],
        "productor": parsed["productor"],
        "season": season,
        "episode": episode,
        "is_ova": is_ova,
        "is_part": is_part,
        "special_type": special_type
    }


def classify(filename: str, mime: str = "", caption: str = "", tipo: str = None) -> str:
    text = (filename + " " + (caption or "")).lower()

    if "forte" in text and ("sora" in text or "otoshimono" in text):
        return "series"

    if any(x in text for x in ["tokeijikake", "angeloid of clockwork", "final eternal", "eternal my master", "my master"]):
        return "movies"

    if tipo:
        t = tipo.lower()
        if any(x in t for x in ["serie", "ova", "anime"]):
            return "series"
        if any(x in t for x in ["película", "pelicula", "movie"]):
            return "movies"
        if any(x in t for x in ["música", "musica", "music"]):
            return "music"

    if any(x in text for x in [".mp3", ".flac", ".m4a", ".wav", ".aac", ".ogg", "music", "ost"]) or "audio/" in mime:
        return "music"

    if re.search(r"(s\d{1,2}e\d{1,2}|season|temporada|[-–]\s*\d{1,3})", text) and "part" not in text:
        return "series"

    if any(x in text for x in [".mkv", ".mp4", "1080p", "720p", "bluray", "bd", "movie", "pelicula"]):
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


def get_all_items() -> List[Dict]:
    items = []
    for cat in catalog.values():
        items.extend(cat)
    return items


def build_groups(all_items: List[Dict]) -> Dict[str, Dict]:
    grouped = {}

    for item in all_items:
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

        # Mejor título
        if item.get("title") and len(item["title"]) > len(g["title"] or ""):
            g["title"] = item["title"]

        # Mejor thumbnail
        if item.get("thumbnail"):
            g["thumbnail"] = item["thumbnail"]

        if item.get("date") and item["date"] > (g.get("latest_date") or ""):
            g["latest_date"] = item["date"]

        # Mejor metadata
        for field in ["year", "genero", "sinopsis", "quality", "estudio", "episodios",
                      "secuela", "precuela", "resolucion", "duracion", "audios",
                      "subtitulos", "fansub", "formato", "peso", "artista", "album", "label", "productor"]:
            if item.get(field):
                if not g.get(field) or len(str(item[field])) > len(str(g.get(field) or "")):
                    g[field] = item[field]

        # Guardamos la categoría original
        g["categories"].append(item.get("original_category", "other"))

        # Solo archivos reales
        if not item.get("is_photo") and item.get("size_mb", 0) > 0:
            g["episodes"].append(item)

    # Decidir categoría final + títulos bonitos + agrupar por temporada
    for key, g in grouped.items():
        # Título bonito Sora
        if "forte" in key:
            g["title"] = "Sora no Otoshimono Forte, Heaven’s Lost Property 2"
        elif "tokeijikake" in key:
            g["title"] = "Sora no Otoshimono: Tokeijikake no Angeloid, Heaven’s Lost Property the Movie: The Angeloid of Clockwork"
        elif "final eternal" in key or "my master" in key:
            g["title"] = "Sora no Otoshimono Final: Eternal My Master, Heaven’s Lost Property: My Eternal Master"

        # ========== DECISIÓN INTELIGENTE DE CATEGORÍA ==========
        num_episodes = len(g["episodes"])

        # 1. Si tiene archivos de música → prioridad máxima a "music"
        if "music" in g["categories"]:
            g["final_category"] = "music"

        # 2. Reglas especiales Sora
        elif "tokeijikake" in key or "final eternal" in key or "my master" in key:
            g["final_category"] = "movies"
        elif "forte" in key:
            g["final_category"] = "series"

        # 3. Si tiene muchos episodios → serie
        elif num_episodes >= 4:
            g["final_category"] = "series"

        # 4. Si tiene 1-2 archivos y son partes → película
        elif num_episodes <= 2 and any(ep.get("is_part") for ep in g["episodes"]):
            g["final_category"] = "movies"

        # 5. Por mayoría
        elif g["categories"]:
            most_common = Counter(g["categories"]).most_common(1)[0][0]
            g["final_category"] = most_common
        else:
            g["final_category"] = "other"

        # Ordenar episodios
        g["episodes"].sort(key=lambda x: (
            0 if x.get("is_ova") or x.get("is_part") else 1,
            x.get("season") or 0,
            x.get("episode") or 999
        ))

        # Agrupar para la vista
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


def get_all_years() -> list:
    years = set()
    for items in catalog.values():
        for item in items:
            if item.get("year"):
                try:
                    years.add(int(item["year"]))
                except:
                    pass
    return sorted(list(years), reverse=True)


def get_all_studios() -> list:
    studios = set()
    for items in catalog.values():
        for item in items:
            if item.get("estudio"):
                for s in item["estudio"].split(","):
                    s = s.strip()
                    if s:
                        studios.add(s)
    return sorted(list(studios))


@app.on_event("startup")
async def startup_event():
    await client.start()
    await reindex()

    @client.on(events.NewMessage(chats=CHANNEL_ID))
    async def handler(event):
        await process_message(event.message)


@app.get("/stream/{msg_id}")
async def stream_audio(msg_id: int, username: str = Depends(verify_credentials)):
    try:
        msg = await client.get_messages(CHANNEL_ID, ids=int(msg_id))
        if not msg or not msg.document:
            raise HTTPException(status_code=404, detail="Archivo no encontrado")

        mime = msg.document.mime_type or "audio/mpeg"
        filename = "track.mp3"
        for attr in msg.document.attributes:
            if hasattr(attr, "file_name") and attr.file_name:
                filename = attr.file_name
                break

        async def file_iterator():
            async for chunk in client.iter_download(msg.media):
                yield chunk

        return StreamingResponse(
            file_iterator(),
            media_type=mime,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Disposition": f'inline; filename="{filename}"'
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    category: str = "all",
    q: str = "",
    exact: str = "",
    page: int = 1,
    sort: str = "title",
    year: str = "",
    genero: str = "",
    estudio: str = "",
    username: str = Depends(verify_credentials)
):
    all_items = get_all_items()
    groups = build_groups(all_items)

    if category != "all":
        all_series = [g for g in groups.values() if g.get("final_category") == category]
    else:
        all_series = list(groups.values())

    if year:
        all_series = [s for s in all_series if s.get("year") and str(s["year"]) == str(year)]

    if genero:
        genero_lower = genero.lower().strip()
        all_series = [s for s in all_series if s.get("genero") and genero_lower in s["genero"].lower()]

    if estudio:
        estudio_lower = estudio.lower().strip()
        all_series = [s for s in all_series if s.get("estudio") and estudio_lower in s["estudio"].lower()]

    if q:
        q_lower = q.lower().strip()
        filtered = []
        for series in all_series:
            if (q_lower in (series["title"] or "").lower() or
                q_lower in (series.get("genero") or "").lower() or
                q_lower in (series.get("sinopsis") or "").lower() or
                q_lower in (series.get("estudio") or "").lower() or
                q_lower in (series.get("artista") or "").lower() or
                q_lower in (series.get("album") or "").lower()):
                filtered.append(series)
        all_series = filtered

    if sort == "year":
        all_series.sort(key=lambda x: int(x["year"]) if x.get("year") and str(x["year"]).isdigit() else 0, reverse=True)
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
        request,
        "index.html",
        {
            "request": request,
            "items": items,
            "total": sum(len(v) for v in catalog.values()),
            "total_series": total_series,
            "counts": counts,
            "category": category,
            "q": q,
            "page": page,
            "total_pages": total_pages,
            "all_genres": get_all_genres(),
            "all_years": get_all_years(),
            "all_studios": get_all_studios(),
            "sort": sort,
            "year": year,
            "genero": genero,
            "estudio": estudio,
        }
    )


@app.get("/health")
async def health():
    return {"status": "ok", "total_files": sum(len(v) for v in catalog.values())}


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
