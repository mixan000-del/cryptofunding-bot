import os, time, json, math, asyncio, sys
import aiohttp

BINANCE_URL = os.getenv("BINANCE_URL", "https://fapi.binance.com/fapi/v1/premiumIndex")

# === ENV ===
POLL_SEC      = int(os.getenv("POLL_SEC", "30"))
THRESHOLD     = float(os.getenv("THRESHOLD", "-1.0"))      # включать всё <= этого порога (в %)
DOWN_STEP     = float(os.getenv("DOWN_STEP", "0.25"))      # шаг на углублении
REBOUND_STEP  = float(os.getenv("REBOUND_STEP", "0.05"))   # шаг на откате после -2%
REBOUND_START = float(os.getenv("REBOUND_START", "-2.0"))  # точка включения сетки отката
ONLY_USDT     = os.getenv("ONLY_USDT", "1") not in ("0","false","False")
SNAPSHOT_MODE = os.getenv("SNAPSHOT_MODE", "0") not in ("0","false","False")
UPDATE_POLL   = int(os.getenv("UPDATE_POLL", "2"))
TIMEOUT_SEC   = int(os.getenv("TIMEOUT_SEC", "15"))

TG_TOKEN      = os.getenv("TG_TOKEN", "")
TG_CHAT_ID    = os.getenv("TG_CHAT_ID", "")
STATE_FILE    = os.getenv("STATE_FILE", "/data/binance_funding_state.json")

# Прокси (HTTP/SOCKS5 через aiohttp 'proxy' параметр)
PROXY_URL     = os.getenv("PROXY_URL", "").strip()  # http://user:pass@host:port

def log(*a): print(*a, flush=True)

# ========= Telegram =========
async def tg_send_text(session, chat_id, text, reply_markup=None):
    if not TG_TOKEN or not chat_id: return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if reply_markup: payload["reply_markup"] = reply_markup
    try:
        async with session.post(url, json=payload, timeout=TIMEOUT_SEC) as r:
            await r.read()
    except Exception as e:
        log("TG send error:", e)

async def tg_answer_cbq(session, cb_id, text="OK", show_alert=False):
    if not TG_TOKEN: return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/answerCallbackQuery"
    try:
        async with session.post(url, json={"callback_query_id": cb_id, "text": text, "show_alert": show_alert}, timeout=TIMEOUT_SEC) as r:
            await r.read()
    except Exception as e:
        log("TG cbq error:", e)

async def tg_get_updates(session, offset):
    if not TG_TOKEN: return offset, []
    url = f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates"
    try:
        async with session.get(url, params={"timeout": 0, "offset": offset}, timeout=TIMEOUT_SEC) as r:
            data = await r.json()
        if not data.get("ok"): return offset, []
        updates = data.get("result", [])
        new_offset = offset
        if updates: new_offset = updates[-1]["update_id"] + 1
        return new_offset, updates
    except Exception:
        return offset, []

def status_keyboard():
    return {"inline_keyboard": [[{"text": "🔎 Проверить сейчас", "callback_data": "check_now"}]]}

def format_status(meta, last_err=None):
    last_ts = meta.get("last_scan_ts", 0); hits = meta.get("last_hits", 0); inst_n = meta.get("inst_count", 0)
    ts_txt = f"<t:{last_ts}:T> (<t:{last_ts}:R>)" if last_ts else "—"
    err = f"\nПоследняя ошибка запроса: {last_err}" if last_err else ""
    proxy = f"\nProxy: {'ON' if PROXY_URL else 'OFF'}"
    return (f"🟢 Binance бот запущен\n"
            f"Перпов (USDT): {inst_n}{proxy}\n"
            f"Режим: автоскан {POLL_SEC}s\n"
            f"Порог: ≤{THRESHOLD:.2f}% | Вниз {DOWN_STEP:.2f}% | Откат {REBOUND_STEP:.2f}% от {REBOUND_START:.2f}%\n"
            f"Последний скан: {ts_txt}\nСовпадений: {hits}{err}")

# ========= State =========
def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except:
        return {"symbols": {}, "meta": {"last_scan_ts": 0, "last_hits": 0, "inst_count": 0}}

def save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        log("Save state error:", e)

# ========= Funding logic =========
def to_pct(v):
    try: return float(v) * 100.0
    except: return 0.0

def grid_down(rate_pct):
    if rate_pct > THRESHOLD: return None
    steps = math.floor((abs(rate_pct) - abs(THRESHOLD)) / DOWN_STEP + 1e-9)
    lvl = THRESHOLD - steps * DOWN_STEP
    return round(lvl, 2)

def grid_rebound(rate_pct):
    if rate_pct > THRESHOLD or rate_pct < REBOUND_START: return None
    k = math.ceil((rate_pct - REBOUND_START) / REBOUND_STEP - 1e-9)
    lvl = REBOUND_START + k * REBOUND_STEP
    if lvl > THRESHOLD: lvl = THRESHOLD
    return round(lvl, 2)

def fmt_msg(sym, curr, tag):
    return f"{tag} {sym}: фандинг {curr:.2f}%"

def process_symbol(sym, curr_pct, st):
    if curr_pct > THRESHOLD: return [], None
    if st is None: st = {"last_sent": None, "min_seen": None, "touched_rebound": False, "last_mode": None}
    if st["min_seen"] is None or curr_pct < st["min_seen"]: st["min_seen"] = curr_pct
    if curr_pct <= REBOUND_START or (st["min_seen"] and st["min_seen"] <= REBOUND_START): st["touched_rebound"] = True

    msgs = []; last = st["last_sent"]; mode = st["last_mode"]
    if last is not None and curr_pct < last: mode = "down"
    elif st["touched_rebound"] and st["min_seen"] and curr_pct > st["min_seen"]: mode = "rebound"
    st["last_mode"] = mode

    level, tag = (grid_rebound(curr_pct), "↗️") if (mode=="rebound" and st["touched_rebound"]) else (grid_down(curr_pct), "⬇️")
    if level is None: return [], st

    if last is None or (tag == "⬇️" and level < last) or (tag == "↗️" and level > last):
        msgs.append(fmt_msg(sym, curr_pct, tag)); st["last_sent"] = level
    return msgs, st

def snapshot_text(rows):
    ts = int(time.time())
    lines = [f"📊 Binance: фандинг ≤ {THRESHOLD:.2f}% (каждые {POLL_SEC}s)",
             f"Время: <t:{ts}:T>  (<t:{ts}:R>)",
             "—"*32]
    for sym, curr in rows:
        lines.append(f"{sym:>16}  {curr:.2f}%")
    return "\n".join(lines)

# ========= Binance fetch =========
async def fetch_binance(session):
    try:
        kwargs = {}
        if PROXY_URL:
            kwargs["proxy"] = PROXY_URL
        async with session.get(BINANCE_URL, timeout=TIMEOUT_SEC, **kwargs) as r:
            if r.status == 451:
                raise RuntimeError("451 from Binance")
            data = await r.json()
    except Exception as e:
        return [], 0, str(e)

    if isinstance(data, dict): data = [data]
    rows = []
    inst_count = 0
    for x in data:
        sym = x.get("symbol","")
        # оставляем только USDT перпы
        if ONLY_USDT and not sym.endswith("USDT"):
            continue
        inst_count += 1
        rate = to_pct(x.get("lastFundingRate", 0.0))
        if rate <= THRESHOLD:
            # унифицируем формат имени для сообщений
            rows.append((f"{sym.replace('USDT','')}-USDT-SWAP", rate))
    rows.sort(key=lambda t: t[1])
    return rows, inst_count, None

# ========= Main loop =========
async def main():
    if not TG_TOKEN or not TG_CHAT_ID:
        log("ERROR: set TG_TOKEN,TG_CHAT_ID"); sys.exit(1)

    state = load_state()
    sym_state = state.get("symbols", {})
    meta = state.get("meta", {"last_scan_ts": 0, "last_hits": 0, "inst_count": 0})
    last_fetch_error = None
    updates_offset = 0
    next_scan_at = 0
    next_updates_at = 0

    timeout = aiohttp.ClientTimeout(total=None)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        log("Binance monitor started. Poll", POLL_SEC, "sec", "| Proxy:", "ON" if PROXY_URL else "OFF")

        while True:
            now = time.time()

            # Telegram updates
            if now >= next_updates_at:
                next_updates_at = now + UPDATE_POLL
                updates_offset, updates = await tg_get_updates(session, updates_offset)
                for u in updates:
                    msg, cbq = u.get("message") or {}, u.get("callback_query")
                    if msg and str(msg.get("text","")).startswith("/status"):
                        await tg_send_text(session, msg["chat"]["id"], format_status(meta, last_fetch_error),
                                           reply_markup=status_keyboard())
                    if cbq and cbq.get("data") == "check_now":
                        rows, inst_n, err = await fetch_binance(session)
                        meta["last_scan_ts"] = int(time.time()); meta["last_hits"] = len(rows); meta["inst_count"] = inst_n
                        await tg_send_text(session, cbq["message"]["chat"]["id"],
                                           snapshot_text(rows) if rows else "🆗 Ни одной монеты ≤ порога сейчас.")
                        await tg_answer_cbq(session, cbq["id"], "Готово")

            # Автоскан
            if now >= next_scan_at:
                next_scan_at = now + POLL_SEC
                rows, inst_n, err = await fetch_binance(session)
                meta["last_scan_ts"] = int(time.time())
                meta["last_hits"] = len(rows)
                meta["inst_count"] = inst_n
                last_fetch_error = err

                if SNAPSHOT_MODE and rows:
                    await tg_send_text(session, TG_CHAT_ID, snapshot_text(rows))

                # триггеры
                for inst_id, curr in rows:
                    st = sym_state.get(inst_id)
                    msgs, new_st = process_symbol(inst_id, curr, st)
                    sym_state[inst_id] = new_st
                    for m in msgs:
                        await tg_send_text(session, TG_CHAT_ID, m)

                # сброс ушедших выше порога
                active = {iid for iid, _ in rows}
                for s in list(sym_state.keys()):
                    if s not in active:
                        sym_state.pop(s, None)

                state["symbols"], state["meta"] = sym_state, meta
                save_state(state)

            await asyncio.sleep(0.2)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
