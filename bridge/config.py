"""Конфиг: JSON без внешних зависимостей. Все опкоды/URL/лимиты — здесь."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _op(value: str | int) -> int:
    return int(value, 16) if isinstance(value, str) else int(value)


def _deep_set(obj: dict, keys: tuple[str, ...], value: Any) -> None:
    cur = obj
    for key in keys[:-1]:
        nxt = cur.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[key] = nxt
        cur = nxt
    cur[keys[-1]] = value


@dataclass(slots=True)
class Config:
    raw: dict[str, Any]
    path: Path

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        p = Path(path)
        raw = json.loads(p.read_text(encoding="utf-8"))
        cfg = cls(raw=raw, path=p)
        cfg._load_credentials()
        cfg.validate()
        return cfg

    def _load_credentials(self) -> None:
        """Подмешать секретные настройки из secrets/credentials.json.

        Один файл для всего, что меняется при переезде: токен Telegram-бота,
        группа и id аккаунта, токен MAX и id аккаунта MAX. Путь можно
        переопределить ключом `secrets_file` в корне config.json.
        """
        rel = str(self.get("secrets_file",
                           default="secrets/credentials.json"))
        cred_path = Path(rel)
        if not cred_path.is_absolute():
            cred_path = self.path.parent / cred_path
        if not cred_path.exists():
            return
        try:
            cred = json.loads(cred_path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            raise ValueError(f"не читается {cred_path}: {exc}") from exc
        if not isinstance(cred, dict):
            raise ValueError(f"{cred_path}: ожидается JSON-объект")

        mapping = {
            "tg_bot_token": ("tg", "bot_token"),
            "tg_group_id": ("tg", "group_id"),
            "tg_my_user_id": ("tg", "my_user_id"),
            "tg_service_topic_id": ("tg", "service_topic_id"),
            "max_token": ("max", "token"),
            "max_my_id": ("bridge", "my_id"),
        }
        for key, target in mapping.items():
            value = cred.get(key)
            if value is None or value == "":
                continue
            # Пробрасываем токен и id поверх config.json.
            _deep_set(self.raw, target, value)

        # Токен MAX живёт в отдельном файле (его перезаписывает вход).
        # Если файла нет, а токен задан в credentials.json — засеваем.
        token = cred.get("max_token")
        token_file = self.max_token_file
        if isinstance(token, str) and token.strip() and not token_file.exists():
            token_file.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(token.strip() + "\n")

    def validate(self) -> None:
        mode = self.raw["tg"]["mode"]
        if mode not in ("bot", "user"):
            raise ValueError("tg.mode must be 'bot' or 'user'")
        if self.raw["bridge"]["on_remote_delete"] not in ("delete", "mark"):
            raise ValueError("on_remote_delete must be 'delete' or 'mark'")
        if int(self.raw["tg"].get("group_id") or 0) == 0:
            raise ValueError(
                "не задан tg.group_id — заполни secrets/credentials.json "
                "(пример: secrets/credentials.example.json), поле tg_group_id")
        if int(self.raw["tg"].get("my_user_id") or 0) == 0:
            raise ValueError(
                "не задан tg.my_user_id — заполни secrets/credentials.json, "
                "поле tg_my_user_id")

    def get(self, *path: str, default: Any = None) -> Any:
        cur: Any = self.raw
        for part in path:
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    # --- MAX ----------------------------------------------------------------
    @property
    def opcodes(self) -> dict[str, int]:
        return {k: _op(v) for k, v in self.get("max", "opcodes", default={}).items()}

    @property
    def max_ws_url(self) -> str:
        return self.get("max", "ws_url", default="wss://api.oneme.ru/websocket")

    @property
    def max_token_file(self) -> Path:
        path = Path(self.get("max", "token_file",
                             default="secrets/max_token"))
        if not path.is_absolute():
            path = self.path.parent / path
        return path

    @property
    def max_token(self) -> str:
        """Токен MAX: сначала файл сессии, затем значение из конфига."""
        path = self.max_token_file
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        return str(self.get("max", "token", default="") or "").strip()

    # --- Telegram ------------------------------------------------------------
    @property
    def tg_mode(self) -> str:
        return self.get("tg", "mode", default="bot")

    @property
    def tg_group_id(self) -> int:
        return int(self.get("tg", "group_id"))

    @property
    def tg_my_user_id(self) -> int:
        """Числовой id, по которому фильтруем исходящие (не is_bot/username)."""
        return int(self.get("tg", "my_user_id"))

    @property
    def tg_bot_token(self) -> str:
        inline = str(self.get("tg", "bot_token", default="") or "").strip()
        if inline:
            return inline
        token_file = self.get("tg", "bot_token_file")
        if token_file:
            path = Path(str(token_file))
            if not path.is_absolute():
                path = self.path.parent / path
            if path.exists():
                return path.read_text(encoding="utf-8").strip()
        raise ValueError(
            "не задан токен Telegram-бота — впиши tg_bot_token в "
            "secrets/credentials.json")

    @property
    def service_topic_key(self) -> str:
        return self.get("tg", "service_topic_key", default="__system__")

    # --- Bridge --------------------------------------------------------------
    @property
    def my_id(self) -> int:
        return int(self.get("bridge", "my_id"))

    @property
    def on_remote_delete(self) -> str:
        return self.get("bridge", "on_remote_delete", default="mark")

    @property
    def temp_dir(self) -> Path:
        p = Path(self.get("bridge", "media", "temp_dir", default="tmp_media"))
        if not p.is_absolute():
            p = self.path.parent / p
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def db_path(self) -> Path:
        path = Path(self.get("bridge", "db_path",
                             default="state/state.sqlite3"))
        if not path.is_absolute():
            path = self.path.parent / path
        return path

    def is_allowed_chat(self, max_chat_id: int) -> bool:
        """Whitelist имеет приоритет; blacklist — запрет. Пустые списки = всё разрешено."""
        white = self.get("bridge", "whitelist", default=[]) or []
        black = self.get("bridge", "blacklist", default=[]) or []
        if white and max_chat_id not in white:
            return False
        return max_chat_id not in black

    def apply_env(self) -> None:
        """Устанавливает переменные окружения до создания event loop."""
        for k, v in self.get("env", default={}).items():
            os.environ[str(k)] = str(v)
