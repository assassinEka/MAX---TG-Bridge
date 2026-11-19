"""Система входа в MAX: QR с нуля, авто-продление, валидация, алерты в TG.

Работает без aiogram/Telethon: MAX — через aiohttp WSS, Telegram — через
Bot API HTTP. Поэтому вход можно прогнать здесь, на десктопе, до установки
боевых зависимостей.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import aiohttp

from .config import Config
from .max import codec
from .models import QrSession
from .net import make_proxy
from .qr import make_png

log = logging.getLogger("login")


def save_token(path: Path, token: str) -> None:
    """Атомарно, права 600, без бэкапа в облако."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token.strip() + "\n")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    log.info("токен MAX сохранён: %s", path)


class TelegramNotifier:
    def __init__(self, cfg: Config, proxy: str | None = None) -> None:
        self.cfg = cfg
        self.proxy = proxy
        self.session: aiohttp.ClientSession | None = None

    async def setup(self) -> None:
        if self.session is None:
            self.session = aiohttp.ClientSession()

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.cfg.tg_bot_token}/{method}"

    async def notice(self, text: str) -> None:
        await self.setup()
        payload: dict[str, Any] = {
            "chat_id": self.cfg.tg_group_id,
            "text": text[:4000],
            "disable_web_page_preview": False,
        }
        topic = int(self.cfg.get("tg", "service_topic_id", default=0))
        if topic:
            payload["message_thread_id"] = topic
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                assert self.session is not None
                async with self.session.post(
                        self._url("sendMessage"), json=payload, proxy=self.proxy,
                        headers={"Connection": "close"},
                        timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    body = await resp.text()
                    if resp.status != 200:
                        log.error("telegram notice failed: %s", body[:300])
                        return
                    try:
                        msg_id = json.loads(body)["result"]["message_id"]
                    except Exception:
                        msg_id = "?"
                    log.info("telegram notice sent: id=%s", msg_id)
                    return
            except (aiohttp.ClientConnectionError, OSError) as exc:
                last_exc = exc
                log.warning("telegram notice retry %d: %s", attempt + 1, exc)
                await self.close()
                await self.setup()
        log.error("telegram notice не отправлен: %s", last_exc)

    async def send_photo(self, path: Path, caption: str = "") -> None:
        await self.setup()
        topic = int(self.cfg.get("tg", "service_topic_id", default=0))
        for attempt in range(3):
            try:
                assert self.session is not None
                with path.open("rb") as fh:
                    form = aiohttp.FormData()
                    form.add_field("chat_id", str(self.cfg.tg_group_id))
                    if topic:
                        form.add_field("message_thread_id", str(topic))
                    form.add_field("caption", caption[:1000])
                    form.add_field("photo", fh, filename=path.name,
                                   content_type="image/png")
                    async with self.session.post(
                            self._url("sendPhoto"), data=form, proxy=self.proxy,
                            timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        body = await resp.text()
                        if resp.status != 200:
                            log.error("sendPhoto failed: %s", body[:300])
                            return
                        log.info("QR photo sent")
                        return
            except (aiohttp.ClientConnectionError, OSError) as exc:
                log.warning("sendPhoto retry %d: %s", attempt + 1, exc)
                await self.close()
                await self.setup()
        log.error("QR photo не отправлен")

    async def get_updates(self, offset: int | None, timeout: int = 10) -> list[dict]:
        await self.setup()
        params: dict[str, Any] = {"timeout": timeout}
        if offset:
            params["offset"] = offset
        for attempt in range(3):
            try:
                assert self.session is not None
                async with self.session.get(
                        self._url("getUpdates"), params=params, proxy=self.proxy,
                        headers={"Connection": "close"},
                        timeout=aiohttp.ClientTimeout(total=timeout + 15)) as resp:
                    body = await resp.json(content_type=None)
                    return list(body.get("result", []))
            except (aiohttp.ClientConnectionError, OSError) as exc:
                log.warning("getUpdates retry %d: %s", attempt + 1, exc)
                await self.close()
                await self.setup()
        return []


class TelegramCommands:
    """Минимальный обработчик команд в сервисном топике без aiogram."""

    def __init__(self, cfg: Config, notifier: TelegramNotifier,
                 login: "MaxLogin", ready: asyncio.Future[str]) -> None:
        self.cfg = cfg
        self.notifier = notifier
        self.login = login
        self.ready = ready
        self.topic = int(cfg.get("tg", "service_topic_id", default=0))
        self.offset_file = cfg.path.parent / "state" / "tg_offset.txt"
        self.task: asyncio.Task[None] | None = None

    def _load_offset(self) -> int:
        try:
            return int(self.offset_file.read_text(encoding="utf-8").strip())
        except Exception:
            return 0

    def _save_offset(self, value: int) -> None:
        self.offset_file.parent.mkdir(parents=True, exist_ok=True)
        self.offset_file.write_text(str(value), encoding="utf-8")

    async def run(self) -> None:
        offset = self._load_offset()
        while True:
            try:
                updates = await self.notifier.get_updates(offset)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("getUpdates error: %s", exc)
                await asyncio.sleep(3)
                continue
            for upd in updates:
                offset = int(upd.get("update_id", 0)) + 1
                self._save_offset(offset)
                msg = upd.get("message") or {}
                if self.topic and int(msg.get("message_thread_id") or 0) != self.topic:
                    continue
                sender = (msg.get("from") or {}).get("id")
                if sender != self.cfg.tg_my_user_id:
                    continue
                text = str(msg.get("text") or "").strip()
                await self._handle(text)

    async def _handle(self, text: str) -> None:
        if text.startswith("/token"):
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                await self.notifier.notice("Формат: /token <токен из кадра 0x0013>")
                return
            token = parts[1].strip()
            await self.notifier.notice("Проверяю токен в MAX…")
            try:
                ok = await self.login.validate_token(token)
            except Exception as exc:
                await self.notifier.notice(f"Ошибка проверки: {exc}")
                return
            if not ok:
                await self.notifier.notice("❌ MAX отверг токен. Сними свежий из 0x0013.")
                return
            save_token(self.cfg.max_token_file, token)
            await self.notifier.notice("✅ Токен принят и сохранён.")
            if not self.ready.done():
                self.ready.set_result(token)
        elif text.startswith("/login"):
            try:
                await self.login.start_qr(announce=True)
            except RuntimeError as exc:
                if "qr_login.disabled" in str(exc):
                    await self.notifier.notice(
                        "⚠ QR-вход на сервере MAX отключён.\n"
                        "Войди в https://web.max.ru на десктопе, открой DevTools → "
                        "Network → WS → api.oneme.ru/websocket, найди отправленный кадр "
                        "0x0013 и пришли мне сюда команду:\n/token <строка token>")
                else:
                    await self.notifier.notice(f"QR недоступен: {exc}")
        elif text.startswith("/status"):
            await self.notifier.notice("Лисенер команд активен; MAX см. /login")


class MaxLogin:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.proxy: str | None = None
        self._relay: Any = None
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self.seq = 0
        self._hello_sent = False
        self.notifier = TelegramNotifier(cfg)
        self._headers = {
            "Origin": cfg.get("max", "origin", default="https://web.max.ru"),
            "User-Agent": cfg.get("max", "user_agent", default="Mozilla/5.0"),
            "Accept-Language": cfg.get("max", "locale", default="ru"),
        }

    async def setup(self) -> None:
        if self.proxy is not None:
            return
        self.proxy, self._relay = await make_proxy(self.cfg)
        self.notifier.proxy = self.proxy
        self._session = aiohttp.ClientSession(headers=self._headers)

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        if self._session is not None:
            await self._session.close()
            self._session = None
        if self._relay is not None:
            await self._relay.stop()
            self._relay = None
        await self.notifier.close()

    async def _open_ws(self) -> None:
        await self.setup()
        assert self._session is not None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        self._ws = await self._session.ws_connect(
            self.cfg.max_ws_url,
            ssl=not bool(self.cfg.get("max", "insecure", default=False)),
            proxy=self.proxy,
            heartbeat=None,
        )
        log.info("[login] WSS открыт%s", f" через {self.proxy}" if self.proxy else "")

    def _render_frame(self, op_name: str, op: int, seq: int, flags: int,
                      payload: dict[str, Any], prefix: bytes) -> bytes:
        """clientHello/auth шлём побайтовыми шаблонами из HAR.

        Наш упрощённый msgpack-энкодер не знает кастомных полей clientHello,
        поэтому для этих двух opcode'ов берём реальный кадр из разведки и
        подменяем только seq и токен. Остальные opcode — обычная сборка.
        """
        name = {"client_hello": "client_hello.bin", "auth": "auth.bin"}.get(op_name)
        if name:
            path = self.cfg.path.parent / "fixtures" / name
            if path.exists():
                raw = bytearray(path.read_bytes())
                raw[2:4] = seq.to_bytes(2, "big")
                if op_name == "auth":
                    token = str(payload.get("token") or "")
                    mark = b"__TOKEN__"
                    pos = bytes(raw).find(mark)
                    if pos < 0:
                        raise RuntimeError("в auth-шаблоне нет маркера токена")
                    token_bytes = token.encode("utf-8")
                    # В шаблоне уже сохранён str16-заголовок DA <len> перед маркером.
                    if pos >= 3 and bytes(raw[pos - 3]) == 0xDA:
                        raw[pos - 3:pos] = b"\xda" + len(token_bytes).to_bytes(2, "big")
                    raw[pos:pos + len(mark)] = token_bytes
                    raw[7:10] = (len(raw) - 10).to_bytes(3, "big")
                return bytes(raw)
        return codec.frame(op, seq, payload)

    async def _request(self, op_name: str, payload: dict[str, Any],
                       timeout: float = 12.0) -> dict[str, Any]:
        if self._ws is None or self._ws.closed:
            await self._open_ws()
        assert self._ws is not None
        self.seq += 1
        seq = self.seq
        op = self.cfg.opcodes[op_name]
        flags_s = str(self.cfg.get("max", "flags", op_name, default="0x0000"))
        flags = int(flags_s, 16) if flags_s.lower().startswith("0x") else int(flags_s)
        prefix_s = str(self.cfg.get("max", "prefixes", op_name, default=""))
        prefix = bytes.fromhex(prefix_s.replace(" ", "")) if prefix_s else b""
        await self._ws.send_bytes(self._render_frame(op_name, op, seq, flags, payload, prefix))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = await asyncio.wait_for(self._ws.receive(), timeout=timeout)
            if msg.type == aiohttp.WSMsgType.CLOSE:
                raise ConnectionError("MAX закрыл WSS")
            if msg.type != aiohttp.WSMsgType.BINARY:
                continue
            r_op, r_seq, _, body, err = codec.parse_frame(msg.data)
            if r_seq == seq:
                return {"op": r_op, "payload": body, "decode_error": err,
                        "raw": msg.data}
            log.debug("[login] event op=0x%04X seq=%d", r_op, r_seq)
        raise TimeoutError(f"нет ответа на {op_name}")

    # --- QR ------------------------------------------------------------------
    async def start_qr(self, announce: bool = True) -> QrSession:
        if not self._hello_sent:
            await self._request("client_hello",
                                self.cfg.get("max", "client_hello", default={}))
            self._hello_sent = True
        resp = await self._request("login_qr_start", {})
        p = resp["payload"]
        track_id = str(p.get("trackId") or "")
        qr_link = str(p.get("qrLink") or "")
        raw = resp.get("raw") or b""
        try:
            (self.cfg.path.parent / "state" / "qr_raw.hex").write_text(raw.hex(" "), encoding="utf-8")
        except OSError:
            pass
        # MAX кодирует строку с кастомными int-хвостами; сам URL — до первого
        # управляющего байта (observed: https://max.ru/:auth/B).
        match = re.search(rb"https://max\.ru/:auth/[A-Za-z0-9_-]+", raw)
        if match:
            qr_link = match.group(0).decode("ascii", "replace")
        if not track_id or ":auth/" not in qr_link:
            raise RuntimeError(
                "некорректный ответ 0x0120: " +
                json.dumps(p, ensure_ascii=False, default=str)[:600])
        ttl = p.get("ttl")
        ttl_ms = int(ttl) if isinstance(ttl, int) and 10_000 <= ttl <= 300_000 else 120_000
        session = QrSession(track_id, qr_link, int(p.get("expiresAt") or 0), ttl_ms)
        log.info("[login] QR получен: trackId=%s ttl=%s", track_id, session.ttl_ms)
        try:
            qr_file = self.cfg.path.parent / "state" / "last_qr.txt"
            qr_file.parent.mkdir(parents=True, exist_ok=True)
            qr_file.write_text(f"{qr_link}\n{track_id}\n{time.time()}\n", encoding="utf-8")
        except OSError:
            pass
        # В разведке (HAR(2), кадр 68) qrLink — строка str8 длиной 57 байт:
        # короткий URL + кастомные байты. QR должен кодировать ВСЁ поле.
        qr_bytes: bytes | None = None
        marker = b"\xa6qrLink"
        pos = raw.find(marker)
        if pos >= 0 and pos + 9 <= len(raw) and raw[pos + 7] == 0xD9:
            n = raw[pos + 8]
            qr_bytes = raw[pos + 9:pos + 9 + n]
        if announce:
            await self.notifier.notice(
                "🔐 Вход в MAX по QR.\n"
                "Отсканируй присланную ниже картинку в приложении MAX "
                "(Профиль → сканер QR / вход по QR) и подтверди вход.\n"
                f"trackId: {track_id}\n"
                f"Ссылка живёт ~{max(1, session.ttl_ms // 1000)} с; "
                "если не успеешь — пришлю новую.")
            try:
                qr_path = self.cfg.path.parent / "state" / f"qr_{track_id}.png"
                qr_path.parent.mkdir(parents=True, exist_ok=True)
                make_png(qr_bytes if qr_bytes else qr_link.encode("utf-8"), qr_path)
                await self.notifier.send_photo(
                    qr_path, caption="QR для входа в MAX — отсканируй приложением MAX")
            except Exception as exc:
                log.warning("не удалось отправить QR-картинку: %s", exc)
                await self.notifier.notice(f"Ссылка: {qr_link}")
        return session

    async def poll_once(self, track_id: str) -> str | None:
        resp = await self._request("login_qr_poll", {"trackId": track_id})
        p = resp["payload"]
        status = p.get("status")
        if isinstance(status, dict):
            if status.get("loginAvailable") or status.get("success"):
                return await self._confirm(track_id)
            ttl = status.get("ttl")
            if isinstance(ttl, int) and ttl <= 0:
                return None
        if p.get("loginAvailable"):
            return await self._confirm(track_id)
        return None

    async def _confirm(self, track_id: str) -> str:
        resp = await self._request("login_qr_confirm", {"trackId": track_id})
        token = resp["payload"].get("token")
        if not isinstance(token, str) or len(token) < 100:
            raise RuntimeError(f"в ответе 0x0123 нет токена: {list(resp['payload'])}")
        return token

    async def validate_token(self, token: str) -> bool:
        """Новый WSS -> clientHello -> auth. Только после этого токен считаем живым."""
        saved = self._ws
        self._ws = None
        self.seq = -1  # шаблон из HAR: clientHello seq=0, auth seq=1
        try:
            await self._open_ws()
            await self._request("client_hello",
                                self.cfg.get("max", "client_hello", default={}))
            resp = await self._request("auth", {"token": token})
            body = json.dumps(resp["payload"], ensure_ascii=False, default=str)
            if "error.login.token" in body or "FAIL_LOGIN_TOKEN" in body:
                return False
            return "profile" in resp["payload"]
        finally:
            if self._ws is not None:
                await self._ws.close()
            self._ws = saved

    # --- главный сценарий -----------------------------------------------------
    async def interactive_login(self, reason: str = "") -> str:
        """Полный цикл: QR -> ожидание -> подтверждение -> валидация -> файл."""
        await self.setup()
        if reason:
            await self.notifier.notice(f"🔐 Требуется вход в MAX: {reason}")
        interval = float(self.cfg.get("bridge", "qr_poll_seconds", default=3))
        # 0 = обновлять QR бесконечно, пока пользователь не подтвердит вход.
        max_rounds = int(self.cfg.get("bridge", "qr_max_rounds", default=0))
        round_no = 0
        while max_rounds <= 0 or round_no < max_rounds:
            round_no += 1
            session: QrSession | None = None
            last_error: Exception | None = None
            for attempt in range(3):
                try:
                    session = await self.start_qr(announce=(attempt == 0))
                    break
                except Exception as exc:
                    last_error = exc
                    log.warning("[login] start_qr attempt %d: %s", attempt + 1, exc)
                    if "qr_login.disabled" in str(exc):
                        break
                    self._ws = None
                    await self._open_ws()
                    await self._request("client_hello",
                                        self.cfg.get("max", "client_hello", default={}))
                    await asyncio.sleep(1)

            if session is None:
                if last_error is not None and "qr_login.disabled" in str(last_error):
                    await self.notifier.notice(
                        "⚠ QR-вход отключён сервером MAX. Жду /token в этом топике.\n"
                        "Войди в https://web.max.ru на десктопе → DevTools → Network → WS → "
                        "кадр 0x0013 (send), скопируй строку token и пришли:\n"
                        "/token <токен>")
                    ready: asyncio.Future[str] = asyncio.get_running_loop().create_future()
                    commands = TelegramCommands(self.cfg, self.notifier, self, ready)
                    listener = asyncio.create_task(commands.run(), name="tg-login-commands")
                    try:
                        timeout = float(self.cfg.get("bridge", "token_wait_seconds", default=1800))
                        return await asyncio.wait_for(ready, timeout=timeout)
                    finally:
                        listener.cancel()
                raise RuntimeError("не удалось получить QR после 3 попыток")

            deadline = time.monotonic() + max(30.0, session.ttl_ms / 1000.0 + 5)
            while time.monotonic() < deadline:
                await asyncio.sleep(interval)
                try:
                    token = await self.poll_once(session.track_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("[login] poll error: %s", exc)
                    self._ws = None
                    await self._open_ws()
                    await self._request("client_hello",
                                        self.cfg.get("max", "client_hello", default={}))
                    self._hello_sent = True
                    continue
                if not token:
                    continue
                if not await self.validate_token(token):
                    await self.notifier.notice("⚠ MAX отверг новый токен, пробую ещё раз.")
                    break
                save_token(self.cfg.max_token_file, token)
                await self.notifier.notice("✅ Вход в MAX выполнен, токен сохранён и проверен.")
                return token
            await self.notifier.notice("⏱ QR-сессия истекла, выпускаю новую…")
        raise RuntimeError("не удалось войти в MAX: исчерпаны попытки QR")
