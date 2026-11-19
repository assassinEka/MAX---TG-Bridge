# Полная установка MAX ↔ Telegram bridge с нуля

Пошаговая инструкция для **Windows 10/11** и **Linux** (Debian/Ubuntu).
Мост соединяет чаты MAX с форум-топиками Telegram: **1 чат MAX = 1 топик**.
Работает в обе стороны: текст, фото, видео, файлы, голосовые, кружки,
ответы, правки и удаление.

Если что-то не работает — сначала загляни в раздел
[«Частые проблемы»](#10-частые-проблемы) и в `logs/bridge.log`.

---

## Содержание

1. [Что понадобится](#1-что-понадобится)
2. [Telegram: бот и группа](#2-telegram-бот-и-группа)
3. [MAX: аккаунт и токен](#3-max-аккаунт-и-токен)
4. [Установка на Linux](#4-установка-на-linux)
5. [Установка на Windows](#5-установка-на-windows)
6. [Первый запуск и проверка](#6-первый-запуск-и-проверка)
7. [Автозапуск](#7-автозапуск)
8. [Обновление и бэкап](#8-обновление-и-бэкап)
9. [Как это работает](#9-как-это-работает)
10. [Частые проблемы](#10-частые-проблемы)
11. [Безопасность](#11-безопасность)
12. [Структура проекта](#12-структура-проекта)

---

## 1. Что понадобится

- **Python 3.11 или новее.**
- Аккаунт **Telegram** и аккаунт **MAX**.
- Сервер или компьютер, который будет работать постоянно:
  - Linux: Debian 12/13, Ubuntu 24.04+ (на Ubuntu 22.04 Python старый,
    см. примечание ниже);
  - Windows 10/11.
- Исходящий доступ в интернет к `api.oneme.ru`, `web.max.ru`,
  `api.telegram.org`. Входящие порты не нужны.
- Память: ~150 МБ в работе, пики до ~300 МБ. Chromium, Postgres и Redis
  не нужны.
- Прокси не обязателен. Поддерживаются `http://` и `socks5://`.

> **Важно:** мост работает через web-протокол MAX. Используй **свой**
> аккаунт. Массовая рассылка, спам и автоматизация чужих аккаунтов
> приводят к блокировке номера со стороны MAX.

---

## 2. Telegram: бот и группа

### 2.1. Создай бота

1. Открой [@BotFather](https://t.me/BotFather) → `/newbot`.
2. Придумай имя и username (должен заканчиваться на `bot`).
3. Сохрани токен вида `123456789:AAH...` — он понадобится в
   `secrets/credentials.json`.

### 2.2. Отключи privacy mode

По умолчанию бот в группах видит только команды. Нужно выключить:

- BotFather → `/setprivacy` → выбери бота → **Disable**.

Либо просто сделай бота администратором группы — админы получают все
сообщения независимо от privacy mode. Для моста всё равно нужны права
администратора, так что этот вариант обычно проще.

### 2.3. Создай супергруппу с темами

1. Создай группу в Telegram.
2. Настройки группы → **Темы** (Topics / Форум) → включить.
3. Добавь бота в группу.
4. Сделай бота **администратором** и выдай права:
   - **Управление темами** — обязательно, бот создаёт топики;
   - отправка сообщений;
   - редактирование сообщений;
   - удаление сообщений;
   - чтение сообщений (в админке доступно по умолчанию).

### 2.4. Узнай id группы и свой id

**`tg_group_id`** (id супергруппы, всегда начинается с `-100`):

- добавь в группу [@getmyid_bot](https://t.me/getmyid_bot) — он пришлёт
  `chat_id`;
- либо напиши что-нибудь в группе и выполни:

```bash
curl -s "https://api.telegram.org/bot<ТОКЕН>/getUpdates"
```

В ответе найди `"chat":{"id":-100...}`. Если ответ пустой — проверь
privacy mode и права администратора.

**`tg_my_user_id`** (твой личный id): напиши [@userinfobot](https://t.me/userinfobot).

### 2.5. Служебный топик (необязательно)

Служебные сообщения — «Мост запущен», `/status`, ошибки и подсказки.
Можно оставить `tg_service_topic_id: 0` — тогда они пойдут в **General**.

Если хочешь отдельный топик:

1. Создай тему, например «MAX Bridge — система».
2. Отправь в неё сообщение и скопируй ссылку
   (`https://t.me/c/.../<topic_id>/<message_id>`).
3. `topic_id` из ссылки — это и есть `tg_service_topic_id`.

### 2.6. Выключи вебхук

Мост работает через `getUpdates`, вебхук должен быть пустым:

```bash
curl -s "https://api.telegram.org/bot<ТОКЕН>/getWebhookInfo"
```

Если `"url"` не пустой:

```bash
curl -s "https://api.telegram.org/bot<ТОКЕН>/deleteWebhook"
```

---

## 3. MAX: аккаунт и токен

Нужен токен web-сессии MAX. Рабочих способов два; QR-вход пока в
разработке.

### 3.1. Способ A — HAR (рекомендуется)

1. Открой `https://web.max.ru` в браузере (Chrome/Edge/Firefox) и войди
   в свой аккаунт.
2. Открой DevTools (`F12`) → вкладка **Network**.
3. Включи фильтр **WS** и галочку **Preserve log**.
4. Обнови страницу (`F5`) и дождись, пока загрузятся чаты.
5. Правый клик в списке запросов → **Save all as HAR with content**.
6. Получи токен одним из способов:
   - локально:
     ```bash
     python tools/extract_max_token.py web.max.ru.har
     ```
     скрипт напечатает токен (и не отправит его никуда);
   - на сервере: положи HAR в файл, доступный пользователю моста, и
     отправь боту в служебный топик:
     ```
     /har /opt/max-bridge/web.max.ru.har
     ```
     Бот сам найдёт токен в кадре `0x0013`, проверит его и сохранит.
     После извлечения HAR лучше удалить.

     > В systemd-unit включён `PrivateTmp=true`, поэтому путь `/tmp`
     > внутри службы не виден. Клади HAR в `/opt/max-bridge/` (или в
     > `tmp_media/`), а не в `/tmp`.
7. Либо просто вставь токен вручную:
   ```
   /token An_ExampleToken...
   ```

Файл HAR содержит данные сессии — после извлечения токена его лучше
удалить.

### 3.2. Способ B — QR-вход (в разработке)

QR-вход пока работает нестабильно и не рекомендуется к использованию.
Команда `/login` оставлена для отладки; если она не сработает — используй
`/har` или `/token` из способа A.

Для экспериментов с QR нужен пакет `Pillow` (см. установку ниже), но для
обычной работы он не требуется.

### 3.3. Про токен MAX

- Токен **постоянный**: привязан к «устройству» и не меняется при
  перезапусках моста или обычном входе в браузере.
- Новый токен нужен, только если ты отозвал устройство в MAX →
  **Устройства** или MAX сбросил сессию.
- Не используй один токен одновременно на двух мостах: сессия может
  отозваться. На время локальных тестов останавливай серверный экземпляр.
- Никому не передавай токен — это доступ к аккаунту.

---

## 4. Установка на Linux

Пример для Debian/Ubuntu. Путь установки — `/opt/max-bridge`,
пользователь — `maxbridge`.

### 4.1. Проверь Python

```bash
python3 --version
```

Нужен **3.11+**.

- Debian 12 — 3.11, Debian 13 — 3.13: всё в порядке.
- Ubuntu 24.04 — 3.12: всё в порядке.
- Ubuntu 22.04 — 3.10: поставь Python 3.11 отдельно:

```bash
sudo apt update
sudo apt install -y software-properties-common
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.11 python3.11-venv
```

Дальше в командах используй `python3.11` вместо `python3`.

### 4.2. Пакеты

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip unzip nano
```

### 4.3. Пользователь и каталог

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin maxbridge
sudo mkdir -p /opt/max-bridge
sudo chown maxbridge:maxbridge /opt/max-bridge
```

### 4.4. Распакуй проект

Вариант с архивом:

```bash
sudo tar -xzf max-tg-bridge.tar.gz -C /opt/max-bridge --strip-components=1
sudo chown -R maxbridge:maxbridge /opt/max-bridge
```

Если в архиве нет общей верхней папки, убери `--strip-components=1`.

Скрипт запуска должен быть исполняемым:

```bash
sudo -u maxbridge chmod +x /opt/max-bridge/run.sh
```

Вариант с git:

```bash
sudo -u maxbridge git clone <URL-репозитория> /opt/max-bridge
```

Если копируешь папку с другого компьютера — просто положи файлы в
`/opt/max-bridge` и выполни `chown -R`.

### 4.5. Виртуальное окружение

```bash
sudo -u maxbridge python3 -m venv /opt/max-bridge/.venv
sudo -u maxbridge /opt/max-bridge/.venv/bin/pip install -U pip
sudo -u maxbridge /opt/max-bridge/.venv/bin/pip install -r /opt/max-bridge/requirements.txt
```

Необязательно: только для QR-входа (сейчас в разработке — можно
пропустить):

```bash
sudo -u maxbridge /opt/max-bridge/.venv/bin/pip install Pillow
```

### 4.6. Заполни секреты

```bash
sudo -u maxbridge cp /opt/max-bridge/secrets/credentials.example.json \
    /opt/max-bridge/secrets/credentials.json
sudo -u maxbridge chmod 600 /opt/max-bridge/secrets/credentials.json
sudo -u maxbridge nano /opt/max-bridge/secrets/credentials.json
```

Минимум:

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

`max_token` можно оставить пустым — войдёшь командой `/token`.

### 4.7. Проверь `config.json`

Файл уже лежит в корне. Обычно менять ничего не нужно:

```bash
sudo -u maxbridge nano /opt/max-bridge/config.json
```

Что может понадобиться:

- `network.proxy` — если без прокси MAX/Telegram не открываются;
- `logging.level` — `INFO` для обычной работы, `DEBUG` для отладки;
- `bridge.whitelist` / `blacklist` — ограничить чаты MAX.

Важно: `config.json` должен быть **без BOM**. Если редактируешь его на
Windows, сохраняй как «UTF-8 без BOM».

### 4.8. Первый запуск вручную

```bash
cd /opt/max-bridge
sudo -u maxbridge .venv/bin/python -u tools/console.py
```

В служебный топик Telegram должно прийти «Мост запущен».
Остановка — `Ctrl+C`.

### 4.9. systemd (автозапуск)

В проекте есть готовый unit:

```bash
sudo cp /opt/max-bridge/deploy/max-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now max-bridge
sudo systemctl status max-bridge --no-pager
```

Если ставил в другой каталог — поправь `WorkingDirectory` и `ExecStart`
в `/etc/systemd/system/max-bridge.service`.

Полезные команды:

```bash
sudo systemctl restart max-bridge
sudo systemctl stop max-bridge
sudo journalctl -u max-bridge -n 100 --no-pager
tail -f /opt/max-bridge/logs/bridge.log
```

Unit ограничивает память (`MemoryMax=300M`). Если у тебя много чатов и
большие медиа, лимит можно поднять.

### 4.10. Firewall

Входящие порты не нужны. Достаточно исходящих HTTPS/WSS. Если на сервере
стоит строгий firewall, разреши исходящие к `api.oneme.ru`,
`web.max.ru`, `api.telegram.org`.

---

## 5. Установка на Windows

Пример для Windows 10/11. Путь — `C:\max-bridge`.

### 5.1. Установи Python

1. Скачай Python 3.11+ с [python.org](https://www.python.org/downloads/windows/).
2. При установке включи **Add python.exe to PATH**.
3. Проверь в `cmd`:

```bat
py --version
py -0p
```

`py -0p` покажет установленные версии. Нужна 3.11 или новее. Если `py`
не найден — переустанови Python с галочкой PATH или используй полный путь
к `python.exe`.

### 5.2. Распакуй проект

Скопируй папку проекта в `C:\max-bridge` (или распакуй архив туда).
Не используй OneDrive и пути с длинными именами — меньше сюрпризов.

### 5.3. Виртуальное окружение

Открой `cmd`:

```bat
cd C:\max-bridge
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -U pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Необязательно: только для QR-входа (сейчас в разработке — можно
пропустить):

```bat
.venv\Scripts\python.exe -m pip install Pillow
```

### 5.4. Заполни секреты

```bat
copy secrets\credentials.example.json secrets\credentials.json
notepad secrets\credentials.json
```

Заполни как минимум `tg_bot_token`, `tg_group_id`, `tg_my_user_id`,
`tg_service_topic_id`.

`secrets\credentials.json` не должен попадать в git — он уже в
`.gitignore`.

### 5.5. Проверь `config.json`

Открой `config.json` в блокноте и проверь:

- `network.proxy`: `null`, если прокси не нужен;
- `logging.level`: `INFO`;
- остальное можно оставить по умолчанию.

Сохраняй `config.json` как **UTF-8 без BOM** (в Notepad: «UTF-8»,
не «UTF-8 with BOM»).

### 5.6. Запуск

Два варианта:

```bat
cd C:\max-bridge
.venv\Scripts\python.exe -u tools\console.py
```

или просто двойной клик по `run.bat`.

В служебный топик Telegram придёт «Мост запущен». Остановка — `Ctrl+C`
или закрыть окно.

Лог: `C:\max-bridge\logs\bridge.log`.

---

## 6. Первый запуск и проверка

1. В Telegram в служебном топике появилось **«Мост запущен»**.
2. Отправь `/status` — бот ответит состоянием.
3. Отправь `/token <токен MAX>` или `/har /путь/к/файлу.har`.
4. После успешного входа в логе появится `MAX подключён`, а `/status`
   покажет, что токен есть.
5. Напиши сообщение в любой чат MAX со своего телефона или второго
   аккаунта.
6. В Telegram должен появиться **новый топик** с названием чата MAX и
   текстом сообщения.
7. Ответь в этом топике — сообщение должно уйти в MAX.
8. Проверь медиа:
   - фото и видео — в обе стороны;
   - голосовое из TG и из MAX;
   - кружок из TG (в MAX придёт квадратным видео);
   - файл с подписью и без.
9. Проверь правку сообщения в MAX — в Telegram появится `✏️`.
10. Проверь пересылку (forward) сообщения с видео из MAX — видео должно
    дойти, а не превратиться в текст.

Если что-то не сработало — смотри `logs/bridge.log` и раздел ниже.

---

## 7. Автозапуск

### 7.1. Linux — systemd

Уже описан в [4.9](#49-systemd-автозапуск). Основное:

```bash
sudo systemctl enable --now max-bridge
```

### 7.2. Windows — Планировщик заданий

1. `Win+R` → `taskschd.msc`.
2. **Create Task…** (не Basic Task).
3. **General**: имя `MAX-TG Bridge`, выбери «Run only when user is logged on».
4. **Triggers** → New → **At log on** → OK.
5. **Actions** → New:
   - Program/script: `C:\max-bridge\run.bat`
   - Start in: `C:\max-bridge`
6. **Settings**:
   - сними «Stop the task if it runs longer than…»;
   - при желании включи «If the task fails, restart every 1 minute».
7. OK. Проверь: правый клик по задаче → **Run**.

Минус: при выключении/входе окно консоли будет появляться. Если нужен
тихий фоновый сервис — используй NSSM.

### 7.3. Windows — служба через NSSM (опционально)

1. Скачай [NSSM](https://nssm.cc/download) и распакуй.
2. В `cmd` от администратора:

```bat
nssm install MaxBridge "C:\max-bridge\.venv\Scripts\python.exe" "-u tools/console.py"
nssm set MaxBridge AppDirectory "C:\max-bridge"
nssm set MaxBridge AppStdout "C:\max-bridge\logs\service.out"
nssm set MaxBridge AppStderr "C:\max-bridge\logs\service.err"
nssm start MaxBridge
```

Проверка:

```bat
nssm status MaxBridge
```

Остановка/удаление:

```bat
nssm stop MaxBridge
nssm remove MaxBridge confirm
```

Папку `logs` создай заранее (`mkdir C:\max-bridge\logs`), если её нет.

---

## 8. Обновление и бэкап

### 8.1. Обновление

1. Останови мост.
2. Замени файлы кода: папки `bridge/`, `tools/`, файлы
   `requirements.txt`, `fixtures/`. `config.json` не перезаписывай, если
   правил его под себя — сравни с новым вручную.
3. Если менялись зависимости:
   ```bash
   .venv/bin/pip install -r requirements.txt
   ```
4. Запусти мост снова. База `state/state.sqlite3` мигрирует
   автоматически.

**Не удаляй** `state/` и `secrets/` при обновлении.

### 8.2. Бэкап

Достаточно двух папок:

- `secrets/` — токены;
- `state/` — база с маппингами сообщений и топиков.

```bash
tar -czf max-bridge-backup-$(date +%F).tar.gz secrets state
```

`logs/` и `tmp_media/` бэкапить не нужно.

### 8.3. Удаление

Linux:

```bash
sudo systemctl disable --now max-bridge
sudo rm /etc/systemd/system/max-bridge.service
sudo systemctl daemon-reload
sudo rm -rf /opt/max-bridge
sudo userdel maxbridge
```

Windows:

- удали задачу в Планировщике или службу NSSM;
- удали папку `C:\max-bridge`.

---

## 9. Как это работает

- Мост подключается к MAX по WSS и к Telegram Bot API по HTTPS.
- При первом сообщении из нового чата MAX создаётся топик в Telegram;
  связка хранится в `state/state.sqlite3`.
- Всё, что приходит из MAX, публикуется в топик от имени бота.
- Всё, что пишешь **ты** (`tg_my_user_id`) в топике, уходит в MAX.
  Сообщения других участников игнорируются.
- Ответы — нативные (через `link` MAX), без цитат.
- Правки из MAX применяются к сообщению бота и помечаются `✏️`. Если
  сообщение писал ты сам, бот не может его редактировать — приходит
  новое сообщение-ответ с `✏️`.
- Пересланные сообщения MAX разворачиваются: текст и вложения берутся из
  `link.message`.
- Кружок Telegram отправляется в MAX обычным квадратным видео; кружок
  MAX приходит в Telegram как видеосообщение (`sendVideoNote`).
- Удаление в MAX по умолчанию помечает сообщение в TG (`on_remote_delete:
  "mark"`); можно переключить на удаление.
- `/del` ответом на сообщение моста удаляет его в MAX.

Ограничения:

- Bot API скачивает файлы из Telegram до 20 МБ.
- Стикеры, GIF и музыка из TG пока не поддерживаются — придёт
  предупреждение.
- MAX не умеет подпись к файлу: подпись уходит отдельным сообщением.

---

## 10. Частые проблемы

| Симптом | Причина / решение |
|---|---|
| В группе ничего не происходит | бот не администратор, нет права «Управление темами» или неверный `tg_group_id` |
| `409 Conflict` в логе | запущено два экземпляра бота (локальный + сервер) — оставь один |
| `getUpdates` пустой | privacy mode включён и бот не админ; либо вебхук занят |
| Вебхук занят | `curl .../deleteWebhook` |
| Сообщения из MAX не приходят | сессия MAX сброшена; в служебный топик пришёл алерт, проверь `/status` |
| `FAIL_LOGIN_TOKEN` | токен отозван в MAX → «Устройства»; получи новый и отправь `/token` |
| `frame() got an unexpected keyword argument 'prefix'` | на сервере старый `bridge/login.py` — обнови файлы кода целиком |
| `'WebMaxTransport' object has no attribute 'audio_upload_target'` | на сервере старый `bridge/max/web.py` — обнови файлы кода |
| `MAX отклонил clientHello: userAgent required` | нет папки `fixtures/` рядом с `config.json` |
| `не задан tg.group_id` | не заполнен или не сохранён `secrets/credentials.json` |
| JSON error / не читается конфиг | лишняя запятая, комментарии или BOM в `config.json`; сохрани как UTF-8 без BOM |
| `Bad Gateway` / обрывы к Telegram | сеть или прокси; попробуй `network.proxy: null` |
| Не создаются топики | у бота нет права «Управление темами» |
| Мои сообщения не уходят в MAX | `tg_my_user_id` не совпадает с твоим id |
| Бот дублирует мои сообщения | задай `max_my_id` (id твоего аккаунта MAX) |
| Файл из TG не приходит | больше 20 МБ — ограничение Bot API |
| Кириллица в логе «кракозябрами» | смотри `logs/bridge.log` в UTF-8; консоль Windows может отображать иначе |

Проверка Telegram-бота:

```bash
curl -s "https://api.telegram.org/bot<ТОКЕН>/getMe"
curl -s "https://api.telegram.org/bot<ТОКЕН>/getWebhookInfo"
```

Что смотреть в логе при проблемах:

```
MAX подключён                 — соединение с MAX установлено
0x0052 (audio) url: ...       — получен адрес загрузки голосового
uploadAudio: HTTP 200         — голосовое загружено
TG <id> -> MAX <id>           — сообщение ушло в MAX
MAX <id> -> TG <id>           — сообщение ушло в Telegram
MAX <id>: переслано (...)     — развернули forwarded-сообщение
MAX вложение ... type=...     — какое вложение пришло из MAX
```

---

## 11. Безопасность

- `secrets/credentials.json` и `secrets/max_token` — доступ к боту и
  аккаунту MAX. Не коммить, не публиковать, не пересылать.
- На Linux:
  ```bash
  chmod 600 secrets/credentials.json secrets/max_token
  ```
- HAR-файлы содержат сессию MAX. После извлечения токена удаляй их.
- Мост не слушает входящие порты — только исходящие соединения.
- Один токен MAX — один активный экземпляр моста.
- Используй собственный аккаунт MAX и не занимайся спамом: MAX
  блокирует номера за автоматическую массовую отправку.
- Если проект публикуется на GitHub — не добавляй `secrets/`, `state/`,
  `logs/` и HAR-файлы. Они уже в `.gitignore`.
- Шаблоны в `fixtures/` обезличены: в них нет токенов и id аккаунтов,
  `deviceId` — случайный.

---

## 12. Структура проекта

```
INSTALL.md                  эта инструкция
README.md                   краткое описание и быстрый старт
config.json                 настройки: прокси, лимиты, опкоды, пути
requirements.txt            зависимости (aiohttp)
run.sh / run.bat            запуск через venv (Linux / Windows)
bridge/                     код: конфиг, MAX WSS/кодек, вход, сеть
fixtures/                   шаблоны кадров clientHello/auth/edit/delete (обязательны)
tools/console.py            супервизор (запускает мост)
tools/bridge_app.py         сам мост
tools/login_from_har.py     вход по токену из HAR
tools/extract_max_token.py  автономное извлечение токена из HAR
secrets/credentials.example.json
deploy/max-bridge.service   unit для systemd
logs/                       логи (создаётся при запуске)
state/                      SQLite с маппингами (создаётся при запуске)
tmp_media/                  временные файлы (создаётся при запуске)
```

Не входят в репозиторий и создаются локально:

```
secrets/credentials.json
secrets/max_token
state/state.sqlite3
logs/bridge.log
tmp_media/
.venv/
__pycache__/
```