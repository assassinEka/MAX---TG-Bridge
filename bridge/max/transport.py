"""Интерфейс MAX-стороны. Реализация заменяема без правки ядра."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import AsyncIterator

from ..models import MaxCaps, MaxEvent, Media, OutboundMessage, QrSession


class MaxTransport(ABC):
    @abstractmethod
    async def connect(self) -> None:
        """Установить соединение и авторизоваться. NeedLogin -> запрос QR."""

    @abstractmethod
    async def iter_events(self) -> AsyncIterator[MaxEvent]:
        """Поток входящих событий (исключая ответы на наши запросы)."""

    @abstractmethod
    async def send(self, chat_id: int, msg: OutboundMessage) -> int:
        """Отправить сообщение, вернуть max_msg_id из эха сервера."""

    @abstractmethod
    async def edit(self, chat_id: int, max_msg_id: int, text: str) -> None:
        """Правка текста/подписи (в MAX подпись — поле text, F03=OK)."""

    @abstractmethod
    async def delete(self, chat_id: int, max_msg_ids: list[int]) -> None:
        """Удаление у всех (forMe:false — наблюдалось в HAR)."""

    @abstractmethod
    async def upload(self, path: Path, kind: str) -> Media:
        """Загрузить файл, вернуть вложение с наблюдаемым _type."""

    @abstractmethod
    async def download(self, url: str, dest: Path) -> Path:
        """Стрим URL -> диск. Возвращает dest."""

    @abstractmethod
    async def login_qr_start(self) -> QrSession:
        """0x0120: получить trackId/qrLink/expiresAt/ttl."""

    @abstractmethod
    async def login_qr_poll(self, track_id: str) -> str | None:
        """0x0121: вернуть новый token, если loginAvailable, иначе None."""

    @abstractmethod
    async def login_qr_confirm(self, track_id: str) -> str:
        """0x0123: подтвердить вход, вернуть новый token."""

    @abstractmethod
    def caps(self) -> MaxCaps:
        """Неизвестные лимиты = None, не выдумываем числа."""

    @abstractmethod
    async def close(self) -> None:
        ...

    def seconds_since_frame(self) -> float:
        """Для watchdog; Transport без фреймов может вернуть 0."""
        return 0.0

    def save_token(self, token: str) -> None:
        """Сохранить новую сессию (права 600), если transport её поддерживает."""
        raise NotImplementedError("save_token не реализован в этом transport")


class NeedLogin(Exception):
    """Токен отсутствует/отвергнут — нужно QR-перелогинивание."""
