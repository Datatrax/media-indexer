"""Diagnóstico de la biblioteca: qué quedó sin clasificar, sin portada, sin datos, repetido..."""
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional

from groups import human_size, slug

CATEGORY_LABELS = {"series": "Series", "movies": "Películas", "docs": "Documentales", "music": "Álbumes",
                   "podcasts": "Podcasts", "books": "Libros", "comics": "Cómics", "photos": "Álbumes de fotos"}
LIMIT = 40


def _dir_size(path: Optional[Path]) -> int:
    if not path or not Path(path).exists():
        return 0
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _first_link(g: Dict) -> str:
    eps = g.get("episodes") or []
    return eps[0].get("link", "") if eps else ""


def _problem(pid: str, title: str, hint: str, severity: str, rows: List[Dict]) -> Optional[Dict]:
    if not rows:
        return None
    return {"id": pid, "title": title, "hint": hint, "severity": severity,
            "count": len(rows), "rows": rows[:LIMIT], "hidden": max(0, len(rows) - LIMIT)}


def run(lib, sync_state: Optional[Dict] = None, data_dir: Optional[Path] = None,
        thumb_dir: Optional[Path] = None, db_path: Optional[Path] = None) -> Dict:
    groups = lib.groups()
    real = [g for g in groups if not g.get("is_artist")]
    items = list(lib.items.values())
    problems: List[Optional[Dict]] = []

    # --- archivos que no se pudieron clasificar ---
    other = [i for i in items if i["category"] == "other" and not i.get("is_photo")]
    problems.append(_problem(
        "sin_clasificar", "Archivos sin clasificar",
        "Agrega «Tipo:» (Serie, Película, Documental, Álbum, Podcast, Libro, Manga…) al caption del mensaje en Telegram.", "alto",
        [{"title": i["filename"], "detail": f"{i['size_mb']} MB" + (f" · Tipo: {i['tipo']}" if i.get("tipo") else ""), "link": i.get("link", "")} for i in other]))
    photos = [i for i in items if i["category"] == "other" and i.get("is_photo")]
    problems.append(_problem(
        "fotos_sueltas", "Fotos sin «Tipo:»",
        "Si son portadas, agrega «Tipo:» y «Nombre:» (o «Álbum:» y «Artista:»). Si son fotos tuyas, usa «Tipo: Fotos» y «Álbum:».", "info",
        [{"title": i["filename"], "detail": (i.get("caption") or "sin caption")[:80].replace("\n", " "), "link": i.get("link", "")} for i in photos]))

    # --- música ---
    music = [g for g in real if g["final_category"] == "music"]
    problems.append(_problem(
        "pistas_sin_album", "Pistas sin álbum",
        "Sube la portada del álbum (con «Álbum:» y «Artista:») justo antes de sus pistas, o agrega «Álbum:» al caption de cada pista.", "alto",
        [{"title": f"{g.get('artista') or 'sin artista'} · {g['title']}", "detail": f"{len(g['episodes'])} pista(s)", "link": _first_link(g)} for g in music if not g.get("album")]))
    problems.append(_problem(
        "albumes_sin_artista", "Álbumes sin artista", "Agrega «Artista:» a la portada del álbum.", "medio",
        [{"title": g["title"], "detail": f"{len(g['episodes'])} pistas", "link": _first_link(g)} for g in music if g.get("album") and not g.get("artista")]))
    artists = [g for g in groups if g.get("is_artist")]
    problems.append(_problem(
        "artistas_sin_perfil", "Artistas sin mensaje de perfil",
        "Publica una foto con «Tipo: Artista», «Nombre:», «Desde:», «Género:» y «Biografía:».", "info",
        [{"title": g["title"], "detail": "se muestra con la portada de uno de sus álbumes", "link": ""} for g in artists if g.get("synthetic")]))
    problems.append(_problem(
        "artistas_incompletos", "Perfiles de artista sin foto o sin biografía", "Edita el mensaje de perfil del artista.", "info",
        [{"title": g["title"], "detail": ("sin foto" if not g.get("thumbnail") else "sin biografía"), "link": ""}
         for g in artists if not g.get("synthetic") and (not g.get("thumbnail") or not g.get("sinopsis"))]))

    # --- datos que faltan, por tipo de contenido ---
    def missing(field, cats):
        return [g for g in real if g["final_category"] in cats and not g.get(field)]
    row = lambda g, extra="": {"title": g["title"], "detail": (CATEGORY_LABELS.get(g["final_category"], "") + extra), "link": _first_link(g)}
    all_cats = ("series", "movies", "docs", "music", "podcasts", "books", "comics")
    problems.append(_problem("sin_portada", "Sin portada", "Sube una foto con el mismo «Nombre:» (o «Álbum:» y «Artista:») justo antes de los archivos.", "medio",
                             [row(g) for g in real if g["final_category"] != "photos" and not g.get("thumbnail")]))
    problems.append(_problem("sin_anio", "Sin año", "Agrega «Año:» al caption.", "info", [row(g) for g in missing("year", all_cats)]))
    problems.append(_problem("sin_genero", "Sin género", "Agrega «Género:» al caption.", "info", [row(g) for g in missing("genero", all_cats)]))
    problems.append(_problem("sin_sinopsis", "Sin sinopsis", "Agrega «Sinopsis:» (o «Descripción:») al caption. Con AUTO_META=1 se completa sola para anime, libros y cómics.", "info",
                             [row(g) for g in missing("sinopsis", ("series", "movies", "docs", "books", "comics", "podcasts"))]))

    # --- series con capítulos sin número ---
    problems.append(_problem(
        "sin_numero", "Capítulos sin número (aparecen en «Otros archivos»)",
        "Incluye el número en el nombre del archivo (S01E03, «Cap 3») o «Temporada:» en el caption.", "medio",
        [{"title": g["title"], "detail": f"{len(g['parts'])} archivo(s)", "link": (g["parts"][0].get("link", "") if g["parts"] else "")}
         for g in real if g["final_category"] in ("series", "docs") and g.get("parts")]))

    # --- portadas que no encontraron sus archivos ---
    keys = {g.get("key") for g in groups}
    orphan = []
    for i in items:
        if i.get("is_photo") and i["category"] in ("music", "books", "comics", "podcasts") and i.get("album") and not i.get("is_artist"):
            if f"{i['category']}::{slug(i['album'])}" not in keys:
                orphan.append({"title": i["album"], "detail": f"portada de {CATEGORY_LABELS.get(i['category'], i['category'])} sin archivos", "link": i.get("link", "")})
    problems.append(_problem("portadas_huerfanas", "Portadas sin archivos",
                             "Sube los archivos justo después de la portada, o repite el mismo «Nombre:»/«Álbum:» en sus captions.", "medio", orphan))

    # --- miniaturas que fallaron ---
    failed = [{"title": lib.items[i]["filename"], "detail": "no se pudo bajar la miniatura", "link": lib.items[i].get("link", "")}
              for i, r in lib.raws.items() if r.get("thumb_src") and not r.get("thumb") and i in lib.items]
    problems.append(_problem("miniaturas_fallidas", "Miniaturas que no se pudieron descargar",
                             "Se reintentan solas en la próxima sincronización completa (botón de arriba).", "medio", failed))

    # --- archivos repetidos ---
    seen_files = defaultdict(list)
    for i in items:
        if not i.get("is_photo") and i.get("size_mb"):
            seen_files[(i["filename"].lower(), i["size_mb"])].append(i)
    dup = [{"title": v[0]["filename"], "detail": f"{len(v)} copias · {v[0]['size_mb']} MB", "link": v[0].get("link", "")} for v in seen_files.values() if len(v) > 1]
    problems.append(_problem("repetidos", "Archivos repetidos", "Mismo nombre y mismo tamaño: borra las copias del canal para ahorrar espacio.", "info", dup))

    problems = [p for p in problems if p]
    order = {"alto": 0, "medio": 1, "info": 2}
    problems.sort(key=lambda p: (order[p["severity"]], -p["count"]))

    cats = Counter(g["final_category"] for g in real)
    summary = {
        "files": lib.total, "size": human_size(lib.total_bytes), "groups": len(real),
        "by_category": [(CATEGORY_LABELS[c], cats[c]) for c in CATEGORY_LABELS if cats[c]],
        "artists": len(artists), "thumbs": len(list(Path(thumb_dir).glob("*.jpg"))) if thumb_dir and Path(thumb_dir).exists() else 0,
        "db_size": human_size(os.path.getsize(db_path)) if db_path and Path(db_path).exists() else "—",
        "data_size": human_size(_dir_size(data_dir)),
        "meta_entries": len(getattr(lib, "meta", {})),
    }
    return {"summary": summary, "problems": problems, "sync": dict(sync_state or {})}
