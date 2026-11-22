# Cómo escribir los captions

Todos los captions usan `Campo: valor`, uno por línea. Puedes usar la función de **cita** de Telegram.
Lo único obligatorio para las secciones nuevas es `Tipo:`.

| Contenido | Caption de la portada (foto) o del archivo |
|---|---|
| Serie / anime | `Tipo: Serie` `Nombre:` `Año:` `Género:` `Estudio:` `Sinopsis:` (+ `Temporada: 2` si hace falta) |
| Película | `Tipo: Película` `Nombre:` `Año:` `Género:` `Sinopsis:` |
| Documental | `Tipo: Documental` `Nombre:` `Año:` `Género:` `Sinopsis:` |
| Música: artista | `Tipo: Artista` `Nombre:` `Desde:` `Género:` `Biografía:` (varias líneas) |
| Música: álbum | `Tipo: Álbum` `Álbum:` `Artista:` `Año:` `Género:` `Sello:` |
| Podcast / audiolibro | `Tipo: Podcast` (o `Audiolibro`) `Nombre:` `Autor:` `Sinopsis:` |
| Libro | `Tipo: Libro` `Nombre:` `Autor:` `Año:` `Género:` `Sinopsis:` |
| Manga / cómic | `Tipo: Manga` (o `Cómic`) `Nombre:` `Autor:` `Año:` `Género:` `Sinopsis:` |
| Fotos | `Tipo: Fotos` `Álbum:` (en cada foto, o en la primera) |

## Cómo se agrupan los archivos
- **Portada + archivos:** sube la foto con los datos y, justo después, los archivos. Los que no tengan
  `Nombre:` heredan el de la portada (libros, cómics y podcasts; la música enlaza por artista).
- **Tomos:** nombres como `One Piece - Tomo 03.cbz` o `Vol 3` se ordenan solos.
- **Podcasts:** `Ep 12 - Título.mp3` se ordena por número de episodio.
- Sin `Tipo:`, los `.pdf/.epub` van a Libros, los `.cbz` a Cómics y los audios a Música.
- `Fotos`: la portada de cada álbum de fotos es la primera foto.
