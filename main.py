import os
import re
import secrets
import base64
import zipfile
from io import BytesIO
from typing import Dict, Any, List, Optional
from collections import defaultdict


from telethon import TelegramClient
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




def parse_caption(caption: str) -> Dict[str, str]:
    info = {}
    if not caption:
        return info


    for line in caption.split('\n'):
        line = line.strip()
        if ':' not in line:
            continue
        key, val = line.split(':', 1)
        key = key.strip().lower()
        val = val.strip()
        if not val:
            continue


        if key in ['tipo', 'type']:
            info['tipo'] = val.lower()
        elif key in ['artista', 'artist', 'autor']:
            info['artista'] = val
        elif key in ['álbum', 'album', 'disco', 'nombre']:
            # Nombre se usa tanto para álbum como para título de película
            if 'album' not in info:
                info['album'] = val
            info['nombre'] = val
        elif key in ['año', 'year', 'fecha']:
            info['year'] = val
        elif key in ['género', 'genero', 'genre', 'géneros', 'generos']:
            info['genero'] = val
        elif key in ['estudio', 'studio']:
            info['estudio'] = val
        elif key in ['sello', 'label', 'discográfica', 'discografica']:
            info['label'] = val
        elif key in ['productor', 'producer']:
            info['productor'] = val
        elif key in ['precuela', 'prequel']:
            info['precuela'] = val
        elif key in ['secuela', 'sequel']:
            info['secuela'] = val
        elif key in ['sinopsis', 'descripción', 'descripcion', 'synopsis', 'plot']:
            info['sinopsis'] = val


    year_match = re.search(r'\b(19|20)\d{2}\b', caption)
    if year_match and 'year' not in info:
        info['year'] = year_match.group(0)


    return info




def classify(filename: str, mime: str, size: int, caption: str, parsed: Dict) -> str:
    fname = (filename or "").lower()
    cap = (caption or "").lower()
    tipo = parsed.get('tipo', '')


    if any(x in tipo for x in ['película', 'pelicula', 'movie', 'film']):
        return 'movies'
    if any(x in tipo for x in ['serie', 'series', 'anime']):
        return 'series'
    if any(x in tipo for x in ['música', 'musica', 'music', 'álbum', 'album']):
        return 'music'


    audio_ext = ('.mp3', '.flac', '.wav', '.m4a', '.aac', '.ogg', '.wma', '.alac')
    if any(fname.endswith(ext) for ext in audio_ext) or (mime and mime.startswith('audio/')):
        return 'music'


    if size > 60 * 1024 * 1024 and any(x in fname for x in ['.mkv', '.mp4', '.avi', '.rar', '.zip']):
        if any(x in cap for x in ['película', 'pelicula', 'movie', 'film', 'tipo: película']):
            return 'movies'


    if re.search(r's\d{1,2}e\d{1,3}|temporada\s*\d+|season\s*\d+|cap\.?\s*\d+', fname + " " + cap, re.I):
        return 'series'


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
            if hasattr(attr, 'file_name') and attr.file_name:
                filename = attr.file_name
            if hasattr(attr, 'duration'):
                duration = getattr(attr, 'duration', 0) or 0
        mime = getattr(doc, 'mime_type', '') or ""
        size = getattr(doc, 'size', 0) or 0
    elif isinstance(msg.media, MessageMediaPhoto):
        filename = f"photo_{msg.id}.jpg"
        mime = "image/jpeg"
    else:
        return None


    caption = msg.message or ""
    parsed = parse_caption(caption)
    category = classify(filename, mime, size, caption, parsed)


    # --- Limpieza de título de pista ---
    track_title = filename
    track_title = re.sub(r'\.[a-z0-9]{2,5}$', '', track_title, flags=re.I)
    track_title = re.sub(r'^\d{1,3}[\s\.\-_]+', '', track_title)


    # Separar "Artista – Canción"
    if '–' in track_title or ' - ' in track_title:
        parts = re.split(r'\s*[–-]\s*', track_title, maxsplit=1)
        if len(parts) == 2:
            if not parsed.get('artista'):
                parsed['artista'] = parts[0].strip()
            track_title = parts[1].strip()


    track_title = clean_text(track_title)
    if not track_title or len(track_title) < 2:
        track_title = filename


    info = {
        "id": msg.id,
        "title": track_title,
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
        **parsed
    }


    # Temporada / Episodio
    ep_match = re.search(r'(?:s|temporada|season)?[\s\._-]*(\d{1,2})[\s\._-]*[ex][\s\._-]*(\d{1,3})', filename + " " + caption, re.I)
    if ep_match:
        info["season"] = int(ep_match.group(1))
        info["episode"] = int(ep_match.group(2))
    else:
        ep_only = re.search(r'(?:ep|capítulo|capitulo|cap)[\s\._-]*(\d{1,3})', filename + " " + caption, re.I)
        if ep_only:
            info["episode"] = int(ep_only.group(1))


    if re.search(r'\b(ova|ona|special|especial)\b', filename + " " + caption, re.I):
        info["is_ova"] = True


    return info




def normalize_title(title: str) -> str:
    if not title:
        return ""
    t = title.lower().strip()


    if "final eternal" in t or "eternal my master" in t:
        return "sora no otoshimono final eternal my master"
    if "tokeijikake" in t:
        return "sora no otoshimono tokeijikake no angeloid"
    if "forte" in t and "sora" in t:
        return "sora no otoshimono forte"
    if "ribbon hero" in t:
        return "the ribbon hero"


    # Limpiar basura de episodios y calidad
    t = re.sub(r'\b(s\d{1,2}[\s\._-]*e\d{1,3}|temporada\s*\d+|season\s*\d+|cap[ií]tulo\s*\d+|ep\.?\s*\d+|ova|ona|special|especial)\b', '', t, flags=re.I)
    t = re.sub(r'\b(bd|bluray|1080p|720p|480p|x264|x265|hevc|aac|flac|mp3|mkv|mp4|avi|rar|zip|part\d+)\b', '', t, flags=re.I)
    t = re.sub(r'\[.*?\]|\(.*?\)|\{.*?\}', '', t)
    t = re.sub(r'[^\w\s]', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t




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
            if cat == "music":
                artist = clean_text(item.get("artista") or "").lower()
                album = clean_text(item.get("album") or item.get("nombre") or "").lower()


                if artist and album:
                    key = f"music::{artist}::{album}"
                    display_title = item.get("album") or item.get("nombre") or item.get("title")
                elif artist:
                    key = f"music::{artist}::{normalize_title(item.get('title', ''))}"
                    display_title = f"{item.get('artista')} - {item.get('title')}"
                else:
                    key = f"music::{normalize_title(item.get('title', ''))}"
                    display_title = item.get("title")
            else:
                # Series / Películas: usar nombre del caption si existe
                raw = item.get("nombre") or item.get("title") or item.get("filename") or ""
                key = normalize_title(raw)
                display_title = re.sub(
                    r'\s*[-–]?\s*(s\d{1,2}[\s\._-]*e\d{1,3}|temporada\s*\d+|season\s*\d+|cap[ií]tulo\s*\d+|ep\.?\s*\d+|ova|ona).*$',
                    '', raw, flags=re.I
                ).strip(" -–_")
                if not display_title or len(display_title) < 3:
                    display_title = raw


            g = groups[key]
            g["normalized"] = key
            g["episodes"].append(item)


            # Preferir título más limpio y largo
            if not g["title"] or (display_title and len(display_title) > len(g["title"]) and not re.search(r's\d+e\d+|ova|bd-', display_title, re.I)):
                g["title"] = display_title


            for field in ["year", "genero", "estudio", "artista", "album", "label", "productor", "precuela", "secuela", "sinopsis"]:
                if item.get(field) and not g.get(field):
                    g[field] = item.get(field)


            if item.get("thumbnail") and not g["thumbnail"]:
                g["thumbnail"] = item["thumbnail"]


            if item.get("date") and item["date"] > (g["latest_date"] or ""):
                g["latest_date"] = item["date"]


            if cat == "music":
                g["final_category"] = "music"
            elif cat == "movies":
                g["final_category"] = "movies"
            elif cat == "series":
                g["final_category"] = "series"
            else:
                if not g["final_category"]:
                    g["final_category"] = cat


            if g["final_category"] in ["series", "movies"]:
                if item.get("is_ova"):
                    g["ovas"].append(item)
                elif item.get("season") is not None:
                    g["seasons"][item["season"]].append(item)
                elif item.get("episode") is not None and g["final_category"] == "movies":
                    g["parts"].append(item)


    result = []
    for key, g in groups.items():
        if not g["episodes"]:
            continue


        if g["final_category"] == "music":
            g["episodes"].sort(key=lambda x: x.get("id", 0))
        else:
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


    if section == "music":
        items = [g for g in groups if g["final_category"] == "music"]
    elif section == "movies":
        items = [g for g in groups if g["final_category"] == "movies"]
    else:
        items = [g for g in groups if g["final_category"] in ["series", "movies"]]


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


    if year:
        items = [g for g in items if str(g.get("year")) == year]
    if genero:
        items = [g for g in items if genero.lower() in (g.get("genero") or "").lower()]
    if estudio:
        items = [g for g in items if estudio.lower() in (g.get("estudio") or g.get("label") or "").lower()]
    if artista:
        items = [g for g in items if artista.lower() in (g.get("artista") or "").lower()]


    if sort == "year":
        items.sort(key=lambda x: int(x["year"]) if x.get("year") and str(x["year"]).isdigit() else 0, reverse=True)
    elif sort == "date":
        items.sort(key=lambda x: x.get("latest_date") or "", reverse=True)
    else:
        items.sort(key=lambda x: (x.get("title") or "").lower())


    filters = get_filters_from_groups(groups, section)
    total_items = len(items)
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
        mime = getattr(msg.media.document, 'mime_type', 'application/octet-stream') if hasattr(msg.media, 'document') else "application/octet-stream"
        filename = "file"
        for attr in getattr(msg.media.document, 'attributes', []):
            if hasattr(attr, 'file_name') and attr.file_name:
                filename = attr.file_name
                break


        range_header = request.headers.get("range")
        if range_header and file_size:
            start = 0
            end = file_size - 1
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


            return StreamingResponse(
                ranged(),
                status_code=206,
                media_type=mime,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(length),
                }
            )


        async def full():
            async for chunk in client.iter_download(msg.media):
                yield chunk


        return StreamingResponse(
            full(),
            media_type=mime,
            headers={
                "Content-Disposition": f'inline; filename="{filename}"',
                "Accept-Ranges": "bytes",
                "Content-Length": str(file_size) if file_size else "0",
            }
        )
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
                    if hasattr(attr, 'file_name') and attr.file_name:
                        filename = attr.file_name
                        break
                data = await client.download_media(msg, file=bytes)
                if data:
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
