"""Catálogo en memoria (respaldado por SQLite) con caché de grupos."""
from pathlib import Path
from typing import Dict, Iterable, List

from captions import extract_info
from db import Database
from groups import build_groups, add_synthetic_artists


class Library:
    def __init__(self, db: Database, thumb_dir: Path):
        self.db = db
        self.thumb_dir = Path(thumb_dir)
        self.raws: Dict[int, Dict] = {}    # datos crudos por id de mensaje
        self.items: Dict[int, Dict] = {}   # fichas interpretadas
        self.version = 0
        self._groups: List[Dict] = []
        self._groups_version = -1

    # ---- carga / escritura ----
    def load(self) -> int:
        for raw in self.db.load_all():
            self._set(raw)
        self.version += 1
        return len(self.items)

    def _set(self, raw: Dict) -> None:
        info = extract_info(raw)
        if info:
            self.raws[raw["id"]] = raw
            self.items[raw["id"]] = info

    def upsert(self, raw: Dict, commit: bool = True) -> None:
        self.db.upsert(raw, commit=commit)
        self._set(raw)
        self.version += 1

    def delete(self, ids: Iterable[int]) -> int:
        ids = [i for i in ids if i in self.raws]
        if not ids:
            return 0
        self.db.delete(ids)
        for i in ids:
            self.raws.pop(i, None)
            self.items.pop(i, None)
            try:
                (self.thumb_dir / f"{int(i)}.jpg").unlink()
            except FileNotFoundError:
                pass
        self.version += 1
        return len(ids)

    def flush(self) -> None:
        self.db.commit()

    def bump_last_id(self, msg_id: int) -> None:
        if msg_id > int(self.db.get_meta("last_id") or 0):
            self.db.set_meta("last_id", msg_id)

    # ---- consulta ----
    @property
    def total(self) -> int:
        return len(self.items)

    def groups(self) -> List[Dict]:
        """Grupos (series, películas, álbumes, artistas). Se recalculan solo si el catálogo cambió."""
        if self._groups_version != self.version:
            self._groups = add_synthetic_artists(build_groups(list(self.items.values())))
            self._groups_version = self.version
        return self._groups
