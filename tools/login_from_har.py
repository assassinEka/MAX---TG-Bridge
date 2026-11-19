#!/usr/bin/env python3
"""Вход в MAX по токену из свежего HAR (путь B09, подтверждённый разведкой).

Сними HAR после входа в https://web.max.ru на десктопе:
  DevTools -> Network -> Preserve log -> WS -> кадр 0x0013 (send)
  -> правой кнопкой по HAR -> Save all as HAR.

Запуск: python tools/login_from_har.py path/to/fresh.har
Скрипт найдёт последний токен в отправленных 0x0013, проверит его новым
WSS (clientHello + 0x0013 -> profile) и только после этого сохранит в
secrets/max_token с правами 600. Токен никуда не печатается.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge.config import Config  # noqa: E402
from bridge.login import MaxLogin, save_token  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("login_from_har")


def extract_token(har_path: Path) -> str:
    har = json.loads(har_path.read_text(encoding="utf-8"))
    candidates: list[tuple[float, str]] = []
    for entry in har.get("log", {}).get("entries", []):
        for msg in entry.get("_webSocketMessages", []):
            if msg.get("type") != "send":
                continue
            data = msg.get("data", "")
            raw = base64.b64decode(data + "=" * (-len(data) % 4))
            if len(raw) < 10 or int.from_bytes(raw[4:6], "big") != 0x0013:
                continue
            body = raw[10:]
            pos = body.find(b"token")
            if pos < 0:
                continue
            marker = body[pos + 5]
            if marker not in (0xD9, 0xDA, 0xDB):
                continue
            nb = {0xD9: 1, 0xDA: 2, 0xDB: 4}[marker]
            n = int.from_bytes(body[pos + 6:pos + 6 + nb], "big")
            token = body[pos + 6 + nb:pos + 6 + nb + n].decode("ascii", "replace")
            if len(token) >= 100:
                candidates.append((float(msg.get("time", 0)), token))
    if not candidates:
        raise RuntimeError("в HAR нет отправленного кадра 0x0013 с токеном")
    return max(candidates)[1]


async def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python tools/login_from_har.py <fresh.har>")
    cfg = Config.load("config.json")
    token = extract_token(Path(sys.argv[1]))
    log.info("токен найден, длина %d — проверяю через MAX WSS", len(token))
    login = MaxLogin(cfg)
    try:
        if not await login.validate_token(token):
            raise RuntimeError("MAX отверг токен — сними свежий HAR после входа в web.max.ru")
        save_token(cfg.max_token_file, token)
        log.info("токен сохранён: %s", cfg.max_token_file)
    finally:
        await login.close()


if __name__ == "__main__":
    asyncio.run(main())
