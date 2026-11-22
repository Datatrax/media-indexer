"""Agrupación de archivos en series, películas, álbumes y artistas."""
import re
from collections import defaultdict
from typing import Dict, List
from urllib.parse import quote_plus

from captions import clean_text, CATEGORIES, parse_caption, podcast_fields
from search import fold


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


def link_music(items: List[Dict]) -> None:
    """Enlaza pistas sueltas con su álbum: usa la portada (foto con Álbum/Artista)
    publicada justo antes y, si no hay, el único álbum de ese artista."""
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


SIMPLE_CATS = ("podcasts", "books", "comics", "photos")   # se agrupan por nombre, sin temporadas
VIDEO_CATS = ("series", "movies", "docs")
COVER_INHERIT = ["autor", "year", "genero", "sinopsis"]


def group_href(g: Dict) -> str:
    """Enlace para abrir un grupo desde la portada, favoritos o 'continuar'."""
    if g.get("is_artist"):
        return f"?section=music&view=artists&artista={quote_plus(g['title'])}"
    if g["final_category"] == "music":
        return f"?section=music&album={quote_plus(g['title'])}&artista={quote_plus(g.get('artista') or '')}"
    return f"?section={g['final_category']}&q={quote_plus(g['title'])}"


def link_covers(items: List[Dict]) -> None:
    """Podcasts, libros y cómics: los archivos sin 'Nombre' heredan el de la portada (foto) publicada justo antes."""
    ctx = None
    for it in sorted(items, key=lambda x: x["id"]):
        cat = it["category"]
        if it.get("is_photo"):
            ctx = it if (cat in ("podcasts", "books", "comics") and it.get("album")) else None
            continue
        if ctx is None:
            continue
        same = cat == ctx["category"]
        audio_after_podcast = ctx["category"] == "podcasts" and cat == "music" and not it.get("album")
        if not (same or audio_after_podcast):
            ctx = None
            continue
        if it.get("album"):
            continue
        it["album"] = ctx["album"]
        it["_cover"] = ctx["id"]
        if audio_after_podcast:
            # el nombre "Ep 2 - La caída" se había leído como "artista - canción": se vuelve a interpretar como episodio
            it["category"] = "podcasts"
            if not parse_caption(it.get("caption")).get("artista"):
                it["artista"] = None
            it["title"], ep_n = podcast_fields(it.get("filename"))
            if ep_n is not None:
                it["episode"] = ep_n
        for f in COVER_INHERIT:
            if not it.get(f) and ctx.get(f):
                it[f] = ctx[f]


def build_groups(items: List[Dict]) -> List[Dict]:
    items = [dict(i) for i in items]   # copias: no se toca el catálogo original
    link_covers(items)
    link_music(items)
    by_cat = defaultdict(list)
    for i in sorted(items, key=lambda x: x["id"], reverse=True):
        by_cat[i["category"]].append(i)
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
        "autor": None,
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

    for cat in CATEGORIES + ["other"]:
        for item in by_cat.get(cat, []):
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
            elif cat in SIMPLE_CATS:
                album = clean_text(item.get("album") or "")
                if album:
                    key = f"{cat}::{slug(album)}"
                    display = album
                elif item.get("is_photo"):
                    continue   # portada sin nombre: no se puede agrupar
                else:
                    key = f"{cat}::{slug(item.get('title') or item['filename'])}"
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
            g["key"] = key if "::" in key else f"{cat}|{key}"

            if item.get("is_artist"):
                g["is_artist"] = True
                g["title"] = display
                g["final_category"] = ""
                for field in ["year", "genero", "sinopsis", "thumbnail"]:
                    if item.get(field):
                        g[field] = item[field]
                continue

            if cat == "photos" or not item.get("is_photo"):
                g["episodes"].append(item)
                g["total_size"] += item.get("size_mb", 0)

            if not g["title"]:
                g["title"] = display or item.get("title")

            for field in ["year", "genero", "estudio", "artista", "autor", "album", "label", "productor", "precuela", "secuela", "sinopsis"]:
                if item.get(field):
                    if item.get("is_photo") or not g.get(field):
                        g[field] = item[field]

            if item.get("thumbnail"):
                if item.get("is_photo") or not g["thumbnail"]:
                    g["thumbnail"] = item["thumbnail"]

            if item.get("date") and item["date"] > (g["latest_date"] or ""):
                g["latest_date"] = item["date"]

            g["final_category"] = cat if cat in CATEGORIES else "other"

            if cat in VIDEO_CATS and not item.get("is_photo"):
                if item.get("is_ova"):
                    g["ovas"].append(item)
                elif item.get("season") is not None:
                    g["seasons"][item["season"]].append(item)
                elif cat in ("series", "docs") and item.get("episode") is not None:
                    g["seasons"][1].append(item)   # tiene número de capítulo pero no temporada: es la 1
                else:
                    g["parts"].append(item)

    result = []
    for g in groups.values():
        if g.get("is_artist"):
            g["href"] = group_href(g)
            result.append(g)
            continue
        if not g["episodes"]:
            continue
        fc = g["final_category"]
        if fc in ("music", "podcasts", "photos"):
            g["episodes"].sort(key=lambda x: (x.get("episode") if fc == "podcasts" and x.get("episode") is not None else 10 ** 6, x.get("id", 0)) if fc == "podcasts" else x.get("id", 0))
            if fc != "photos":   # lista compacta para el reproductor
                g["playlist"] = [{"id": e["id"], "title": e["title"], "album": g.get("title") or "",
                                  "artist": e.get("artista") or e.get("autor") or g.get("artista") or g.get("autor") or ""}
                                 for e in g["episodes"]]
        elif fc in ("books", "comics"):
            g["episodes"].sort(key=lambda x: (x.get("episode") if x.get("episode") is not None else 10 ** 6, x.get("id", 0)))
        else:
            sorted_seasons = dict(sorted(g["seasons"].items()))
            g["seasons"] = sorted_seasons
            for sn in g["seasons"]:
                g["seasons"][sn].sort(key=lambda x: x.get("episode") or 0)
            g["ovas"].sort(key=lambda x: x.get("episode") or 0)
            g["parts"].sort(key=lambda x: x.get("episode") or 0)
            g["episodes"].sort(key=lambda x: (x.get("season") or 0, x.get("episode") or 0, x.get("id", 0)))
        g["href"] = group_href(g)
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
            "title": name, "is_artist": True, "synthetic": True, "final_category": "", "key": f"artist::{slug(name)}",
            "href": f"?section=music&view=artists&artista={quote_plus(name)}",
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
        elif section in CATEGORIES:
            if g["final_category"] != section or g.get("is_artist"):
                continue
        else:
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


def continue_list(by_key: Dict[str, Dict], seen: Dict[int, tuple], limit: int = 12) -> List[Dict]:
    """'Continuar': por cada grupo con algo visto, el siguiente archivo sin ver."""
    last_by_key: Dict[str, tuple] = {}
    for item_id, (key, at) in seen.items():
        cur = last_by_key.get(key)
        if not cur or at > cur[1]:
            last_by_key[key] = (item_id, at)
    out = []
    for key, (last_id, at) in sorted(last_by_key.items(), key=lambda kv: -kv[1][1]):
        g = by_key.get(key)
        if not g or g.get("final_category") in ("music", "photos", "") or not g.get("episodes"):
            continue
        eps = g["episodes"]
        ids = [e["id"] for e in eps]
        start = ids.index(last_id) + 1 if last_id in ids else 0
        nxt = (next((e for e in eps[start:] if e["id"] not in seen), None)
               or next((e for e in eps if e["id"] not in seen), None))
        if nxt:
            out.append({"group": g, "ep": nxt, "at": at})
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- álbumes parecidos
def _genre_set(value) -> set:
    return {fold(x) for x in re.split(r"[,;/]", value or "") if fold(x)}


def similar_albums(target: Dict, albums: List[Dict], artist_genres: Dict[str, str] = None, limit: int = 8):
    """Mini algoritmo de recomendación para la página de un álbum.
    Devuelve (mismo_artista, parecidos). Puntúa por: mismo artista, géneros en común, géneros que suelen
    aparecer juntos en tu biblioteca (p. ej. 'dream pop' y 'shoegaze'), palabras en común entre géneros
    ('indie pop' ~ 'synth pop') y cercanía de año. Si un álbum no tiene géneros usa los de su artista."""
    artist_genres = artist_genres or {}

    def genres_of(a: Dict) -> set:
        return _genre_set(a.get("genero")) or _genre_set(artist_genres.get(slug(a.get("artista") or "")))

    co: Dict[tuple, int] = defaultdict(int)
    count: Dict[str, int] = defaultdict(int)
    for a in albums:
        s = genres_of(a)
        for x in s:
            count[x] += 1
            for y in s:
                if x < y:
                    co[(x, y)] += 1

    def related(x: str, y: str) -> float:
        c = co.get((x, y) if x < y else (y, x), 0)
        return c / min(count[x], count[y]) if c else 0.0

    def year(a):
        try:
            return int(str(a.get("year"))[:4])
        except (TypeError, ValueError):
            return None

    target_genres = genres_of(target)
    target_artist = slug(target.get("artista") or "")
    same, others = [], []
    for a in albums:
        if a.get("key") == target.get("key"):
            continue
        if target_artist and slug(a.get("artista") or "") == target_artist:
            same.append(a)
            continue
        ag = genres_of(a)
        score = 3.0 * len(target_genres & ag)
        extra_t, extra_a = target_genres - ag, ag - target_genres
        for x in extra_t:
            score += 1.5 * max((related(x, y) for y in extra_a), default=0.0)
            score += 1.0 * max((len(set(x.split()) & set(y.split())) / max(len(x.split()), len(y.split()))
                                for y in extra_a), default=0.0)
        ty, ay = year(target), year(a)
        if score > 0 and ty and ay and abs(ty - ay) <= 3:
            score += 0.5
        if score >= 1.0:
            others.append((score, a))

    same.sort(key=lambda a: (year(a) or 0), reverse=True)
    others.sort(key=lambda t: (-t[0], (t[1].get("title") or "").lower()))
    return same[:limit], [a for _, a in others[:limit]]
