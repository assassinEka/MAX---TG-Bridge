"""Кодек MAX: 10-байтный заголовок + msgpack; payload может быть сжат LZ4.

Формат кадра (по JS-клиенту web.max.ru, подтверждено на 490 кадрах из HAR):

    байт 0     0x0A
    байт 1     команда: 0=от нас, 1=от сервера, 3=ошибка
    байты 2-3  seq (int16 BE)
    байты 4-5  opcode (int16 BE)
    байт 6     ratio: 0 = payload как есть, N>0 = LZ4-блок
    байты 7-9  длина payload (uint24 BE)
    с 10       payload: msgpack; при ratio>0 — LZ4 block (без frame-заголовка)

Распаковка LZ4 — это и есть то, что раньше выглядело как «сжатые строки
MAX» и давало мусор в тексте.
"""

from __future__ import annotations

import struct
from typing import Any

MARKER = 0x0A
HEADER_LEN = 10
DIR_SEND = 0
DIR_RECV = 1
CMD_ERR = 3
MINMATCH = 4


class DecodeError(Exception):
    pass


# --- LZ4 block -----------------------------------------------------------------
def lz4_block_decompress(src: bytes, max_out: int) -> bytes:
    dst = bytearray(max_out)
    n_src = len(src)
    i = a = 0
    while i < n_src:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                if i >= n_src:
                    raise DecodeError(f"lz4: обрыв длины литерала @{i}")
                t = src[i]
                i += 1
                lit += t
                if t != 255:
                    break
        if i + lit > n_src or a + lit > max_out:
            raise DecodeError(f"lz4: плохой литерал @{i}")
        if i + lit == n_src:  # последний литерал
            dst[a:a + lit] = src[i:i + lit]
            a += lit
            break
        dst[a:a + lit] = src[i:i + lit]
        i += lit
        a += lit
        if i + 2 > n_src:
            raise DecodeError(f"lz4: обрыв смещения @{i}")
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        if offset == 0 or offset > a:
            raise DecodeError(f"lz4: битое смещение {offset} @{i}")
        mlen = (token & 0x0F) + MINMATCH
        if (token & 0x0F) == 0x0F:
            while True:
                if i >= n_src:
                    raise DecodeError(f"lz4: обрыв длины копии @{i}")
                t = src[i]
                i += 1
                mlen += t
                if t != 255:
                    break
        if a + mlen > max_out:
            raise DecodeError(f"lz4: переполнение @{i}")
        u = a - offset
        if offset >= mlen:
            dst[a:a + mlen] = dst[u:u + mlen]
            a += mlen
        else:
            for _ in range(mlen):
                dst[a] = dst[u]
                a += 1
                u += 1
    return bytes(dst[:a])


def lz4_literals_only(src: bytes) -> bytes:
    """Аварийный разбор: только литералы (когда поток битый)."""
    out = bytearray()
    n = len(src)
    i = 0
    while i < n:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while i < n:
                t = src[i]
                i += 1
                lit += t
                if t != 255:
                    break
        out += src[i:i + lit]
        i += lit
        if i + 2 > n:
            break
        if (src[i] | (src[i + 1] << 8)) == 0:
            break
        i += 2
        if (token & 0x0F) == 0x0F:
            while i < n:
                t = src[i]
                i += 1
                if t != 255:
                    break
    return bytes(out)


# --- msgpack -------------------------------------------------------------------
class Ext:
    """msgpack extension; у MAX type=1 — это int64 (payload = маркер + байты)."""

    __slots__ = ("type", "data")

    def __init__(self, type_: int, data: bytes) -> None:
        self.type = type_
        self.data = data

    def as_int(self) -> int:
        if self.type != 1 or not self.data:
            raise DecodeError(f"ext {self.type} не число")
        signed = self.data[0] in (0xD0, 0xD1, 0xD2, 0xD3)
        return int.from_bytes(self.data[1:], "big", signed=signed)

    def __eq__(self, other: object) -> bool:
        return (isinstance(other, Ext) and other.type == self.type
                and other.data == self.data)

    def __hash__(self) -> int:
        return hash((self.type, self.data))

    def __repr__(self) -> str:
        try:
            return f"Ext({self.type}, {self.as_int()})"
        except DecodeError:
            return f"Ext({self.type}, {self.data.hex()})"


def _need(buf: bytes, i: int, n: int) -> None:
    if i + n > len(buf):
        raise DecodeError(f"msgpack: обрезано {i}+{n}>{len(buf)}")


def unpack(buf: bytes, i: int = 0, depth: int = 0) -> tuple[Any, int]:
    if depth > 64:
        raise DecodeError("msgpack: слишком глубокая вложенность")
    _need(buf, i, 1)
    b = buf[i]
    i += 1
    if b <= 0x7F:
        return b, i
    if b >= 0xE0:
        return b - 0x100, i
    if 0x80 <= b <= 0x8F:
        return _unmap(buf, i, b & 0x0F, depth)
    if 0x90 <= b <= 0x9F:
        return _unarr(buf, i, b & 0x0F, depth)
    if 0xA0 <= b <= 0xBF:
        n = b & 0x1F
        _need(buf, i, n)
        return buf[i:i + n].decode("utf-8", "replace"), i + n
    if b == 0xC0:
        return None, i
    if b == 0xC2:
        return False, i
    if b == 0xC3:
        return True, i
    if b in (0xC4, 0xC5, 0xC6):
        nb = (1, 2, 4)[b - 0xC4]
        _need(buf, i, nb)
        n = int.from_bytes(buf[i:i + nb], "big")
        i += nb
        _need(buf, i, n)
        return buf[i:i + n], i + n
    if b == 0xCA:
        _need(buf, i, 4)
        return struct.unpack(">f", buf[i:i + 4])[0], i + 4
    if b == 0xCB:
        _need(buf, i, 8)
        return struct.unpack(">d", buf[i:i + 8])[0], i + 8
    if b in (0xCC, 0xCD, 0xCE, 0xCF):
        nb = (1, 2, 4, 8)[b - 0xCC]
        _need(buf, i, nb)
        return int.from_bytes(buf[i:i + nb], "big"), i + nb
    if b in (0xD0, 0xD1, 0xD2, 0xD3):
        nb = (1, 2, 4, 8)[b - 0xD0]
        _need(buf, i, nb)
        return int.from_bytes(buf[i:i + nb], "big", signed=True), i + nb
    if b in (0xD9, 0xDA, 0xDB):
        nb = (1, 2, 4)[b - 0xD9]
        _need(buf, i, nb)
        n = int.from_bytes(buf[i:i + nb], "big")
        i += nb
        _need(buf, i, n)
        s = buf[i:i + n]
        try:
            return s.decode("utf-8"), i + n
        except UnicodeDecodeError:
            return s.decode("utf-8", "replace"), i + n
    if b in (0xDC, 0xDD):
        nb = (2, 4)[b - 0xDC]
        _need(buf, i, nb)
        return _unarr(buf, i + nb, int.from_bytes(buf[i:i + nb], "big"), depth)
    if b in (0xDE, 0xDF):
        nb = (2, 4)[b - 0xDE]
        _need(buf, i, nb)
        return _unmap(buf, i + nb, int.from_bytes(buf[i:i + nb], "big"), depth)
    if b in (0xD4, 0xD5, 0xD6, 0xD7, 0xD8):
        n = (1, 2, 4, 8, 16)[b - 0xD4]
        _need(buf, i, 1 + n)
        return Ext(buf[i], buf[i + 1:i + 1 + n]), i + 1 + n
    if b in (0xC7, 0xC8, 0xC9):
        nb = (1, 2, 4)[b - 0xC7]
        _need(buf, i, nb)
        n = int.from_bytes(buf[i:i + nb], "big")
        i += nb
        _need(buf, i, 1 + n)
        return Ext(buf[i], buf[i + 1:i + 1 + n]), i + 1 + n
    raise DecodeError(f"msgpack: маркер 0x{b:02x} @{i - 1}")


def _unarr(buf: bytes, i: int, n: int, depth: int) -> tuple[list, int]:
    out = []
    for _ in range(n):
        v, i = unpack(buf, i, depth + 1)
        out.append(v)
    return out, i


def _unmap(buf: bytes, i: int, n: int, depth: int) -> tuple[dict, int]:
    out: dict = {}
    for _ in range(n):
        k, i = unpack(buf, i, depth + 1)
        v, i = unpack(buf, i, depth + 1)
        out[k if isinstance(k, (str, int)) else repr(k)] = v
    return out, i


# --- сборка --------------------------------------------------------------------
def pack(obj: Any) -> bytes:
    if obj is None:
        return b"\xc0"
    if obj is True:
        return b"\xc3"
    if obj is False:
        return b"\xc2"
    if isinstance(obj, Ext):
        n = len(obj.data)
        if n <= 0xFF:
            head = b"\xc7" + bytes([n])
        elif n <= 0xFFFF:
            head = b"\xc8" + struct.pack(">H", n)
        else:
            head = b"\xc9" + struct.pack(">I", n)
        return head + bytes([obj.type]) + obj.data
    if isinstance(obj, int):
        if 0 <= obj <= 0x7F:
            return bytes([obj])
        if -32 <= obj < 0:
            return bytes([obj & 0xFF])
        if 0 <= obj <= 0xFF:
            return b"\xcc" + bytes([obj])
        if 0 <= obj <= 0xFFFF:
            return b"\xcd" + struct.pack(">H", obj)
        if 0 <= obj <= 0xFFFFFFFF:
            return b"\xce" + struct.pack(">I", obj)
        if 0 <= obj:
            return b"\xcf" + struct.pack(">Q", obj)
        if -0x80 <= obj:
            return b"\xd0" + struct.pack(">b", obj)
        if -0x8000 <= obj:
            return b"\xd1" + struct.pack(">h", obj)
        if -0x80000000 <= obj:
            return b"\xd2" + struct.pack(">i", obj)
        return b"\xd3" + struct.pack(">q", obj)
    if isinstance(obj, str):
        data = obj.encode("utf-8")
        n = len(data)
        if n <= 31:
            return bytes([0xA0 | n]) + data
        if n <= 0xFF:
            return b"\xd9" + bytes([n]) + data
        if n <= 0xFFFF:
            return b"\xda" + struct.pack(">H", n) + data
        return b"\xdb" + struct.pack(">I", n) + data
    if isinstance(obj, (bytes, bytearray)):
        b = bytes(obj)
        n = len(b)
        if n <= 0xFF:
            return b"\xc4" + bytes([n]) + b
        if n <= 0xFFFF:
            return b"\xc5" + struct.pack(">H", n) + b
        return b"\xc6" + struct.pack(">I", n) + b
    if isinstance(obj, (list, tuple)):
        n = len(obj)
        head = bytes([0x90 | n]) if n <= 15 else b"\xdc" + struct.pack(">H", n)
        return head + b"".join(pack(v) for v in obj)
    if isinstance(obj, dict):
        n = len(obj)
        head = bytes([0x80 | n]) if n <= 15 else b"\xde" + struct.pack(">H", n)
        return head + b"".join(pack(k) + pack(v) for k, v in obj.items())
    raise DecodeError(f"не умею упаковать {type(obj)!r}")


def long_ext(value: int, signed: bool = True) -> Ext:
    """MAX «Long»: ext type=1, payload = msgpack-число."""
    if signed and value < 0:
        return Ext(1, b"\xd3" + struct.pack(">q", value))
    if value <= 0xFFFFFFFF:
        return Ext(1, b"\xcf" + struct.pack(">Q", value & 0xFFFFFFFFFFFFFFFF))
    return Ext(1, b"\xcf" + struct.pack(">Q", value))


def frame(op: int, seq: int, payload: dict[str, Any],
          ratio: int = 0) -> bytes:
    """Собрать кадр. По умолчанию без сжатия (ratio=0) — сервер это принимает."""
    body = pack(payload)
    header = (bytes([MARKER, DIR_SEND]) + struct.pack(">HH", seq & 0xFFFF, op)
              + bytes([ratio]) + len(body).to_bytes(3, "big"))
    return header + body


def _normalize(obj: Any) -> Any:
    """MAX-овские «Long» (ext type=1) -> обычный int.

    Сервер отдаёт id/время/chatId как ext-числа. Раньше их разбирал
    эвристический декодер, теперь конвертируем явно — иначе isinstance(x, int)
    везде ломается.
    """
    if isinstance(obj, Ext):
        if obj.type == 1:
            try:
                return obj.as_int()
            except DecodeError:
                return obj
        return obj
    if isinstance(obj, dict):
        return {k: _normalize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize(v) for v in obj]
    return obj


def parse_frame(data: bytes) -> tuple[int, int, int, dict, str | None]:
    """-> (opcode, seq, направление, payload, ошибка-или-None)."""
    if len(data) < HEADER_LEN or data[0] != MARKER:
        return 0, 0, DIR_RECV, {}, "не кадр MAX"
    direction = data[1]
    seq = int.from_bytes(data[2:4], "big", signed=True)
    op = int.from_bytes(data[4:6], "big", signed=True)
    ratio = data[6]
    length = (data[7] << 16) | (data[8] << 8) | data[9]
    if length <= 0:
        return op, seq, direction, {}, None
    body = data[HEADER_LEN:]
    wire = body[:length]
    err: str | None = None
    if len(wire) < length:
        err = f"короткий кадр: {len(wire)} из {length}"
        wire = body
    plain = wire
    if ratio > 0:
        plain = b""
        last: Exception | None = None
        for mult in (ratio, ratio * 2, ratio * 4, ratio * 8):
            try:
                plain = lz4_block_decompress(wire, max(length, 64) * mult)
                break
            except DecodeError as exc:
                last = exc
        else:
            plain = lz4_literals_only(wire)
            if err is None:
                err = f"lz4: {last}"
    try:
        value, end = unpack(plain, 0)
    except DecodeError as exc:
        return op, seq, direction, {}, str(exc)
    if end != len(plain) and err is None:
        err = f"msgpack: разобрано {end} из {len(plain)}"
    if not isinstance(value, dict):
        value = {"value": value}
    return op, seq, direction, _normalize(value), err