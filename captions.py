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

    for raw in caption.replace("\r", "").split("\n"):
        line = re.sub(r"[*_`~]", "", raw).strip()
        if not line:
            continue

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

    text = "\n".join(bio) if bio else "\n".join(free)
    if text:
        info["sinopsis"] = text

    if "year" not in info:
        m = re.search(r"\b(19|20)\d{2}\b", caption)
        if m:
            info["year"] = m.group(0)

    return info


def classify(filename: str, mime: str, size: int, caption: str, parsed: Dict) -> str:
    fname = (filename or "").lower()
    cap = (caption or "").lower()
    tipo = parsed.get('tipo', '')

    if any(x in tipo for x in ['película', 'pelicula', 'movie']):
        return 'movies'
    if any(x in tipo for x in ['serie', 'series', 'anime']):
        return 'series'
    if any(x in tipo for x in ['música', 'musica', 'music', 'álbum', 'album', 'artista', 'artist']):
        return 'music'

    if any(fname.endswith(ext) for ext in ['.mp3', '.flac', '.m4a', '.wav', '.ogg']) or (mime and mime.startswith('audio/')):
        return 'music'

    if mime.startswith('image/') and (parsed.get('album') or parsed.get('nombre') or parsed.get('artista')):
        return 'music'

    if re.search(r's\d{1,2}e\d{1,3}|temporada\s*\d+|season\s*\d+', fname + " " + cap, re.I):
        return 'series'

    if any(x in fname for x in ['.mkv', '.mp4', '.avi', '.rar']):
        return 'movies'
    return 'other'


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

    title = re.sub(r'\.[a-z0-9]{2,5}$', '', filename, flags=re.I)
    title = clean_text(title)

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
    else:
        if nombre and not parsed.get("album"):
            parsed["album"] = nombre   # las series/películas se agrupan por "Nombre"

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
        "duration": duration,
        "duration_str": f"{duration // 60}:{duration % 60:02d}" if duration else "",
        "caption": caption,
        "is_photo": is_photo,
        "is_artist": is_artist,
        **parsed
    }

    if category == "music":
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
                if num_match and category in ["series", "movies"]:
                    info["episode"] = int(num_match.group(1))
                    info["title"] = f"Cap {info['episode']}"

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
