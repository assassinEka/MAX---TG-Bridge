# MAX ↔ Telegram bridge

Мост между мессенджером **MAX** (web-протокол, WSS) и **Telegram**:
переписка живёт в MAX, а Telegram-супергруппа с форум-топиками служит
интерфейсом и архивом. **1 чат MAX = 1 топик в Telegram.**

```
MAX  <—WSS—>  bridge (Python)  <—Bot API—>  Telegram-супергруппа (топики)
```

Работает без браузера, Chromium, Postgres и Redis: только Python и SQLite.
Память — примерно 150 МБ в работе, пики до ~300 МБ.

## Возможности

- Текст, фото, видео, файлы, голосовые и кружки — **в обе стороны**.
- Нативные ответы (без цитат), правки, удаление.
- Правки из MAX помечаются в Telegram значком `✏️`.
- Пересланные сообщения MAX разворачиваются: медиа из них доходит как
  обычное.
- Кружок из Telegram уходит в MAX обычным квадратным видео.
- Новый чат MAX автоматически превращается в новый топик Telegram.
- Фильтр по твоему Telegram-id: сообщения других людей бот не пересылает.

## Требования

- Python **3.11+** (Windows 10/11, Debian/Ubuntu и другие Linux).
- Исходящий доступ к `api.oneme.ru` / `web.max.ru` и `api.telegram.org`.
- Прокси не нужен (по умолчанию `null`), но поддерживается HTTP/SOCKS5.
- Telegram-бот и супергруппа с включёнными темами.
- Свой аккаунт MAX (вход по токену; QR-вход пока в разработке).

## Быстрый старт

Полная пошаговая инструкция, включая настройку Telegram, MAX, автозапуск
и разбор ошибок: **[INSTALL.md](INSTALL.md)**.

### Linux

```bash
cd /opt/max-bridge
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp secrets/credentials.example.json secrets/credentials.json
nano secrets/credentials.json          # заполнить поля
.venv/bin/python -u tools/console.py
```

### Windows (cmd)

```bat
cd C:\max-bridge
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy secrets\credentials.example.json secrets\credentials.json
notepad secrets\credentials.json       :: заполнить поля
run.bat
```

Если установлено несколько версий Python, укажи нужную явно:
`py -3.11 -m venv .venv` (или `py -3.12`).

Дальше в **служебном топике** Telegram (или в General, если
`tg_service_topic_id: 0`) отправь:

```
/token <токен MAX>
```

Подробнее про получение токена — ниже и в [INSTALL.md](INSTALL.md).

## Настройка

### `secrets/credentials.json`

Единственный файл, который обязательно заполнить. Он не попадает в git
(см. `.gitignore`).

```json
{
  "tg_bot_token": "123456789:AAH...",
  "tg_group_id": -1001234567890,
  "tg_my_user_id": 111111111,
  "tg_service_topic_id": 0,
  "max_token": "",
  "max_my_id": 0
}
```

| ключ | что это |
|---|---|
| `tg_bot_token` | токен бота от [@BotFather](https://t.me/BotFather) |
| `tg_group_id` | id супергруппы (с минусом, вида `-100...`) |
| `tg_my_user_id` | **твой** числовой id в Telegram ([@userinfobot](https://t.me/userinfobot)) — остальные игнорируются |
| `tg_service_topic_id` | id топика для служебных сообщений (`0` — General) |
| `max_token` | токен MAX; можно оставить пустым и войти командой `/token` |
| `max_my_id` | id твоего аккаунта MAX (подавление эха своих сообщений) |

Если `max_token` заполнен, при первом запуске он копируется в
`secrets/max_token` (файл сессии, права `600`). Дальше вход обновляет
именно этот файл.

### `config.json`

Обычно трогать не нужно. Полезное:

| ключ | смысл |
|---|---|
| `network.proxy` | `null` — напрямую; либо `"socks5://127.0.0.1:1080"`, `"http://..."` |
| `logging.level` | `INFO` (рекомендуется) или `DEBUG` |
| `logging.file` | путь к логу, по умолчанию `logs/bridge.log` |
| `tg.caps.download_bytes` | предел скачивания из Telegram (20 МБ у Bot API) |
| `bridge.whitelist` / `blacklist` | какие чаты MAX зеркалить |
| `bridge.subscribe_chats` | id чатов MAX для подписки при старте |
| `bridge.throttle.per_minute` | лимит отправки в Telegram |
| `bridge.native_reply` | `true` — настоящие ответы, без цитат |
| `bridge.media.temp_dir` | временные файлы (`tmp_media`) |

Пути в конфиге относительные — считаются от папки, где лежит `config.json`.
Запускать мост нужно из корня проекта (там же, где `config.json`).

## Telegram: что подготовить

1. Создай бота у [@BotFather](https://t.me/BotFather) (`/newbot`) и
   сохрани токен.
2. Отключи privacy mode: BotFather → `/setprivacy` → **Disable**. Либо
   просто сделай бота администратором — админы получают все сообщения.
3. Создай **супергруппу** и включи в ней **Темы** (Topics/Форум).
4. Добавь бота в группу и выдай права администратора:
   - «Управление темами» — обязательно (бот создаёт топики),
   - чтение, отправка, редактирование, удаление сообщений.
5. Впиши `tg_group_id` и `tg_my_user_id` в `secrets/credentials.json`.
6. Вебхук должен быть выключен — мост работает на `getUpdates`:

```bash
curl -s "https://api.telegram.org/bot<ТОКЕН>/getWebhookInfo"
# "url" должен быть пустым; если нет:
curl -s "https://api.telegram.org/bot<ТОКЕН>/deleteWebhook"
```

## Вход в MAX

Все команды отправляются в служебный топик **от твоего `tg_my_user_id`**.

### Вариант A — HAR (рекомендуется)

1. Открой `https://web.max.ru`, войди в свой аккаунт.
2. DevTools → **Network** → фильтр **WS** → включи **Preserve log** →
   обнови страницу.
3. Правый клик в списке запросов → **Save all as HAR**.
4. Либо вытащи токен локально:

```bash
python tools/extract_max_token.py web.max.ru.har
```

5. Либо положи HAR на сервер и пришли боту путь:

```
/har /opt/max-bridge/web.max.ru.har
```

Файл должен быть читаем пользователем моста (`chmod 644`). Бот сам найдёт
токен в кадре `0x0013`, проверит его новым подключением и сохранит в
`secrets/max_token`. Токен в чат не печатается. После извлечения HAR
лучше удалить.

### Вариант B — вставить токен вручную

```
/token An_ExampleToken...
```

### Вариант C — QR (в разработке)

QR-вход пока работает нестабильно, пользоваться им не рекомендуется.
Используй варианты A или B. Команда `/login` оставлена для отладки и
может не дать результата.

Токен MAX постоянный: он привязан к «устройству» и меняется только если
отозвать его в MAX → **Устройства** (или если MAX сбросил сессию).
Один сохранённый `secrets/max_token` может работать очень долго.

## Команды

| команда | что делает |
|---|---|
| `/login` | QR-вход в MAX (в разработке) |
| `/token <токен>` | проверить и сохранить токен MAX |
| `/har <путь>` | взять токен из HAR-файла на сервере |
| `/logout` | удалить сохранённый токен |
| `/status` | состояние моста и токена |
| `/del` | ответом на сообщение моста — удалить его и в MAX |

## Медиа

| направление | поддержка |
|---|---|
| MAX → TG | фото, видео, файлы, голосовые, кружки; пересланные сообщения разворачиваются |
| TG → MAX | фото, видео, файлы, голосовые, кружки (`video_note`) |
| не поддерживается | стикеры, GIF и музыка из TG — придёт предупреждение |

- Файл из Telegram уходит без подписи; если подпись есть, она приходит
  отдельным сообщением (MAX не умеет подпись к файлу).
- Кружок Telegram отправляется в MAX обычным квадратным видео.
- Ограничение Bot API: скачать из Telegram можно файл до 20 МБ.

## Запуск

### Linux, вручную

```bash
cd /opt/max-bridge
.venv/bin/python -u tools/console.py
```

### Linux, systemd

```bash
sudo cp deploy/max-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now max-bridge
sudo systemctl status max-bridge
```

Логи: `journalctl -u max-bridge -f` и `logs/bridge.log`.

### Windows

```bat
run.bat
```

Автозапуск через Планировщик заданий и вариант со службой NSSM описаны в
[INSTALL.md](INSTALL.md).

## Обслуживание

```bash
sudo systemctl restart max-bridge     # перезапуск
tail -f /opt/max-bridge/logs/bridge.log
```

- `state/state.sqlite3` — маппинги сообщений и топиков. **Не удаляй**:
  пропадут связки (история в TG останется).
- `secrets/max_token` — сессия MAX, обновится при `/token`.
- `logs/bridge.log` — лог с ротацией (10 МБ × 5).
- Бэкап: достаточно `secrets/` и `state/`.
- Обновление: замени файлы кода (`bridge/`, `tools/`), перезапусти мост.
  База мигрирует автоматически.

## Частые проблемы

| Симптом | Причина / решение |
|---|---|
| В группе ничего не происходит | бот не админ, нет права «Управление темами» или неверный `tg_group_id` |
| `409 Conflict` в логе | запущено два экземпляра бота или два сервера с одним токеном — оставь один |
| Сообщения из MAX не приходят | сессия MAX сброшена — нужен новый токен; проверь `/status` |
| `FAIL_LOGIN_TOKEN` | токен отозван в MAX → «Устройства»; получи новый и отправь `/token` |
| `frame() got an unexpected keyword argument 'prefix'` | на сервере старый `bridge/login.py` — обнови файлы кода целиком |
| `'WebMaxTransport' object has no attribute 'audio_upload_target'` | на сервере старый `bridge/max/web.py` — обнови файлы кода |
| `MAX отклонил clientHello: userAgent required` | нет папки `fixtures/` рядом с `config.json` |
| `Bad Gateway` / обрывы к Telegram | сеть или прокси; попробуй `network.proxy: null` |
| Не создаются топики | у бота нет права «Управление темами» |
| Мои сообщения не уходят в MAX | `tg_my_user_id` не совпадает с твоим id |
| Бот дублирует мои сообщения | не задан `max_my_id` |
| Вебхук занят | `deleteWebhook`, мост использует `getUpdates` |

Проверка бота:

```bash
curl -s "https://api.telegram.org/bot<ТОКЕН>/getMe"
curl -s "https://api.telegram.org/bot<ТОКЕН>/getWebhookInfo"
```

## Безопасность

- `secrets/credentials.json` и `secrets/max_token` — это доступ к боту и
  аккаунту MAX. Не коммить их, не выкладывай в чаты.
- На Linux: `chmod 600 secrets/credentials.json secrets/max_token`.
- Мост не открывает входящих портов — только исходящие соединения.
- Используй свой аккаунт MAX. Массовая автоматическая отправка и спам
  приводят к блокировке номера со стороны MAX.
- Шаблоны в `fixtures/` обезличены: в них нет токенов и id аккаунтов,
  `deviceId` — случайный.

## Публикация на GitHub

Проект уже подготовлен к публикации: `.gitignore` исключает секреты,
состояние, логи, `.venv` и HAR-файлы.

```bash
git init
git add .
git status          # не должно быть secrets/credentials.json, state/, logs/, *.har
git commit -m "MAX <-> Telegram bridge"
git branch -M main
git remote add origin git@github.com:<user>/<repo>.git
git push -u origin main
```

Перед push проверь, что в `secrets/` нет ничего, кроме
`credentials.example.json`. Если секрет уже попал в историю git — отзови
токен: одного удаления файла недостаточно.

## Состав проекта

```
INSTALL.md                  полная инструкция по установке (Windows/Linux)
README.md                   этот файл
config.json                 настройки: прокси, лимиты, опкоды, пути
requirements.txt            зависимости (aiohttp)
run.sh / run.bat            запуск через venv (Linux / Windows)
bridge/                     код: конфиг, MAX WSS/кодек, вход, сеть
fixtures/                   шаблоны кадров clientHello/auth/edit/delete (обязательны)
tools/console.py            супервизор (запускает мост)
tools/bridge_app.py         сам мост
tools/login_from_har.py     вход по токену из HAR
tools/extract_max_token.py  автономно: только вытащить токен из HAR
secrets/credentials.example.json
deploy/max-bridge.service   unit для systemd
logs/ state/ tmp_media/     создаются при запуске
```

Не входят в репозиторий: `secrets/credentials.json`, `secrets/max_token`,
`state/`, `logs/`, `tmp_media/`, `.venv/`, `__pycache__/`.