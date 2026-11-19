"""Сетевой слой: SOCKS5-прокси без внешних зависимостей.

aiohttp умеет только HTTP-прокси, а у нас socks5://127.0.0.1:1080.
Поднимаем крошечный локальный HTTP-CONNECT -> SOCKS5 релей и отдаём aiohttp
обычный http:// URL. Никаких aiohttp-socks/PySocks не нужно.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger("net")


class Socks5Relay:
    def __init__(self, socks_host: str = "127.0.0.1", socks_port: int = 1080,
                 listen_host: str = "127.0.0.1") -> None:
        self.socks_host = socks_host
        self.socks_port = socks_port
        self.listen_host = listen_host
        self.server: asyncio.AbstractServer | None = None
        self.port = 0
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def proxy_url(self) -> str:
        return f"http://{self.listen_host}:{self.port}"

    async def start(self) -> str:
        async def tracked(reader: asyncio.StreamReader,
                          writer: asyncio.StreamWriter) -> None:
            task = asyncio.current_task()
            if task is not None:
                self._tasks.add(task)
            try:
                await self._handle(reader, writer)
            finally:
                if task is not None:
                    self._tasks.discard(task)

        self.server = await asyncio.start_server(tracked, self.listen_host, 0)
        self.port = self.server.sockets[0].getsockname()[1]
        log.info("SOCKS5 relay: %s -> socks5://%s:%d",
                 self.proxy_url, self.socks_host, self.socks_port)
        return self.proxy_url

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self.tasks_wait():
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    def tasks_wait(self) -> bool:
        return bool(self._tasks)

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        upstream_w: asyncio.StreamWriter | None = None
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=15)
            line = head.split(b"\r\n", 1)[0].decode("ascii", "replace")
            parts = line.split()
            if len(parts) < 3 or parts[0].upper() != "CONNECT":
                writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                return
            target = parts[1]
            if ":" in target:
                host, port_s = target.rsplit(":", 1)
                port = int(port_s)
            else:
                host, port = target, 443
            upstream_r, upstream_w = await asyncio.open_connection(
                self.socks_host, self.socks_port)
            await self._socks5_handshake(upstream_r, upstream_w, host, port)
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            # Обе «трубы» гоняем параллельно, но завершаем, как только
            # закрылась ЛЮБАЯ сторона. Раньше стоял gather(): если клиент
            # отваливался, обратная труба продолжала ждать вечно, задача и
            # сокет к SOCKS5 утекали. За десятки реконнектов это забивало
            # прокси — отсюда и «случайные» close 1006, и зависания TG.
            pipes = [asyncio.create_task(self._pipe(reader, upstream_w)),
                     asyncio.create_task(self._pipe(upstream_r, writer))]
            try:
                await asyncio.wait(pipes, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in pipes:
                    if not t.done():
                        t.cancel()
                await asyncio.gather(*pipes, return_exceptions=True)
        except Exception as exc:
            log.debug("relay error: %s", exc)
        finally:
            if upstream_w is not None:
                upstream_w.close()
            writer.close()

    @staticmethod
    async def _socks5_handshake(reader: asyncio.StreamReader,
                                writer: asyncio.StreamWriter,
                                host: str, port: int) -> None:
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        reply = await asyncio.wait_for(reader.readexactly(2), timeout=10)
        if reply[0] != 0x05 or reply[1] != 0x00:
            raise RuntimeError(f"SOCKS5 auth refused: {reply.hex()}")

        host_b = host.encode("idna")
        req = b"\x05\x01\x00\x03" + bytes([len(host_b)]) + host_b
        req += port.to_bytes(2, "big")
        writer.write(req)
        await writer.drain()
        head = await asyncio.wait_for(reader.readexactly(4), timeout=10)
        if head[1] != 0x00:
            raise RuntimeError(f"SOCKS5 connect refused: {head.hex()}")
        atyp = head[3]
        if atyp == 0x01:
            await reader.readexactly(4)
        elif atyp == 0x03:
            n = (await reader.readexactly(1))[0]
            await reader.readexactly(n)
        elif atyp == 0x04:
            await reader.readexactly(16)
        else:
            raise RuntimeError(f"SOCKS5 bad ATYP: {atyp}")
        await reader.readexactly(2)

    @staticmethod
    async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        while True:
            chunk = await src.read(65536)
            if not chunk:
                break
            dst.write(chunk)
            await dst.drain()
        # Полузакрытие: даём второй стороне увидеть EOF и завершиться самой.
        try:
            if dst.can_write_eof():
                dst.write_eof()
        except Exception:
            pass


def parse_proxy(value: str | None) -> tuple[str, str, int] | None:
    """socks5://host:port или http://host:port -> (scheme, host, port)."""
    if not value:
        return None
    scheme, _, rest = value.partition("://")
    if not rest:
        return None
    host, _, port_s = rest.partition(":")
    return scheme.lower(), host, int(port_s or 1080)


async def make_proxy(cfg: Any) -> tuple[str | None, Socks5Relay | None]:
    """Вернуть (proxy_url для aiohttp, объект релея, который надо закрыть)."""
    parsed = parse_proxy(cfg.get("network", "proxy"))
    if parsed is None:
        return None, None
    scheme, host, port = parsed
    if scheme in ("socks5", "socks5h"):
        relay = Socks5Relay(host, port)
        return await relay.start(), relay
    if scheme in ("http", "https"):
        return f"{scheme}://{host}:{port}", None
    raise ValueError(f"неизвестная схема прокси: {scheme}")
