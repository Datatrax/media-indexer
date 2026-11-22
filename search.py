"""Búsqueda tolerante: ignora acentos y mayúsculas, junta/separa palabras ("highschool" = "high school")
y perdona pequeños errores de escritura."""
import difflib
import re
import unicodedata
from typing import Dict, List


def fold(text: str) -> str:
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def score(group: Dict, tokens: List[str]) -> float:
    title = fold(group.get("title"))
    people = fold(" ".join(str(group.get(f) or "") for f in ("artista", "autor", "album", "estudio", "label")))
    other = fold(" ".join(str(group.get(f) or "") for f in ("genero",)) + " " + (group.get("sinopsis") or "")[:600])
    title_words, people_words = title.split(), people.split()
    compact = "".join(tokens)
    title_compact = title.replace(" ", "")
    people_compact = people.replace(" ", "")

    total = 0.0
    if len(compact) >= 4 and compact in title_compact:
        total += 3          # "highschooldxd" encuentra "High School DxD"
    elif len(compact) >= 4 and compact in people_compact:
        total += 2

    for tok in tokens:
        s = 0.0
        if any(w.startswith(tok) for w in title_words):
            s = 3
        elif tok in title:
            s = 2.5
        elif tok in people:
            s = 2
        elif tok in other:
            s = 1
        elif len(tok) >= 4:
            for w in title_words + people_words:
                if abs(len(w) - len(tok)) <= 2 and difflib.SequenceMatcher(None, tok, w).ratio() >= 0.8:
                    s = 1.5
                    break
        if s == 0 and total < 3:
            return 0.0      # todas las palabras deben coincidir de alguna forma
        total += s
    return total


def search(groups: List[Dict], query: str) -> List[Dict]:
    tokens = fold(query).split()
    if not tokens:
        return list(groups)
    scored = [(score(g, tokens), g) for g in groups]
    scored = [(s, g) for s, g in scored if s > 0]
    scored.sort(key=lambda t: (-t[0], fold(t[1].get("title"))))
    return [g for _, g in scored]
