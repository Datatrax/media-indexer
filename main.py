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
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import uvicorn

import tg
from config import (ADMIN_USER, ADMIN_PASS, AUTO_META, CHANNEL_ID, DB_PATH, FULL_SYNC, TMDB_API_KEY,
                    ITEMS_PER_PAGE, THUMB_DIR)
from db import Database
from groups import slug, same_artist, get_filters_from_groups, continue_list, similar_albums
from library import Library, human_size
from downloads import archive_plan, content_disposition
import zipstream
from meta import enrich_pending
from search import search as fuzzy_search

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
SECTION_LABELS = [("series", "Series"), ("movies", "Películas"), ("docs", "Documentales"), ("music", "Música"),
                  ("podcasts", "Podcasts"), ("books", "Libros"), ("comics", "Cómics"), ("photos", "Fotos")]
CATEGORY_KEYS = {k for k, _ in SECTION_LABELS}


async def _meta_loop():
    """Rellena sinopsis/portadas faltantes en segundo plano (solo con AUTO_META=1)."""
    await asyncio.sleep(30)
    while True:
        n = 0
        try:
            n = await enrich_pending(library, TMDB_API_KEY)
        except Exception as e:
            print("Metadatos automáticos:", e)
        await asyncio.sleep(60 if n else 900)


@app.on_event("startup")
async def startup_event():
    n = library.load()                      # la web ya funciona con lo guardado...
    print(f"Catálogo cargado desde la caché: {n} archivos")
    await client.start()
    tg.register_handlers(library)
    asyncio.create_task(tg.sync(library, full=FULL_SYNC))   # ...mientras se pone al día en segundo plano
    if AUTO_META:
        asyncio.create_task(_meta_loop())


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    section: str = "home",
    q: str = "",
    year: str = "",
    genero: str = "",
    estudio: str = "",
    artista: str = "",
    album: str = "",
    key: str = "",
    sort: str = "",
    page: int = 1,
    view: str = "artists",
    username: str = Depends(verify_credentials)
):
    if section not in CATEGORY_KEYS | {"home", "favs"}:
        section = "home"
    groups = library.groups()
    by_key = {g["key"]: g for g in groups if g.get("key")}
    favs = library.db.favorites()
    seen = library.db.seen_map()
    music_albums = [g for g in groups if g["final_category"] == "music" and not g.get("is_artist")]
    artist_profile = None
    recent, cont, fav_groups = [], [], []

    counts = {cat: sum(1 for g in groups if g["final_category"] == cat and not g.get("is_artist")) for cat in CATEGORY_KEYS}
    counts["artists"] = sum(1 for g in groups if g.get("is_artist"))
    nav = ([{"key": "home", "label": "Inicio", "count": None}]
           + [{"key": k, "label": l, "count": counts[k]} for k, l in SECTION_LABELS if counts[k]]
           + [{"key": "favs", "label": "Favoritos", "count": len(favs)}])

    if section == "home":
        items = []
        for cat, label in SECTION_LABELS:
            lst = [g for g in groups if g["final_category"] == cat and not g.get("is_artist")]
            if lst:
                lst.sort(key=lambda x: x.get("latest_date") or "", reverse=True)
                recent.append({"key": cat, "label": label, "groups": lst[:10]})
        fav_groups = [by_key[k] for k in sorted(favs, key=lambda k: -favs[k]) if k in by_key][:12]
        cont = continue_list(by_key, seen)
    elif section == "favs":
        items = [by_key[k] for k in sorted(favs, key=lambda k: -favs[k]) if k in by_key]
    elif section == "music":
        if album:
            items = [g for g in music_albums if slug(g.get("title")) == slug(album)]
            if not items:
                items = [g for g in music_albums if album.lower() in (g.get("title") or "").lower()]
            if artista:
                narrowed = [g for g in items if same_artist(g.get("artista"), artista)]
                items = narrowed or items
        elif view == "artists" and not artista:
            items = [g for g in groups if g.get("is_artist")]
        else:
            items = music_albums
            if artista:
                exact = [g for g in items if slug(g.get("artista")) == slug(artista)]
                items = exact or [g for g in items if artista.lower() in (g.get("artista") or "").lower()]
                artist_profile = next((g for g in groups if g.get("is_artist") and slug(g["title"]) == slug(artista)), None)
    else:
        items = [g for g in groups if g["final_category"] == section and not g.get("is_artist")]

    focus = bool(key)
    if focus:      # enlace directo a UN elemento (desde "agregado recientemente", favoritos, continuar...)
        items = [by_key[key]] if key in by_key else []
        q = year = genero = estudio = ""

    if q and not album:
        items = fuzzy_search(items, q)      # sin acentos, tolera errores, ordena por relevancia

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
    elif sort == "title":
        items.sort(key=lambda x: (x.get("title") or "").lower())
    elif section == "music" and artista and not album:
        # página de artista: sus álbumes del más nuevo al más viejo (sin año, al final)
        items.sort(key=lambda x: (int(x["year"]) if str(x.get("year") or "").isdigit() else 0, (x.get("title") or "").lower()), reverse=True)
    elif not q and section != "favs":
        items.sort(key=lambda x: (x.get("title") or "").lower())

    filters = get_filters_from_groups(groups, section)
    total = len(items)

    paginate = section != "home" and not focus and not (section == "music" and (artista or album))
    page = max(1, page)
    page_items = items[(page - 1) * ITEMS_PER_PAGE:page * ITEMS_PER_PAGE] if paginate else items
    total_pages = max(1, (total + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE) if paginate else 1

    # cada sección tiene su propio "inicio": continuar, favoritos y agregado recientemente
    landing = (section in CATEGORY_KEYS and not (q or year or genero or estudio or artista or album or key)
               and page == 1 and (section != "music" or view == "artists"))
    sec_cont, sec_favs, sec_recent = [], [], []
    if landing:
        sec_cont = [c for c in continue_list(by_key, seen) if c["group"]["final_category"] == section]
        sec_favs = [by_key[k] for k in sorted(favs, key=lambda k: -favs[k])
                    if k in by_key and (by_key[k]["final_category"] == section or (section == "music" and by_key[k].get("is_artist")))][:12]
        sec_recent = sorted([g for g in groups if g["final_category"] == section and not g.get("is_artist")],
                            key=lambda x: x.get("latest_date") or "", reverse=True)[:10]

    similar_same, similar_other = [], []
    if section == "music" and album and len(items) == 1:
        artist_genres = {slug(x["title"]): x.get("genero") for x in groups if x.get("is_artist") and x.get("genero")}
        similar_same, similar_other = similar_albums(items[0], music_albums, artist_genres)

    albums_list, eps_list = [], []
    if section == "music" and artista and not album:
        albums_list = [g for g in page_items if len(g["episodes"]) > 3]
        eps_list = [g for g in page_items if len(g["episodes"]) <= 3]

    base_params = {"section": section, "view": view if section == "music" else "", "q": q, "sort": sort,
                   "year": year, "genero": genero, "estudio": estudio, "artista": artista}

    def furl(name: str, value: str) -> str:
        p = dict(base_params)
        p[name] = "" if (p.get(name) or "").lower() == value.lower() else value
        if section == "music":
            if name == "artista":
                p["view"] = "artists"
            elif not p["artista"]:
                p["view"] = "albums"      # género/año/sello en música: lista de álbumes, no de artistas
        return "?" + urlencode({k: v for k, v in p.items() if v})

    filter_labels = {"genero": "Género", "year": "Año", "artista": "Artista",
                     "estudio": "Sello" if section == "music" else "Estudio"}
    active_filters = [(filter_labels[k], k, v) for k, v in
                      (("artista", artista), ("genero", genero), ("year", year), ("estudio", estudio)) if v]
    clear_url = "?" + urlencode({"section": section, **({"view": "artists"} if section == "music" else {})})

    qs = urlencode({k: v for k, v in {
        "section": section, "view": view, "q": q, "sort": sort, "year": year,
        "genero": genero, "estudio": estudio, "artista": artista}.items() if v})

    return templates.TemplateResponse("index.html", {
        "request": request,
        "items": page_items,
        "albums_list": albums_list,
        "eps_list": eps_list,
        "landing": landing,
        "focus": focus,
        "section_label": dict(SECTION_LABELS).get(section, ""),
        "furl": furl,
        "active_filters": active_filters,
        "clear_url": clear_url,
        "sec_cont": sec_cont,
        "sec_favs": sec_favs,
        "sec_recent": sec_recent,
        "total_size": human_size(library.total_bytes),
        "similar_same": similar_same,
        "similar_other": similar_other,
        "artist_profile": artist_profile,
        "total": library.total,
        "found": total,
        "counts": counts,
        "nav": nav,
        "recent": recent,
        "cont": cont,
        "fav_groups": fav_groups,
        "favs": set(favs),
        "seen_ids": set(seen),
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


# ---------------------------------------------------------------- favoritos y "visto"
async def _json_body(request: Request) -> dict:
    # exigir JSON impide que otra web dispare estas acciones con un formulario (CSRF)
    if not request.headers.get("content-type", "").startswith("application/json"):
        raise HTTPException(status_code=415, detail="Se esperaba JSON")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON inválido")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON inválido")
    return body


@app.post("/api/fav")
async def api_fav(request: Request, username: str = Depends(verify_credentials)):
    key = str((await _json_body(request)).get("key", ""))[:300]
    if not key:
        raise HTTPException(status_code=400, detail="Falta key")
    return {"fav": library.db.toggle_favorite(key)}


@app.post("/api/seen")
async def api_seen(request: Request, username: str = Depends(verify_credentials)):
    body = await _json_body(request)
    try:
        item_id = int(body.get("id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="id inválido")
    key = str(body.get("key", ""))[:300]
    if item_id not in library.items or not key:
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    if body.get("seen", True):
        library.db.mark_seen(item_id, key)
    else:
        library.db.unmark_seen(item_id)
    return {"ok": True}


# ---------------------------------------------------------------- lector (PDF / EPUB / CBZ)
@app.get("/read/{msg_id}", response_class=HTMLResponse)
async def read_file(msg_id: int, request: Request, key: str = "", username: str = Depends(verify_credentials)):
    item = library.items.get(msg_id)
    if not item:
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    ext = item.get("ext")
    if ext == "pdf":
        return RedirectResponse(f"/stream/{msg_id}")     # el navegador ya sabe abrir PDF
    if ext in ("epub", "cbz"):
        return templates.TemplateResponse("read.html", {"request": request, "item": item, "ext": ext, "key": key})
    return RedirectResponse(item.get("link") or "/")


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



# ---------------------------------------------------------------- descargas en zip (en streaming)
def _opener(msg):
    async def gen():
        async for chunk in client.iter_download(msg.media, request_size=STREAM_CHUNK):
            yield chunk
    return gen


@app.get("/download/group")
async def download_group(key: str = Query(...), username: str = Depends(verify_credentials)):
    """Zip de un álbum (o temporada, libro, etc.): una carpeta con su nombre y las pistas numeradas en orden."""
    g = next((x for x in library.groups() if x.get("key") == key and not x.get("is_artist") and x.get("episodes")), None)
    if not g:
        raise HTTPException(status_code=404, detail="Nada que descargar")
    ids = [e["id"] for e in g["episodes"]]
    msgs = {}
    for i in range(0, len(ids), 100):
        for msg in await client.get_messages(CHANNEL_ID, ids=ids[i:i + 100]):
            if msg and getattr(msg, "document", None):
                msgs[msg.id] = msg
    if any(i not in msgs for i in ids):
        raise HTTPException(status_code=409, detail="Algunos archivos ya no están en el canal. Espera a la sincronización.")
    folder, plan = archive_plan(g, {i: msgs[i].document.size or 0 for i in ids})
    entries = [zipstream.Entry(name, size, _opener(msgs[mid])) for name, size, mid in plan]
    return StreamingResponse(
        zipstream.stream(entries), media_type="application/zip",
        headers={"Content-Length": str(zipstream.total_length(entries)),
                 "Content-Disposition": content_disposition(f"{folder}.zip")})


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
