# bot.py
import os
import json
import math
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from collections import defaultdict, deque
from typing import Deque, Dict, List, Tuple, Optional

import aiosqlite
from aiohttp import ClientSession, web

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart, Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

logging.basicConfig(level=logging.INFO)

# ====== ПАРАМЕТРЫ ЧЕРЕЗ ENV ======
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Порог входа в "негативную зону", в процентах (1.0 = -1.0%)
START_NEG_PCT = float(os.getenv("START_NEG_PCT", "1.0"))

# Размер ступени-алерта в базисных пунктах (20 б.п. = 0.20%)
STEP_BPS = int(os.getenv("STEP_BPS", "20"))

# Период опроса всех пар, секунд
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "30"))

# Через сколько минут без отрицательного фонда сбрасывать прогресс
RESET_MIN = int(os.getenv("RESET_MIN", "30"))

# Сильный алерт (например, −2%)
STRONG_ALERT_PCT = float(os.getenv("STRONG_ALERT_PCT", "2.0"))
STRONG_COOLDOWN_MIN = int(os.getenv("STRONG_COOLDOWN_MIN", "60"))

DB_PATH = "subs.db"

# ====== ПАМЯТЬ/СОСТОЯНИЯ ======
last_step_idx: Dict[str, int] = defaultdict(lambda: -1)  # последняя отправленная "ступень" для символа
last_below_ts: Dict[str, datetime] = defaultdict(lambda: datetime.fromtimestamp(0, tz=timezone.utc))
recent_rates: Dict[str, Deque[Tuple[datetime, float]]] = defaultdict(lambda: deque(maxlen=10))  # последние значения
last_strong_alert_at: Dict[str, datetime] = defaultdict(lambda: datetime.fromtimestamp(0, tz=timezone.utc))

# ====== БД ПОДПИСЧИКОВ ======
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

async def get_all_subs() -> List[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT chat_id FROM subscribers")
        rows = await cur.fetchall()
        return [r[0] for r in rows]

# ====== УТИЛИТЫ ======
def pct(rate_float: float) -> float:
    # из доли в проценты (-0.0123 -> -1.23)
    return rate_float * 100.0

def bps(rate_float: float) -> float:
    # из доли в базисные пункты (-0.0123 -> -123.0)
    return rate_float * 10000.0

def fmt_pct(rate_float: float) -> str:
    return f"{pct(rate_float):.2f}%"

def human_eta_ms(ms: Optional[int]) -> str:
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

# ====== БИНАНС ЭНДПОИНТЫ ======
FAPI_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"  # USDⓈ-M
DAPI_URL = "https://dapi.binance.com/dapi/v1/premiumIndex"  # COIN-M

async def fetch_all_pairs(session: ClientSession) -> List[dict]:
    """
    Забираем все контракты USDⓈ-M и COIN-M с текущим funding.
    Если не указывать symbol — приходит массив объектов по всем контрактам.
    """
    results: List[dict] = []
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

# ====== ОСНОВНОЙ ЦИКЛ ОПРОСА ======
async def poll_loop(bot: Bot):
    step_pct = STEP_BPS / 100.0            # 20 б.п. -> 0.20%
    start_neg_pct = START_NEG_PCT          # 1.0 -> -1.0%

    async with ClientSession() as session:
        while True:
            try:
                all_rows = await fetch_all_pairs(session)
                now = datetime.now(timezone.utc)

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
                    except Exception:
                        continue

                    # копим последние значения для оценки темпа
                    recent_rates[sym].append((now, r))

                    # ---------- СИЛЬНЫЙ АЛЕРТ (например, -2.00%) ----------
                    current_pct = pct(r)  # уже в %
                    if current_pct <= -STRONG_ALERT_PCT:
                        cooldown_ok = now >= (last_strong_alert_at[sym] + timedelta(minutes=STRONG_COOLDOWN_MIN))
                        if cooldown_ok:
                            # простой темп по последним ~5 точкам
                            rate_list = [x for (_, x) in list(recent_rates[sym])[-5:]]
                            delta_bps = (bps(rate_list[-1]) - bps(rate_list[0])) if len(rate_list) >= 2 else 0.0

                            text = (
                                f"🔴 <b>СИЛЬНЫЙ АЛЕРТ</b> — {sym}\n"
                                f"• Funding: <b>{fmt_pct(r)}</b> (≤ -{STRONG_ALERT_PCT:.2f}%)\n"
                                f"• Темп за ~{min(len(rate_list)*POLL_SECONDS//60, 5)}м: <b>{delta_bps:.1f} б.п.</b>\n"
                                f"• Следующая выплата через: <b>{human_eta_ms(nextT)}</b>"
                            )
                            for chat_id in subs:
                                try:
                                    await bot.send_message(chat_id, text)
                                except Exception as e:
                                    logging.warning(f"send_message error chat {chat_id}: {e}")

                            last_strong_alert_at[sym] = now
                            # синхронизируем ступени, чтобы не дублировать «обычный» алерт этим же тиком
                            idx_now = step_index(abs(current_pct), start_neg_pct, step_pct)
                            last_step_idx[sym] = max(last_step_idx[sym], idx_now)
                            continue  # переходим к следующему символу

                    # ---------- ОБЫЧНАЯ ЛОГИКА (ступени от -1%) ----------
                    if current_pct > -start_neg_pct:
                        # если давно выше порога — сбросить ступени
                        if (now - last_below_ts[sym]) > timedelta(minutes=RESET_MIN):
                            last_step_idx[sym] = -1
                        continue

                    # мы в зоне r <= −1% → отметим последнюю "ниже порога"
                    last_below_ts[sym] = now

                    # индекс текущей ступени по абсолютному значению
                    idx = step_index(abs(current_pct), start_neg_pct, step_pct)
                    if idx > last_step_idx[sym] >= -1:
                        # оценим простой "темп" по последним точкам (до ~5 измерений)
                        rate_list = [x for (_, x) in list(recent_rates[sym])[-5:]]
                        delta_bps = (bps(rate_list[-1]) - bps(rate_list[0])) if len(rate_list) >= 2 else 0.0

                        text = (
                            f"⚠️ <b>{sym}</b> funding углубляется:\n"
                            f"• Текущая ставка: <b>{fmt_pct(r)}</b>\n"
                            f"• Ступень: <b>≈ -{start_neg_pct + (idx+1)*step_pct:.2f}%</b> "
                            f"(порог -{start_neg_pct:.2f}%, шаг {step_pct:.2f}%)\n"
                            f"• Темп за ~{min(len(rate_list)*POLL_SECONDS//60, 5)}м: <b>{delta_bps:.1f} б.п.</b>\n"
                            f"• Следующая выплата через: <b>{human_eta_ms(nextT)}</b>"
                        )

                        for chat_id in subs:
                            try:
                                await bot.send_message(chat_id, text)
                            except Exception as e:
                                logging.warning(f"send_message error chat {chat_id}: {e}")

                        last_step_idx[sym] = idx

                await asyncio.sleep(POLL_SECONDS)

            except Exception as e:
                logging.error(f"poll_loop error: {e}")
                await asyncio.sleep(POLL_SECONDS)

# ====== МИНИ HEALTH-СЕРВЕР ДЛЯ ХОСТИНГА ======
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

# ====== ТЕЛЕГРАМ-БОТ ======
async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing")

    await db_init()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    @dp.message(CommandStart())
    async def start(m: Message):
        await add_sub(m.chat.id)
        await m.answer(
            "Привет! Я слежу за funding rate по <b>всем</b> фьючерсным контрактам Binance.\n\n"
            "Правила сигналов:\n"
            f"• Порог входа: ниже <b>-{START_NEG_PCT:.2f}%</b>\n"
            f"• Ступени: <b>{STEP_BPS} б.п.</b> (например: -1.20%, -1.40%, ...)\n"
            f"• Сильный алерт: при ≤ <b>-{STRONG_ALERT_PCT:.2f}%</b> (кулдаун {STRONG_COOLDOWN_MIN} мин)\n"
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
            f"• Сильный алерт: ≤ -{STRONG_ALERT_PCT:.2f}% (кулдаун {STRONG_COOLDOWN_MIN} мин)\n"
            f"• Опрос: {POLL_SECONDS} сек\n"
            f"• Сброс ступеней: после {RESET_MIN} мин вне зоны"
        )

    loop = asyncio.get_event_loop()
    loop.create_task(start_health_server())
    loop.create_task(poll_loop(bot))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
