# OKX Funding Telegram Bot (Koyeb)

Бот мониторит **все SWAP-инструменты на OKX** (перпетуальные фьючерсы) и
присылает сигналы в Telegram:
- вход в зону при `≤ -1.00%`;
- углубление вниз шагом `0.25%`;
- после того как инструмент был `≤ -2.00%`, откат вверх шагом `0.05%`;
- авто-скан раз в `30s`, есть `/status` и кнопка «Проверить сейчас»;
- опционально снапшот-список всех совпадений каждый цикл (`SNAPSHOT_MODE=1`).

## Переменные окружения (Koyeb → Environment)
- `TG_TOKEN` **(required)** — токен от @BotFather
- `TG_CHAT_ID` **(required)** — id вашего чата/канала
- `STATE_FILE` (default `/data/okx_funding_state.json`) — файл состояния
- `POLL_SEC` (default `30`)
- `THRESHOLD` (default `-1.0`)
- `DOWN_STEP` (default `0.25`)
- `REBOUND_STEP` (default `0.05`)
- `REBOUND_START` (default `-2.0`)
- `SNAPSHOT_MODE` (default `0`) — `1` чтобы слать общий список каждый цикл
- `REFRESH_INSTR` (default `600`) — как часто обновлять список инструментов (сек)
- `CONCURRENCY` (default `10`) — параллельных запросов к API OKX
- `TIMEOUT_SEC` (default `15`)
- `OKX_BASE` (опц.) — базовый URL OKX API

## Деплой на Koyeb
1. Залейте файлы в GitHub.
2. Create App → Deploy Service → GitHub repo → Runtime: Dockerfile.
3. Environment: задайте все `TG_*` и остальные переменные (см. выше).
4. Volumes: добавьте volume и примонтируйте к `/data`.
5. Scaling: Minimum=1, Maximum=1, без scale-to-zero.
6. Deploy. Логи → “Started” и число SWAP-инструментов.

## Команды
- `/status` — показывает состояние и кнопку «Проверить сейчас».

## Примечания
- Бот запрашивает список инструментов (`/public/instruments?instType=SWAP`) и каждые `POLL_SEC`
  параллельно тянет `funding-rate?instId=...` для всех SWAP. Результаты фильтруются `≤ THRESHOLD`.
- Если монет много, можно уменьшить `CONCURRENCY` (снизит нагрузку), либо увеличить `POLL_SEC`.
