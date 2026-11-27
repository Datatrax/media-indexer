"""Lectura de captions y clasificación. No depende de Telegram: trabaja con datos crudos."""
import re
from typing import Dict, Any, List, Optional


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
    "temporada": "season", "season": "season",
    "autor": "autor", "autora": "autor", "author": "autor", "escritor": "autor", "creador": "autor",
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
    blanks = 0

    for raw in caption.replace("\r", "").split("\n"):
        line = re.sub(r"[*_`~]", "", raw).strip()
        if not line:
            # línea en blanco (doble Enter): se conserva como separación entre párrafos (máximo 2 seguidas)
            blanks += 1
            target = bio if capturing else free
            if target and blanks <= 2:
                target.append("")
            continue
        blanks = 0

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

    text = "\n".join(bio if bio else free).strip("\n")
    if text:
        info["sinopsis"] = text

    if "year" not in info:
        m = re.search(r"\b(19|20)\d{2}\b", caption)
        if m:
            info["year"] = m.group(0)

    return info


TIPO_RULES = [   # el orden importa: "audiolibro" antes que "libro", "álbum de fotos" antes que "álbum"...
    ("photos",   ["foto", "photo", "imagen"]),
    ("podcasts", ["podcast", "audiolibro", "audiobook", "audio libro"]),
    ("docs",     ["documental", "documentary"]),
    ("comics",   ["manga", "cómic", "comic", "historieta", "manhwa", "webtoon"]),
    ("books",    ["libro", "book", "novela", "revista"]),
    ("movies",   ["película", "pelicula", "movie", "film"]),
    ("series",   ["serie", "anime", "ova", "dorama"]),
    ("music",    ["música", "musica", "music", "álbum", "album", "artista", "artist", "sencillo", "single"]),
]
BOOK_EXT = {"pdf", "epub", "mobi", "azw", "azw3", "djvu", "fb2"}
COMIC_EXT = {"cbz", "cbr", "cb7"}
AUDIO_EXT = {"mp3", "flac", "m4a", "wav", "ogg", "aac", "opus"}
CATEGORIES = ["series", "movies", "docs", "music", "podcasts", "books", "comics", "photos"]


def classify(filename: str, mime: str, size: int, caption: str, parsed: Dict) -> str:
    fname = (filename or "").lower()
    cap = (caption or "").lower()
    tipo = (parsed.get('tipo') or '').lower()
    m = re.search(r'\.([a-z0-9]{2,5})$', fname)
    ext = m.group(1) if m else ""

    for category, words in TIPO_RULES:
        if tipo and any(w in tipo for w in words):
            return category

    if ext in COMIC_EXT:
        return 'comics'
    if ext in BOOK_EXT:
        return 'books'
    if ext in AUDIO_EXT or (mime and mime.startswith('audio/')):
        return 'music'

    if mime.startswith('image/') and (parsed.get('album') or parsed.get('nombre') or parsed.get('artista')):
        return 'music'

    if re.search(r's\d{1,2}e\d{1,3}|temporada\s*\d+|season\s*\d+', fname + " " + cap, re.I):
        return 'series'

    if any(x in fname for x in ['.mkv', '.mp4', '.avi', '.rar']):
        return 'movies'
    return 'other'


def podcast_fields(filename: str):
    """Título limpio y número de episodio a partir del nombre de archivo de un podcast."""
    title = clean_text(re.sub(r'\.[a-z0-9]{2,5}$', '', filename or '', flags=re.I))
    ep = (re.search(r'(?:ep(?:isodio|isode)?\.?|cap(?:ítulo|itulo)?\.?|#)\s*(\d{1,4})', title, re.I)
          or re.match(r'\s*(\d{1,4})[\s._)-]', title))
    episode = int(ep.group(1)) if ep else None
    cleaned = re.sub(r'^\d{1,4}\s*[._)-]\s*', '', title).strip() or title
    return cleaned, episode


def extract_info(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convierte los datos crudos de un mensaje (los que guarda la BD) en la ficha del catálogo."""
    if not raw:
        return None

    msg_id = raw["id"]
    filename = raw.get("filename") or ""
    mime = raw.get("mime") or ""
    size = raw.get("size") or 0
    duration = raw.get("duration") or 0
    is_photo = bool(raw.get("is_photo"))
    caption = raw.get("caption") or ""
    parsed = parse_caption(caption)
    category = classify(filename, mime, size, caption, parsed)

    is_artist = "artista" in (parsed.get("tipo") or "")
    nombre = parsed.pop("nombre", None)
    season_hint = parsed.pop("season", None)   # "Temporada: 2" escrito en el caption

    title = re.sub(r'\.[a-z0-9]{2,5}$', '', filename, flags=re.I)
    title = clean_text(title)
    m_ext = re.search(r'\.([A-Za-z0-9]{2,5})$', filename)
    ext = m_ext.group(1).lower() if m_ext else ""
    episode_hint = None

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
    elif category in ("books", "comics"):
        if nombre and not parsed.get("album"):
            parsed["album"] = nombre
        vol = re.search(r'[\s._-]*(?:vol(?:umen|ume)?\.?|tomo|tom\.?|#)\s*(\d{1,3})\b', title, re.I)
        cap_n = None if vol else re.search(r'[\s._-]*cap(?:ítulo|itulo)?\.?\s*(\d{1,4})\b', title, re.I)
        found = vol or cap_n
        if found:
            base = title[:found.start()].strip(" -_.")
            if base and not parsed.get("album"):
                parsed["album"] = base
            episode_hint = int(found.group(1))
            label = "Cap" if cap_n else ("Tomo" if category == "comics" else "Vol.")
            title = f"{label} {episode_hint}"
    elif category == "podcasts":
        if nombre and not parsed.get("album"):
            parsed["album"] = nombre
        if not is_photo:
            title, ep_n = podcast_fields(filename)
            if ep_n is not None:
                episode_hint = ep_n
    elif category == "photos":
        parsed["album"] = parsed.get("album") or nombre or "Fotos sin álbum"
    else:
        if nombre and not parsed.get("album"):
            parsed["album"] = nombre   # series, películas y documentales se agrupan por "Nombre"

    info = {
        "id": msg_id,
        "title": title,
        "filename": filename,
        "category": category,
        "size_mb": round(size / (1024 * 1024), 2) if size else 0,
        "mime": mime,
        "date": raw.get("date") or "",
        "link": raw.get("link") or "",
        "thumbnail": f"/thumb/{msg_id}?v={raw.get('stamp', '0')}" if raw.get("thumb") else None,
        "thumb_small": f"/thumb/{msg_id}?v={raw.get('stamp', '0')}&s=1" if raw.get("thumb") else None,
        "duration": duration,
        "duration_str": f"{duration // 60}:{duration % 60:02d}" if duration else "",
        "caption": caption,
        "is_photo": is_photo,
        "is_artist": is_artist,
        "ext": ext,
        **parsed
    }
    if episode_hint is not None:
        info["episode"] = episode_hint

    if category in ("music", "books", "comics", "podcasts", "photos"):
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
                if num_match and category in ["series", "movies", "docs"]:
                    info["episode"] = int(num_match.group(1))
                    info["title"] = f"Cap {info['episode']}"

    if info.get("season") is None and season_hint:
        m = re.search(r"\d+", season_hint)
        if m:
            info["season"] = int(m.group())

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
