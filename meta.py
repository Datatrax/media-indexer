"""Datos automáticos (sinopsis, año, géneros, portada) para lo que no tenga caption completo.
Fuentes: Jikan/MyAnimeList (anime y manga, sin clave), TMDB (películas, series y documentales; requiere
TMDB_API_KEY) y Open Library (libros, sin clave). Solo rellena campos vacíos; nunca pisa tus captions."""
import asyncio
import json
import logging
import time
import urllib.parse
import urllib.request
from typing import Dict, Optional

from groups import slug

log = logging.getLogger("biblioteca")
USER_AGENT = "BibliotecaProzac/1.0 (uso personal)"
META_CATS = ("series", "movies", "docs", "books", "comics")
FILL_FIELDS = ("year", "genero", "sinopsis", "estudio", "autor")
RETRY_AFTER = 30 * 86400      # si no se encontró nada, reintentar en 30 días
_tmdb_genres: Dict[str, Dict[int, str]] = {}


def fetch_json(url: str, headers: Optional[Dict] = None, timeout: int = 12):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _clean(d: Dict) -> Dict:
    return {k: v for k, v in d.items() if v not in (None, "", [], 0)}


def lookup_jikan(title: str, kind: str = "anime", movie: bool = False) -> Dict:
    url = f"https://api.jikan.moe/v4/{kind}?q={urllib.parse.quote(title)}&limit=1" + ("&type=movie" if movie else "")
    data = (fetch_json(url).get("data") or [])
    if not data:
        return {}
    a = data[0]
    year = a.get("year") or (((a.get("aired") or a.get("published") or {}).get("prop") or {}).get("from") or {}).get("year")
    names = [g["name"] for g in (a.get("genres") or [])]
    studios = [s["name"] for s in (a.get("studios") or [])]
    authors = [x["name"] for x in (a.get("authors") or [])]
    poster = (((a.get("images") or {}).get("jpg")) or {}).get("large_image_url")
    return _clean({"year": year, "genero": ", ".join(names), "sinopsis": a.get("synopsis"),
                   "estudio": ", ".join(studios[:2]), "autor": ", ".join(authors[:2]), "poster": poster})


def _tmdb_get(path: str, params: Dict, key: str):
    headers = {}
    if key.startswith("eyJ"):                       # token v4 (Bearer)
        headers["Authorization"] = f"Bearer {key}"
    else:
        params = {**params, "api_key": key}
    return fetch_json(f"https://api.themoviedb.org/3/{path}?{urllib.parse.urlencode(params)}", headers)


def lookup_tmdb(title: str, kind: str, key: str, lang: str = "es-MX") -> Dict:
    ep = "tv" if kind in ("series",) else "movie"
    results = _tmdb_get(f"search/{ep}", {"query": title, "language": lang}, key).get("results") or []
    if not results and kind == "docs":
        ep = "tv"
        results = _tmdb_get("search/tv", {"query": title, "language": lang}, key).get("results") or []
    if not results:
        return {}
    r = results[0]
    if ep not in _tmdb_genres:
        gl = _tmdb_get(f"genre/{ep}/list", {"language": lang}, key).get("genres") or []
        _tmdb_genres[ep] = {g["id"]: g["name"] for g in gl}
    names = [_tmdb_genres[ep].get(i) for i in (r.get("genre_ids") or [])]
    year = (r.get("first_air_date") or r.get("release_date") or "")[:4]
    poster = f"https://image.tmdb.org/t/p/w500{r['poster_path']}" if r.get("poster_path") else None
    return _clean({"year": year, "genero": ", ".join(n for n in names if n), "sinopsis": r.get("overview"), "poster": poster})


def lookup_openlibrary(title: str, author: Optional[str] = None) -> Dict:
    q = f"https://openlibrary.org/search.json?title={urllib.parse.quote(title)}&limit=1&fields=title,author_name,first_publish_year,subject,cover_i"
    if author:
        q += f"&author={urllib.parse.quote(author)}"
    docs = fetch_json(q).get("docs") or []
    if not docs:
        return {}
    d = docs[0]
    poster = f"https://covers.openlibrary.org/b/id/{d['cover_i']}-L.jpg" if d.get("cover_i") else None
    return _clean({"year": d.get("first_publish_year"), "genero": ", ".join((d.get("subject") or [])[:4]),
                   "autor": ", ".join((d.get("author_name") or [])[:2]), "poster": poster})


def lookup(category: str, title: str, author: Optional[str] = None, tmdb_key: Optional[str] = None) -> Dict:
    """Busca datos según el tipo de contenido. Puede lanzar excepciones de red."""
    if category == "series":
        return lookup_jikan(title) or (lookup_tmdb(title, "series", tmdb_key) if tmdb_key else {})
    if category == "movies":
        if tmdb_key:
            return lookup_tmdb(title, "movies", tmdb_key) or lookup_jikan(title, movie=True)
        return lookup_jikan(title, movie=True)
    if category == "docs":
        return lookup_tmdb(title, "docs", tmdb_key) if tmdb_key else {}
    if category == "comics":
        return lookup_jikan(title, kind="manga") or lookup_openlibrary(title, author)
    if category == "books":
        return lookup_openlibrary(title, author)
    return {}


def meta_key(group: Dict) -> str:
    return f"{group['final_category']}|{slug(group['title'])}"


def apply_meta(groups, cache: Dict[str, Dict]) -> None:
    """Rellena (sin pisar) los campos vacíos de cada grupo con lo guardado en la caché."""
    for g in groups:
        if g.get("is_artist") or g.get("final_category") not in META_CATS:
            continue
        entry = (cache.get(meta_key(g)) or {}).get("data") or {}
        if not entry:
            continue
        used = False
        for f in FILL_FIELDS:
            if not g.get(f) and entry.get(f):
                g[f] = str(entry[f])
                used = True
        if not g.get("thumbnail") and entry.get("poster"):
            g["thumbnail"] = entry["poster"]
            g["thumb_small"] = entry["poster"]
            used = True
        g["auto_meta"] = used


async def enrich_pending(lib, tmdb_key: Optional[str] = None, limit: int = 20, delay: float = 1.2) -> int:
    """Consulta las fuentes para grupos incompletos que aún no están en caché. Devuelve cuántos consultó."""
    done = 0
    for g in list(lib.groups()):
        if g.get("is_artist") or g.get("final_category") not in META_CATS:
            continue
        if g.get("sinopsis") and g.get("thumbnail") and g.get("year"):
            continue
        key = meta_key(g)
        cached = lib.meta.get(key)
        if cached and time.time() - cached["fetched"] < RETRY_AFTER:
            continue
        try:
            data = await asyncio.to_thread(lookup, g["final_category"], g["title"], g.get("autor"), tmdb_key)
        except Exception as e:      # sin red, límite de peticiones, etc.: no se guarda y se reintenta luego
            log.warning("Metadatos de %r no disponibles: %s", g["title"], e)
            break
        lib.set_meta(key, data)     # también se guarda el "no encontrado" para no insistir
        done += 1
        if done >= limit:
            break
        await asyncio.sleep(delay)
    return done
