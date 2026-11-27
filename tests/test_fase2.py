"""Pruebas de secciones nuevas, búsqueda, favoritos, "continuar", metadatos y plantillas."""
import asyncio, json, re, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jinja2
import meta
from captions import extract_info, parse_caption
from db import Database
from groups import continue_list
from library import Library
from search import search, fold


def raw(i, filename, caption="", mime="application/octet-stream", photo=False, thumb=False, date="2025-01-01T00:00:00"):
    return {"id": i, "filename": filename, "mime": mime, "size": 3_000_000, "duration": 0, "is_photo": photo,
            "caption": caption, "date": date, "stamp": "7", "link": f"https://t.me/c/1/{i}", "thumb_src": thumb, "thumb": thumb}


SAMPLE = [
    # manga con volúmenes sin "Nombre"
    raw(1, "One Piece - Tomo 01.cbz", "Tipo: Manga"), raw(2, "One Piece - Tomo 02.cbz", "Tipo: Manga"),
    # libro con portada y archivo
    raw(3, "photo_3.jpg", "Tipo: Libro\nNombre: Dune\nAutor: Frank Herbert\nAño: 1965", "image/jpeg", True, True),
    raw(4, "dune_final.epub"),
    # podcast: portada + episodios de audio sin caption
    raw(5, "photo_5.jpg", "Tipo: Podcast\nNombre: Historia en voz alta", "image/jpeg", True, True),
    raw(6, "Ep 2 - La caída.mp3", "", "audio/mpeg"), raw(7, "Ep 1 - El inicio.mp3", "", "audio/mpeg"),
    # fotos
    raw(8, "photo_8.jpg", "Tipo: Fotos\nÁlbum: Viaje 2024", "image/jpeg", True, True),
    raw(9, "photo_9.jpg", "Tipo: Fotos\nÁlbum: Viaje 2024", "image/jpeg", True, True),
    # documental y series
    raw(10, "Cosmos S01E01.mkv", "Tipo: Documental\nNombre: Cosmos", "video/x-matroska", date="2025-03-01T00:00:00"),
    raw(11, "High School DxD - 01.mkv", "Tipo: Serie\nNombre: High School DxD", "video/x-matroska"),
    raw(12, "High School DxD - 02.mkv", "Tipo: Serie\nNombre: High School DxD", "video/x-matroska"),
    raw(13, "High School DxD - 03.mkv", "Tipo: Serie\nNombre: High School DxD", "video/x-matroska"),
    # música (sigue funcionando)
    raw(20, "photo_20.jpg", "Tipo: Álbum\nÁlbum: Oshin\nArtista: DIIV\nAño: 2012", "image/jpeg", True, True),
    raw(21, "01 - Druun.mp3", "", "audio/mpeg"),
]


def lib_with(tmp, rows=SAMPLE):
    lib = Library(Database(Path(tmp) / "c.db"), Path(tmp) / "thumbs")
    for r in rows:
        lib.upsert(r, commit=False)
    lib.flush()
    return lib


def by_title(lib):
    return {g["title"]: g for g in lib.groups()}


def test_new_categories_and_grouping():
    with tempfile.TemporaryDirectory() as tmp:
        g = by_title(lib_with(tmp))
        assert g["One Piece"]["final_category"] == "comics" and [e["title"] for e in g["One Piece"]["episodes"]] == ["Tomo 1", "Tomo 2"]
        assert g["Dune"]["final_category"] == "books" and g["Dune"]["autor"] == "Frank Herbert" and len(g["Dune"]["episodes"]) == 1
        assert g["Dune"]["episodes"][0]["ext"] == "epub"                      # el archivo heredó el nombre de la portada
        pod = g["Historia en voz alta"]
        assert pod["final_category"] == "podcasts" and [e["episode"] for e in pod["episodes"]] == [1, 2]   # ordenados por episodio
        assert [p["id"] for p in pod["playlist"]] == [7, 6]
        assert g["Viaje 2024"]["final_category"] == "photos" and len(g["Viaje 2024"]["episodes"]) == 2
        assert g["Cosmos"]["final_category"] == "docs" and 1 in g["Cosmos"]["seasons"]
        assert g["Oshin"]["final_category"] == "music" and g["Oshin"]["episodes"][0]["title"] == "Druun"
        assert len({x["key"] for x in lib_with(tmp).groups() if x.get("key")}) == len([x for x in lib_with(tmp).groups() if x.get("key")])


def test_keys_and_hrefs():
    with tempfile.TemporaryDirectory() as tmp:
        g = by_title(lib_with(tmp))
        assert g["Dune"]["key"] == "books::dune" and "section=books" in g["Dune"]["href"]
        assert g["Oshin"]["href"].startswith("?section=music&album=Oshin")
        assert g["High School DxD"]["key"].startswith("series|")


def test_search_accents_typos_and_compact():
    with tempfile.TemporaryDirectory() as tmp:
        groups = lib_with(tmp).groups()
        titles = lambda q: [g["title"] for g in search(groups, q)]
        assert fold("Película Ñandú") == "pelicula nandu"
        assert "High School DxD" in titles("highschool dxd")      # junto
        assert "High School DxD" in titles("high scool")          # error de escritura
        assert "Historia en voz alta" in titles("HISTORIA voz")
        assert "Dune" in titles("herbert")                        # por autor
        assert titles("zzzzqq") == []


def test_favorites_and_seen_and_continue():
    with tempfile.TemporaryDirectory() as tmp:
        lib = lib_with(tmp)
        key = by_title(lib)["Dune"]["key"]
        assert lib.db.toggle_favorite(key) is True and key in lib.db.favorites()
        assert lib.db.toggle_favorite(key) is False and not lib.db.favorites()

        g = by_title(lib)["High School DxD"]
        eps = g["seasons"][1]
        lib.db.mark_seen(eps[0]["id"], g["key"])
        groups = lib.groups()
        by_key = {x["key"]: x for x in groups if x.get("key")}
        cont = continue_list(by_key, lib.db.seen_map())
        assert cont and cont[0]["ep"]["id"] == eps[1]["id"]               # siguiente sin ver
        for e in eps:
            lib.db.mark_seen(e["id"], g["key"])
        assert continue_list(by_key, lib.db.seen_map()) == []             # todo visto: no aparece
        lib.db.unmark_seen(eps[2]["id"])
        assert continue_list(by_key, lib.db.seen_map())[0]["ep"]["id"] == eps[2]["id"]


def test_meta_fills_only_empty_fields_and_never_overwrites():
    with tempfile.TemporaryDirectory() as tmp:
        rows = SAMPLE + [raw(30, "Naruto - 01.mkv", "Tipo: Serie\nNombre: Naruto\nAño: 2002", "video/x-matroska")]
        lib = lib_with(tmp, rows)
        calls = []

        def fake_lookup(category, title, author=None, tmdb_key=None):
            calls.append(title)
            return {"year": "1999", "genero": "Acción", "sinopsis": "Un ninja.", "poster": "https://x/p.jpg"}
        meta.lookup = fake_lookup
        n = asyncio.run(meta.enrich_pending(lib, delay=0))
        assert n > 0 and "Naruto" in calls
        naruto = by_title(lib)["Naruto"]
        assert naruto["year"] == "2002"                                   # el caption manda
        assert naruto["sinopsis"] == "Un ninja." and naruto["thumbnail"] == "https://x/p.jpg" and naruto["auto_meta"]
        before = len(calls)
        asyncio.run(meta.enrich_pending(lib, delay=0))
        assert len(calls) == before                                       # lo ya consultado se cachea
        lib2 = Library(Database(Path(tmp) / "c.db"), Path(tmp) / "thumbs"); lib2.load()
        assert by_title(lib2)["Naruto"]["sinopsis"] == "Un ninja."        # y sobrevive a reinicios


def test_meta_failure_is_not_cached():
    with tempfile.TemporaryDirectory() as tmp:
        lib = lib_with(tmp, [raw(30, "Naruto - 01.mkv", "Tipo: Serie\nNombre: Naruto", "video/x-matroska")])
        def boom(*a, **k): raise OSError("sin red")
        meta.lookup = boom
        assert asyncio.run(meta.enrich_pending(lib, delay=0)) == 0 and not lib.meta


def test_templates_render_every_view():
    with tempfile.TemporaryDirectory() as tmp:
        lib = lib_with(tmp)
        groups = lib.groups()
        env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).resolve().parent.parent / "templates")), autoescape=True)
        tpl = env.get_template("index.html")
        favs = {by_title(lib)["Dune"]["key"]}
        base = dict(request=None, items=[], albums_list=[], eps_list=[], artist_profile=None, total=lib.total, found=0, counts={}, nav=[{"key": "home", "label": "Inicio", "count": None}],
                    recent=[], cont=[], fav_groups=[], favs=favs, seen_ids={11}, section="home", q="", page=1, total_pages=1, qs="", all_years=[("2012", 1)], all_genres=[("Acción", 2)],
                    all_studios=[], all_artists=[("DIIV", 1)], furl=lambda n, v: "?section=x&" + n + "=" + v, active_filters=[], clear_url="?section=x", focus=False, section_label="", sort="title", year="", genero="", estudio="", artista="", album="", view="artists")

        def render(**kw): return tpl.render(**{**base, **kw})
        cat = lambda c: [g for g in groups if g["final_category"] == c and not g.get("is_artist")]

        html = render(section="home", recent=[{"key": "books", "label": "Libros", "groups": cat("books")}], fav_groups=cat("books"),
                      cont=continue_list({g["key"]: g for g in groups if g.get("key")}, {11: (by_title(lib)["High School DxD"]["key"], 1)}))
        assert "agregado recientemente" in html and "Continuar" in html and "Siguiente:" in html and "fav-btn on" in html

        for section in ("series", "movies", "docs", "podcasts", "books", "comics", "photos", "music", "favs"):
            items = cat(section) if section not in ("favs",) else cat("books")
            html = render(section=section, items=items, found=len(items), nav=[{"key": section, "label": section, "count": 1}])
            assert "Biblioteca de Prozac" in html
        assert 'class="photo"' in render(section="photos", items=cat("photos"))
        assert "Leer" in render(section="books", items=cat("books")) and "/read/4?key=" in render(section="books", items=cat("books"))
        assert "Tomo 1" in render(section="comics", items=cat("comics"))
        assert "album-btn" in render(section="podcasts", items=cat("podcasts"))
        assert 'class="seen' in render(section="series", items=cat("series")) or "seen" in render(section="series", items=cat("series"))
        assert "Aún no tienes favoritos" in render(section="favs", items=[])

        oshin = by_title(lib)["Oshin"]
        assert "track-btn" in render(section="music", album="Oshin", artista="DIIV", items=[oshin])
        assert "Álbumes" in render(section="music", artista="DIIV", items=[oshin], albums_list=[oshin])

        read = env.get_template("read.html")
        assert "ePub(" in read.render(item={"id": 4, "title": "Dune"}, ext="epub", key="books::dune")
        assert "JSZip" in read.render(item={"id": 1, "title": "One Piece"}, ext="cbz", key="")



def test_similar_albums():
    from groups import similar_albums
    def alb(key, title, artist, genres, year):
        return {"key": key, "title": title, "artista": artist, "genero": genres, "year": year}
    oshin = alb("m::oshin", "Oshin", "DIIV", "dream pop, shoegaze", "2012")
    albums = [oshin,
              alb("m::deceiver", "Deceiver", "DIIV", "indie rock", "2019"),
              alb("m::boiled", "Boiled Alive", "DIIV", "", "2022"),
              alb("m::souvlaki", "Souvlaki", "Slowdive", "shoegaze, dream pop", "1993"),
              alb("m::depression", "Depression Cherry", "Beach House", "dream pop", "2015"),
              alb("m::wall", "The Wall", "Pink Floyd", "progressive rock", "1979"),
              alb("m::master", "Master of Puppets", "Metallica", "thrash metal", "1986"),
              alb("m::loveless", "Loveless", "My Bloody Valentine", "noise pop", "1991"),
              alb("m::pet", "Pet Sounds", "The Beach Boys", "pop", "1966")]
    same, other = similar_albums(oshin, albums, {"slowdive": "shoegaze"})
    assert [a["title"] for a in same] == ["Boiled Alive", "Deceiver"]          # mismo artista, más nuevo primero
    titles = [a["title"] for a in other]
    assert titles[:2] == ["Souvlaki", "Depression Cherry"]                     # mismos géneros primero
    assert "Master of Puppets" not in titles and "The Wall" not in titles and "Oshin" not in titles
    assert "Pet Sounds" not in titles                                          # una palabra suelta ("pop") no basta


def test_playlist_has_album_name():
    with tempfile.TemporaryDirectory() as tmp:
        g = by_title(lib_with(tmp))
        assert g["Oshin"]["playlist"][0]["album"] == "Oshin"


def _zip_roundtrip(z64):
    import io, zipfile, zlib
    import zipstream
    files = {"Oshin/01 - Druun.mp3": b"a" * 70000 + b"\x00\xff", "Oshin/02 - Past Lives.mp3": b"hola" * 5, "Oshin/03 - Ñandú ✓.mp3": b""}

    def opener(data):
        async def gen():
            for i in range(0, len(data), 30000):
                yield data[i:i + 30000]
        return gen
    entries = [zipstream.Entry(n, len(d), opener(d)) for n, d in files.items()]

    async def collect():
        out = bytearray()
        async for c in zipstream.stream(entries, z64):
            out += c
        return bytes(out)
    data = asyncio.run(collect())
    assert len(data) == zipstream.total_length(entries, z64)                     # Content-Length exacto
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert zf.testzip() is None
        assert zf.namelist() == list(files)                                      # carpeta + orden + nombres con acentos
        for n, d in files.items():
            assert zf.read(n) == d


def test_zipstream_plain_and_zip64():
    _zip_roundtrip(False)
    _zip_roundtrip(True)


def test_zipstream_aborts_on_wrong_size():
    import zipstream
    async def gen():
        yield b"123"
    e = zipstream.Entry("x/y.bin", 5, lambda: gen())
    async def run():
        async for _ in zipstream.stream([e], False):
            pass
    try:
        asyncio.run(run()); assert False, "debía fallar"
    except ValueError:
        pass


def test_archive_plan_names():
    from downloads import archive_plan, safe_name, content_disposition
    g = {"title": 'Live: "At" The/Murmrr?', "episodes": [
        {"id": 1, "filename": "01 - Druun.mp3"}, {"id": 2, "filename": "DIIV - Past Lives.mp3"}, {"id": 3, "filename": "Past Lives.mp3"}]}
    folder, plan = archive_plan(g, {1: 10, 2: 20, 3: 30})
    assert folder == "Live At The Murmrr"
    assert [p[0] for p in plan] == ["Live At The Murmrr/01 - Druun.mp3", "Live At The Murmrr/02 - DIIV - Past Lives.mp3", "Live At The Murmrr/03 - Past Lives.mp3"]
    assert [p[1] for p in plan] == [10, 20, 30]
    assert safe_name("///") == "Descarga"
    assert "filename*=UTF-8''" in content_disposition("Ñandú.zip")


def test_human_size_and_total_bytes():
    from library import human_size
    assert human_size(0) == "0 B" and human_size(1536) == "1,5 KB" and human_size(5 * 1024 ** 3) == "5,0 GB"
    with tempfile.TemporaryDirectory() as tmp:
        lib = lib_with(tmp)
        assert lib.total_bytes == sum(r["size"] for r in SAMPLE)


def test_synopsis_keeps_blank_lines():
    cap = "Tipo: Serie\nNombre: X\nSinopsis: Primer párrafo.\nSigue el primero.\n\n\nSegundo párrafo.\n\n\n\n\nTercero.\nAño: 2019"
    p = parse_caption(cap)
    assert p["sinopsis"] == "Primer párrafo.\nSigue el primero.\n\n\nSegundo párrafo.\n\n\nTercero."      # se respeta el doble Enter (máx. 2 vacías)
    assert p["year"] == "2019"
    assert parse_caption("Tipo: Serie\nSinopsis: Uno\n\nDos")["sinopsis"] == "Uno\n\nDos"
    assert parse_caption("Sinopsis: Solo\n\n\n")["sinopsis"] == "Solo"                                     # sin vacías al final


def test_filters_return_counts():
    from groups import get_filters_from_groups
    with tempfile.TemporaryDirectory() as tmp:
        lib = lib_with(tmp, [
            raw(1, "p.jpg", "Tipo: Álbum\nÁlbum: A\nArtista: DIIV\nAño: 2012\nGénero: dream pop, shoegaze", "image/jpeg", True, True), raw(2, "01.mp3", "", "audio/mpeg"),
            raw(3, "p.jpg", "Tipo: Álbum\nÁlbum: B\nArtista: DIIV\nAño: 2019\nGénero: shoegaze", "image/jpeg", True, True), raw(4, "01.mp3", "", "audio/mpeg"),
        ])
        f = get_filters_from_groups(lib.groups(), "music")
        assert f["genres"] == [("dream pop", 1), ("shoegaze", 2)]
        assert f["years"] == [("2019", 1), ("2012", 1)] and f["artists"] == [("DIIV", 2)]


def test_group_href_points_to_that_exact_item():
    from urllib.parse import parse_qs
    with tempfile.TemporaryDirectory() as tmp:
        g = by_title(lib_with(tmp))["High School DxD"]
        q = parse_qs(g["href"].lstrip("?"))
        assert q["section"] == ["series"] and q["key"] == [g["key"]] and "q" not in q


def test_small_thumbnails():
    import os
    from PIL import Image
    from thumbs import ensure_small, small_path
    with tempfile.TemporaryDirectory() as tmp:
        big = Path(tmp) / "7.jpg"
        Image.effect_noise((1280, 1920), 80).convert("RGB").save(big, "JPEG", quality=95)
        small = ensure_small(big)
        assert small == small_path(big) and small.is_file()
        w, h = Image.open(small).size
        assert max(w, h) <= 480 and abs(h / w - 1.5) < 0.02                       # misma proporción
        assert small.stat().st_size < big.stat().st_size / 4                       # bastante más ligera
        mt = small.stat().st_mtime
        assert ensure_small(big) == small and small.stat().st_mtime == mt          # no se rehace si no cambió
        os.utime(big, (mt + 100, mt + 100))
        ensure_small(big); assert small.stat().st_mtime > mt                       # si la original cambia, se rehace
        broken = Path(tmp) / "8.jpg"; broken.write_bytes(b"no soy una imagen")
        assert ensure_small(broken) == broken                                      # imagen dañada: se usa la original


def test_group_exposes_small_thumbnail():
    with tempfile.TemporaryDirectory() as tmp:
        g = by_title(lib_with(tmp))["Oshin"]
        assert g["thumbnail"] == "/thumb/20?v=7" and g["thumb_small"] == "/thumb/20?v=7&s=1"


class _FakeFile:
    def __init__(self, name): self.name = name


class _FakeMsg:
    def __init__(self, id, name, data): self.id, self.file, self.data = id, _FakeFile(name) if name else None, data


class _FakeTelegram:
    """Imita lo mínimo de TelegramClient que usa el respaldo (Mensajes guardados)."""
    def __init__(self): self.msgs, self.next = [], 1; self.deleted = []
    async def iter_messages(self, who, limit=None):
        assert who == "me"
        for m in sorted(self.msgs, key=lambda m: -m.id): yield m
    async def send_file(self, who, f, caption=None, force_document=False):
        assert who == "me" and force_document and f.name == "biblioteca-respaldo.json"
        self.msgs.append(_FakeMsg(self.next, f.name, f.getvalue())); self.next += 1
    async def download_media(self, msg, file=bytes): return msg.data
    async def delete_messages(self, who, ids):
        self.deleted += ids; self.msgs = [m for m in self.msgs if m.id not in ids]


def test_backup_export_import_roundtrip_and_merge():
    import backup
    with tempfile.TemporaryDirectory() as tmp:
        a = Database(Path(tmp) / "a.db")
        a.toggle_favorite("books::dune"); a.mark_seen(5, "series|x"); a.set_progress(7, "podcasts::p", 30.5, 600, "", at=1000)
        a.set_meta_cache("series|naruto", {"year": "2002"})
        data = backup.export_data(a)
        b = Database(Path(tmp) / "b.db")
        assert backup.import_data(b, json.loads(json.dumps(data))) == {"favoritos": 1, "vistos": 1, "progreso": 1, "datos_automaticos": 1}
        assert b.favorites().keys() == {"books::dune"} and b.progress_map()[7]["pos"] == 30.5 and b.load_meta_cache()["series|naruto"]["data"] == {"year": "2002"}
        # fusionar: gana lo más reciente y nunca se borra nada
        b.set_progress(7, "podcasts::p", 99, 600, "", at=2000)
        b.toggle_favorite("music::oshin")
        backup.import_data(b, data)
        assert b.progress_map()[7]["pos"] == 99 and "music::oshin" in b.favorites()
        assert backup.digest(data) == backup.digest({**data, "exported_at": 1})


def test_backup_rejects_bad_files():
    import backup
    for bad in [[], {"app": "otra"}, {"app": backup.APP, "format": 99}, {"app": backup.APP, "format": 1, "seen": [{"id": "x"}]},
                {"app": backup.APP, "format": 1, "favorites": [{"key": "", "added": 1}]}, {"app": backup.APP, "format": 1, "progress": [{"id": 1, "key": "k", "pos": -5, "dur": 1, "at": 1}]}]:
        try:
            backup.parse_backup(bad); assert False, bad
        except ValueError:
            pass


def test_telegram_backup_restores_when_empty_and_uploads_changes():
    import backup
    with tempfile.TemporaryDirectory() as tmp:
        tg = _FakeTelegram()
        db1 = Database(Path(tmp) / "1.db"); db1.toggle_favorite("books::dune"); db1.mark_seen(5, "k")
        t1 = backup.TelegramBackup(tg, db1, keep=2)
        assert asyncio.run(t1.restore_if_empty()) is None                      # no hay respaldo todavía, y sus datos no están vacíos
        assert asyncio.run(t1.upload_if_changed()) is False                    # primero fija lo que ya tiene; nada cambió
        db1.toggle_favorite("music::oshin")
        assert asyncio.run(t1.upload_if_changed()) is True and len(tg.msgs) == 1
        assert asyncio.run(t1.upload_if_changed()) is False                    # sin cambios: no sube nada
        # servidor nuevo sin disco: arranca vacío y se recupera solo
        db2 = Database(Path(tmp) / "2.db"); t2 = backup.TelegramBackup(tg, db2)
        assert asyncio.run(t2.restore_if_empty())["favoritos"] == 2 and set(db2.favorites()) == {"books::dune", "music::oshin"}
        assert asyncio.run(t2.upload_if_changed()) is False                    # lo restaurado no se vuelve a subir
        # se conservan solo los 2 respaldos más nuevos
        for i in range(3):
            db1.toggle_favorite(f"k{i}"); asyncio.run(t1.upload_if_changed())
        assert len(tg.msgs) == 2 and max(m.id for m in tg.msgs) == 4
        # un respaldo dañado como el más nuevo no impide usar el anterior
        tg.msgs.append(_FakeMsg(99, "biblioteca-respaldo.json", b"no es json"))
        db3 = Database(Path(tmp) / "3.db")
        assert asyncio.run(backup.TelegramBackup(tg, db3).restore_if_empty())["favoritos"] >= 2


def test_progress_rules_and_labels():
    import progress as P
    audio, pdf, epub = {"ext": "mp3"}, {"ext": "pdf"}, {"ext": "epub"}
    assert P.fmt_time(65) == "1:05" and P.fmt_time(3725) == "1:02:05" and P.fmt_time(None) == "0:00"
    assert not P.is_finished(audio, 100, 600) and P.is_finished(audio, 590, 600) and P.is_finished(audio, 600, 600)
    assert not P.is_finished(pdf, 4, 5) and P.is_finished(pdf, 5, 5)
    assert not P.is_finished(epub, 50, 100) and P.is_finished(epub, 99, 100)
    assert not P.is_finished(audio, 5, 0)                                       # sin duración conocida nunca se da por terminado
    row = {"pos": 90, "dur": 600, "at": 5, "extra": ""}
    assert P.view(audio, row)["label"] == "1:30 de 10:00" and P.view(audio, row)["pct"] == 15
    assert P.view(pdf, {"pos": 3, "dur": 5, "at": 1})["label"] == "pág. 3 de 5"
    assert P.view(epub, {"pos": 35.4, "dur": 100, "at": 1})["label"] == "35%"


def test_continue_offers_unfinished_item_itself():
    from groups import continue_list
    with tempfile.TemporaryDirectory() as tmp:
        lib = lib_with(tmp, [raw(1, "p.jpg", "Tipo: Podcast\nNombre: P", "image/jpeg", True, True),
                             raw(2, "Ep 1 - a.mp3", "", "audio/mpeg"), raw(3, "Ep 2 - b.mp3", "", "audio/mpeg")])
        g = by_title(lib)["P"]
        by_key = {x["key"]: x for x in lib.groups() if x.get("key")}
        seen = {2: (g["key"], 10)}
        assert continue_list(by_key, seen)[0]["ep"]["id"] == 3                       # terminado: sigue el siguiente
        assert continue_list(by_key, seen, unfinished={2: {}})[0]["ep"]["id"] == 2   # a medias: aparece él mismo


def test_diagnostics_reports_what_needs_fixing():
    import diagnostics
    with tempfile.TemporaryDirectory() as tmp:
        lib = lib_with(tmp, SAMPLE + [raw(50, "algo.xyz", "Tipo: Cosa rara"), raw(51, "dup.mkv", "Tipo: Serie\nNombre: D", "video/x-matroska"),
                                      raw(52, "dup.mkv", "Tipo: Serie\nNombre: D", "video/x-matroska"),
                                      raw(60, "p.jpg", "Tipo: Álbum\nÁlbum: Fantasma\nArtista: Nadie", "image/jpeg", True, True)])
        d = diagnostics.run(lib, {}, Path(tmp), Path(tmp) / "thumbs", Path(tmp) / "c.db")
        ids = {p["id"]: p for p in d["problems"]}
        assert [r["title"] for r in ids["sin_clasificar"]["rows"]] == ["algo.xyz"]
        assert [r["title"] for r in ids["portadas_huerfanas"]["rows"]] == ["Fantasma"]
        assert ids["repetidos"]["count"] == 1 and "sin_portada" in ids
        assert d["problems"][0]["severity"] == "alto" and d["summary"]["files"] == lib.total


def test_similar_albums_still_available():
    from groups import similar_albums
    a = {"key": "m::a", "title": "A", "artista": "X", "genero": "dream pop, shoegaze", "year": "2012"}
    b = {"key": "m::b", "title": "B", "artista": "Y", "genero": "shoegaze", "year": "2013"}
    assert similar_albums(a, [a, b])[1] == [b]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
