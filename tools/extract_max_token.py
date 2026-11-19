#!/usr/bin/env python3
"""Вытащить токен MAX из HAR-файла (DevTools -> Network -> Save all as HAR).

Автономный скрипт: только stdlib, никакой связи с ботом.

Использование:
    python max_token.py web.max.ru.har
    python max_token.py web.max.ru.har --all          # все найденные
    python max_token.py web.max.ru.har --save secrets/max_token

Токен MAX постоянный: он привязан к «устройству» и меняется только если
отозвать его в приложении MAX -> Устройства. Перелогины его не меняют,
поэтому один удачный HAR можно использовать долго.

HAR можно просто перетащить на файл скрипта в Проводнике Windows.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

FRAME_MAGIC = 0x0A
MAX_AUTH_OP = 0x0013


def lz4_decompress(src: bytes, limit: int = 64 * 1024 * 1024) -> bytes:
    """LZ4 block format (без заголовка). Нужен, т.к. MAX жмёт payload."""
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        literals = token >> 4
        if literals:
            if i + literals > n:
                raise ValueError("lz4: literals за границей блока")
            out += src[i:i + literals]
            i += literals
        if i >= n:
            break
        if i + 2 > n:
            raise ValueError("lz4: нет offset")
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        if offset == 0:
            raise ValueError("lz4: нулевой offset")
        match = token & 0x0F
        if match == 15:
            while True:
                if i >= n:
                    raise ValueError("lz4: обрыв длины match")
                b = src[i]
                i += 1
                match += b
                if b != 255:
                    break
        match += 4
        start = len(out) - offset
        if start < 0:
            raise ValueError("lz4: offset больше вывода")
        for _ in range(match):
            out.append(out[start])
            start += 1
        if len(out) > limit:
            raise ValueError("lz4: слишком большой вывод")
    return bytes(out)


def frame_payload(raw: bytes) -> tuple[int, bytes]:
    """Вернуть (opcode, payload) кадра MAX (заголовок 10 байт)."""
    if len(raw) < 10 or raw[0] != FRAME_MAGIC:
        return -1, raw
    op = int.from_bytes(raw[4:6], "big")
    ratio = raw[6]
    length = int.from_bytes(raw[7:10], "big")
    body = raw[10:10 + length]
    if ratio == 0:
        return op, body
    # ratio > 0 -> LZ4; точная длина вывода не передаётся, пробуем коэффициенты
    for mult in (ratio, ratio * 2, ratio * 4, ratio * 8):
        try:
            return op, lz4_decompress(body, max(length * mult, length * 2))
        except Exception:
            continue
    return op, body  # как есть — вдруг токен лежит открытым текстом


def _mp_str(buf: bytes, pos: int) -> str | None:
    """Прочитать msgpack-строку по позиции pos."""
    if pos >= len(buf):
        return None
    m = buf[pos]
    if 0xA0 <= m <= 0xBF:
        size, start = m & 0x1F, pos + 1
    elif m == 0xD9:
        if pos + 2 > len(buf):
            return None
        size, start = buf[pos + 1], pos + 2
    elif m == 0xDA:
        if pos + 3 > len(buf):
            return None
        size, start = int.from_bytes(buf[pos + 1:pos + 3], "big"), pos + 3
    elif m == 0xDB:
        if pos + 5 > len(buf):
            return None
        size, start = int.from_bytes(buf[pos + 1:pos + 5], "big"), pos + 5
    else:
        return None
    if start + size > len(buf):
        return None
    return buf[start:start + size].decode("ascii", "replace")


def token_from_payload(payload: bytes) -> list[str]:
    """Найти все значения поля token в payload кадра."""
    found: list[str] = []
    pos = 0
    while True:
        idx = payload.find(b"token", pos)
        if idx < 0:
            break
        pos = idx + 5
        value = _mp_str(payload, idx + 5)
        if not value:
            continue
        value = value.strip()
        # токен MAX — длинная base64url-подобная строка
        if len(value) < 40 or len(value) > 4000:
            continue
        if any(ch not in ("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
                          "0123456789_-+/=.") for ch in value):
            continue
        found.append(value)
    return found


def iter_ws_messages(har: dict):
    """Пройтись по всем WS-сообщениям HAR (Chrome/Firefox/Edge)."""
    entries = (har.get("log") or {}).get("entries") or []
    for entry in entries:
        messages = (entry.get("_webSocketMessages")
                    or entry.get("webSocketMessages") or [])
        for msg in messages:
            yield entry, msg


def decode_ws_data(msg: dict) -> bytes | None:
    data = msg.get("data")
    if not isinstance(data, str):
        return None
    if msg.get("opcode") == 1 and msg.get("type") in ("send", "receive"):
        # текстовый кадр — не наш случай
        return None
    try:
        return base64.b64decode(data + "=" * (-len(data) % 4))
    except Exception:
        return None


def extract(path: Path) -> list[tuple[float, str]]:
    har = json.loads(path.read_text(encoding="utf-8-sig"))
    result: list[tuple[float, str]] = []
    for entry, msg in iter_ws_messages(har):
        if str(msg.get("type", "send")).lower() != "send":
            continue
        raw = decode_ws_data(msg)
        if not raw:
            continue
        op, payload = frame_payload(raw)
        if op != MAX_AUTH_OP:
            continue
        ts = 0.0
        started = (entry.get("startedDateTime") or "").replace("Z", "+00:00")
        if started:
            try:
                from datetime import datetime
                ts = datetime.fromisoformat(started).timestamp()
            except Exception:
                ts = 0.0
        for token in token_from_payload(payload):
            result.append((ts, token))
    result.sort(key=lambda item: item[0])
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Вытащить токен MAX из HAR (WS-кадр 0x0013).")
    parser.add_argument("har", help="путь к .har файлу")
    parser.add_argument("--all", action="store_true",
                        help="показать все найденные токены, не только последний")
    parser.add_argument("--save", metavar="FILE",
                        help="сохранить токен в файл (права 600)")
    args = parser.parse_args()

    path = Path(args.har)
    if not path.exists():
        print(f"нет файла: {path}", file=sys.stderr)
        return 2

    tokens = extract(path)
    if not tokens:
        print("Токен не найден.\n"
              "Проверь: HAR снят на https://web.max.ru после входа, "
              "в Network включён Preserve log, есть кадры WS (0x0013).",
              file=sys.stderr)
        return 1

    if args.all:
        for ts, token in tokens:
            print(f"{len(token):5d}  {token}")
    else:
        token = tokens[-1][1]
        print(f"Токен MAX ({len(token)} символов):")
        print(token)

    if args.save:
        out = Path(args.save)
        out.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(tokens[-1][1].strip() + "\n")
        print(f"\nСохранено: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())