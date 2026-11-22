"""ZIP en streaming: se arma al vuelo mientras llegan los archivos de Telegram, sin guardar nada en memoria ni
en disco. Los archivos van sin comprimir (el audio y el video ya vienen comprimidos), así que el tamaño final se
conoce de antemano y el navegador puede mostrar el progreso. Si todo pasa de 4 GB usa ZIP64 automáticamente."""
import struct
import time
import zlib
from typing import AsyncIterator, Callable, List, Optional

LIMIT32 = 0xFFFFFFFF


class Entry:
    def __init__(self, name: str, size: int, opener: Callable[[], AsyncIterator[bytes]]):
        self.name, self.size, self.opener = name, size, opener
        self.nb = name.encode("utf-8")


def _dos_datetime(ts: Optional[float] = None):
    t = time.localtime(ts or time.time())
    return ((t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2),
            ((max(t.tm_year, 1980) - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday)


def _local_header(e: Entry, z64: bool, tm: int, dt: int) -> bytes:
    extra = struct.pack("<HHQQ", 1, 16, 0, 0) if z64 else b""
    big = LIMIT32 if z64 else 0
    return struct.pack("<IHHHHHIIIHH", 0x04034B50, 45 if z64 else 20, 0x0808, 0, tm, dt, 0, big, big,
                       len(e.nb), len(extra)) + e.nb + extra


def _descriptor(crc: int, size: int, z64: bool) -> bytes:
    return struct.pack("<IIQQ" if z64 else "<IIII", 0x08074B50, crc, size, size)


def _central_header(e: Entry, crc: int, size: int, offset: int, z64: bool, tm: int, dt: int) -> bytes:
    if z64:
        extra = struct.pack("<HHQQQ", 1, 24, size, size, offset)
        head = struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 45, 45, 0x0808, 0, tm, dt, crc, LIMIT32, LIMIT32,
                           len(e.nb), len(extra), 0, 0, 0, 0, LIMIT32)
    else:
        extra = b""
        head = struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, 0x0808, 0, tm, dt, crc, size, size,
                           len(e.nb), 0, 0, 0, 0, 0, offset)
    return head + e.nb + extra


def _end_records(count: int, cd_size: int, cd_offset: int, z64: bool) -> bytes:
    if not z64:
        return struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, count, count, cd_size, cd_offset, 0)
    z_eocd = struct.pack("<IQHHIIQQQQ", 0x06064B50, 44, 45, 45, 0, 0, count, count, cd_size, cd_offset)
    locator = struct.pack("<IIQI", 0x07064B50, 0, cd_offset + cd_size, 1)
    eocd = struct.pack("<IHHHHIIH", 0x06054B50, 0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF, LIMIT32, LIMIT32, 0)
    return z_eocd + locator + eocd


def total_length(entries: List[Entry], z64: Optional[bool] = None) -> int:
    """Tamaño exacto del zip resultante (para el encabezado Content-Length)."""
    if z64 is None:
        z64 = needs_zip64(entries)
    total = 0
    for e in entries:
        local = 30 + len(e.nb) + (20 if z64 else 0)
        central = 46 + len(e.nb) + (28 if z64 else 0)
        total += local + e.size + (24 if z64 else 16) + central
    return total + ((56 + 20 + 22) if z64 else 22)


def needs_zip64(entries: List[Entry]) -> bool:
    return total_length(entries, z64=False) >= LIMIT32 - 4096


async def stream(entries: List[Entry], z64: Optional[bool] = None):
    if z64 is None:
        z64 = needs_zip64(entries)
    tm, dt = _dos_datetime()
    offset, central = 0, []
    for e in entries:
        start = offset
        head = _local_header(e, z64, tm, dt)
        yield head
        offset += len(head)
        crc = n = 0
        async for chunk in e.opener():
            crc = zlib.crc32(chunk, crc)
            n += len(chunk)
            if n > e.size:
                raise ValueError(f"{e.name}: llegaron más bytes de los esperados")
            offset += len(chunk)
            yield chunk
        if n != e.size:
            raise ValueError(f"{e.name}: llegaron {n} bytes y se esperaban {e.size}")   # corta la descarga en vez de entregar un zip dañado
        desc = _descriptor(crc, n, z64)
        yield desc
        offset += len(desc)
        central.append(_central_header(e, crc, n, start, z64, tm, dt))
    cd = b"".join(central)
    yield cd
    yield _end_records(len(entries), len(cd), offset, z64)
