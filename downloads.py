"""Nombres de archivo para los zips: una carpeta con el nombre del álbum y las pistas numeradas en orden."""
import re
import unicodedata
from typing import Dict, List, Tuple
from urllib.parse import quote

_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(text: str, default: str = "Descarga", limit: int = 80) -> str:
    t = _BAD.sub(" ", text or "")
    t = re.sub(r"\s+", " ", t).strip(" .")
    return (t[:limit].strip(" .")) or default


def archive_plan(group: Dict, sizes: Dict[int, int]) -> Tuple[str, List[Tuple[str, int, int]]]:
    """Devuelve (nombre de carpeta, [(ruta dentro del zip, tamaño, id del mensaje)])."""
    folder = safe_name(group.get("title") or "Descarga")
    eps = group["episodes"]
    width = max(2, len(str(len(eps))))
    plan, used = [], set()
    for i, ep in enumerate(eps, 1):
        filename = ep.get("filename") or f"{ep['id']}.bin"
        stem, dot, ext = filename.rpartition(".")
        if not dot:
            stem, ext = filename, ""
        stem = re.sub(r"^\d{1,3}\s*[-._)]\s*", "", stem).strip() or stem     # evita "01 - 01 - Canción"
        name = f"{safe_name(f'{i:0{width}d} - {stem}', default=str(i), limit=120)}" + (f".{safe_name(ext, 'bin', 8)}" if ext else "")
        path = f"{folder}/{name}"
        while path in used:
            path = f"{folder}/{i:0{width}d}b - {name}"
        used.add(path)
        plan.append((path, int(sizes[ep["id"]]), ep["id"]))
    return folder, plan


def content_disposition(filename: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode() or "descarga.zip"
    ascii_name = ascii_name.replace('"', "").replace("\\", "")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
