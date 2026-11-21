"""Pruebas rápidas (sin Telegram): python -m pytest tests  (o ejecutar este archivo directamente)."""
import os, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from captions import extract_info, parse_caption
from db import Database
from library import Library


def raw(i, filename, caption="", mime="audio/mpeg", photo=False, thumb=False, size=5_000_000):
    return {"id": i, "filename": filename, "mime": mime, "size": size, "duration": 200, "is_photo": photo,
            "caption": caption, "date": "2025-01-01T00:00:00", "stamp": "100", "link": f"https://t.me/c/1/{i}",
            "thumb_src": thumb, "thumb": thumb}


SAMPLE = [
    raw(10, "photo_10.jpg", "Tipo: Artista\n> Nombre: DIIV\nDesde: 2011\nGénero: Dream pop\nBiografía: Banda de Brooklyn.\nSegunda línea.", "image/jpeg", True, True),
    raw(20, "photo_20.jpg", "Tipo: Álbum\nÁlbum: Oshin\nArtista: DIIV\nAño: 2012-06-26\nSello: Captured Tracks", "image/jpeg", True, True),
    raw(21, "01 - Druun.mp3"), raw(22, "DIIV - Past Lives.mp3"), raw(23, "03. Air Conditioning.mp3"),
    raw(30, "Sora S01E01.mkv", "Nombre: Sora no Otoshimono\nTipo: Serie", "video/x-matroska"),
    raw(31, "Sora S01E02.mkv", "Nombre: Sora no Otoshimono\nTipo: Serie", "video/x-matroska"),
]


def make_lib(tmp):
    lib = Library(Database(Path(tmp) / "c.db"), Path(tmp) / "thumbs")
    (Path(tmp) / "thumbs").mkdir(exist_ok=True)
    for r in SAMPLE:
        lib.upsert(r, commit=False)
    lib.flush()
    return lib


def test_caption_bio_and_keys():
    p = parse_caption("> Biografía: línea uno\nlínea dos\nAño: 2012-06-26")
    assert p["sinopsis"] == "línea uno\nlínea dos" and p["year"] == "2012"


def test_thumbnail_url_only_when_downloaded():
    assert extract_info(raw(1, "a.mp3"))["thumbnail"] is None
    assert extract_info(raw(2, "photo_2.jpg", "Álbum: X\nArtista: Y", "image/jpeg", True, True))["thumbnail"] == "/thumb/2?v=100"


def test_groups():
    with tempfile.TemporaryDirectory() as tmp:
        g = {x["title"]: x for x in make_lib(tmp).groups()}
        assert [e["title"] for e in g["Oshin"]["episodes"]] == ["Druun", "Past Lives", "Air Conditioning"]
        assert g["Oshin"]["year"] == "2012" and g["Oshin"]["thumbnail"] == "/thumb/20?v=100"
        assert g["DIIV"]["is_artist"] and "Segunda línea" in g["DIIV"]["sinopsis"]
        assert len(g["Sora no Otoshimono"]["episodes"]) == 2


def test_cache_roundtrip_and_reinterpretation():
    with tempfile.TemporaryDirectory() as tmp:
        make_lib(tmp)
        lib2 = Library(Database(Path(tmp) / "c.db"), Path(tmp) / "thumbs")
        assert lib2.load() == len(SAMPLE)
        assert {x["title"] for x in lib2.groups()} >= {"Oshin", "DIIV"}


def test_delete_removes_item_and_thumb_and_invalidates_cache():
    with tempfile.TemporaryDirectory() as tmp:
        lib = make_lib(tmp)
        (Path(tmp) / "thumbs" / "20.jpg").write_bytes(b"x")
        before = lib.groups()
        assert lib.delete([20, 999]) == 1
        assert not (Path(tmp) / "thumbs" / "20.jpg").exists()
        assert lib.groups() is not before
        assert "Oshin" not in {x["title"] for x in lib.groups()}


def test_groups_cache_reused_until_change():
    with tempfile.TemporaryDirectory() as tmp:
        lib = make_lib(tmp)
        assert lib.groups() is lib.groups()


def test_build_groups_does_not_mutate_catalog():
    with tempfile.TemporaryDirectory() as tmp:
        lib = make_lib(tmp)
        lib.groups()
        assert not lib.items[21].get("album")   # la herencia de álbum solo vive en la copia


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
