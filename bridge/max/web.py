"""Неофициальный websocket-клиент веб-версии MAX (скелет).

Реализовано по фактам HAR: заголовок 10 байт, heartbeat 0x0001 каждые 30 c,
авторизация 0x0013 токеном (не в HTTP-заголовках), эхо отправки 0x0040,
входящие 0x0080 + ACK, QR-флоу 0x0120/0x0121/0x0123.

TODO (не выдумывать):
  * точная кодировка int64 в ИСХОДЯЩИХ payload (в дампе ext8(type=1), но
    часть полей идёт паттерном 0xF0 <tag> <8 байт> — семантика неизвестна);
  * точные поля запросов 0x0120/0x0121/0x0123 (наблюдались только ответы);
  * multipart/тело uploadImage и raw-загрузка 0x0052/0x0053.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import random
import string
import struct
import time
import uuid
from pathlib import Path
from typing import Any

import aiohttp

from ..config import Config
from ..models import MaxCaps, MaxEvent, Media, OutboundMessage, QrSession
from . import codec
from .transport import MaxTransport, NeedLogin

log = logging.getLogger("max.web")


class WebMaxTransport(MaxTransport):
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.session: aiohttp.ClientSession | None = None
        self.seq = 0
        self.events: asyncio.Queue[MaxEvent] = asyncio.Queue(maxsize=1024)
        self.pending: dict[int, asyncio.Future[MaxEvent]] = {}
        self.reader: asyncio.Task[None] | None = None
        self.heartbeat: asyncio.Task[None] | None = None
        self.last_frame_ts = time.monotonic()
        self._ops = cfg.opcodes
        self._token = ""
        self._proxy: str | None = None
        self._relay: Any = None
        self._closing = False
        self._opened_at = 0.0
        # короткая история кадров: по ней видно, на чём именно рвётся сессия
        self._trace: collections.deque[str] = collections.deque(maxlen=30)

    # --- helpers -------------------------------------------------------------
    def _read_token(self) -> str:
        path = self.cfg.max_token_file
        if not path.exists():
            token = self.cfg.max_token
            if not token:
                raise NeedLogin(f"нет токена MAX (файл {path})")
            self._token = token
            return self._token
        if os.name == "posix" and path.stat().st_mode & 0o077:
            log.warning("слабые права на файл токена: %s", path)
        self._token = path.read_text(encoding="utf-8").strip()
        if not self._token:
            raise NeedLogin("пустой файл токена")
        return self._token

    def save_token(self, token: str) -> None:
        from ..login import save_token as _save
        _save(self.cfg.max_token_file, token)
        self._token = token.strip()

    def _render(self, op_name: str, op: int, seq: int, flags: int,
                payload: dict[str, Any], prefix: bytes) -> bytes:
        """clientHello/auth — побайтовые шаблоны из HAR (там свои поля).

        Остальные запросы собираются из payload обычным msgpack (ratio=0).
        Байт 6 заголовка — это ratio сжатия, длина payload — 24 бита (7..9).
        """
        name = {"client_hello": "client_hello.bin", "auth": "auth.bin"}.get(op_name)
        if name:
            path = self.cfg.path.parent / "fixtures" / name
            if path.exists():
                raw = bytearray(path.read_bytes())
                raw[2:4] = seq.to_bytes(2, "big")
                if op_name == "auth":
                    token = str(payload.get("token") or "").encode("utf-8")
                    mark = b"__TOKEN__"
                    pos = bytes(raw).find(mark)
                    if pos < 0:
                        raise RuntimeError("в auth-шаблоне нет маркера токена")
                    if pos >= 3 and bytes(raw[pos - 3]) == 0xDA:
                        raw[pos - 3:pos] = b"\xda" + len(token).to_bytes(2, "big")
                    raw[pos:pos + len(mark)] = token
                raw[7:10] = (len(raw) - 10).to_bytes(3, "big")
                return bytes(raw)
        return codec.frame(op, seq, payload)

    async def request(self, op_name: str, payload: dict[str, Any],
                      timeout: float = 15.0) -> MaxEvent:
        op = self._ops[op_name]
        flags_raw = str(self.cfg.get("max", "flags", op_name, default="0x0000"))
        flags = int(flags_raw, 16) if flags_raw.lower().startswith("0x") else int(flags_raw)
        prefix_raw = str(self.cfg.get("max", "prefixes", op_name, default="")).replace(" ", "")
        prefix = bytes.fromhex(prefix_raw) if prefix_raw else b""
        self.seq = (self.seq + 1) & 0xFFFF
        fut: asyncio.Future[MaxEvent] = asyncio.get_running_loop().create_future()
        self.pending[self.seq] = fut
        assert self.ws is not None
        frame = self._render(op_name, op, self.seq, flags, payload, prefix)
        self._trace.append(f"-> {op_name} seq={self.seq} len={len(frame)}")
        await self.ws.send_bytes(frame)
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self.pending.pop(self.seq, None)

    # --- connect / loop ------------------------------------------------------
    async def connect(self, require_auth: bool = True) -> None:
        """require_auth=False — сырой WSS для QR-входа, когда токена нет."""
        headers = {
            "Origin": self.cfg.get("max", "origin", default="https://web.max.ru"),
            "User-Agent": self.cfg.get("max", "user_agent", default="Mozilla/5.0"),
            "Accept-Language": self.cfg.get("max", "locale", default="ru"),
        }
        if self._proxy is None:
            from ..net import make_proxy
            self._proxy, self._relay = await make_proxy(self.cfg)
        self.session = aiohttp.ClientSession(
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=None),
        )
        self.ws = await self.session.ws_connect(
            self.cfg.max_ws_url,
            heartbeat=None,
            proxy=self._proxy,
            ssl=False if self.cfg.get("max", "insecure", default=False) else None,
        )
        self.reader = asyncio.create_task(self._read_loop(), name="max-reader")
        self._opened_at = time.monotonic()
        if not require_auth:
            log.info("MAX WSS открыт без авторизации (QR-флоу)")
            return
        self.seq = -1  # шаблон HAR: clientHello seq=0, auth seq=1
        hello = await self.request("client_hello",
                                   self.cfg.get("max", "client_hello", default={}))
        hello_err = hello.payload.get("error") if isinstance(hello.payload, dict) else None
        if hello_err:
            raise RuntimeError(
                "MAX отклонил clientHello: "
                f"{hello.payload.get('message') or hello_err}. "
                "Проверь fixtures/client_hello.bin (без шаблонов сервер "
                "требует userAgent в строгом формате)")
        token = self.cfg.get("max", "token_override") or self._read_token()
        auth = await self.request("auth", {"token": token})
        auth_err = auth.payload.get("error") if isinstance(auth.payload, dict) else None
        if auth_err or "error.login.token" in json.dumps(auth.payload, default=str):
            detail = ""
            if isinstance(auth.payload, dict):
                detail = str(auth.payload.get("message") or auth_err or "")
            if "login.token" in json.dumps(auth.payload, default=str) \
                    or "FAIL_LOGIN_TOKEN" in detail:
                raise NeedLogin(
                    "сервер отверг токен MAX (FAIL_LOGIN_TOKEN) — токен "
                    "отозван/просрочен, нужен новый" + (f": {detail}" if detail else ""))
            raise RuntimeError(f"MAX отклонил вход: {detail or auth_err}")
        self.heartbeat = asyncio.create_task(self._heartbeat_loop(), name="max-heartbeat")
        log.info("MAX подключён, seq=%d", self.seq)

    async def _heartbeat_loop(self) -> None:
        period = float(self.cfg.get("max", "heartbeat_seconds", default=30))
        while True:
            await asyncio.sleep(period)
            try:
                await self.request("heartbeat", {
                    "interactive": bool(self.cfg.get("max", "heartbeat_interactive",
                                                    default=True)),
                }, timeout=10)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Ни в коем случае не выходим из цикла: без heartbeat сервер
                # закрывает сессию (наблюдали обрыв ~через 8 минут).
                log.warning("heartbeat MAX не прошёл: %s", exc)

    async def _read_loop(self) -> None:
        assert self.ws is not None
        try:
            async for message in self.ws:
                self.last_frame_ts = time.monotonic()
                if message.type != aiohttp.WSMsgType.BINARY:
                    log.warning("MAX WSS non-binary: type=%s data=%s",
                                message.type, str(message.data)[:200])
                    continue
                op, seq, direction, payload, error = codec.parse_frame(message.data)
                self._trace.append(
                    f"<- op=0x{op:04x} seq={seq} d={direction} "
                    f"len={len(message.data)}")
                if direction == 3:
                    # Кадр-ошибка сервера: без этого текста причины отказов
                    # (кривой clientHello, отозванный токен) не видно.
                    log.warning("MAX error op=0x%04X seq=%d: %s", op, seq,
                                json.dumps(payload, ensure_ascii=False,
                                           default=str)[:400])
                event = MaxEvent(op=op, seq=seq,
                                 direction="send" if direction == 0 else "receive",
                                 payload=payload, raw=message.data,
                                 ts=time.time(), decode_error=error)
                # Входящие события MAX (0x0080/0x0081/0x0084/0x00B1/...) идут
                # со СВОИМ счётчиком seq, который不定期 совпадает с seq нашего
                # запроса. Если не различать, сообщение съедается как «ответ»
                # на heartbeat/send и вообще не попадает в мост.
                server_events = {self._ops.get(k) for k in (
                    "incoming", "peer_typing", "reactions")} - {None}
                if op not in server_events:
                    fut = self.pending.get(seq)
                    if fut is not None and not fut.done():
                        fut.set_result(event)
                        continue
                if error and self.cfg.get("debug", "log_decode_errors", default=True):
                    # Частичный разбор — норма (MAX кладёт «мусорные» поля
                    # вроде prevMessage), сообщение всё равно извлекается.
                    # WARNING только если payload вообще пустой.
                    if payload:
                        log.debug("op=0x%04X decode: %s", op, error)
                    else:
                        log.warning("op=0x%04X decode: %s raw=%s", op, error,
                                    message.data[:80].hex(" "))
                try:
                    self.events.put_nowait(event)
                except asyncio.QueueFull:
                    log.warning("очередь событий MAX переполнена, кадр отброшен")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("read loop MAX прерван: %s", exc)
        finally:
            ws = self.ws
            code = getattr(ws, "close_code", None) if ws is not None else None
            werr = None
            if ws is not None:
                try:
                    werr = ws.exception()
                except Exception:
                    werr = None
            lived = (time.monotonic() - self._opened_at) if self._opened_at else 0
            if self._closing:
                log.info("read loop MAX остановлен (close_code=%s, %.0f c)",
                         code, lived)
            else:
                log.warning("read loop MAX завершён сервером: close_code=%s "
                            "exception=%r (сессия %.0f c); трасса: %s",
                            code, werr, lived, " | ".join(self._trace))
            for fut in list(self.pending.values()):
                if not fut.done():
                    fut.set_exception(ConnectionError("MAX websocket closed"))

    async def iter_events(self):  # type: ignore[override]
        while True:
            yield await self.events.get()

    def seconds_since_frame(self) -> float:
        return time.monotonic() - self.last_frame_ts

    @staticmethod
    def _msgpack_str(text: str) -> bytes:
        """Строка как в HAR: fixstr<=31, str8<=255, str16 дальше.

        Нужна тест-хуку `photo_frame` (сборка кадра по HAR); основной тракт
        отправки целиком на codec.frame.
        """
        data = text.encode("utf-8")
        n = len(data)
        if n <= 31:
            return bytes([0xA0 | n]) + data
        if n <= 0xFF:
            return b"\xd9" + bytes([n]) + data
        return b"\xda" + struct.pack(">H", n) + data

    # --- outgoing ------------------------------------------------------------
    async def send(self, chat_id: int, msg: OutboundMessage) -> int:
        """0x0040: обычный msgpack (ratio=0), без шаблонов и LZ4-вставок."""
        message: dict[str, Any] = {
            "text": msg.text,
            "cid": codec.long_ext(-int(time.time() * 1000)),
            "elements": [],
            "attaches": [],
        }
        if msg.reply_to is not None:
            # факт HAR: link = {"type": "REPLY", "messageId": Long}
            message["link"] = {"type": "REPLY",
                               "messageId": codec.long_ext(int(msg.reply_to))}
        return await self._send_message(chat_id, message, "текст")

    # --- media ---------------------------------------------------------------
    async def photo_upload_url(self, timeout: float = 45) -> str:
        """0x0050: получить URL загрузки фото (можно заранее, параллельно)."""
        ev = await self.request("upload_photo", {"count": 1}, timeout=timeout)
        url = ev.payload.get("url") if isinstance(ev.payload, dict) else None
        if not isinstance(url, str) or not url.startswith("http"):
            raise RuntimeError("0x0050 не вернул url загрузки")
        log.info("0x0050 url: %s", url[:160])
        return url

    async def upload_photo_data(self, url: str, data: bytes, filename: str,
                                mime: str) -> str:
        """Multipart POST на готовый uploadImage URL -> photoToken."""
        if self.session is None:
            raise RuntimeError("нет HTTP-сессии")
        # Имя файла — uuid, как у браузера; часть серверов проверяет шаблон.
        fname = f"{uuid.uuid4()}{Path(filename).suffix or '.jpg'}"
        # Тело собираем ровно как Chrome: boundary вида ----WebKitFormBoundary…
        boundary = "----WebKitFormBoundary" + "".join(
            random.choice(string.ascii_letters + string.digits) for _ in range(16))
        body = b"".join([
            b"--", boundary.encode(), b"\r\n",
            b'Content-Disposition: form-data; name="file"; filename="',
            fname.encode(), b'"\r\n',
            b"Content-Type: ", mime.encode(), b"\r\n\r\n",
            data, b"\r\n",
            b"--", boundary.encode(), b"--\r\n",
        ])
        up_headers = {
            "Origin": "https://web.max.ru",
            "Referer": "https://web.max.ru/",
            "Accept": "*/*",
            "Accept-Language": "ru,en;q=0.9,en-GB;q=0.8,en-US;q=0.7",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Sec-CH-UA": '"Not-A.Brand";v="99", "Microsoft Edge";v="157", '
                         '"Chromium";v="157"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "cross-site",
            "Priority": "u=1, i",
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/157.0.0.0 Safari/537.36 Edg/157.0.0.0"),
        }
        async with self.session.post(url, data=body, headers=up_headers,
                                     proxy=(None if self.cfg.get(
                                         "max", "upload_direct", default=False)
                                            else self._proxy),
                                     timeout=aiohttp.ClientTimeout(total=120)) as resp:
            js = await resp.json(content_type=None)
            log.info("загрузка фото: HTTP %d, ответ %s", resp.status, js)
        try:
            token = js["photos"]["0"]["token"]
        except Exception:
            raise RuntimeError(f"загрузка фото: {str(js)[:140]}")
        if not isinstance(token, str) or not token:
            raise RuntimeError("пустой photoToken")
        log.info("photoToken: %d символов", len(token))
        return token

    async def upload_photo(self, data: bytes, filename: str, mime: str) -> str:
        """0x0050 + multipart POST (последовательно) -> photoToken."""
        url = await self.photo_upload_url()
        return await self.upload_photo_data(url, data, filename, mime)

    async def video_upload_target(self, timeout: float = 45) -> tuple[str, int, str]:
        """0x0052: (upload URL, videoId, token) — можно получить заранее."""
        ev = await self.request("upload_url", {"count": 1}, timeout=timeout)
        return self._parse_upload_target(ev, "0x0052")

    async def audio_upload_target(self, timeout: float = 45) -> tuple[str, int, str]:
        """0x0052 для голосового: type=2, uploaderType=1 (факт HAR).

        С одним `count` сервер отдаёт URL ВИДЕО-загрузки, и OGG потом
        «обрабатывается» как видео (attachment.not.ready навсегда).
        """
        ev = await self.request("upload_url", {
            "count": 1, "type": 2, "uploaderType": 1,
        }, timeout=timeout)
        return self._parse_upload_target(ev, "0x0052 (audio)")

    @staticmethod
    def _parse_upload_target(ev: MaxEvent, what: str) -> tuple[str, int, str]:
        info = ev.payload.get("info") if isinstance(ev.payload, dict) else None
        item = info[0] if isinstance(info, list) and info else {}
        url = item.get("url")
        vid = item.get("videoId")
        token = item.get("token")
        if not isinstance(url, str) or not url.startswith("http"):
            raise RuntimeError(f"{what} не вернул url загрузки")
        if not isinstance(vid, int) or not isinstance(token, str):
            raise RuntimeError(f"{what} не вернул id/token")
        log.info("%s url: %s", what, url[:100])
        return url, int(vid), token

    async def upload_video_data(self, url: str, data: bytes,
                                filename: str) -> None:
        """Сырой POST видео на готовый upload.do URL (Content-Range)."""
        if self.session is None:
            raise RuntimeError("нет HTTP-сессии")
        headers = {
            "Content-Type": "video/mp4",
            "Content-Disposition": f"attachment; filename={filename}",
            "Content-Range": f"bytes 0-{len(data) - 1}/{len(data)}",
        }
        async with self.session.post(url, data=data, headers=headers,
                                     proxy=self._proxy,
                                     timeout=aiohttp.ClientTimeout(total=300)) as resp:
            await resp.read()
            if resp.status >= 400:
                raise RuntimeError(f"upload.do HTTP {resp.status}")

    async def upload_video(self, data: bytes, filename: str) -> tuple[int, str]:
        """0x0052 -> upload.do (сырой POST) -> (videoId, token)."""
        url, vid, token = await self.video_upload_target()
        await self.upload_video_data(url, data, filename)
        return vid, token

    async def resolve_video(self, chat_id: int, message_id: int,
                            video_id: Any, token: str) -> str | None:
        """0x0053 {videoId, token, chatId, messageId} -> прямая MP4-ссылка.

        Сервер отдаёт словарь вида {"MP4_720": "https://maxvd...", ...}.
        """
        ev = await self.request("upload_confirm", {
            "videoId": video_id, "token": token,
            "chatId": chat_id, "messageId": message_id,
        }, timeout=45)
        pl = ev.payload if isinstance(ev.payload, dict) else {}
        for key in ("MP4_1080", "MP4_720", "MP4_480", "MP4_360", "MP4_240",
                    "MP4_144", "EXTERNAL"):
            val = pl.get(key)
            if isinstance(val, str) and val.startswith("http"):
                return val
        log.warning("0x0053: нет MP4-ссылки, ключи=%s", list(pl))
        return None

    async def prepare_attach(self, chat_id: int, kind: str = "PHOTO") -> None:
        """0x0041 {chatId, type} — «готовлю вложение» перед отправкой.

        В HAR этот шаг стоит между загрузкой картинки и кадром 0x0040.
        Тип: PHOTO / VIDEO / AUDIO / TEXT / FILE.
        """
        op = int(self._ops.get("typing") or 0x0041)
        raw = bytearray(codec.frame(op, 0, {
            "chatId": codec.long_ext(int(chat_id)),
            "type": kind,
        }))
        await self._send_frame(raw)

    async def send_photo(self, chat_id: int, token: str,
                         text: str = "", reply_to: int | None = None) -> int:
        await self.prepare_attach(chat_id, "PHOTO")
        message: dict[str, Any] = {
            "cid": codec.long_ext(-int(time.time() * 1000)),
            "elements": [],
            "attaches": [{"_type": "PHOTO", "photoToken": token}],
        }
        if text:
            message["text"] = text
        if reply_to is not None:
            message["link"] = {"type": "REPLY",
                               "messageId": codec.long_ext(int(reply_to))}
        return await self._send_message(chat_id, message, "фото")

    async def file_upload_target(self, timeout: float = 45) -> tuple[str, int]:
        """0x0057: (upload URL, fileId) — можно получить заранее."""
        ev = await self.request("upload_file", {"count": 1}, timeout=timeout)
        info = ev.payload.get("info") if isinstance(ev.payload, dict) else None
        item = info[0] if isinstance(info, list) and info else {}
        url, fid = item.get("url"), item.get("fileId")
        if not isinstance(url, str) or not url.startswith("http"):
            raise RuntimeError("0x0057 не вернул url загрузки")
        if not isinstance(fid, int):
            raise RuntimeError("0x0057 не вернул fileId")
        return url, int(fid)

    async def upload_file_data(self, url: str, data: bytes,
                               filename: str) -> None:
        """Сырой POST файла на готовый upload.do URL."""
        if self.session is None:
            raise RuntimeError("нет HTTP-сессии")
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Disposition": f"attachment; filename={filename}",
            "Content-Range": f"bytes 0-{len(data) - 1}/{len(data)}",
            "Origin": "https://web.max.ru",
            "Referer": "https://web.max.ru/",
        }
        async with self.session.post(url, data=data, headers=headers,
                                     proxy=self._proxy,
                                     timeout=aiohttp.ClientTimeout(total=300)) as resp:
            body = await resp.read()
            if resp.status >= 400:
                raise RuntimeError(f"upload.do HTTP {resp.status}: "
                                   f"{body[:120].decode('utf-8', 'replace')}")

    async def upload_audio_data(self, url: str, data: bytes,
                                filename: str) -> None:
        """Сырой POST голосового на au.oneme.ru/uploadAudio.

        Факты HAR: Content-Type: application/octet-stream,
        Content-Disposition: attachment; filename=...ogg,
        Content-Range: bytes 0-N/N.
        """
        if self.session is None:
            raise RuntimeError("нет HTTP-сессии")
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Disposition": f"attachment; filename={filename}",
            "Content-Range": f"bytes 0-{len(data) - 1}/{len(data)}",
            "Origin": "https://web.max.ru",
            "Referer": "https://web.max.ru/",
        }
        async with self.session.post(url, data=data, headers=headers,
                                     proxy=self._proxy,
                                     timeout=aiohttp.ClientTimeout(total=300)) as resp:
            body = await resp.read()
            log.info("uploadAudio: HTTP %d, ответ %s", resp.status,
                     body[:160].decode("utf-8", "replace"))
            if resp.status >= 400:
                raise RuntimeError(f"uploadAudio HTTP {resp.status}: "
                                   f"{body[:120].decode('utf-8', 'replace')}")

    async def send_voice(self, chat_id: int, audio_id: int, token: str,
                         duration_ms: int, reply_to: int | None = None,
                         prepared: bool = False,
                         wave: bytes | None = None) -> int:
        """0x0040 с вложением AUDIO (голосовое).

        prepared=True — 0x0041 уже отправлен ДО запроса URL загрузки
        (порядок как в HAR: 0x0041 -> 0x0052 -> POST -> 0x0040).
        """
        if not prepared:
            await self.prepare_attach(chat_id, "AUDIO")
        op = int(self._ops.get("send") or 0x0040)
        message: dict[str, Any] = {
            "cid": codec.long_ext(-int(time.time() * 1000)),
            "attaches": [{"_type": "AUDIO",
                          "audioId": int(audio_id),
                          "duration": int(duration_ms),
                          "token": token}],
        }
        if wave:
            message["attaches"][0]["wave"] = wave
        if reply_to is not None:
            message["link"] = {"type": "REPLY",
                               "messageId": codec.long_ext(int(reply_to))}
        return await self._send_message(chat_id, message, "голосовое",
                                        ready_retry=True)

    async def upload_file(self, data: bytes, filename: str) -> int:
        """0x0057 -> fu.oneme.ru/api/upload.do (сырой POST) -> fileId.

        Факты HAR: ответ 0x0057 = {"info":[{url, fileId, token}]}, затем
        POST application/octet-stream, ответ вида "0-63049/63050".
        """
        url, fid = await self.file_upload_target()
        await self.upload_file_data(url, data, filename)
        log.info("файл загружен в MAX: %d б, fileId=%s", len(data), fid)
        return fid

    async def send_file(self, chat_id: int, file_id: int,
                        filename: str = "", reply_to: int | None = None) -> int:
        await self.prepare_attach(chat_id, "FILE")
        message: dict[str, Any] = {
            "cid": codec.long_ext(-int(time.time() * 1000)),
            "attaches": [{"_type": "FILE",
                          "fileId": codec.long_ext(int(file_id))}],
        }
        if reply_to is not None:
            message["link"] = {"type": "REPLY",
                               "messageId": codec.long_ext(int(reply_to))}
        return await self._send_message(chat_id, message, "файл",
                                        ready_retry=True)

    async def send_video(self, chat_id: int, video_id: int, token: str,
                         text: str = "", reply_to: int | None = None,
                         video_type: int = 0,
                         duration_ms: int = 0,
                         wave: bytes | None = None) -> int:
        """video_type=1 — кружок (видеосообщение), факт из клиента MAX.

        Для кружка сервер ждёт ещё и `wave` (как в HAR у видео-сообщений):
        без него вложение может не отрисоваться у получателя.
        """
        await self.prepare_attach(chat_id, "VIDEO")
        attach: dict[str, Any] = {
            "_type": "VIDEO",
            "videoId": codec.long_ext(int(video_id)),
            "token": token,
        }
        if video_type:
            attach["videoType"] = int(video_type)
            if duration_ms:
                attach["duration"] = int(duration_ms)
            if wave:
                attach["wave"] = wave
        message: dict[str, Any] = {
            "cid": codec.long_ext(-int(time.time() * 1000)),
            "elements": [],
            "attaches": [attach],
        }
        if text:
            message["text"] = text
        if reply_to is not None:
            message["link"] = {"type": "REPLY",
                               "messageId": codec.long_ext(int(reply_to))}
        return await self._send_message(chat_id, message, "видео",
                                        ready_retry=True)

    async def ack(self, seq: int, chat_id: int, message_id: int) -> None:
        """ACK входящего 0x0080 тем же seq (факт HAR: кадр 123/130/135)."""
        if self.ws is None or self.ws.closed:
            return
        body = (bytes.fromhex("f01482a6636861744964ce") + struct.pack(">I", chat_id) +
                bytes.fromhex("a96d6573736167654964c70901cf") + struct.pack(">Q", message_id))
        header = bytes([0x0A, 0x01]) + struct.pack(">HHHH", seq & 0xFFFF, 0x0080,
                                                    0x0100, len(body))
        try:
            await self.ws.send_bytes(header + body)
        except Exception as exc:
            log.debug("ACK send failed: %s", exc)

    async def _send_frame(self, raw: bytearray) -> MaxEvent:
        self.seq = (self.seq + 1) & 0xFFFF
        raw[2:4] = self.seq.to_bytes(2, "big")
        raw[7:10] = (len(raw) - 10).to_bytes(3, "big")
        fut: asyncio.Future[MaxEvent] = asyncio.get_running_loop().create_future()
        self.pending[self.seq] = fut
        assert self.ws is not None
        self._trace.append(
            f"-> frame op=0x{int.from_bytes(raw[4:6], 'big'):04x} "
            f"seq={self.seq} len={len(raw)}")
        await self.ws.send_bytes(bytes(raw))
        try:
            return await asyncio.wait_for(fut, 15.0)
        finally:
            self.pending.pop(self.seq, None)

    # --- отправка 0x0040 -----------------------------------------------------
    @staticmethod
    def _echo_payload(echo: MaxEvent) -> dict:
        return echo.payload if isinstance(echo.payload, dict) else {}

    def _echo_error(self, echo: MaxEvent) -> str | None:
        """Текст ошибки из эха сервера (у ошибок message — строка)."""
        payload = self._echo_payload(echo)
        err = payload.get("error")
        if not err:
            return None
        detail = payload.get("message")
        if isinstance(detail, str) and detail:
            return f"{err}: {detail}"
        return str(err)

    async def _send_message(self, chat_id: int, message: dict,
                            what: str, ready_retry: bool = False) -> int:
        """Послать 0x0040 и вернуть id.

        ready_retry=True — для медиа: MAX после загрузки ещё обрабатывает
        вложение и отвечает attachment.not.ready; ждём и повторяем кадр.
        """
        op = int(self._ops.get("send") or 0x0040)
        attempts = 15 if ready_retry else 1
        last_err = ""
        for attempt in range(attempts):
            echo = await self._send_frame(bytearray(codec.frame(op, 0, {
                "chatId": codec.long_ext(int(chat_id)),
                "message": message,
                "notify": True,
            })))
            err = self._echo_error(echo)
            if err is None:
                msg = self._echo_payload(echo).get("message")
                mid = msg.get("id") if isinstance(msg, dict) else None
                if isinstance(mid, int):
                    return mid
                raise RuntimeError(f"нет id в эхе ({what})")
            last_err = err
            ready = ("attachment.not.ready" in err
                     or "not.processed" in err)
            if ready_retry and ready and attempt + 1 < attempts:
                log.info("%s: вложение обрабатывается, ждём (%d c)",
                         what, attempt + 1)
                await asyncio.sleep(1.0)
                continue
            raise RuntimeError(f"MAX отклонил {what}: {err}")
        raise RuntimeError(f"{what}: вложение не готово за {attempts} c "
                           f"({last_err})")

    @staticmethod
    def _encode_text(text: str) -> bytes:
        data = text.encode("utf-8")
        n = len(data)
        if n <= 31:
            return bytes([0xA0 | n]) + data
        if n <= 0xFF:
            return b"\xd9" + bytes([n]) + data
        return b"\xda" + struct.pack(">H", n) + data

    async def edit(self, chat_id: int, max_msg_id: int, text: str) -> None:
        """Кадр 0x0043 по HAR. Два отличия от наивной подстановки:

        1) `F0 XX` внутри поля `message` — тоже длина: XX = len(text)+9
           (проверено HAR(1) f57: 29+9=38=0x26 и HAR(3) f70: 16+9=25=0x19).
        2) id сообщения лежит сразу после `F0 XX` (8 байт), а не на байт позже —
           previous agent патчил m+3:m+11 и съедал маркер `a4` ключа `text`,
           из-за чего сервер рвал WSS при любом редактировании.
        """
        path = self.cfg.path.parent / "fixtures" / "edit.bin"
        if not path.exists():
            raise RuntimeError("нет шаблона edit.bin — пересоздайте fixtures")
        raw = bytearray(path.read_bytes())
        m = bytes(raw).find(bytes.fromhex("c70901cf"))
        if m >= 0:
            raw[m + 4:m + 12] = struct.pack(">q", chat_id)

        pos = bytes(raw).find(b"__TEXT__")
        if pos < 0:
            raise RuntimeError("в edit-шаблоне нет маркера текста")
        raw[pos:pos + 8] = self._encode_text(text)

        # кастомный префикс id: F0 XX (в шаблоне XX=0x26), затем 8 байт id,
        # сразу за ними идёт a4 'text' — патчим строго эти 8 байт.
        mid = bytes(raw).find(bytes.fromhex("f026"))
        if mid < 0 or raw[mid + 10] != 0xA4:
            raise RuntimeError("в edit-шаблоне нет поля message id")
        raw[mid + 1] = (len(text.encode("utf-8")) + 9) & 0xFF
        raw[mid + 2:mid + 10] = struct.pack(">q", max_msg_id)
        await self._send_frame(raw)

    async def delete(self, chat_id: int, max_msg_ids: list[int]) -> None:
        path = self.cfg.path.parent / "fixtures" / "delete.bin"
        if not path.exists():
            raise RuntimeError("нет шаблона delete.bin — пересоздайте fixtures")
        raw = bytearray(path.read_bytes())
        m = bytes(raw).find(bytes.fromhex("c70901cf"))
        if m >= 0:
            raw[m + 4:m + 12] = struct.pack(">q", chat_id)
        m = bytes(raw).find(bytes.fromhex("f000"))
        if m >= 0 and max_msg_ids:
            raw[m + 2:m + 10] = struct.pack(">q", int(max_msg_ids[0]))
        await self._send_frame(raw)

    # --- media ---------------------------------------------------------------
    async def upload(self, path: Path, kind: str) -> Media:
        """TODO: 0x0052 -> POST по url из info -> 0x0053.

        Факты: для видео/аудио 0x0052 даёт info[{url,videoId,token}],
        затем raw POST и 0x0053 с videoId/token/chatId/messageId -> cache:true.
        Фото идёт multipart на iu.oneme.ru/uploadImage, поле `file`.
        Не хватает: точных заголовков/полей запроса и ограничений размера.
        """
        raise NotImplementedError("upload: ждём точный флоу (не выдумываем поля)")

    async def download(self, url: str, dest: Path) -> Path:
        # Подписанные ссылки MAX публичны (G24=OK): без Cookie/Authorization.
        assert self.session is not None
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as fh:
            async with self.session.get(url) as resp:
                resp.raise_for_status()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    fh.write(chunk)
        return dest

    # --- QR login / logout ---------------------------------------------------
    async def login_qr_start(self) -> QrSession:
        if self.ws is None or self.ws.closed:
            await self.connect(require_auth=False)
        resp = await self.request("login_qr_start", {})
        track_id = str(resp.payload.get("trackId", ""))
        qr_link = str(resp.payload.get("qrLink", ""))
        expires = int(resp.payload.get("expiresAt", 0))
        ttl = int(resp.payload.get("ttl", 0))
        if not track_id or not qr_link:
            raise RuntimeError("некорректный ответ 0x0120")
        # TODO: в ответе 0x0120 Fields содержат кастомные int'ы; qrLink
        # частично не разобран декодером — проверить сырой payload в HAR(2).
        return QrSession(track_id, qr_link, expires, ttl)

    async def login_qr_poll(self, track_id: str) -> str | None:
        resp = await self.request("login_qr_poll", {"trackId": track_id})
        status = resp.payload.get("status") or {}
        if isinstance(status, dict) and status.get("loginAvailable"):
            return await self.login_qr_confirm(track_id)
        return None

    async def login_qr_confirm(self, track_id: str) -> str:
        resp = await self.request("login_qr_confirm", {"trackId": track_id})
        token = resp.payload.get("token")
        if not isinstance(token, str) or not token:
            raise RuntimeError("в ответе 0x0123 нет токена")
        return token

    # --- misc ----------------------------------------------------------------
    def caps(self) -> MaxCaps:
        limits = self.cfg.get("max", "limits", default={})
        return MaxCaps(
            media_size_limit_bytes=limits.get("media_size_bytes"),
            edit_window_seconds=limits.get("edit_window_seconds"),
            delete_window_seconds=limits.get("delete_window_seconds"),
        )

    async def close(self) -> None:
        self._closing = True
        for task in (self.heartbeat, self.reader):
            if task is not None:
                task.cancel()
        if self.ws is not None:
            await self.ws.close()
        if self.session is not None:
            await self.session.close()
        if self._relay is not None:
            await self._relay.stop()
            self._relay = None
