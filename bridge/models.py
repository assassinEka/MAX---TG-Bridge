"""Типы ядра. Никаких aiogram/Telethon/MAX-типов здесь нет."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class Origin(str, Enum):
    TG = "tg"          # операция пришла из Telegram
    MAX = "max"        # операция пришла из MAX
    BRIDGE = "bridge"  # служебная операция самого моста


class MediaKind(str, Enum):
    PHOTO = "photo"
    VIDEO = "video"
    AUDIO = "audio"       # музыка -> sendAudio
    VOICE = "voice"       # голосовуха -> sendVoice
    VIDEO_NOTE = "video_note"
    DOCUMENT = "document"
    STICKER = "sticker"


@dataclass(slots=True)
class Media:
    """Описание вложения. Путь всегда на диске, никогда в RAM."""

    kind: MediaKind
    path: Path | None = None          # локальный файл (stream-to-disk)
    url: str | None = None            # публичная подписанная ссылка MAX
    file_id: str | None = None        # кэш Telegram file_id
    mime: str | None = None
    size: int = 0
    duration: int | None = None
    title: str | None = None
    performer: str | None = None
    width: int | None = None
    height: int | None = None
    raw_type: str | None = None       # наблюдаемый MAX _type, без догадок
    link: str | None = None           # ссылка-источник, если файл слишком большой
    cache_key: str = ""               # photoToken/videoId/audioId для file_id-кэша


@dataclass(slots=True)
class Incoming:
    """Единый вход из любого TgTransport."""

    msg_id: int
    topic_id: int
    sender_id: int
    text: str = ""
    media: Media | None = None
    reply_to: int | None = None
    # Дополнительные поля с дефолтами: ядро не обязано о них знать.
    edit_of: int | None = None
    deleted: bool = False
    origin: Origin = Origin.TG


@dataclass(slots=True)
class MaxEvent:
    """Сырое событие MAX после разбора фрейма."""

    op: int
    seq: int
    direction: str
    payload: dict[str, Any] = field(default_factory=dict)
    raw: bytes = b""
    ts: float = 0.0
    decode_error: str | None = None


@dataclass(slots=True)
class OutboundMessage:
    text: str = ""
    media: Media | None = None
    reply_to: int | None = None
    parse_mode: str | None = None


@dataclass(slots=True)
class QrSession:
    track_id: str
    qr_link: str
    expires_at: int
    ttl_ms: int


@dataclass(slots=True)
class TgCaps:
    max_download_bytes: int
    max_upload_bytes: int
    supports_forum_topics: bool = True
    mode: str = "bot"


@dataclass(slots=True)
class MaxCaps:
    """Только наблюдаемые лимиты; неподтверждённое остаётся None."""

    media_size_limit_bytes: int | None = None
    edit_window_seconds: int | None = None
    delete_window_seconds: int | None = None
