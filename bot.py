import os, json, asyncio, logging, math
from datetime import datetime, timezone, timedelta
from collections import defaultdict, deque

import aiosqlite
from aiogram import Bot, Dispatcher
from aiogram.types import Message
from aiogram.filters import CommandStart, Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import ClientSession, web

logging.basicConfig(level=logging.INFO)

# ===== Настройки через переменные окружения =====
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Порог входа в "негативную зону", в процентах (1.0 = -1.0%)
START_NEG_PCT = float(os.getenv("START_NEG_PCT", "1.0"))

# Размер ступени-алерта в базисных пунктах (20 б.п. = 0.20%)
STEP_BPS = int(os.getenv("STEP_BPS", "20"))

# Период опроса всех пар, секунд
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "30"))

# Через сколько минут без отрицательного фонда сбрасывать прогресс
RESET_MIN = int(os.getenv("RESET_MIN", "30"))

DB_PATH = "subs.db"

# ===== Хранилища в памяти =====
# последняя ступень для каждой пары (чтобы не спамить каждые 0.01%)
last_step_idx: dict[str, int] = defaultdict(lambda: -1)

# когда пара в последний раз была ниже порога -1%
last_below_ts: dict[str, datetime] = defaultdict(lambda: datetime.fromtimestamp(0, tz=timezone.utc))

# небольшое окно последних значений для оценки "темпа"
recent_rates: dict[str, deque] = defaultdict(lambda: deque(maxlen=10))  # храним последние ~5 минут при POLL_SECONDS=30

# ===== БД подписчиков =====
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS subscribers (
  chat_id INTEGER PRIMARY KEY,
  joined_at TEXT
);
"""

async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_SQL)
        await db.commit()

async def add_sub(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO subscribers (chat_id, joined_at) VALUES (?, ?)",
            (chat_id, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()

async def remove_sub(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM subscribers WHERE chat_id = ?", (chat_id,))
        await db.commit()

async def get_all_subs():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT chat_id FROM subscribers")
        rows = await cur.fetchall()
        return [r[0] for r in rows]

# ===== Утилиты =====
def pct(rate_float: float) -> float:
    # из доли в проценты
    return rate_float * 100.0

def bps(rate_float: float) -> float:
    # из доли в базисные пункты
    return rate_float * 10000.0

def fmt_pct(rate_float: float) -> str:
    return f"{pct(rate_float):.2f}%"

def human_eta_ms(ms: int | None) -> str:
    if ms is None:
        return "—"
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc) - datetime.now(timezone.utc)
    if dt.total_seconds() <= 0:
        return "soon"
    h, rem = divmod(int(dt.total_seconds()), 3600)
    m, _ = divmod(rem, 60)
    return f"{h}h {m}m"

def step_index(current_pct_abs: float, start_neg_pct: float, step_pct: float) -> int:
    """
    current_pct_abs — абсолютное значение процента, напр. 1.37 (для -1.37%)
    Возвращает индекс ступени: 0 для -1.20%, 1 для -1.40% при step=0.20% и т.д.
    """
    extra = current_pct_abs - start_neg_pct
    if extra < 0:
        return -1
    return math.floor(extra / step_pct)

# ===== Основной цикл опроса =====
FAPI_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"  # USDⓈ-M: вернёт массив всех символов, если без параметра
DAPI_URL = "https://dapi.binance.com/dapi/v1/premiumIndex"  # COIN-M: тоже вернёт список

async def fetch_all_pairs(session: ClientSession) -> list[dict]:
    """Забираем все контракты USDⓈ-M и COIN-M с текущим funding."""
    results = []
    for url in (FAPI_URL, DAPI_URL):
        try:
            async with session.get(url, timeout=20) as resp:
                data = await resp.json()
                if isinstance(data, list):
                    results.extend(data)
                elif isinstance(data, dict):
                    results.append(data)
        except Exception as e:
            logging.warning(f"Fetch error {url}: {e}")
    return results

async def poll_loop(bot: Bot):
    step_pct = STEP_BPS / 100.0            # 20 б.п. -> 0.20%
    start_neg_pct = START_NEG_PCT          # 1.0 -> -1.0%
    start_neg_bps = START_NEG_PCT * 100.0  # 1.0% -> 100 б.п.

    async with ClientSession() as session:
        while True:
            try:
                all_rows = await fetch_all_pairs(session)
                now = datetime.now(timezone.utc)

                # карта подписчиков на этот тик
                subs = await get_all_subs()
                if not subs:
                    await asyncio.sleep(POLL_SECONDS)
                    continue

                for row in all_rows:
                    sym = row.get("symbol")
                    r_str = row.get("lastFundingRate")
                    nextT = row.get("nextFundingTime")
                    if not sym or r_str is None:
                        continue

                    try:
                        r = float(r_str)  # доля, напр. -0.0123 = -1.23%
                    except:
                        continue

                    # копим последние значения для оценки темпа
                    recent_rates[sym].append((now, r))

                    # если выше порога −1% — проверим, не пора ли сбросить ступени после RESET_MIN
                    if pct(r) > -start_neg_pct:
                        # удерживался выше порога — проверяем время
                        # если хотя бы RESET_MIN мин выше порога -> сброс
                        # (смотрим последний раз, когда был ниже порога)
                        if (now - last_below_ts[sym]) > timedelta(minutes=RESET_MIN):
                            last_step_idx[sym] = -1
                        continue

                    # тут r <= −1% → обновим отметку "ниже порога"
                    last_below_ts[sym] = now

                    # считаем индекс текущей ступени по абсолютному значению
                    idx = step_index(abs(pct(r)), start_neg_pct, step_pct)

                    if idx > last_step_idx[sym] >= -1:
                        # подготовим текст и метрики
                        # Δ за окно (~последние 5 измерений): в б.п.
                        rate_list = [x for (_, x) in list(recent_rates[sym])[-5:]]
                        delta_bps = (bps(rate_list[-1]) - bps(rate_list[0])) if len(rate_list) >= 2 else 0.0

                        text = (
                            f"⚠️ <b>{sym}</b> funding углубляется:\n"
                            f"• Текущая ставка: <b>{fmt_pct(r)}</b>\n"
                            f"• Ступень: <b>≈ -{start_neg_pct + (idx+1)*step_pct:.2f}%</b> от порога -{start_neg_pct:.2f}%\n"
                            f"• Темп за ~{min(len(rate_list)*POLL_SECONDS//60, 5)}м: <b>{delta_bps:.1f} б.п.</b>\n"
                            f"• Следующая выплата через: <b>{human_eta_ms(nextT)}</b>"
                        )

                        # отправим всем подписчикам
                        for chat_id in subs:
                            try:
                                await bot.send_message(chat_id, text, parse_mode="HTML")
                            except Exception as e:
                                logging.warning(f"send_message error chat {chat_id}: {e}")

                        last_step_idx[sym] = idx

                await asyncio.sleep(POLL_SECONDS)

            except Exception as e:
                logging.error(f"poll_loop error: {e}")
                await asyncio.sleep(POLL_SECONDS)

# ===== Мини health-сервер для Koyeb =====
async def start_health_server():
    async def health(_):
        return web.Response(text="ok")
    app = web.Application()
    app.router.add_get("/healthz", health)
    port = int(os.getenv("PORT", "8080"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Health server on :{port}")

# ===== Телеграм-бот =====
async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing")

    await db_init()

    bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
    dp = Dispatcher(storage=MemoryStorage())

    @dp.message(CommandStart())
    async def start(m: Message):
        await add_sub(m.chat.id)
        await m.answer(
            "Привет! Я слежу за funding rate по <b>всем</b> фьючерсным контрактам Binance.\n\n"
            "Правила сигналов:\n"
            f"• Пара вошла в зону ниже <b>-{START_NEG_PCT:.2f}%</b>\n"
            f"• Дальше шлются ступени по <b>{STEP_BPS} б.п.</b> (например: -1.20%, -1.40%, -1.60% ...)\n"
            f"• Период опроса: <b>{POLL_SECONDS}s</b>\n\n"
            "Команды:\n"
            "• /subscribe — включить сигналы\n"
            "• /unsubscribe — выключить сигналы\n"
            "• /status — текущие параметры"
        )

    @dp.message(Command("subscribe"))
    async def sub(m: Message):
        await add_sub(m.chat.id)
        await m.answer("✅ Подписка включена.")

    @dp.message(Command("unsubscribe"))
    async def unsub(m: Message):
        await remove_sub(m.chat.id)
        await m.answer("⛔ Подписка отключена.")

    @dp.message(Command("status"))
    async def st(m: Message):
        await m.answer(
            "⚙️ Настройки:\n"
            f"• Порог входа: -{START_NEG_PCT:.2f}%\n"
            f"• Шаг ступени: {STEP_BPS} б.п. (~{STEP_BPS/100:.2f}%)\n"
            f"• Опрос: {POLL_SECONDS} сек\n"
            f"• Сброс после: {RESET_MIN} мин без ухода ниже порога"
        )

    loop = asyncio.get_event_loop()
    loop.create_task(start_health_server())
    loop.create_task(poll_loop(bot))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
