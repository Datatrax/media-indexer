"""Miniaturas ligeras: las listas y tarjetas usan una versión chica (unos 480 px) y solo el visor/lightbox
carga la original. Se generan bajo demanda la primera vez que se piden y quedan en disco."""
import shutil
from pathlib import Path

SMALL_SIDE = 480


def small_path(original: Path) -> Path:
    return original.with_name(original.stem + ".s.jpg")


def make_small(src: Path, dst: Path, max_side: int = SMALL_SIDE, quality: int = 82) -> bool:
    """Crea la versión chica de `src` en `dst`. Devuelve False si no se pudo (p. ej. sin Pillow)."""
    try:
        from PIL import Image
    except ImportError:
        return False
    try:
        with Image.open(src) as img:
            if max(img.size) <= max_side and src.stat().st_size < 60_000:
                shutil.copyfile(src, dst)             # ya es chica: no se recomprime
                return True
            img = img.convert("RGB") if img.mode not in ("RGB", "L") else img
            img.thumbnail((max_side, max_side))
            tmp = dst.with_suffix(".tmp")
            img.save(tmp, "JPEG", quality=quality, optimize=True, progressive=True)
            tmp.replace(dst)
        return True
    except Exception:
        return False


def ensure_small(original: Path) -> Path:
    """Ruta de la miniatura chica (la crea si falta o quedó vieja); si no se puede, devuelve la original."""
    dst = small_path(original)
    try:
        if not dst.is_file() or dst.stat().st_mtime < original.stat().st_mtime:
            if not make_small(original, dst):
                return original
        return dst
    except OSError:
        return original
