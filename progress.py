"""Progreso de lectura y escucha: textos para mostrar y regla para saber cuándo algo ya se terminó."""
from typing import Dict


def fmt_time(seconds: float) -> str:
    s = max(0, int(seconds or 0))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def kind_of(item: Dict) -> str:
    """'audio' (segundos), 'pages' (PDF/CBZ) o 'epub' (porcentaje)."""
    ext = (item.get("ext") or "").lower()
    if ext == "epub":
        return "epub"
    if ext in ("pdf", "cbz", "cbr", "cb7"):
        return "pages"
    return "audio"


def is_finished(item: Dict, pos: float, dur: float) -> bool:
    if not dur or dur <= 0:
        return False
    kind = kind_of(item)
    if kind == "audio":
        return dur - pos <= 15 or pos / dur >= 0.98
    if kind == "pages":
        return pos >= dur
    return pos >= 99


def view(item: Dict, row: Dict) -> Dict:
    kind = kind_of(item)
    pos, dur = float(row["pos"]), float(row["dur"])
    if kind == "audio":
        label = f"{fmt_time(pos)} de {fmt_time(dur)}"
    elif kind == "pages":
        label = f"pág. {int(pos)} de {int(dur)}"
    else:
        label = f"{int(pos)}%"
    pct = max(0, min(100, round(pos / dur * 100))) if dur else 0
    return {"pos": pos, "dur": dur, "at": row["at"], "label": label, "pct": pct, "extra": row.get("extra") or ""}


def build_map(items: Dict[int, Dict], rows: Dict[int, Dict]) -> Dict[int, Dict]:
    return {i: view(items[i], r) for i, r in rows.items() if i in items}
