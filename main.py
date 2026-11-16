import os
import re
import secrets
import base64
import zipfile
from io import BytesIO
from typing import Dict, Any, List, Optional
from collections import defaultdict, Counter
from datetime import datetime


from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto


from fastapi import FastAPI, Request, Depends, HTTPException, status, Query
from fastapi.responses import HTMLResponse, StreamingResponse, Response
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
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username




# ==================== UTILIDADES ====================


def clean_text(text: str) -> str:
    if not text:
        return ""
    return re.sub(r'\s+', ' ', text).strip()




def normalize_title(title: str) -> str:
    if not title:
        return ""
    t = title.lower().strip()


    # Casos especiales de películas
    if "final eternal" in t or "eternal my master" in t or ("final" in t and "my master" in t):
        return "sora no otoshimono final eternal my master"
    if "tokeijikake" in t or "clockwork" in t:
        return "sora no otoshimono tokeijikake no angeloid"
    if "forte" in t and "sora" in t:
        return "sora no otoshimono forte"
    if "ribbon hero" in t:
        return "the ribbon hero"


    # Limpieza general
    t = re.sub(r'\[.*?\]|\(.*?\)|\{.*?\}', '', t)
    t = re.sub(r'\b(s\d{1,2}e\d{1,3}|temporada\s*\d+|season\s*\d+|cap[ií]tulo\s*\d+|ep\.?\s*\d+|ova|ona|special)\b', '', t, flags=re.I)
    t = re.sub(r'\b(bd|bluray|1080p|720p|480p|x264|x265|hevc|aac|flac|mp3|mkv|mp4|avi|rar|zip)\b', '', t, flags=re.I)
    t = re.sub(r'[^\w\s]', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t




def parse_caption(caption: str) -> Dict[str, str]:
    info = {}
    if not caption:
        return info


    lines = caption.split('\n')
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Formato "Clave: Valor"
        if ':' in line:
            key, val = line.split(':', 1)
            key = key.strip().lower()
            val = val.strip()
            if key in ['tipo', 'type']:
                info['tipo'] = val.lower()
            elif key in ['artista', 'artist', 'autor']:
                info['artista'] = val
            elif key in ['álbum', 'album', 'disco']:
                info['album'] = val
            elif key in ['año', 'year', 'fecha']:
                info['year'] = val
            elif key in ['género', 'genero', 'genre', 'géneros']:
                info['genero'] = val
            elif key in ['estudio', 'studio']:
                info['estudio'] = val
            elif key in ['sello', 'label', 'discográfica']:
                info['label'] = val
            elif key in ['productor', 'producer']:
                info['productor'] = val
            elif key in ['precuela', 'prequel']:
                info['precuela'] = val
            elif key in ['secuela', 'sequel']:
                info['secuela'] = val
            elif key in ['sinopsis', 'descripción', 'descripcion', 'synopsis', 'plot']:
                info['sinopsis'] = val


    # También buscar patrones sueltos
    year_match = re.search(r'\b(19|20)\d{2}\b', caption)
    if year_match and 'year' not in info:
        info['year'] = year_match.group(0)


    return info




def classify(filename: str, mime: str, size: int, caption: str, parsed: Dict) -> str:
    fname = (filename or "").lower()
    cap = (caption or "").lower()
    tipo = parsed.get('tipo', '')


    # Prioridad al campo Tipo del caption
    if 'película' in tipo or 'pelicula' in tipo or 'movie' in tipo:
        return 'movies'
    if 'serie' in tipo or 'series' in tipo or 'anime' in tipo:
        return 'series'
    if 'música' in tipo or 'musica' in tipo or 'music' in tipo or 'álbum' in tipo or 'album' in tipo:
        return 'music'


    # Extensiones de audio
    audio_ext = ('.mp3', '.flac', '.wav', '.m4a', '.aac', '.ogg', '.wma', '.alac')
    if any(fname.endswith(ext) for ext in audio_ext) or (mime and mime.startswith('audio/')):
        return 'music'


    # Videos grandes → probablemente película
    if size > 80 * 1024 * 1024 and any(x in fname for x in ['.mkv', '.mp4', '.avi', '.rar', '.zip']):
        if any(x in cap for x in ['película', 'pelicula', 'movie', 'film']):
            return 'movies'


    # Series por patrones de episodios
    if re.search(r's\d{1,2}e\d{1,3}|temporada\s*\d+|season\s*\d+|cap\.?\s*\d+', fname + " " + cap, re.I):
        return 'series'


    # Por defecto
    if any(x in fname for x in ['.mkv', '.mp4', '.avi']):
        return 'movies'
    return 'other'




def extract_info(msg) -> Optional[Dict[str, Any]]:
    if not msg.media:
        return None


    filename = ""
    mime = ""
    size = 0
    duration = 0


    if isinstance(msg.media, MessageMediaDocument):
        doc = msg.media.document
        for attr in doc.attributes:
            if hasattr(attr, 'file_name'):
                filename = attr.file_name or ""
            if hasattr(attr, 'duration'):
                duration = attr.duration or 0
        mime = doc.mime_type or ""
        size = doc.size or 0
    elif isinstance(msg.media, MessageMediaPhoto):
        filename = f"photo_{msg.id}.jpg"
        mime = "image/jpeg"
    else:
        return None


    caption = msg.message or ""
    parsed = parse_caption(caption)


    category = classify(filename, mime, size, caption, parsed)


    # Título limpio
    title = filename
    if parsed.get('album') and category == 'music':
        title = parsed.get('album')
    elif parsed.get('artista') and category == 'music':
        title = f"{parsed.get('artista')} - {filename}"


    # Limpiar título de la canción
    clean_title = re.sub(r'\.[a-z0-9]{2,4}$', '', filename, flags=re.I)
    clean_title = re.sub(r'^\d+[\.\-\s]+', '', clean_title)  # quitar número de track


    # Thumbnail
    thumbnail = None
    try:
        if msg.photo or (hasattr(msg.media, 'document') and msg.media.document.thumbs):
            thumb = client.loop.run_until_complete(client.download_media(msg, file=bytes, thumb=-1))
            if thumb:
                thumbnail = "data:image/jpeg;base64," + base64.b64encode(thumb).decode()
    except Exception:
        pass


    # Duración formateada
    duration_str = ""
    if duration:
        m, s = divmod(int(duration), 60)
        duration_str = f"{m}:{s:02d}"


    info = {
        "id": msg.id,
        "title": clean_title or filename,
        "filename": filename,
        "category": category,
        "size_mb": round(size / (1024 * 1024), 2) if size else 0,
        "mime": mime,
        "date": msg.date.isoformat() if msg.date else "",
        "link": f"https://t.me/c/{str(CHANNEL_ID)[4:]}/{msg.id}" if str(CHANNEL_ID).startswith("-100") else "",
        "thumbnail": thumbnail,
        "duration": duration,
        "duration_str": duration_str,
        "caption": caption,
        **parsed
    }


    # Extraer episodio / temporada
    ep_match = re.search(r'(?:s|temporada|season)?\s*(\d{1,2})\s*[ex]\s*(\d{1,3})', filename + " " + caption, re.I)
    if ep_match:
        info["season"] = int(ep_match.group(1))
        info["episode"] = int(ep_match.group(2))
    else:
        ep_only = re.search(r'(?:ep|capítulo|capitulo|cap)\.?\s*(\d{1,3})', filename + " " + caption, re.I)
        if ep_only:
            info["episode"] = int(ep_only.group(1))


    if re.search(r'\b(ova|ona|special|especial)\b', filename + " " + caption, re.I):
        info["is_ova"] = True
        info["special_type"] = "OVA"


    return info




def build_groups():
    groups = defaultdict(lambda: {
        "title": "",
        "normalized": "",
        "final_category": "",
        "episodes": [],
        "seasons": defaultdict(list),
        "ovas": [],
        "parts": [],
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
    })


    for cat in ["movies", "series", "music", "other"]:
        for item in catalog[cat]:
            # ========== CLAVE DE AGRUPACIÓN ==========
            if cat == "music":
                artist = (item.get("artista") or "").strip().lower()
                album = (item.get("album") or "").strip().lower()


                if artist and album:
                    key = f"music::{artist}::{album}"
                    display_title = item.get("album") or item.get("title")
                elif artist:
                    key = f"music::{artist}::{normalize_title(item.get('title', ''))}"
                    display_title = item.get("title")
                else:
                    key = f"music::{normalize_title(item.get('title', ''))}"
                    display_title = item.get("title")
            else:
                key = normalize_title(item.get("title") or item.get("filename", ""))
                display_title = item.get("title") or item.get("filename")


            g = groups[key]
            g["normalized"] = key
            g["episodes"].append(item)


            if not g["title"]:
                g["title"] = display_title


            # Metadata (priorizar la que tenga más info)
            for field in ["year", "genero", "estudio", "artista", "album", "label", "productor", "precuela", "secuela", "sinopsis"]:
                if item.get(field) and not g.get(field):
                    g[field] = item.get(field)


            if item.get("thumbnail") and not g["thumbnail"]:
                g["thumbnail"] = item["thumbnail"]


            if item.get("date") and item["date"] > (g["latest_date"] or ""):
                g["latest_date"] = item["date"]


            # Clasificación final
            if cat == "music":
                g["final_category"] = "music"
            elif cat == "movies":
                g["final_category"] = "movies"
            elif cat == "series":
                g["final_category"] = "series"
            else:
                if not g["final_category"]:
                    g["final_category"] = cat


            # Temporadas / OVAs / Parts (solo series/movies)
            if g["final_category"] in ["series", "movies"]:
                if item.get("is_ova"):
                    g["ovas"].append(item)
                elif item.get("season") is not None:
                    g["seasons"][item["season"]].append(item)
                elif item.get("episode") is not None and g["final_category"] == "movies":
                    g["parts"].append(item)


    # Ordenar episodios
    result = []
    for key, g in groups.items():
        if not g["episodes"]:
            continue


        # Ordenar tracks de música por orden de subida (id) o por nombre
        if g["final_category"] == "music":
            g["episodes"].sort(key=lambda x: x.get("id", 0))
        else:
            # Ordenar temporadas
            for season in g["seasons"]:
                g["seasons"][season].sort(key=lambda x: x.get("episode") or 0)
            g["ovas"].sort(key=lambda x: x.get("episode") or 0)
            g["parts"].sort(key=lambda x: x.get("episode") or 0)


        result.append(g)


    return result




def get_filters_from_groups(groups: List[Dict], section: str):
    years = set()
    genres = set()
    studios = set()
    artists = set()
    labels = set()


    for g in groups:
        if section == "music" and g["final_category"] != "music":
            continue
        if section in ["series", "movies"] and g["final_category"] == "music":
            continue
        if section == "movies" and g["final_category"] != "movies":
            continue
        if section == "series" and g["final_category"] not in ["series", "movies"]:
            continue


        if g.get("year"):
            years.add(str(g["year"]))
        if g.get("genero"):
            for gen in re.split(r'[,;/]', g["genero"]):
                gen = gen.strip()
                if gen:
                    genres.add(gen)
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




# ==================== INDEXACIÓN ====================


async def process_message(msg):
    info = extract_info(msg)
    if not info:
        return
    cat = info["category"]
    if cat in catalog:
        catalog[cat].append(info)
    else:
        catalog["other"].append(info)




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




# ==================== RUTAS ====================


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    section: str = "series",
    q: str = "",
    year: str = "",
    genero: str = "",
    estudio: str = "",
    artista: str = "",
    sort: str = "title",
    page: int = 1,
    username: str = Depends(verify_credentials)
):
    groups = build_groups()


    # Filtrar por sección
    if section == "music":
        items = [g for g in groups if g["final_category"] == "music"]
    elif section == "movies":
        items = [g for g in groups if g["final_category"] == "movies"]
    else:  # series
        items = [g for g in groups if g["final_category"] in ["series", "movies"]]


    # Búsqueda
    if q:
        q_lower = q.lower()
        items = [
            g for g in items
            if q_lower in (g.get("title") or "").lower()
            or q_lower in (g.get("artista") or "").lower()
            or q_lower in (g.get("album") or "").lower()
            or q_lower in (g.get("sinopsis") or "").lower()
            or any(q_lower in (ep.get("title") or "").lower() for ep in g["episodes"])
        ]


    # Filtros avanzados
    if year:
        items = [g for g in items if str(g.get("year")) == year]
    if genero:
        items = [g for g in items if genero.lower() in (g.get("genero") or "").lower()]
    if estudio:
        items = [g for g in items if estudio.lower() in (g.get("estudio") or g.get("label") or "").lower()]
    if artista:
        items = [g for g in items if artista.lower() in (g.get("artista") or "").lower()]


    # Orden
    if sort == "year":
        items.sort(key=lambda x: int(x["year"]) if x.get("year") and str(x["year"]).isdigit() else 0, reverse=True)
    elif sort == "date":
        items.sort(key=lambda x: x.get("latest_date") or "", reverse=True)
    else:
        items.sort(key=lambda x: (x.get("title") or "").lower())


    total_items = len(items)
    filters = get_filters_from_groups(groups, section)


    start = (page - 1) * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    page_items = items[start:end]
    total_pages = max(1, (total_items + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)


    counts = {
        "series": sum(1 for g in groups if g["final_category"] == "series"),
        "movies": sum(1 for g in groups if g["final_category"] == "movies"),
        "music": sum(1 for g in groups if g["final_category"] == "music"),
    }


    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "items": page_items,
            "total": sum(len(v) for v in catalog.values()),
            "counts": counts,
            "section": section,
            "q": q,
            "page": page,
            "total_pages": total_pages,
            "all_years": filters["years"],
            "all_genres": filters["genres"],
            "all_studios": filters["labels"] if section == "music" else filters["studios"],
            "all_artists": filters["artists"],
            "sort": sort,
            "year": year,
            "genero": genero,
            "estudio": estudio,
            "artista": artista,
        }
    )




@app.get("/stream/{msg_id}")
async def stream_file(msg_id: int, request: Request, username: str = Depends(verify_credentials)):
    try:
        msg = await client.get_messages(CHANNEL_ID, ids=msg_id)
        if not msg or not msg.media:
            raise HTTPException(404, "Archivo no encontrado")


        file_size = msg.media.document.size if hasattr(msg.media, 'document') else 0
        mime = msg.media.document.mime_type if hasattr(msg.media, 'document') else "application/octet-stream"
        filename = "file"
        for attr in getattr(msg.media.document, 'attributes', []):
            if hasattr(attr, 'file_name'):
                filename = attr.file_name
                break


        range_header = request.headers.get("range")
        if range_header and file_size:
            start, end = 0, file_size - 1
            match = re.search(r'bytes=(\d+)-(\d*)', range_header)
            if match:
                start = int(match.group(1))
                if match.group(2):
                    end = int(match.group(2))
            end = min(end, file_size - 1)
            length = end - start + 1


            async def ranged():
                async for chunk in client.iter_download(msg.media, offset=start, limit=length):
                    yield chunk


            headers = {
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(length),
                "Content-Type": mime,
            }
            return StreamingResponse(ranged(), status_code=206, headers=headers, media_type=mime)


        async def full():
            async for chunk in client.iter_download(msg.media):
                yield chunk


        return StreamingResponse(full(), media_type=mime, headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Accept-Ranges": "bytes",
            "Content-Length": str(file_size) if file_size else undefined,
        })
    except Exception as e:
        raise HTTPException(500, str(e))




@app.get("/download-album")
async def download_album(ids: str = Query(...), username: str = Depends(verify_credentials)):
    try:
        id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()]
        if not id_list:
            raise HTTPException(400, "No IDs")


        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for mid in id_list:
                msg = await client.get_messages(CHANNEL_ID, ids=mid)
                if not msg or not msg.media:
                    continue
                filename = f"{mid}.bin"
                for attr in getattr(msg.media.document, 'attributes', []):
                    if hasattr(attr, 'file_name'):
                        filename = attr.file_name
                        break
                data = await client.download_media(msg, file=bytes)
                zf.writestr(filename, data)


        buffer.seek(0)
        return StreamingResponse(
            buffer,
            media_type="application/zip",
            headers={"Content-Disposition": "attachment; filename=album.zip"}
        )
    except Exception as e:
        raise HTTPException(500, str(e))




@app.get("/health")
async def health():
    return {"status": "ok", "total_files": sum(len(v) for v in catalog.values())}




if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
