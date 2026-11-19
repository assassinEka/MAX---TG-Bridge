"""Минимальный QR-кодер (byte mode, EC=L, version 1..5) + рендер через PIL.

Внешних пакетов нет: только stdlib. Для картинки нужен Pillow (импорт
ленивый — без него мост работает, QR-вход просто недоступен).
"""

from __future__ import annotations

from pathlib import Path

# (data codewords, ecc codewords) для EC level L, version 1..5, 1 block.
CAPACITY = {
    1: (19, 7),
    2: (34, 10),
    3: (55, 15),
    4: (80, 20),
    5: (108, 26),
}


def _gf_mul(a: int, b: int) -> int:
    r = 0
    while b:
        if b & 1:
            r ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x11D
        b >>= 1
    return r


def _rs_generator(degree: int) -> list[int]:
    poly = [1]
    root = 1
    for _ in range(degree):
        nxt = [0] * (len(poly) + 1)
        for i, c in enumerate(poly):
            nxt[i] ^= c
            nxt[i + 1] ^= _gf_mul(c, root)
        poly = nxt
        root = _gf_mul(root, 2)
    return poly


def _rs_encode(data: list[int], ecc_len: int) -> list[int]:
    gen = _rs_generator(ecc_len)
    rem = [0] * ecc_len
    for byte in data:
        factor = byte ^ rem[0]
        rem = rem[1:] + [0]
        for i in range(ecc_len):
            rem[i] ^= _gf_mul(gen[i + 1], factor)
    return rem


def _bit_buffer(data: bytes, version: int) -> list[int]:
    bits: list[int] = []
    bits.extend((0b0100 >> i) & 1 for i in range(3, -1, -1))
    bits.extend((len(data) >> i) & 1 for i in range(7, -1, -1))
    for byte in data:
        bits.extend((byte >> i) & 1 for i in range(7, -1, -1))
    data_cap = CAPACITY[version][0] * 8
    bits.append(0)
    while len(bits) % 8:
        bits.append(0)
    codewords = [int("".join(map(str, bits[i:i + 8])), 2)
                 for i in range(0, len(bits), 8)]
    data_words = CAPACITY[version][0]
    while len(codewords) < data_words:
        codewords.append(0xEC if len(codewords) % 2 == 0 else 0x11)
    out: list[int] = []
    for byte in codewords:
        out.extend((byte >> i) & 1 for i in range(7, -1, -1))
    return out


def _blank(size: int) -> tuple[list[list[int]], list[list[int]]]:
    mods = [[0] * size for _ in range(size)]
    reserved = [[False] * size for _ in range(size)]
    return mods, reserved


def _finder(mods: list[list[int]], reserved: list[list[int]], r: int, c: int) -> None:
    for dr in range(-1, 8):
        for dc in range(-1, 8):
            rr, cc = r + dr, c + dc
            if 0 <= rr < len(mods) and 0 <= cc < len(mods):
                in_ring = dr in (0, 6) or dc in (0, 6)
                in_core = 2 <= dr <= 4 and 2 <= dc <= 4
                mods[rr][cc] = 1 if (in_ring or in_core) else 0
                reserved[rr][cc] = True


def _format_bits(mask: int) -> list[int]:
    # EC level L = 0b01
    value = (0b01 << 3) | mask
    rem = value << 10
    for i in range(14, 9, -1):
        if rem & (1 << i):
            rem ^= 0x537 << (i - 10)
    bits = ((value << 10) | rem) ^ 0x5412
    return [(bits >> i) & 1 for i in range(14, -1, -1)]


def make_matrix(data: str | bytes) -> list[list[int]]:
    data = data.encode("utf-8") if isinstance(data, str) else data
    version = next((v for v, (cap, _) in CAPACITY.items()
                    if len(data) + 2 <= cap), None)
    if version is None:
        raise ValueError("строка слишком длинная для QR v1..5")
    data_cap, ecc_len = CAPACITY[version]
    all_bits = _bit_buffer(data, version)
    codewords = [int("".join(map(str, all_bits[i:i + 8])), 2)
                 for i in range(0, len(all_bits), 8)]
    ecc = _rs_encode(codewords, ecc_len)
    bits = all_bits + [int(b) for byte in ecc for b in f"{byte:08b}"]

    size = 17 + 4 * version
    mods, reserved = _blank(size)
    _finder(mods, reserved, 0, 0)
    _finder(mods, reserved, 0, size - 7)
    _finder(mods, reserved, size - 7, 0)

    for i in range(8, size - 8):
        mods[6][i] = reserved[6][i] = 1 if i % 2 == 0 else 0
        mods[i][6] = reserved[i][6] = 1 if i % 2 == 0 else 0

    align = {2: (18, 18), 3: (22, 22), 4: (26, 26), 5: (30, 30)}[version]
    for dr in range(-2, 3):
        for dc in range(-2, 3):
            rr, cc = align[0] + dr, align[1] + dc
            mods[rr][cc] = 1 if max(abs(dr), abs(dc)) != 1 else 0
            reserved[rr][cc] = True

    fmt = _format_bits(0)
    for i in range(6):
        mods[8][i] = fmt[i]
        reserved[8][i] = True
    mods[8][7] = fmt[6]
    mods[8][8] = fmt[7]
    mods[7][8] = fmt[8]
    reserved[8][7] = reserved[8][8] = reserved[7][8] = True
    # вертикальная часть format info в левом верхнем углу (биты 9..14)
    for i in range(9, 15):
        mods[14 - i][8] = fmt[i]
        reserved[14 - i][8] = True
    for i in range(8):
        mods[size - 1 - i][8] = fmt[i]
        reserved[size - 1 - i][8] = True
    for i in range(8, 15):
        mods[8][size - 15 + i] = fmt[i]
        reserved[8][size - 15 + i] = True
    mods[size - 8][8] = 1
    reserved[size - 8][8] = True

    idx = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for c in (col, col - 1):
                if reserved[row][c]:
                    continue
                mods[row][c] = bits[idx] if idx < len(bits) else 0
                idx += 1
        upward = not upward
        col -= 2

    # mask 0: (row + col) % 2 == 0
    for row in range(size):
        for col_i in range(size):
            if not reserved[row][col_i] and (row + col_i) % 2 == 0:
                mods[row][col_i] ^= 1
    return mods


def make_png(data: str | bytes, path: Path, scale: int = 8,
             border: int = 4) -> Path:
    try:
        from PIL import Image
    except ImportError as exc:  # Pillow — необязательная зависимость (QR-вход)
        raise RuntimeError("для QR-картинки нужен Pillow: pip install Pillow") from exc
    matrix = make_matrix(data)
    size = (len(matrix) + border * 2) * scale
    img = Image.new("L", (size, size), 255)
    px = img.load()
    for r, row in enumerate(matrix):
        for c, value in enumerate(row):
            if value:
                for dy in range(scale):
                    for dx in range(scale):
                        px[(c + border) * scale + dx, (r + border) * scale + dy] = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path
