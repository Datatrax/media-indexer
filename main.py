import asyncio
import re
import secrets
import time
import zipfile
from collections import defaultdict
from io import BytesIO
from typing import Dict, List
from urllib.parse import urlencode

from fastapi import FastAPI, Request, Depends, HTTPException, status, Query
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import uvicorn

import tg
from config import (ADMIN_USER, ADMIN_PASS, CHANNEL_ID, DB_PATH, FULL_SYNC,
                    ITEMS_PER_PAGE, THUMB_DIR)
from db import Database
from groups import slug, same_artist, get_filters_from_groups
from library import Library

STREAM_CHUNK = 512 * 1024  # múltiplo de 4096 y divisor de 1 MB (requisito de Telegram)

app = FastAPI(title="Biblioteca de Prozac")
templates = Jinja2Templates(directory="templates")
security = HTTPBasic()
library = Library(Database(DB_PATH), THUMB_DIR)
client = tg.client

# ---------------------------------------------------------------- seguridad
_failed: Dict[str, List[float]] = defaultdict(list)
MAX_FAILS, WINDOW = 8, 300  # 8 intentos fallidos cada 5 minutos por IP


def verify_credentials(request: Request, credentials: HTTPBasicCredentials = Depends(security)):
    ip = request.client.host if request.client else "?"
    now = time.time()
    recent = [t for t in _failed[ip] if now - t < WINDOW]
    _failed[ip] = recent
    if len(recent) >= MAX_FAILS:
        raise HTTPException(status_code=429, detail="Demasiados intentos. Espera unos minutos.",
                            headers={"Retry-After": str(WINDOW)})
    # .encode(): compare_digest falla con acentos/ñ si se le pasan str
    ok_user = secrets.compare_digest(credentials.username.encode(), ADMIN_USER.encode())
    ok_pass = secrets.compare_digest(credentials.password.encode(), ADMIN_PASS.encode())
    if not (ok_user and ok_pass):
        _failed[ip].append(now)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciales incorrectas",
                            headers={"WWW-Authenticate": "Basic"})
    return credentials.username


# ---------------------------------------------------------------- arranque
@app.on_event("startup")
async def startup_event():
    n = library.load()                      # la web ya funciona con lo guardado...
    print(f"Catálogo cargado desde la caché: {n} archivos")
    await client.start()
    tg.register_handlers(library)
    asyncio.create_task(tg.sync(library, full=FULL_SYNC))   # ...mientras se pone al día en segundo plano


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
    groups = library.groups()
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
        "total": library.total,
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



# ---------------------------------------------------------------- miniaturas
@app.get("/thumb/{msg_id}")
async def thumb(msg_id: int, v: str = "", username: str = Depends(verify_credentials)):
    path = THUMB_DIR / f"{int(msg_id)}.jpg"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Sin miniatura")
    # la URL lleva ?v=<fecha de edición>, así que puede cachearse "para siempre"
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=31536000, immutable"})



# ---------------------------------------------------------------- descargas en zip
async def _zip_response(ids: str, name: str, default: str):
    id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    safe_name = re.sub(r'[<>:"/\\|?*]', '', name).strip() or default
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for mid in id_list:
            msg = await client.get_messages(CHANNEL_ID, ids=mid)
            doc = getattr(msg, "document", None) if msg else None
            if not doc:
                continue
            filename = f"{mid}.bin"
            for attr in doc.attributes:
                if getattr(attr, "file_name", None):
                    filename = attr.file_name
                    break
            data = await client.download_media(msg, file=bytes)
            if data:
                zf.writestr(f"{safe_name}/{filename}", data)
    buffer.seek(0)
    return StreamingResponse(buffer, media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{safe_name}.zip"'})


@app.get("/download-album")
async def download_album(ids: str = Query(...), name: str = Query("Album"), username: str = Depends(verify_credentials)):
    return await _zip_response(ids, name, "Album")


@app.get("/download-season")
async def download_season(ids: str = Query(...), name: str = Query("Temporada"), username: str = Depends(verify_credentials)):
    return await _zip_response(ids, name, "Temporada")


# ---------------------------------------------------------------- admin / estado
@app.post("/admin/resync")
async def admin_resync(username: str = Depends(verify_credentials)):
    """Revisa todo el canal (ediciones y borrados). Uso: curl -u usuario:clave -X POST https://tu-sitio/admin/resync"""
    if tg.sync_state["running"]:
        return {"status": "ya hay una sincronización en curso", **tg.sync_state}
    asyncio.create_task(tg.sync(library, full=True))
    return {"status": "sincronización completa iniciada"}


@app.get("/health")
async def health():
    return {"status": "ok", "total_files": library.total, "sync": tg.sync_state}


if __name__ == "__main__":
    import os
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
