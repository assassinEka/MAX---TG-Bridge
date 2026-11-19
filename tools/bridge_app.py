#!/usr/bin/env python3
"""Единый мост: MAX <-> Telegram-топики + команды входа в сервисном топике.

Один процесс, один Telegram getUpdates-поллер (иначе 409), одно MAX WSS.
Приём MAX -> TG, отправка TG -> MAX, маппинг id, дедуп эха, /login /har
/token /logout /status. Медиа/reply/edit/delete — следующие этапы.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bridge.config import Config  # noqa: E402
from bridge.login import MaxLogin, save_token  # noqa: E402
from bridge.max.transport import NeedLogin  # noqa: E402
from bridge.max.web import WebMaxTransport  # noqa: E402
from bridge.models import MaxEvent, OutboundMessage  # noqa: E402
from bridge.net import make_proxy  # noqa: E402
from login_from_har import extract_token  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler(
                  LOG_DIR / "bridge.log",
                  encoding="utf-8")],
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    force=True,
)
log = logging.getLogger("bridge")


def setup_logging(cfg: Config) -> None:
    """Уровень и ротация лога из config.json (logging.*)."""
    import logging.handlers

    level = getattr(logging, str(cfg.get("logging", "level",
                                         default="INFO")).upper(),
                    logging.INFO)
    rel = str(cfg.get("logging", "file", default="logs/bridge.log"))
    path = Path(rel)
    if not path.is_absolute():
        path = ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    max_bytes = int(cfg.get("logging", "max_bytes", default=10 * 1024 * 1024))
    backups = int(cfg.get("logging", "backups", default=5))
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    file_handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    stream_handler = logging.StreamHandler(sys.stdout)
    for handler in (file_handler, stream_handler):
        handler.setFormatter(fmt)
        root.addHandler(handler)
    root.setLevel(level)

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats(
  max_chat_id INTEGER PRIMARY KEY, topic_id INTEGER NOT NULL UNIQUE,
  title TEXT, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS messages(
  max_msg_id INTEGER PRIMARY KEY, max_chat_id INTEGER NOT NULL,
  tg_msg_id INTEGER NOT NULL, sender_id INTEGER NOT NULL, created_at REAL NOT NULL,
  kind TEXT);
CREATE INDEX IF NOT EXISTS msg_tg ON messages(tg_msg_id);
CREATE TABLE IF NOT EXISTS processed_events(
  event_key TEXT PRIMARY KEY, processed_at REAL NOT NULL);
"""


def extract_chat_ids(raw: bytes) -> set[int]:
    """Достаём id чатов из ответа get_chats (0x0030).

    В объекте чата сразу после `id` идёт ключ `type`; у сообщения — `time`.
    Поэтому ищем `id`-ext с int64, за которым следует ровно `A4 'type'`.
    Так id сообщений (они тоже ext int64) не попадают в выборку."""
    out: set[int] = set()
    sig = b"\xa2id\xc7\x09\x01\xd3"
    p = 0
    while True:
        p = raw.find(sig, p)
        if p < 0:
            break
        start = p + len(sig)
        if start + 8 + 5 <= len(raw) and raw[start + 8:start + 13] == b"\xa4type":
            out.add(int.from_bytes(raw[start:start + 8], "big", signed=True))
        p = start
    return out


class Api:
    def __init__(self, cfg: Config, proxy: str | None) -> None:
        self.cfg = cfg
        self.proxy = proxy
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()
        # Прокси может быть не поднят (например, после перезагрузки ПК):
        # тогда не долбим сеть и не засоряем лог.
        self.offline = False
        self._timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=15)
        # Telegram сам скачивает медиа по URL — на это нужно больше времени.
        self._media_timeout = aiohttp.ClientTimeout(total=90, connect=10,
                                                    sock_read=60)

    # Методы, которые можно безопасно повторять: дубля сообщения не будет.
    IDEMPOTENT = frozenset({
        "getUpdates", "getMe", "getChat", "deleteMessage", "editMessageText",
        "editMessageCaption", "sendChatAction", "getFile",
    })
    MEDIA_METHODS = frozenset({
        "sendPhoto", "sendVideo", "sendDocument", "sendVoice", "sendAudio",
        "sendAnimation",
    })

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def _reset_session(self) -> None:
        # Только один вызов пересоздаёт сессию — иначе параллельные
        # getUpdates/send/edit убивают сессию друг друга ('Connector is closed').
        async with self._lock:
            if self._session is not None and not self._session.closed:
                await self._session.close()
            self._session = aiohttp.ClientSession(timeout=self._timeout)

    def url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.cfg.tg_bot_token}/{method}"

    async def call(self, method: str, payload: dict[str, Any] | None = None,
                   params: dict[str, Any] | None = None, get: bool = False) -> dict:
        """Вызов Bot API.

        ВАЖНО: повторять send* при таймауте нельзя — Telegram мог уже
        принять запрос, и повтор даёт дубль (так дублировались фото).
        Повторяем только идемпотентные методы и ошибки соединения, когда
        запрос точно не был доставлен.
        """
        timeout = self._media_timeout if method in self.MEDIA_METHODS else self._timeout
        for attempt in range(5):
            try:
                request = self.session.get if get else self.session.post
                # keep-alive вместо 'Connection: close': меньше TCP+SOCKS5
                # handshake'ов и заметно ниже задержка в обе стороны.
                async with request(self.url(method),
                                   json=None if get else payload, params=params,
                                   proxy=self.proxy, timeout=timeout) as resp:
                    body = await resp.json(content_type=None)
                    if not body.get("ok"):
                        desc = str(body.get("description") or body)[:160]
                        code = int(body.get("error_code") or 0)
                        if code == 429:
                            wait = float((body.get("parameters") or {}).get(
                                "retry_after", 1))
                            await asyncio.sleep(min(wait + 0.2, 10))
                            continue
                        if code == 409:
                            log.warning("TG 409: %s", desc)
                            await asyncio.sleep(1)
                            continue
                        # 5xx (Bad Gateway и т.п.) — временный сбой на стороне
                        # Telegram/прокси: повторяем только безопасные методы.
                        if code >= 500 and method in self.IDEMPOTENT \
                                and attempt < 4:
                            log.warning("TG %s: HTTP %d, повтор %d",
                                        method, code, attempt + 1)
                            await asyncio.sleep(min(0.6 * (attempt + 1), 3))
                            continue
                        log.error("TG %s: %s", method, desc)
                        return {}
                    result = body.get("result")
                    self.offline = False
                    return result if isinstance(result, dict) else {"value": result}
            except aiohttp.ClientConnectorError as exc:
                # соединение не установлено -> запрос точно не дошёл
                self.offline = True
                if attempt == 0:
                    log.warning("TG %s: прокси недоступен (%s)", method, exc)
                else:
                    log.debug("TG %s connect retry %d: %s", method, attempt + 1, exc)
                await self._reset_session()
                await asyncio.sleep(min(0.5 * (attempt + 1), 3))
            except TimeoutError as exc:
                self.offline = True
                if method not in self.IDEMPOTENT:
                    log.warning("TG %s: таймаут, повтор опасен (дубль) — "
                                "считаю отправленным", method)
                    return {}
                log.debug("TG %s timeout retry %d: %s", method, attempt + 1, exc)
                await self._reset_session()
                await asyncio.sleep(0.5 * (attempt + 1))
            except (aiohttp.ClientError, OSError) as exc:
                self.offline = True
                if method not in self.IDEMPOTENT:
                    log.warning("TG %s: обрыв (%s), повтор опасен (дубль) — "
                                "считаю отправленным", method, exc)
                    return {}
                if attempt == 0:
                    log.warning("TG %s: прокси недоступен (%s)", method, exc)
                else:
                    log.debug("TG %s retry %d: %s", method, attempt + 1, exc)
                await self._reset_session()
                await asyncio.sleep(min(0.5 * (attempt + 1), 3))
        return {}

    async def say(self, text: str, topic: int | None = None) -> None:
        payload: dict[str, Any] = {"chat_id": self.cfg.tg_group_id, "text": text[:4000]}
        if topic:
            payload["message_thread_id"] = topic
        await self.call("sendMessage", payload)

    async def updates(self, offset: int) -> list[dict]:
        # Long polling через SOCKS5-релей нестабилен (ok:false/409), поэтому
        # короткий опрос timeout=0 + маленький sleep в poll_tg (~3 раза/с).
        res = await self.call("getUpdates", params={
            "offset": offset, "timeout": 0, "limit": 30}, get=True)
        value = res.get("value") or []
        return value if isinstance(value, list) else []

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()


class App:
    def __init__(self, cfg: Config, proxy: str | None, api: Api,
                 db: sqlite3.Connection, login: MaxLogin) -> None:
        self.cfg = cfg
        self.proxy = proxy
        self.api = api
        self.db = db
        self.login = login
        self.service_topic = int(cfg.get("tg", "service_topic_id", default=0))
        self.offset_file = cfg.path.parent / "state" / "tg_offset.txt"
        self.version_file = cfg.path.parent / "state" / "auth_version.txt"
        self.max: WebMaxTransport | None = None
        self.waiting = False
        self.reader_done = asyncio.Event()
        self.max_ready = asyncio.Event()
        self.event_queue: asyncio.Queue = asyncio.Queue(maxsize=512)
        self._tokens = 2.0
        self._last_refill = time.monotonic()
        self._alerted = False
        # max_msg_id -> (max_chat_id, tg_msg_id) для восстановления reply
        # MAX -> TG (link.type = REPLY).
        self._tg_by_max: dict[int, tuple[int, int]] = {
            int(r[0]): (int(r[1]), int(r[2]))
            for r in self.db.execute(
                "SELECT max_msg_id, max_chat_id, tg_msg_id FROM messages")
        }

    def _remember(self, max_msg_id: int, chat_id: int, tg_msg_id: int) -> None:
        self._tg_by_max[int(max_msg_id)] = (int(chat_id), int(tg_msg_id))

    def _mark_edited(self, body: str) -> str:
        """Пометить сообщение, изменённое в MAX.

        Bot API не умеет «стикер в тексте», поэтому метка — эмодзи сверху,
        а сам текст идёт со второй строки. Повторная правка метку не
        дублирует.
        """
        mark = "✏️"
        text = (body or "").strip()
        if text.startswith(mark):
            return text
        return f"{mark}\n{text}" if text else mark

    def _reply_from_raw(self, event: Any, chat_id: int, mid: int) -> int | None:
        """id цели reply из кадра MAX.

        Факт HAR (после правильной LZ4-распаковки):
          * исходящий кадр:  link = {"type":"REPLY", "messageId": <Long>};
          * входящий кадр:   link = {"type":"REPLY", "message": {...полный
            объект цели...}, "chatId": <Long>} — id цели в message.id.
        Fallback по сырым байтам не нужен (он давал ложные попадания
        в prevMessage).
        """
        payload = event.payload if isinstance(event.payload, dict) else {}
        msg = payload.get("message")
        link = msg.get("link") if isinstance(msg, dict) else None
        if not isinstance(link, dict) or link.get("type") != "REPLY":
            return None
        target = link.get("messageId")
        if not isinstance(target, int):
            inner = link.get("message")
            if isinstance(inner, dict):
                target = inner.get("id")
        if not isinstance(target, int):
            log.info("MAX reply: не удалось разобрать цель: %r", link)
            return None
        row = self._tg_by_max.get(int(target))
        if row is not None and int(row[0]) == int(chat_id):
            log.info("MAX reply: цель %x -> TG %s", int(target), row[1])
            return int(row[1])
        log.info("MAX reply: цель %x не смаплена (чат %d)", int(target), chat_id)
        return None

    def version(self) -> str:
        try:
            return self.version_file.read_text(encoding="utf-8").strip()
        except Exception:
            return "0"

    def bump(self, action: str) -> None:
        self.version_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            n = int(self.version_file.read_text(encoding="utf-8").strip()) + 1
        except Exception:
            n = 1
        self.version_file.write_text(str(n), encoding="utf-8")
        log.info("auth_version -> %d (%s)", n, action)

    def offset(self) -> int:
        try:
            return int(self.offset_file.read_text(encoding="utf-8").strip())
        except Exception:
            return 0

    def save_offset(self, value: int) -> None:
        self.offset_file.parent.mkdir(parents=True, exist_ok=True)
        self.offset_file.write_text(str(value), encoding="utf-8")

    def mark(self, key: str) -> bool:
        cur = self.db.execute("INSERT OR IGNORE INTO processed_events VALUES(?,?)",
                              (key, asyncio.get_running_loop().time()))
        self.db.commit()
        return cur.rowcount > 0

    def topic_for(self, chat_id: int, title: str = "") -> int:
        row = self.db.execute("SELECT topic_id FROM chats WHERE max_chat_id=?",
                              (chat_id,)).fetchone()
        if row:
            return int(row[0])
        raise KeyError(chat_id)

    def ensure_topic(self, chat_id: int, title: str) -> int:
        try:
            return self.topic_for(chat_id)
        except KeyError:
            pass
        # Синхронно создать топик из MAX-потока нельзя — делаем в async-методе.
        raise KeyError(chat_id)

    async def create_topic(self, chat_id: int, title: str) -> int:
        try:
            return self.topic_for(chat_id)
        except KeyError:
            pass
        result = await self.api.call("createForumTopic", {
            "chat_id": self.cfg.tg_group_id,
            "name": f"MAX {chat_id} {title}"[:128],
        })
        tid = int(result.get("message_thread_id") or 0)
        if not tid:
            raise RuntimeError("не удалось создать топик")
        self.db.execute("INSERT INTO chats VALUES(?,?,?,?)",
                        (chat_id, tid, title, asyncio.get_running_loop().time()))
        self.db.commit()
        log.info("топик %d создан для чата %d", tid, chat_id)
        return tid

    async def _send_max(self, chat_id: int, text: str,
                        reply_to: int | None, attempts: int = 3) -> int:
        """Отправка в MAX через ТЕКУЩЕЕ соединение.

        Второе соединение (старый _fresh_max) гонялось с основным циклом:
        run() закрывал только что созданный транспорт, и отправка падала с
        'MAX websocket closed'. Поэтому здесь только ждём готовности
        основного соединения и повторяем на нём же.
        """
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                await self._wait_max()
            except ConnectionError as exc:
                last = exc
                log.warning("MAX send: соединения нет (%s)", exc)
                continue
            assert self.max is not None
            try:
                return await self.max.send(chat_id, OutboundMessage(
                    text=text, reply_to=reply_to))
            except Exception as exc:
                last = exc
                self.max_ready.clear()
                log.warning("MAX send попытка %d: %s", attempt + 1, exc)
                await asyncio.sleep(0.5 * (attempt + 1))
        raise last if last is not None else RuntimeError("MAX send failed")

    async def _wait_max(self, timeout: float = 8) -> None:
        """Дождаться живого соединения MAX (в т.ч. после обрыва).

        Бюджет жёстко ограничен: раньше одно зависание здесь блокировало
        обработку Telegram на минуты. Если сокет открыт, но флаг снят
        (например, после ошибки отправки) — самовосстанавливаемся.
        """
        for _ in range(2):
            if self.max is not None and self.max.ws is not None \
                    and not self.max.ws.closed:
                if not self.max_ready.is_set():
                    # сокет жив — флаг мог быть снят после неудачной попытки
                    self.max_ready.set()
                return
            self.max_ready.clear()
            try:
                await asyncio.wait_for(self.max_ready.wait(), timeout)
            except asyncio.TimeoutError:
                pass
        if self.max is None or self.max.ws is None or self.max.ws.closed:
            raise ConnectionError("MAX websocket closed")

    def _prefetch(self, coro: Any) -> asyncio.Task:
        """Запустить запрос URL загрузки MAX, не дожидаясь его.

        Пока MAX выдаёт URL, мы параллельно скачиваем файл из Telegram —
        это два независимых round-trip'а, последовательно было медленнее.
        ВАЖНО: обычный def (не async) — иначе вернётся корутина, а не Task.
        """
        return asyncio.create_task(coro, name="prefetch-upload-url")

    async def _download_or_cancel(self, file_id: str,
                                  task: asyncio.Task) -> tuple[bytes, str]:
        """Скачать файл из TG; при ошибке отменить висящий запрос URL."""
        try:
            return await self._tg_file(file_id)
        except BaseException:
            task.cancel()
            raise

    async def _tg_file(self, file_id: str) -> tuple[bytes, str]:
        """Скачать файл из Telegram (Bot API getFile + /file/bot...).

        getFile безопасен для повтора, а через SOCKS5-прокси соединение
        рвётся регулярно (Bad Gateway / обрыв), поэтому и запрос метаданных,
        и скачивание делаем с несколькими попытками.
        """
        res: dict = {}
        path = None
        for attempt in range(2):
            res = await self.api.call("getFile", {"file_id": file_id})
            path = res.get("file_path") if isinstance(res, dict) else None
            if isinstance(path, str) and path:
                break
            log.warning("TG getFile: пустой ответ (%s), повтор %d",
                        res, attempt + 1)
            await asyncio.sleep(0.2)
        size = int(res.get("file_size") or 0) if isinstance(res, dict) else 0
        if not isinstance(path, str) or not path:
            raise RuntimeError("getFile не вернул file_path")
        log.info("TG getFile: %s, %d б", path, size)
        cap = int(self.cfg.get("tg", "caps", "download_bytes",
                               default=20 * 1024 * 1024))
        if size and size > cap:
            raise RuntimeError(f"файл {size} б больше лимита {cap} б")
        url = (f"https://api.telegram.org/file/bot"
               f"{self.cfg.tg_bot_token}/{path}")
        data = b""
        last: Exception | None = None
        for attempt in range(3):
            try:
                async with self.api.session.get(
                        url, proxy=self.api.proxy,
                        timeout=aiohttp.ClientTimeout(total=180)) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"скачивание из TG: HTTP {resp.status}")
                    data = await resp.read()
                last = None
                break
            except (aiohttp.ClientError, TimeoutError) as exc:
                last = exc
                log.warning("TG скачивание: %s, повтор %d", exc, attempt + 1)
                await asyncio.sleep(0.2)
        if last is not None:
            raise RuntimeError(f"скачивание из TG не удалось: {last}")
        if len(data) > cap:
            raise RuntimeError(f"файл больше лимита {cap} б")
        log.info("TG файл скачан: %d б", len(data))
        return data, path

    async def _send_media_max(self, chat_id: int, msg: dict,
                              caption: str,
                              reply_to_max: int | None = None) -> int:
        """Фото/видео/файл из TG -> MAX: загрузка по протоколу MAX, затем 0x0040.

        Скорость: URL загрузки MAX запрашиваем ПАРАЛЛЕЛЬНО скачиванию из
        Telegram (это два независимых round-trip'а). Сама отправка кадра
        не повторяется (эхо могло потеряться).
        """
        photo = msg.get("photo")
        video = msg.get("video")
        doc = msg.get("document")
        voice = msg.get("voice")
        note = msg.get("video_note")
        assert self.max is not None
        t0 = time.monotonic()
        if isinstance(doc, dict):
            file_id = str(doc.get("file_id") or "")
            if not file_id:
                raise RuntimeError("в сообщении нет file_id документа")
            name = str(doc.get("file_name") or "file")
            task = self._prefetch(self.max.file_upload_target())
            data, _ = await self._download_or_cancel(file_id, task)
            target = await task
            fid = await self._upload_with_retry(
                lambda: self._upload_file_ready(data, name, target))
            assert self.max is not None
            max_id = await self.max.send_file(chat_id, int(fid), name, reply_to_max)
            if caption:
                # MAX не умеет подпись к файлу: шлём отдельным сообщением
                try:
                    await self._send_max(chat_id, caption, None)
                except Exception as exc:
                    log.warning("подпись к файлу не ушла: %s", exc)
            caption = ""
        elif isinstance(photo, list) and photo:
            sizes = [p for p in photo if isinstance(p, dict)]
            # Самый большой размер до 3 МБ: быстрее скачать/залить, качество
            # для MAX избыточно выше.
            pick = None
            for item in sizes:
                if int(item.get("file_size") or 0) <= 3_000_000:
                    pick = item
            pick = pick or (sizes[-1] if sizes else {})
            file_id = str(pick.get("file_id") or "")
            if not file_id:
                raise RuntimeError("в сообщении нет file_id фото")
            target = self._prefetch(self.max.photo_upload_url())
            data, path = await self._download_or_cancel(file_id, target)
            url = await target
            ext = ".png" if path.lower().endswith(".png") else ".jpg"
            token = await self._upload_with_retry(
                lambda: self._upload_photo_ready(data, file_id, ext, url))
            assert self.max is not None
            # подпись уходит внутри кадра фото (поле text), как у клиента MAX
            max_id = await self.max.send_photo(chat_id, token, caption, reply_to_max)
            caption = ""
        elif isinstance(video, dict):
            file_id = str(video.get("file_id") or "")
            if not file_id:
                raise RuntimeError("в сообщении нет file_id видео")
            target = self._prefetch(self.max.video_upload_target())
            data, _ = await self._download_or_cancel(file_id, target)
            info = await target
            vid, tok = await self._upload_with_retry(
                lambda: self._upload_video_ready(data, file_id, info))
            assert self.max is not None
            # подпись уходит внутри кадра видео (поле text)
            max_id = await self.max.send_video(chat_id, vid, tok, caption,
                                               reply_to_max)
            caption = ""
        elif isinstance(voice, dict):
            file_id = str(voice.get("file_id") or "")
            if not file_id:
                raise RuntimeError("в сообщении нет file_id голосового")
            duration_ms = int(float(voice.get("duration") or 0) * 1000)
            # Порядок из HAR: сначала 0x0041 AUDIO, затем 0x0052 и загрузка.
            assert self.max is not None
            await self.max.prepare_attach(chat_id, "AUDIO")
            target = self._prefetch(self.max.audio_upload_target())
            data, _ = await self._download_or_cancel(file_id, target)
            info = await target
            aid, tok = await self._upload_with_retry(
                lambda: self._upload_voice_ready(data, file_id, info))
            assert self.max is not None
            max_id = await self.max.send_voice(chat_id, aid, tok, duration_ms,
                                               reply_to_max, prepared=True,
                                               wave=b"\x7f" * 63)
        elif isinstance(note, dict):
            # Кружок Telegram -> ОБЫЧНОЕ видео MAX (без videoType/wave).
            # Кружок и так квадратный (384x384), поэтому в MAX он смотрится
            # квадратным видео; videoType=1 включал режим «видеосообщение»
            # и тянул за собой оформление/водяной знак Telegram.
            file_id = str(note.get("file_id") or "")
            if not file_id:
                raise RuntimeError("в сообщении нет file_id кружка")
            target = self._prefetch(self.max.video_upload_target())
            data, _ = await self._download_or_cancel(file_id, target)
            info = await target
            vid, tok = await self._upload_with_retry(
                lambda: self._upload_video_ready(data, file_id, info))
            assert self.max is not None
            max_id = await self.max.send_video(chat_id, vid, tok, "",
                                               reply_to_max)
        else:
            raise RuntimeError("неподдерживаемый тип медиа")
        if caption:
            # Медиа-шаблон MAX не содержит текста: подпись шлём отдельно.
            try:
                await self._send_max(chat_id, caption, None)
            except Exception as exc:
                log.warning("подпись к медиа не ушла: %s", exc)
        log.info("медиа TG -> MAX: %d б, всего %.2f c",
                 len(data), time.monotonic() - t0)
        return max_id

    async def _upload_with_retry(self, factory: Any, attempts: int = 2) -> Any:
        """Повтор только для сетевых сбоев: ошибка кода повтором не лечится,
        а каждая лишняя попытка блокирует обработку Telegram."""
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                await self._wait_max()
                return await factory()
            except Exception as exc:
                if not isinstance(exc, (ConnectionError, TimeoutError,
                                        aiohttp.ClientError, OSError)):
                    log.error("загрузка в MAX: %s", exc)
                    raise
                last = exc
                self.max_ready.clear()
                log.warning("загрузка в MAX, попытка %d: %s", attempt + 1, exc)
                await asyncio.sleep(0.3 * (attempt + 1))
        raise last if last is not None else RuntimeError("загрузка не удалась")

    async def _upload_photo_ready(self, data: bytes, file_id: str, ext: str,
                                  url: str | None = None) -> str:
        assert self.max is not None
        mime = "image/png" if ext == ".png" else "image/jpeg"
        name = f"tg_{file_id[-10:]}{ext}"
        if url is None:
            return await self.max.upload_photo(data, name, mime)
        return await self.max.upload_photo_data(url, data, name, mime)

    async def _upload_video_ready(self, data: bytes, file_id: str,
                                  target: tuple[str, int, str] | None = None
                                  ) -> tuple[int, str]:
        assert self.max is not None
        name = f"tg_{file_id[-10:]}.mp4"
        if target is None:
            return await self.max.upload_video(data, name)
        url, vid, token = target
        await self.max.upload_video_data(url, data, name)
        return vid, token

    async def _upload_file_ready(self, data: bytes, name: str,
                                 target: tuple[str, int] | None = None) -> int:
        assert self.max is not None
        if target is None:
            return await self.max.upload_file(data, name)
        url, fid = target
        await self.max.upload_file_data(url, data, name)
        return fid

    async def _upload_voice_ready(self, data: bytes, file_id: str,
                                  target: tuple[str, int, str] | None = None
                                  ) -> tuple[int, str]:
        assert self.max is not None
        name = f"tg_{file_id[-10:]}.ogg"
        if target is None:
            target = await self.max.audio_upload_target()
        url, audio_id, token = target
        await self.max.upload_audio_data(url, data, name)
        return int(audio_id), str(token)

    async def discover_chats(self) -> None:
        """Периодически узнаём о новых чатах MAX и подписываемся на них.

        MAX пушит 0x0080 только для подписанных чатов (0x004B). Новый
        собеседник появляется в списке чатов (0x0030), но о нём надо
        подписаться, иначе его сообщения не приходят."""
        if self.max is None:
            return
        ev = await self.max.request("get_chats", {"chatIds": []}, timeout=20)
        raw = getattr(ev, "raw", b"") or b""
        ids = extract_chat_ids(raw)
        if ids:
            log.info("get_chats: найдено %d чатов %s", len(ids), sorted(ids)[:20])
        for chat_id in sorted(ids):
            if chat_id <= 0 or not self.cfg.is_allowed_chat(chat_id):
                continue
            if self.db.execute(
                    "SELECT 1 FROM chats WHERE max_chat_id=?", (chat_id,)).fetchone():
                continue
            try:
                await self._throttle()
                topic = await self.create_topic(chat_id, "")
                await self.max.request("subscribe", {"chatId": chat_id, "subscribe": True})
                log.info("новый чат MAX %d -> топик %d, подписка", chat_id, topic)
                await self.api.say(f"➕ Новый чат MAX {chat_id}", self.service_topic)
            except Exception as exc:
                log.warning("новый чат %d: %s", chat_id, exc)

    # --- MAX -> TG -----------------------------------------------------------
    async def on_max(self, event: Any) -> None:
        if event.op in (self.cfg.opcodes.get("incoming"),
                        self.cfg.opcodes.get("peer_typing")):
            log.info("MAX event op=0x%04X seq=%d keys=%s",
                     event.op, event.seq,
                     list(event.payload)[:6] if isinstance(event.payload, dict) else [])
        # временная диагностика: сырой кадр входящего сообщения для чатов
        # из debug.raw_chats (нужно, чтобы разобрать «сжатые» строки MAX)
        if (event.op == self.cfg.opcodes.get("incoming")
                and isinstance(event.payload, dict)
                and int(event.payload.get("chatId") or 0)
                in {int(x) for x in self.cfg.get("debug", "raw_chats",
                                                 default=[]) or []}):
            log.info("RAW %s", (event.raw or b"").hex(" "))
        # 0x008E — обновление чата (в нём приходит lastMessage). Ловим новые
        # чаты, чтобы не зависеть только от периодического get_chats.
        if event.op == 0x008E and isinstance(event.payload, dict):
            chat = event.payload.get("chat")
            if isinstance(chat, dict) and isinstance(chat.get("id"), int):
                chat_id = int(chat["id"])
                if chat_id > 0 and self.cfg.is_allowed_chat(chat_id):
                    known = self.db.execute(
                        "SELECT 1 FROM chats WHERE max_chat_id=?", (chat_id,)).fetchone()
                    if not known:
                        try:
                            await self.create_topic(chat_id, str(chat.get("title") or ""))
                            if self.max is not None:
                                await self.max.request(
                                    "subscribe", {"chatId": chat_id, "subscribe": True})
                            log.info("0x008E: новый чат MAX %d", chat_id)
                        except Exception as exc:
                            log.warning("0x008E новый чат %d: %s", chat_id, exc)
                    lm = chat.get("lastMessage")
                    if isinstance(lm, dict) and isinstance(lm.get("id"), int):
                        synth = MaxEvent(op=self.cfg.opcodes.get("incoming"), seq=0,
                                         direction="receive",
                                         payload={"chatId": chat_id, "message": lm,
                                                  "attaches": lm.get("attaches")},
                                         raw=b"", ts=time.time(), decode_error=None)
                        await self.on_max(synth)
            return
        # deletion от MAX (если сервер пришлёт 0x0042 событием)
        if event.op == self.cfg.opcodes.get("delete") and isinstance(
                event.payload.get("messageIds"), list):
            for raw_id in event.payload["messageIds"]:
                row = self.db.execute("SELECT tg_msg_id, max_chat_id FROM messages "
                                      "WHERE max_msg_id=?", (int(raw_id),)).fetchone()
                if not row:
                    continue
                if self.cfg.on_remote_delete == "mark":
                    await self.api.call("editMessageText", {
                        "chat_id": self.cfg.tg_group_id,
                        "message_id": int(row[0]),
                        "text": "⬛ удалено (исходник в MAX)",
                    })
                else:
                    await self.api.call("deleteMessage", {
                        "chat_id": self.cfg.tg_group_id,
                        "message_id": int(row[0]),
                    })
            return
        if event.op != self.cfg.opcodes.get("incoming"):
            return
        msg = event.payload.get("message")
        if not isinstance(msg, dict):
            return
        mid, chat_id = msg.get("id"), event.payload.get("chatId")
        if not isinstance(mid, int) or not isinstance(chat_id, int):
            return
        # MAX: пересланное сообщение — это обёртка link.type=FORWARD, а сам
        # контент (text/attaches/elements) лежит в link.message (схема клиента:
        # zf = {type, chatId, chatName, get message(){return Wf}}). Без
        # разворота видео/фото из «пересланного» терялись, и в TG уходила
        # текстовая заглушка «[медиа без подписи]».
        fw_chat, fw_mid = 0, 0
        link = msg.get("link")
        if isinstance(link, dict) and str(link.get("type") or "").upper() == "FORWARD":
            inner = link.get("message")
            if isinstance(inner, dict):
                merged = dict(msg)
                if (not str(merged.get("text") or "").strip()
                        and isinstance(inner.get("text"), str)):
                    merged["text"] = inner["text"]
                if not merged.get("attaches") and inner.get("attaches"):
                    merged["attaches"] = inner["attaches"]
                if not merged.get("elements") and inner.get("elements"):
                    merged["elements"] = inner["elements"]
                msg = merged
                fw_chat = int(link.get("chatId") or 0)
                fw_mid = int(inner.get("id") or 0)
                log.info("MAX %s: переслано (чат %s, сообщение %s)",
                         mid, fw_chat or "?", fw_mid or "?")
        # HAR(2) f53 / HAR(3) f49: при правке MAX кладёт НОВЫЙ текст на
        # верхний уровень payload, а в message оставляет только status/sender.
        if isinstance(event.payload.get("text"), str) and event.payload["text"]:
            msg = dict(msg)
            msg["text"] = event.payload["text"]
        # ACK входящего 0x0080 тем же seq — сервер ждёт подтверждения,
        # иначе может рвать соединение (факт HAR: кадры 123/130/135).
        if self.max is not None and event.seq:
            try:
                await self.max.ack(event.seq, chat_id, mid)
            except Exception:
                pass
        # Дедуп: текст в ключ НЕ включаем — иначе повторная доставка того же
        # сообщения с другим «мусорным» текстом (частичный разбор) проходит
        # мимо и фото уходит в TG дважды. Для правок учитываем хэш текста,
        # иначе вторая правка того же сообщения была бы отброшена.
        st = str(msg.get("status") or "").upper()
        if st in ("DELETED", "REMOVED"):
            event_key = f"max:{mid}:del"
        elif st == "EDITED":
            digest = hashlib.sha1(
                str(msg.get("text") or "").encode("utf-8")).hexdigest()[:16]
            event_key = f"max:{mid}:edit:{digest}"
        else:
            event_key = f"max:{mid}:msg"
        if not self.mark(event_key):
            return
        if not self.cfg.is_allowed_chat(chat_id):
            return
        sender = int(msg.get("sender") or 0)
        row = self.db.execute("SELECT tg_msg_id, kind FROM messages WHERE max_msg_id=?",
                              (mid,)).fetchone()
        text = str(msg.get("text") or "") or "[медиа без подписи]"

        # MAX -> TG: удаление (если сервер пришлёт статус в 0x0080 — в HAR
        # такой кадр не попал, но обработка дешёвая и безвредная).
        if str(msg.get("status") or "").upper() in ("DELETED", "REMOVED") and row:
            if int(row[0]) == 0:
                log.info("MAX delete %s: в TG id неизвестен, пропуск", mid)
                return
            if self.cfg.on_remote_delete == "mark":
                await self.api.call("editMessageText", {
                    "chat_id": self.cfg.tg_group_id,
                    "message_id": int(row[0]),
                    "text": "⬛ удалено (исходник в MAX)",
                })
            else:
                await self.api.call("deleteMessage", {
                    "chat_id": self.cfg.tg_group_id,
                    "message_id": int(row[0]),
                })
            self.db.execute("DELETE FROM messages WHERE max_msg_id=?", (mid,))
            self.db.commit()
            self._tg_by_max.pop(int(mid), None)
            log.info("MAX delete %s -> TG %s", mid, row[0])
            return

        #MAX -> TG: правка
        if msg.get("status") == "EDITED" and row:
            if int(row[0]) == 0:
                log.warning("MAX edit %s: исходное сообщение без id в TG — пропуск", mid)
                return
            row_kind = str(row[1] or "text") if len(row) > 1 else "text"
            # У медиа без подписи text — заглушка «[медиа без подписи]»;
            # в подпись её не тащим, оставляем только метку.
            src = "" if text == "[медиа без подписи]" else text
            body = self._mark_edited(src)
            is_media = row_kind in ("photo", "video", "document", "voice",
                                    "audio", "animation", "out_media")
            can_edit = row_kind not in ("out", "out_media")
            res: dict = {}
            if not can_edit:
                # Сообщение в TG отправлено пользователем (TG -> MAX):
                # бот не вправе его править — сразу публикуем новую версию.
                res = {}
            elif is_media:
                # Подпись под медиа правится другим методом Bot API.
                res = await self.api.call("editMessageCaption", {
                    "chat_id": self.cfg.tg_group_id,
                    "message_id": int(row[0]),
                    "caption": body[:1024] if body.strip() else " ",
                })
            else:
                res = await self.api.call("editMessageText", {
                    "chat_id": self.cfg.tg_group_id,
                    "message_id": int(row[0]),
                    "text": body[:4096],
                })
            if not res:
                # Правка невозможна (чужое сообщение или отказ API) —
                # публикуем новую версию ответом на оригинал.
                res = await self.api.call("sendMessage", {
                    "chat_id": self.cfg.tg_group_id,
                    "text": body[:4096],
                    "reply_to_message_id": int(row[0]),
                })
                log.info("MAX edit %s -> TG %s: новым сообщением (%s)",
                         mid, row[0], row_kind)
            else:
                log.info("MAX edit %s -> TG %s (%s)", mid, row[0], row_kind)
            return
        if row:
            return
        if sender == self.cfg.my_id:
            log.info("эхо без маппинга — игнор %s", mid)
            return
        topic = await self.create_topic(chat_id, str(msg.get("chatTitle") or ""))

        # MAX -> TG: reply (link.type = REPLY). Один путь разбора на все
        # варианты кадра (messageId / полный message) + проверка чата.
        reply_to = self._reply_from_raw(event, chat_id, mid)
        if str(msg.get("status") or "").upper() == "EDITED":
            # Правка без известного TG-сообщения (например, мост лежал) —
            # публикуем сразу с меткой.
            text = self._mark_edited(text)
        payload = {
            "chat_id": self.cfg.tg_group_id,
            "message_thread_id": topic,
            "text": text[:4096],
        }
        if reply_to:
            payload["reply_to_message_id"] = reply_to

        # MAX -> TG: медиа.
        # Фото приходит подписанной ссылкой baseUrl; у видео вместо ссылки
        # только videoId+token — прямую MP4-ссылку отдаёт сервер по 0x0053.
        attaches = msg.get("attaches")
        att = None
        if isinstance(attaches, dict):
            att = attaches
        elif isinstance(attaches, list) and attaches and isinstance(attaches[0], dict):
            att = attaches[0]
        # Пустой attaches — это ТЕКСТ. Никакого сканирования сырого кадра:
        # после перехода на нормальный кодек вложения всегда разбираются, а
        # скан цеплял вложение сообщения-цели (reply на видео превращался
        # в попытку отправить видео и падал «failed to get HTTP URL content»).
        send_method, media_key, media_kind = "sendMessage", None, "text"
        if isinstance(att, dict):
            att_type = str(att.get("_type") or "").upper()
            log.info("MAX вложение %s: type=%s videoType=%s url=%s",
                     mid, att_type, att.get("videoType"),
                     "есть" if (att.get("url") or att.get("baseUrl")) else "нет")
            url = att.get("baseUrl") or att.get("url")
            if att_type == "VIDEO":
                vid, tok = att.get("videoId"), att.get("token")
                if isinstance(vid, int) and isinstance(tok, str) and self.max is not None:
                    try:
                        url = await self.max.resolve_video(chat_id, mid, vid, tok)
                    except Exception as exc:
                        url = None
                        # Для пересланного видео исходник живёт в другом чате.
                        if fw_chat and fw_mid:
                            try:
                                url = await self.max.resolve_video(
                                    fw_chat, fw_mid, vid, tok)
                            except Exception as exc2:
                                exc = exc2
                        log.warning("видео %s: 0x0053 без ссылки: %s", mid, exc)
                else:
                    url = None
            if isinstance(url, str) and url.startswith("http"):
                if att_type == "PHOTO":
                    send_method, media_key, media_kind = "sendPhoto", "photo", "photo"
                elif att_type == "VIDEO":
                    # videoType=1 — кружок (факт из клиента MAX).
                    if int(att.get("videoType") or 0) == 1:
                        send_method, media_key = "sendVideoNote", "video_note"
                        media_kind = "video_note"
                        dur = int(att.get("duration") or 0)
                        if dur:
                            payload["duration"] = max(1, dur // 1000)
                    else:
                        send_method, media_key, media_kind = "sendVideo", "video", "video"
                elif att_type == "AUDIO":
                    send_method, media_key, media_kind = "sendVoice", "voice", "voice"
                else:
                    send_method, media_key, media_kind = ("sendDocument", "document",
                                                          "document")
                payload[media_key] = url
                if str(msg.get("text") or "").strip():
                    payload["caption"] = text[:1024]
                else:
                    payload.pop("caption", None)
                payload.pop("text", None)
            elif att_type == "VIDEO":
                # Ссылку не получили — хотя бы сообщим, что видео было.
                payload["text"] = (text if str(msg.get("text") or "").strip()
                                   else "▶ видео из MAX (ссылку получить не удалось)")
        res = await self.api.call(send_method, payload)
        tg_id = int(res.get("message_id") or 0)
        if not tg_id and send_method == "sendVideoNote" and isinstance(att, dict):
            # Telegram не принял URL как кружок — шлём обычным видео.
            note_url = att.get("url") or att.get("baseUrl")
            if isinstance(note_url, str) and note_url.startswith("http"):
                fb = {"chat_id": self.cfg.tg_group_id,
                      "message_thread_id": topic, "video": note_url}
                if reply_to:
                    fb["reply_to_message_id"] = reply_to
                log.warning("MAX %s: sendVideoNote не прошёл, шлю видео", mid)
                res = await self.api.call("sendVideo", fb)
                tg_id = int(res.get("message_id") or 0)
                if tg_id:
                    send_method, media_kind = "sendVideo", "video"
        if not tg_id and send_method == "sendVoice" and isinstance(att, dict):
            # Telegram не принял URL как голосовое — присылаем файлом,
            # чтобы сообщение не пропало.
            voice_url = att.get("url") or att.get("baseUrl")
            if isinstance(voice_url, str) and voice_url.startswith("http"):
                fb = {"chat_id": self.cfg.tg_group_id,
                      "message_thread_id": topic,
                      "document": voice_url}
                if reply_to:
                    fb["reply_to_message_id"] = reply_to
                log.warning("MAX %s: sendVoice не прошёл, шлю файлом", mid)
                res = await self.api.call("sendDocument", fb)
                tg_id = int(res.get("message_id") or 0)
                if tg_id:
                    send_method, media_kind = "sendDocument", "document"
        if not tg_id and send_method != "sendMessage" \
                and str(msg.get("text") or "").strip():
            # Медиа не приняли (например, MAX отдал не прямой URL) — шлём
            # хотя бы текст, иначе сообщение и ответ пропадают бесследно.
            fallback = {"chat_id": self.cfg.tg_group_id,
                        "message_thread_id": topic,
                        "text": text[:4096]}
            if reply_to:
                fallback["reply_to_message_id"] = reply_to
            log.warning("MAX %s: %s не прошёл, отправляю текстом",
                        mid, send_method)
            res = await self.api.call("sendMessage", fallback)
            tg_id = int(res.get("message_id") or 0)
            if tg_id:
                send_method, media_kind = "sendMessage", "text"
        if tg_id:
            self.db.execute(
                "INSERT OR REPLACE INTO messages(max_msg_id,max_chat_id,tg_msg_id,"
                "sender_id,created_at,kind) VALUES(?,?,?,?,?,?)",
                (mid, chat_id, tg_id, sender,
                 asyncio.get_running_loop().time(), media_kind))
            self.db.commit()
            self._remember(mid, chat_id, tg_id)
            log.info("MAX %s -> TG %s (чат %d, %s)", mid, tg_id, chat_id, media_kind)
        else:
            # Отправка могла пройти, но подтверждение потерялось. Ставим
            # заглушку (tg=0), чтобы повторное событие не создало дубль.
            self.db.execute(
                "INSERT OR REPLACE INTO messages(max_msg_id,max_chat_id,tg_msg_id,"
                "sender_id,created_at,kind) VALUES(?,?,?,?,?,?)",
                (mid, chat_id, 0, sender,
                 asyncio.get_running_loop().time(), media_kind))
            self.db.commit()
            self._remember(mid, chat_id, 0)
            log.warning("MAX %s -> TG %s: без подтверждения, дубль заблокирован",
                        mid, send_method)
            await self.api.say(f"⚠ MAX {mid}: отправка в TG не подтверждена",
                               self.service_topic)

    async def _throttle(self) -> None:
        rate = float(self.cfg.get("bridge", "throttle", "per_minute", default=20)) / 60.0
        burst = float(self.cfg.get("bridge", "throttle", "burst", default=2))
        while True:
            now = time.monotonic()
            self._tokens = min(burst, self._tokens + (now - self._last_refill) * rate)
            self._last_refill = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            await asyncio.sleep(max(0.05, (1.0 - self._tokens) / rate))

    async def watchdog(self) -> None:
        limit = float(self.cfg.get("bridge", "watchdog_no_frames_minutes", default=10)) * 60
        while True:
            await asyncio.sleep(30)
            lag = self.max.seconds_since_frame() if self.max is not None else limit + 1
            if lag > limit and not self._alerted:
                self._alerted = True
                await self.api.say(f"⚠ Нет входящих фреймов MAX {lag / 60:.0f} мин")
            elif lag < limit:
                self._alerted = False

    async def backfill(self) -> None:
        if not self.cfg.get("bridge", "backfill", "enabled", default=False):
            return
        if self.max is None:
            return
        batch = int(self.cfg.get("bridge", "backfill", "batch", default=50))
        for chat_id in self.cfg.get("bridge", "subscribe_chats", default=[]) or []:
            resp = await self.max.request("get_history", {
                "chatId": int(chat_id), "from": 0, "forward": True,
                "back": 0, "getMessages": True, "count": batch,
            })
            messages = resp.payload.get("messages") or []
            for item in messages if isinstance(messages, list) else []:
                if not isinstance(item, dict):
                    continue
                ev = MaxEvent(op=self.cfg.opcodes.get("incoming"), seq=0,
                              direction="receive",
                              payload={"chatId": int(chat_id), "message": item})
                await self.on_max(ev)
            log.info("бэкфилл чата %d: %d сообщений", int(chat_id), len(messages))

    # --- TG -> MAX -----------------------------------------------------------
    async def on_tg_message(self, msg: dict, edited: bool = False) -> None:
        sender = (msg.get("from") or {}).get("id")
        if sender != self.cfg.tg_my_user_id:
            log.info("TG update: отправитель %s не мой (%s) — игнор",
                     sender, self.cfg.tg_my_user_id)
            return
        topic = int(msg.get("message_thread_id") or 0)
        text = str(msg.get("text") or "")
        if topic == self.service_topic:
            await self.on_command(text)
            return
        # /del ответом на сообщение — удалить его и в MAX (Bot API не шлёт
        # уведомлений об удалении, поэтому обычное удаление в TG не ловится).
        if text.strip().lower().startswith("/del"):
            rep = msg.get("reply_to_message") or {}
            rid = rep.get("message_id") if isinstance(rep, dict) else None
            mapped = self.db.execute("SELECT max_msg_id, max_chat_id FROM messages "
                                     "WHERE tg_msg_id=?", (int(rid),)).fetchone() if rid else None
            if mapped and self.max is not None:
                try:
                    await self.max.delete(int(mapped[1]), [int(mapped[0])])
                    await self.api.call("deleteMessage", {
                        "chat_id": self.cfg.tg_group_id, "message_id": int(rid)})
                    log.info("TG /del %s -> MAX %s", rid, mapped[0])
                except Exception as exc:
                    await self.api.say(f"Не удалось удалить в MAX: {exc}", topic)
            else:
                await self.api.say("Ответь /del на сообщение моста.", topic)
            return
        await self._throttle()
        has_media = bool(msg.get("photo") or msg.get("video")
                         or msg.get("document") or msg.get("voice")
                         or msg.get("video_note"))
        if not text and not has_media:
            if msg.get("audio") or msg.get("sticker") or msg.get("animation"):
                await self.api.say("⚠ Этот тип вложения в MAX пока не поддерживается",
                                   topic)
            return
        tg_id = int(msg.get("message_id") or 0)
        tg_key = "edit" if edited else "new"
        if edited:
            tg_key = "edit:" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
        if not self.mark(f"tg:{tg_id}:{tg_key}"):
            return
        if edited:
            if has_media:
                return
            mapped = self.db.execute("SELECT max_msg_id, max_chat_id FROM messages "
                                     "WHERE tg_msg_id=?", (tg_id,)).fetchone()
            if not mapped:
                return
            if self.max is not None:
                await self.max.edit(int(mapped[1]), int(mapped[0]), text)
                log.info("TG edit %s -> MAX %s", tg_id, mapped[0])
            return
        row = self.db.execute("SELECT max_chat_id FROM chats WHERE topic_id=?", (topic,)).fetchone()
        if not row:
            log.info("топик %d не смаплен на чат MAX", topic)
            await self.api.say("Этот топик не смаплен на чат MAX.", topic)
            return
        chat_id = int(row[0])
        reply_to_max = None
        rep = msg.get("reply_to_message") or {}
        if isinstance(rep, dict) and rep.get("message_id"):
            rr = self.db.execute("SELECT max_msg_id, max_chat_id FROM messages "
                                 "WHERE tg_msg_id=?", (int(rep["message_id"]),)).fetchone()
            if rr and int(rr[1]) == chat_id and int(rr[0]):
                reply_to_max = int(rr[0])
        if has_media:
            try:
                max_id = await self._send_media_max(
                    chat_id, msg, str(msg.get("caption") or "").strip(),
                    reply_to_max)
            except Exception as exc:
                log.error("TG media -> MAX: %s", exc)
                await self.api.say(f"⚠ Медиа в MAX не отправлено: {exc}", topic)
                return
            self.db.execute(
                "INSERT OR REPLACE INTO messages(max_msg_id,max_chat_id,tg_msg_id,"
                "sender_id,created_at,kind) VALUES(?,?,?,?,?,?)",
                (max_id, chat_id, tg_id, self.cfg.my_id,
                 asyncio.get_running_loop().time(), "out_media"))
            self.db.commit()
            self._remember(max_id, chat_id, tg_id)
            log.info("TG %s -> MAX %s (медиа, чат %d)", tg_id, max_id, chat_id)
            if msg.get("document") and str(msg.get("caption") or "").strip():
                await self.api.say(
                    "⚠ Подпись к файлу в MAX приложить нельзя — она ушла "
                    "отдельным сообщением.", topic)
            return
        if self.max is None:
            return
        snippet = ""
        if isinstance(rep, dict):
            snippet = str(rep.get("text") or rep.get("caption") or "").strip()
            snippet = snippet.splitlines()[0][:80] if snippet else ""
        # Нативный reply из TG пока выключен: формат `link` у MAX — сжатый
        # бинарный блоб, и подстановка чужого id рвёт WSS (close 1006),
        # после чего падает и загрузка медиа. Ответ уходит цитатой.
        # Включается через bridge.native_reply.
        native = bool(self.cfg.get("bridge", "native_reply", default=False))
        send_text = text
        if reply_to_max is not None and not native:
            send_text = f"↩ {snippet}\n{text}" if snippet else text
            reply_to_max = None
        try:
            max_id = await self._send_max(
                chat_id, send_text, reply_to_max,
                attempts=1 if reply_to_max is not None else 3)
        except Exception as exc:
            log.warning("send -> MAX failed (%s)", exc)
            if reply_to_max is not None:
                # Сервер мог отвергнуть reply-кадр (рвёт WSS). Повторяем без
                # reply, но с цитатой, чтобы сообщение всё равно дошло.
                send_text = f"↩ {snippet}\n{text}" if snippet else text
                reply_to_max = None
                log.warning("reply -> MAX не прошёл, повтор без reply (цитатой)")
            try:
                max_id = await self._send_max(chat_id, send_text, None)
            except Exception as exc2:
                log.error("send -> MAX retry failed: %s", exc2)
                await self.api.say(f"⚠ Не удалось отправить в MAX: {exc2}", topic)
                return
        self.db.execute(
            "INSERT OR REPLACE INTO messages(max_msg_id,max_chat_id,tg_msg_id,"
            "sender_id,created_at,kind) VALUES(?,?,?,?,?,?)",
            (max_id, chat_id, tg_id, self.cfg.my_id,
             asyncio.get_running_loop().time(), "out"))
        self.db.commit()
        self._remember(max_id, chat_id, tg_id)
        log.info("TG %s -> MAX %s (чат %d)%s", tg_id, max_id, chat_id,
                 " [reply]" if reply_to_max else "")

    async def on_command(self, text: str) -> None:
        cmd, _, arg = text.strip().partition(" ")
        arg = arg.strip()
        if cmd == "/login":
            self.waiting = True
            await self.api.say("Жду /har <путь> или /token <токен>.", self.service_topic)
        elif cmd in ("/har", "/token"):
            try:
                token = extract_token(Path(arg)) if cmd == "/har" else arg
                if cmd == "/har" and not Path(arg).is_absolute():
                    token = extract_token(self.cfg.path.parent / arg)
            except Exception as exc:
                await self.api.say(f"Ошибка: {exc}", self.service_topic)
                return
            if not token:
                await self.api.say("Нет данных.", self.service_topic)
                return
            try:
                ok = await self.login.validate_token(token)
            except Exception as exc:
                await self.api.say(f"Ошибка проверки: {exc}", self.service_topic)
                return
            if not ok:
                await self.api.say("MAX отверг данные.", self.service_topic)
                return
            save_token(self.cfg.max_token_file, token)
            self.waiting = False
            self.bump("login")
            await self.api.say("✅ Токен сохранён, переподключаюсь.", self.service_topic)
        elif cmd == "/logout":
            self.cfg.max_token_file.unlink(missing_ok=True)
            self.waiting = False
            self.bump("logout")
            await self.api.say("Токен удалён.", self.service_topic)
        elif cmd == "/status":
            state = "есть" if self.cfg.max_token_file.exists() else "нет"
            await self.api.say(f"MAX: токен {state}; v={self.version()}; "
                               f"жду данных: {'да' if self.waiting else 'нет'}",
                               self.service_topic)
        else:
            await self.api.say("Команды: /login /har /token /logout /status; "
                               "в топике — /del ответом на сообщение", self.service_topic)

    async def poll_tg(self) -> None:
        offset = self.offset()
        while True:
            updates = await self.api.updates(offset)
            if not updates:
                # Прокси/сеть лежит — не крутимся вхолостую.
                await asyncio.sleep(5.0 if self.api.offline else 0.3)
                continue
            for upd in updates:
                new_offset = int(upd.get("update_id", 0)) + 1
                msg = upd.get("message") or {}
                edited = upd.get("edited_message") or {}
                # Диагностика: видно КАЖДОЕ полученное сообщение, даже если
                # оно дальше отфильтруется — иначе «не дошло» не отличить
                # от «бот не получил».
                if msg:
                    kinds = [k for k in ("text", "photo", "video", "document",
                                         "voice", "audio", "sticker", "animation")
                             if msg.get(k)]
                    log.info("TG update %s: чат %s, топик %s, %s",
                             upd.get("update_id"),
                             (msg.get("chat") or {}).get("id"),
                             msg.get("message_thread_id"),
                             kinds)
                try:
                    if msg:
                        try:
                            await self.on_tg_message(msg)
                        except Exception as exc:
                            log.exception("TG update error: %s", exc)
                    if edited:
                        try:
                            await self.on_tg_message(edited, edited=True)
                        except Exception as exc:
                            log.exception("TG edit error: %s", exc)
                finally:
                    # offset сохраняем ПОСЛЕ обработки: иначе сообщение,
                    # пришедшее в момент перезапуска, теряется навсегда
                    # (Telegram считает апдейт доставленным).
                    offset = new_offset
                    self.save_offset(offset)

    async def _pump_test_commands(self) -> None:
        """Тестовый хук: выполнить команду из state/test_cmd.json.

        Нужен, чтобы гонять тракт TG→MAX на живой сессии моста, не открывая
        вторую MAX-сессию (сервер её выкидывает) и не трогая чужие топики.
        Файл читается и удаляется; формат:
          {"action": "tg",   "messages": [<как в апдейте Telegram>, ...]}
          {"action": "text", "chat": <id>, "text": "...",
           "reply_to": <max_id|null>}
        """
        path = self.cfg.path.parent / "state" / "test_cmd.json"
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            log.error("тест-хук: битый JSON: %s", exc)
            path.unlink(missing_ok=True)
            return
        path.unlink(missing_ok=True)
        if not isinstance(payload, dict):
            return
        action = str(payload.get("action") or "")
        log.info("тест-хук: %s", action)
        # Выполняем ОТДЕЛЬНОЙ задачей: медиа-операции (скачивание из TG +
        # загрузка в MAX) идут десятки секунд и не должны блокировать цикл
        # чтения MAX (иначе встают ACK/heartbeat и соединение умирает).
        asyncio.create_task(self._run_test_action(action, payload),
                            name=f"test-{action}")

    async def _run_test_action(self, action: str, payload: dict) -> None:
        task = asyncio.create_task(self._do_test_action(action, payload),
                                   name=f"test-run-{action}")
        done, pending = await asyncio.wait({task}, timeout=150)
        if pending:
            task.cancel()
            log.error("тест-хук: %s не завершилось за 150 c", action)
            return
        exc = task.exception()
        if exc is not None:
            # Отдельно от «не завершилось»: внутренние таймауты (aiohttp,
            # ожидание opcode) раньше писались как будто завис весь тест.
            log.error("тест-хук: %s упало: %r", action, exc)

    async def _do_test_action(self, action: str, payload: dict) -> None:
        if action == "tg":
            for item in payload.get("messages") or []:
                if isinstance(item, dict):
                    await self.on_tg_message(item)
        elif action == "text":
            reply_to = payload.get("reply_to")
            mid = await self._send_max(
                int(payload["chat"]), str(payload.get("text") or ""),
                int(reply_to) if reply_to else None)
            log.info("тест-хук: MAX id=%s", mid)
        elif action == "photo_token":
            assert self.max is not None
            mid = await self.max.send_photo(int(payload["chat"]),
                                            str(payload["token"]))
            log.info("тест-хук: фото по готовому токену -> MAX id=%s", mid)
        elif action == "photo_frame":
            # A/B-проверка: готовый кадр из HAR, опционально со своим токеном
            assert self.max is not None
            chat = int(payload.get("chat") or 0)
            raw = bytearray(bytes.fromhex(str(payload["frame"])))
            tok = str(payload.get("token") or "")
            if payload.get("upload"):
                data = Path(str(payload["path"])).read_bytes()
                tok = await self.max.upload_photo(
                    data, Path(str(payload["path"])).name, "image/jpeg")
            if tok:
                p = bytes(raw).find(b"photoToken") + 10
                b = raw[p]
                if b == 0xD9:
                    end = p + 2 + raw[p + 1]
                elif b == 0xDA:
                    end = p + 3 + int.from_bytes(raw[p + 1:p + 3], "big")
                else:
                    raise RuntimeError(f"неизвестный маркер токена {hex(b)}")
                raw[p:end] = self.max._msgpack_str(tok)
                log.info("тест-хук: токен %d симв. (было %d)", len(tok), end - p)
            if chat:
                m = bytes(raw).find(bytes.fromhex("c70901cf"))
                raw[m + 4:m + 12] = __import__("struct").pack(">q", chat)
            raw[8:10] = (len(raw) - 10).to_bytes(2, "big")
            await self.max.prepare_attach(chat, "PHOTO")
            ev = await self.max._send_frame(bytearray(raw))
            log.info("тест-хук: кадр из HAR прошёл -> %s",
                     (ev.payload.get("message") or {}).get("id"))
        elif action == "media_pipeline":
            # Полный реальный тракт TG-медиа -> MAX, но файл берём локальный:
            # подменяем только скачивание из Telegram.
            p = Path(str(payload["path"]))
            data_bytes = p.read_bytes()
            kind = str(payload.get("kind") or "photo")
            orig = self._tg_file

            async def fake_tg_file(file_id: str):
                return data_bytes, str(p)

            self._tg_file = fake_tg_file  # type: ignore[method-assign]
            ext = p.suffix.lower() or ".jpg"
            fake_msg = {
                "photo": [{"file_id": "LOCAL" + "0" * 20 + ext,
                           "file_size": len(data_bytes)}],
                "document": {"file_id": "LOCALDOC", "file_name": p.name},
                "video": {"file_id": "LOCALVID", "file_size": len(data_bytes)},
                "voice": {"file_id": "LOCALVOICE", "duration": 3,
                          "file_size": len(data_bytes)},
                "video_note": {"file_id": "LOCALNOTE", "duration": 3,
                               "file_size": len(data_bytes)},
            }
            if kind == "photo":
                for k in ("document", "video", "voice", "video_note"):
                    fake_msg.pop(k)
            elif kind == "document":
                for k in ("photo", "video", "voice", "video_note"):
                    fake_msg.pop(k)
            elif kind == "video":
                for k in ("photo", "document", "voice", "video_note"):
                    fake_msg.pop(k)
            elif kind == "voice":
                for k in ("photo", "document", "video", "video_note"):
                    fake_msg.pop(k)
            elif kind == "video_note":
                for k in ("photo", "document", "video", "voice"):
                    fake_msg.pop(k)
            try:
                mid = await self._send_media_max(
                    int(payload["chat"]), fake_msg,
                    str(payload.get("caption") or ""),
                    int(payload["reply_to"]) if payload.get("reply_to") else None)
                log.info("тест-хук: media_pipeline (%s) -> MAX id=%s", kind, mid)
            finally:
                self._tg_file = orig  # type: ignore[method-assign]
        elif action == "max_event":
            # Синтетическое входящее событие MAX (проверка разбора без
            # ожидания живого сообщения). payload — как в кадре.
            from bridge.models import MaxEvent
            ev = MaxEvent(op=int(payload.get("op") or 0x0080), seq=0,
                          direction="receive",
                          payload=payload.get("payload") or {})
            await self.on_max(ev)
            log.info("тест-хук: max_event обработан")
        elif action == "file_path":
            assert self.max is not None
            p = Path(str(payload["path"]))
            data = p.read_bytes()
            fid = await self._upload_file_ready(data, p.name)
            rt = payload.get("reply_to")
            mid = await self.max.send_file(int(payload["chat"]), int(fid), p.name,
                                           int(rt) if rt else None)
            log.info("тест-хук: файл -> MAX id=%s (fileId=%s, reply_to=%s)",
                     mid, fid, rt)
        elif action == "video_path":
            assert self.max is not None
            p = Path(str(payload["path"]))
            data = p.read_bytes()
            vid, tok = await self._upload_video_ready(data, p.name)
            rt = payload.get("reply_to")
            mid = await self.max.send_video(int(payload["chat"]), vid, tok,
                                            str(payload.get("caption") or ""),
                                            int(rt) if rt else None)
            log.info("тест-хук: видео -> MAX id=%s (reply_to=%s)", mid, rt)
        elif action == "photo_file":
            # проверка протокола: загрузить локальный файл и отправить фото
            p = Path(str(payload["path"]))
            assert self.max is not None
            if payload.get("prepare_first"):
                await self.max.prepare_attach(int(payload["chat"]), "PHOTO")
            data = p.read_bytes()
            assert self.max is not None
            token = await self.max.upload_photo(data, p.name, "image/jpeg")
            log.info("тест-хук: пауза %s c перед кадром (жив ли WSS?)",
                     payload.get("pause", 5))
            await asyncio.sleep(float(payload.get("pause", 5)))
            alive = self.max.ws is not None and not self.max.ws.closed
            log.info("тест-хук: после загрузки WSS жив=%s", alive)
            if not alive:
                raise ConnectionError("WSS умер уже на загрузке")
            mid = await self.max.send_photo(int(payload["chat"]), token,
                                            str(payload.get("caption") or ""),
                                            int(payload["reply_to"])
                                            if payload.get("reply_to") else None)
            log.info("тест-хук: фото из файла -> MAX id=%s (reply_to=%s)",
                     mid, payload.get("reply_to"))
        else:
            log.warning("тест-хук: неизвестное действие %r", action)

    async def run(self) -> None:
        tg_task = asyncio.create_task(self.poll_tg(), name="tg-poll")
        wd_task = asyncio.create_task(self.watchdog(), name="watchdog")
        try:
            while True:
                v = self.version()
                self.max = WebMaxTransport(self.cfg)
                try:
                    await self.max.connect()
                    await self.max.request("get_chats", {"chatIds": [0]})
                    for cid in self.cfg.get("bridge", "subscribe_chats", default=[]) or []:
                        await self.max.request("subscribe", {"chatId": int(cid), "subscribe": True})
                    await self.max.request("sync", {"cursor": "0", "count": 15})
                    await self.backfill()
                except NeedLogin:
                    self.max_ready.clear()
                    log.info("нет токена — жду /login")
                    await asyncio.sleep(5)
                    continue
                except Exception as exc:
                    self.max_ready.clear()
                    hint = ""
                    if "disconnected" in str(exc).lower() or "connector" in str(exc).lower():
                        hint = " (проверь SOCKS5-прокси 127.0.0.1:1080)"
                    log.warning("MAX connect: %s%s", exc, hint)
                    await asyncio.sleep(10)
                    continue
                log.info("MAX подключён (v=%s)", v)
                self.max_ready.set()
                self.reader_done.clear()
                next_chat_poll = 0.0
                poll_period = float(self.cfg.get("bridge", "chat_poll_seconds", default=15))
                try:
                    # Читаем события напрямую из очереди транспорта: никакого
                    # дополнительного async-генератора (искусственные ошибки
                    # 'aclose(): asynchronous generator is already running').
                    while (self.version() == v and not self.reader_done.is_set()
                           and self.max.ws is not None and not self.max.ws.closed):
                        try:
                            ev = await asyncio.wait_for(self.max.events.get(), timeout=1.0)
                        except asyncio.TimeoutError:
                            ev = None
                        if ev is not None:
                            try:
                                await self.on_max(ev)
                            except Exception as exc:
                                log.exception("MAX event error: %s", exc)
                        try:
                            await self._pump_test_commands()
                        except Exception as exc:
                            log.exception("тест-хук: %s", exc)
                        now = time.monotonic()
                        if poll_period > 0 and now >= next_chat_poll:
                            next_chat_poll = now + poll_period
                            try:
                                await self.discover_chats()
                            except Exception as exc:
                                log.warning("discover_chats: %s", exc)
                finally:
                    self.max_ready.clear()
                    if self.max is not None:
                        try:
                            await self.max.close()
                        except Exception:
                            pass
                    log.info("переподключение MAX")
        finally:
            tg_task.cancel()
            wd_task.cancel()


async def _task_dumper(period: float) -> None:
    """Диагностика: печатает стеки всех живых asyncio-задач (в stderr)."""
    import traceback
    while True:
        await asyncio.sleep(period)
        for task in asyncio.all_tasks():
            if task is asyncio.current_task() or task.done():
                continue
            print(f"--- задача {task.get_name()} ---", file=sys.stderr, flush=True)
            for frame in task.get_stack():
                text = "".join(traceback.format_stack(frame))
                print(text[-800:], file=sys.stderr, flush=True)


async def main() -> None:
    cfg = Config.load("config.json")
    setup_logging(cfg)
    # Диагностика зависаний: периодический дамп стеков всех задач в stderr.
    dump_s = float(cfg.get("debug", "dump_stacks_seconds", default=0) or 0)
    if dump_s > 0:
        import faulthandler
        faulthandler.dump_traceback_later(dump_s, repeat=True)
        asyncio.create_task(_task_dumper(dump_s), name="task-dump")
        log.info("дамп стеков каждые %.0f c включён", dump_s)
    proxy, relay = await make_proxy(cfg)
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(cfg.db_path)
    db.executescript(SCHEMA)
    # миграция: в старых БД нет колонки kind (тип сообщения в TG)
    cols = {r[1] for r in db.execute("PRAGMA table_info(messages)")}
    if "kind" not in cols:
        db.execute("ALTER TABLE messages ADD COLUMN kind TEXT")
        log.info("миграция БД: добавлена колонка messages.kind")
    db.commit()
    api = Api(cfg, proxy)
    login = MaxLogin(cfg)
    login.notifier.proxy = proxy
    app = App(cfg, proxy, api, db, login)
    await app.api.say("Мост запущен. /login /logout /status", app.service_topic)
    try:
        await app.run()
    finally:
        await api.close()
        await login.close()
        if relay is not None:
            await relay.stop()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
