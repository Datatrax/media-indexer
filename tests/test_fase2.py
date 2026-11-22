"""Pruebas de secciones nuevas, búsqueda, favoritos, "continuar", metadatos y plantillas."""
import asyncio, re, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jinja2
import meta
from captions import extract_info
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
                    recent=[], cont=[], fav_groups=[], favs=favs, seen_ids={11}, section="home", q="", page=1, total_pages=1, qs="", all_years=["2012"], all_genres=["Acción"],
                    all_studios=[], all_artists=["DIIV"], sort="title", year="", genero="", estudio="", artista="", album="", view="artists")

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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
