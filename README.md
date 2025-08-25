# Binance Funding Telegram Bot (Koyeb)

Бот мониторит Binance funding rate и шлёт сигналы в Telegram.

## Возможности
- Автоскан каждые 30s (можно менять `POLL_SEC`).
- Порог включения: `THRESHOLD` (по умолчанию -1.0%).
- Шаг вниз: `DOWN_STEP` (0.25%), шаг отката от `REBOUND_START` (-2.0%) вверх: `REBOUND_STEP` (0.05%).
- Команда /status в Telegram показывает состояние + кнопку «Проверить сейчас».

## Деплой на Koyeb
1. Загрузите эти файлы в GitHub.
2. На Koyeb → Create App → Deploy Service → выберите репозиторий.
3. Runtime: Dockerfile.
4. Environment variables:
   - `TG_TOKEN` — токен вашего бота.
   - `TG_CHAT_ID` — ваш chat id.
   - (опционально) `POLL_SEC`, `THRESHOLD`, `DOWN_STEP`, `REBOUND_STEP`, `REBOUND_START`.
   - `STATE_FILE=/data/funding_state.json`.
5. Добавьте Volume → mount path `/data`.
6. Deploy.

## Проверка
- Напишите боту `/status`.
- Нажмите кнопку «Проверить сейчас».
- Бот ответит текущим списком монет ниже порога.

## Локально
```bash
export TG_TOKEN=... TG_CHAT_ID=...
docker build -t funding-bot .
docker run --rm -e TG_TOKEN -e TG_CHAT_ID -v $(pwd)/data:/data funding-bot
