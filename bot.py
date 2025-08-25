import os, time, json, math, requests, sys

BINANCE_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"

# === ENV ===
POLL_SEC      = int(os.getenv("POLL_SEC", "30"))
THRESHOLD     = float(os.getenv("THRESHOLD", "-1.0"))      # включать всё <= этого порога
DOWN_STEP     = float(os.getenv("DOWN_STEP", "0.25"))      # шаг на углублении
REBOUND_STEP  = float(os.getenv("REBOUND_STEP", "0.05"))   # шаг на откате после -2%
REBOUND_START = float(os.getenv("REBOUND_START", "-2.0"))  # точка включения откатной сетки
ONLY_USDT     = os.getenv("ONLY_USDT", "1") not in ("0","false","False")
SNAPSHOT_MODE = os.getenv("SNAPSHOT_MODE", "0") not in ("0","false","False")  # 1=снапшот каждые POLL_SEC
TG_TOKEN      = os.getenv("TG_TOKEN", "")
TG_CHAT_ID    = os.getenv("TG_CHAT_ID", "")
STATE_FILE    = os.getenv("STATE_FILE", "/data/funding_state.json")
UPDATE_POLL   = int(os.getenv("UPDATE_POLL", "2"))  # как часто опрашивать getUpdates

def log(*a): print(*a, flush=True)

# --- Telegram ---
def tg_send_text(chat_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    requests.post(url, json=payload, timeout=20)

def tg_answer_cbq(cb_id, text="OK", show_alert=False):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/answerCallbackQuery"
    requests.post(url, json={"callback_query_id": cb_id, "text": text, "show_alert": show_alert}, timeout=15)

def tg_get_updates(offset):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates"
    r = requests.get(url, params={"timeout": 0, "offset": offset}, timeout=20)
    data = r.json()
    if not data.get("ok"): return offset, []
    updates = data.get("result", [])
    new_offset = offset
    if updates: new_offset = updates[-1]["update_id"] + 1
    return new_offset, updates

# --- State ---
def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except:
        return {"symbols": {}, "meta": {"last_scan_ts": 0, "last_hits": 0}}

def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

# --- Funding logic ---
def to_pct(v): return float(v) * 100.0 if v else 0.0

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

def fmt_msg(sym, curr, tag): return f"{tag} {sym}: фандинг {curr:.2f}%"

def process_symbol(sym, curr_pct, st):
    if curr_pct > THRESHOLD: return [], None
    if st is None: st = {"last_sent": None, "min_seen": None, "touched_rebound": False, "last_mode": None}
    if st["min_seen"] is None or curr_pct < st["min_seen"]: st["min_seen"] = curr_pct
    if curr_pct <= REBOUND_START or (st["min_seen"] and st["min_seen"] <= REBOUND_START): st["touched_rebound"] = True

    msgs = []; last = st["last_sent"]; mode = st["last_mode"]
    if last is not None and curr_pct < last: mode = "down"
    elif st["touched_rebound"] and st["min_seen"] and curr_pct > st["min_seen"]: mode = "rebound"
    st["last_mode"] = mode

    level, tag = None, "⚠️"
    if mode == "rebound" and st["touched_rebound"]:
        level, tag = grid_rebound(curr_pct), "↗️"
    else:
        level, tag = grid_down(curr_pct), "⬇️"
    if not level: return [], st

    if last is None or (tag == "⬇️" and level < last) or (tag == "↗️" and level > last):
        msgs.append(fmt_msg(sym, curr_pct, tag)); st["last_sent"] = level
    return msgs, st

def fetch_currents():
    r = requests.get(BINANCE_URL, timeout=25); r.raise_for_status(); data = r.json()
    if isinstance(data, dict): data = [data]
    rows = []
    for x in data:
        sym = x.get("symbol", "")
        if ONLY_USDT and not sym.endswith("USDT"): continue
        curr = to_pct(x.get("lastFundingRate", 0.0))
        if curr <= THRESHOLD: rows.append((sym, curr))
    rows.sort(key=lambda t: t[1]); return rows

def snapshot_text(rows):
    ts = int(time.time())
    lines = [f"📊 Фандинг ≤ {THRESHOLD:.2f}% (каждые {POLL_SEC}s)",
             f"Время: <t:{ts}:T>  (<t:{ts}:R>)","—"*28]
    for sym, curr in rows: lines.append(f"{sym:>10}  {curr:.2f}%")
    return "\n".join(lines)

def status_keyboard():
    return {"inline_keyboard": [[{"text": "🔎 Проверить сейчас", "callback_data": "check_now"}]]}

def format_status(meta):
    last_ts = meta.get("last_scan_ts", 0); hits = meta.get("last_hits", 0)
    ts_txt = f"<t:{last_ts}:T> (<t:{last_ts}:R>)" if last_ts else "—"
    return (f"🟢 Бот запущен\n"
            f"Режим: автоскан {POLL_SEC}s\n"
            f"Порог: ≤{THRESHOLD:.2f}% | Шаг вниз {DOWN_STEP:.2f}% | Откат {REBOUND_STEP:.2f}%\n"
            f"Последний скан: {ts_txt}\nСовпадений: {hits}")

# --- Main ---
def main():
    if not TG_TOKEN or not TG_CHAT_ID: log("ERROR: set TG_TOKEN,TG_CHAT_ID"); sys.exit(1)
    state = load_state(); sym_state = state.get("symbols", {}); meta = state.get("meta", {"last_scan_ts":0,"last_hits":0})
    next_scan_at, next_upd_at, upd_offset = 0, 0, 0
    log("Started: poll",POLL_SEC,"sec")

    while True:
        now = time.time()
        # Telegram updates
        if now >= next_upd_at:
            next_upd_at = now + UPDATE_POLL
            upd_offset, updates = tg_get_updates(upd_offset)
            for u in updates:
                msg, cbq = u.get("message") or {}, u.get("callback_query")
                if msg and msg.get("text","").startswith("/status"):
                    tg_send_text(msg["chat"]["id"], format_status(meta), reply_markup=status_keyboard())
                if cbq and cbq.get("data")=="check_now":
                    rows = fetch_currents(); meta["last_scan_ts"]=int(time.time()); meta["last_hits"]=len(rows)
                    tg_send_text(cbq["message"]["chat"]["id"],
                                 snapshot_text(rows) if rows else "🆗 Ни одной монеты ≤ порога.")
                    tg_answer_cbq(cbq["id"], "Готово")

        # Auto scan
        if now >= next_scan_at:
            next_scan_at = now + POLL_SEC
            rows = fetch_currents(); meta["last_scan_ts"]=int(time.time()); meta["last_hits"]=len(rows)
            if SNAPSHOT_MODE and rows: tg_send_text(TG_CHAT_ID, snapshot_text(rows))
            for sym,curr in rows:
                st = sym_state.get(sym); msgs,new_st = process_symbol(sym,curr,st)
                sym_state[sym]=new_st
                for m in msgs: tg_send_text(TG_CHAT_ID, m)
            # save
            state["symbols"],state["meta"]=sym_state,meta; save_state(state)
        time.sleep(0.5)

if __name__=="__main__": main()
