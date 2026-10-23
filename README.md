# TG Cursor News

Poller: новые записи из публичных фидов Cursor и xAI → Telegram-канал. Без базы, без браузера, только стандартная библиотека Python 3.12.

Источники:

- [Cursor Changelog](https://cursor.com/changelog/rss.xml) — RSS, ключ `guid`
- [xAI Release Notes](https://docs.x.ai/developers/release-notes) — markdown по заголовкам `###`
- [status.x.ai](https://status.x.ai/feed.xml) — RSS, дедуп по `guid` инцидента (не по title/link)

`status.cursor.com` в этот срез не входит (после 26.10 уведомления уезжают на status.x.ai). Webhook Statuspage не используем.

Правила доставки:

- первый успешный прогон по источнику только запоминает ключи, в Telegram ничего не шлёт
- статус: только открытие аварии и Resolved; промежуточные апдейты и scheduled maintenance — нет
- язык как у источника: RU, если отдают русскую версию (`Accept-Language: ru`), иначе EN как есть, без перевода

## Локально

Нужен Python 3.11+. Зависимостей нет.

```bash
python3 -m unittest test_poller.py -v
```

Секреты только в окружении, не в репозитории:

```bash
export TELEGRAM_BOT_TOKEN='123456:ABC…'
export TELEGRAM_CHAT_ID='-100…'   # канал, бот — админ с правом постить
python3 poller.py --dry-run       # печать, без Telegram и без пометки ключей
python3 poller.py                 # первый запуск = bootstrap, тишина в канале
```

Файл `last-seen.json` появится в корне (он в `.gitignore`). Путь можно сменить через `--state` или `LAST_SEEN_PATH`.

Без `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` poller всё равно ходит в фиды и делает bootstrap; сообщения не отправляет и уже увиденные новые ключи не выбрасывает.

Проверка бота без кода:

```bash
curl -sS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
  --data-urlencode "text=ping"
```

## GitHub Actions

Воркфлоу `.github/workflows/poll.yml`: cron `*/10 * * * *` и ручной `workflow_dispatch`. `last-seen.json` живёт в Actions cache.

Secrets репозитория (Settings → Secrets and variables → Actions):

| Secret | Что это |
|---|---|
| `TELEGRAM_BOT_TOKEN` | токен от [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | id канала вида `-100…` |

Пока секретов нет, джоба всё равно гоняет парсеры и bootstrap. Token бота не нужен, чтобы слить этот код.
