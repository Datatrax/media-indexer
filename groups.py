"""Agrupación de archivos en series, películas, álbumes y artistas."""
import re
from collections import defaultdict
from typing import Dict, List

from captions import clean_text


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


def build_groups(items: List[Dict]) -> List[Dict]:
    items = [dict(i) for i in items]   # copias: no se toca el catálogo original
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
