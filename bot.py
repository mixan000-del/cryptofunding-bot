import os, json, asyncio, logging
from datetime import datetime, timezone, timedelta
from collections import deque, defaultdict

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import CommandStart, Command
from aiogram.fsm.storage.memory import MemoryStorage
import websockets

# мини HTTP-сервер для Koyeb/Railway/Render (health check)
from aiohttp import web

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
SYMBOLS = [s.strip().upper() for s in os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT").split(",") if s.strip()]
WINDOW_MIN = int(os.getenv("WINDOW_MIN", "5"))
NEGATIVE_BPS = float(os.getenv("NEGATIVE_BPS", "5"))
DROP_BPS = float(os.getenv("DROP_BPS", "3"))
COOLDOWN_MIN = int(os.getenv("COOLDOWN_MIN", "15"))

WS_URL = "wss://fstream.binance.com/stream?streams=" + "/".join(
    f"{sym.lower()}@markPrice@1s" for sym in SYMBOLS
)

DB_PATH = "subs.db"
rates_window = {sym: deque() for sym in SYMBOLS}
last_alert_at = defaultdict(lambda: datetime.fromtimestamp(0, tz=timezone.utc))

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
            (chat_id, datetime.now(timezone.utc).isoformat())
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

def to_bps(rate_float: float) -> float:
    return rate_float * 10000.0

def fmt_pct(rate_float: float) -> str:
    return f"{rate_float * 100:.3f}%"

def human_eta(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc) - datetime.now(timezone.utc)
    if dt.total_seconds() < 0: return "soon"
    h, rem = divmod(int(dt.total_seconds()), 3600)
    m, _ = divmod(rem, 60)
    return f"{h}h {m}m"

def check_signal(sym: str, now: datetime, rate: float):
    window = rates_window[sym]
    cutoff = now - timedelta(minutes=WINDOW_MIN)
    while window and window[0][0] < cutoff:
        window.popleft()
    window.append((now, rate))

    r_bps = to_bps(rate)
    max_rate = max((r for _, r in window), default=rate)
    drop_bps = (to_bps(max_rate) - r_bps)

    cooled = now >= (last_alert_at[sym] + timedelta(minutes=COOLDOWN_MIN))
    cond1 = r_bps < -NEGATIVE_BPS
    cond2 = drop_bps >= DROP_BPS

    if cooled and (cond1 or cond2):
        last_alert_at[sym] = now
        return {"r_bps": r_bps, "drop_bps": drop_bps}
    return None

async def ws_loop(bot: Bot):
    backoff = 1
    while True:
        try:
            logging.info(f"WS connect: {WS_URL}")
            async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20) as ws:
                backoff = 1
                while True:
                    raw = await ws.recv()
                    data = json.loads(raw)
                    payload = data.get("data", data)
                    sym = payload.get("s")
                    r = payload.get("r")
                    T = payload.get("T")
                    if not sym or r is None: 
                        continue
                    try:
                        rate = float(r)
                    except:
                        continue
                    now = datetime.now(timezone.utc)
                    sig = check_signal(sym, now, rate)
                    if sig:
                        for chat_id in await get_all_subs():
                            txt = (
                                f"⚠️ <b>{sym}</b> funding: <b>{fmt_pct(rate)}</b>\n"
                                f"• Δ за {WINDOW_MIN}м: <b>{sig['drop_bps']:.1f} б.п.</b>\n"
                                f"• До выплаты: <b>{human_eta(T)}</b>\n"
                                f"• Пороги: r < -{NEGATIVE_BPS:.1f} б.п. или падение ≥ {DROP_BPS:.1f} б.п.\n"
                                f"• Кулдаун: {COOLDOWN_MIN} мин"
                            )
                            try:
                                await bot.send_message(chat_id, txt, parse_mode="HTML")
                            except Exception as e:
                                logging.warning(f"send error {chat_id}: {e}")
        except Exception as e:
            logging.error(f"WS error: {e}; reconnect in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

# маленький health-сервер (для Koyeb)
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
            "Привет! Я пришлю сигнал, когда ставка финансирования резко уходит в минус.\n\n"
            "Команды:\n"
            "• /subscribe — включить сигналы\n"
            "• /unsubscribe — выключить\n"
            "• /status — текущие пороги\n"
            f"Отслеживаю: {', '.join(SYMBOLS)}"
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
            f"• Символы: {', '.join(SYMBOLS)}\n"
            f"• Окно: {WINDOW_MIN} мин\n"
            f"• r < -{NEGATIVE_BPS:.1f} б.п.  ИЛИ падение ≥ {DROP_BPS:.1f} б.п.\n"
            f"• Кулдаун: {COOLDOWN_MIN} мин"
        )

    # стартуем health-сервер + поток Binance + поллинг Telegram
    asyncio.get_event_loop().create_task(start_health_server())
    asyncio.get_event_loop().create_task(ws_loop(bot))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
