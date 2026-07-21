# =============================================================================
# scanner.py  |  OKX_PAMP_bot  |  лише OKX USDT-SWAP
# БЛОК 1: памп з об'ємами  — ціна < 5 USDT, ріст >= 50%, об'єм >= 10х
# БЛОК 2: рух без об'ємів — ціна < 5 USDT, ріст >= 50% АБО падіння >= 50%
# ЧЕРГА: без сигналу — рядок часу у чергу; з сигналом — надсилаємо все
# =============================================================================

import requests, json, os, time
from datetime import datetime, timezone

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
OKX_BASE_URL     = "https://www.okx.com"
STATE_FILE       = "state.json"
CANDLES_COUNT    = 32
MAX_PRICE_USDT   = 5.0
GROWTH_THRESHOLD = 50.0
VOLUME_SPIKE_X   = 10.0
VOLUME_TAIL_X    = 5.0
HALF_CANDLES     = CANDLES_COUNT // 2
REQUEST_DELAY    = 0.12

# ── state.json ────────────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        if os.path.getsize(STATE_FILE) == 0:
            print("state.json порожній — починаємо з нуля")
            return {}
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            print(f"Помилка читання state.json: {e} — починаємо з нуля")
            return {}
    return {}

def save_state(state):
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except (OSError, TypeError, ValueError) as e:
        print(f"Помилка збереження state.json: {e}")

# ── OKX API ───────────────────────────────────────────────────────────────────

def okx_get_instruments():
    try:
        resp = requests.get(f"{OKX_BASE_URL}/api/v5/public/instruments",
                            params={"instType": "SWAP"}, timeout=15)
        data = resp.json()
        if data.get("code") != "0":
            print(f"OKX instruments помилка: {data.get('msg')}")
            return []
        return [i["instId"] for i in data.get("data", [])
                if i.get("instId", "").endswith("-USDT-SWAP")]
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"Виняток okx_get_instruments: {e}")
        return []

def okx_get_candles(inst_id):
    try:
        resp = requests.get(f"{OKX_BASE_URL}/api/v5/market/candles",
                            params={"instId": inst_id, "bar": "15m",
                                    "limit": str(CANDLES_COUNT)}, timeout=10)
        data = resp.json()
        if data.get("code") != "0" or not data.get("data"):
            return []
        candles = data["data"]
        candles.reverse()
        return candles
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"Виняток okx_get_candles {inst_id}: {e}")
        return []

# ── Допоміжні функції ─────────────────────────────────────────────────────────

def ts_to_utc(ts_ms):
    try:
        return datetime.fromtimestamp(int(ts_ms)/1000, tz=timezone.utc).strftime("%H:%M")
    except (ValueError, TypeError, OSError):
        return "--:--"

def fmt_price(p):
    if p >= 1.0:   return f"{p:.4f}"
    if p >= 0.01:  return f"{p:.5f}"
    return f"{p:.7f}"

# ── Аналіз об'ємів ────────────────────────────────────────────────────────────

def analyze_volumes(candles, saved_avg):
    if not candles:
        return False, -1, 0, (saved_avg or 0.0)
    volumes_in_base = []
    if saved_avg and saved_avg > 0:
        current_avg = saved_avg; start_idx = 0
    else:
        first_vol = float(candles[0][5] or 0)
        current_avg = first_vol if first_vol > 0 else 1.0
        volumes_in_base.append(first_vol); start_idx = 1
    signal_found = False; signal_idx = -1; tail_count = 0; tail_indices = []
    i = start_idx
    while i < len(candles):
        vol = float(candles[i][5] or 0)
        if not signal_found:
            if current_avg > 0 and vol >= current_avg * VOLUME_SPIKE_X:
                signal_found = True; signal_idx = i
                tail_count = 1; tail_indices = [i]
            else:
                volumes_in_base.append(vol)
                current_avg = sum(volumes_in_base) / len(volumes_in_base)
        else:
            if vol >= current_avg * VOLUME_TAIL_X:
                tail_indices.append(i); tail_count = len(tail_indices)
                if tail_count >= HALF_CANDLES:
                    for ti in tail_indices:
                        volumes_in_base.append(float(candles[ti][5] or 0))
                    current_avg = sum(volumes_in_base) / len(volumes_in_base)
                    signal_found = False; signal_idx = -1
                    tail_count = 0; tail_indices = []
            else:
                volumes_in_base.append(vol)
                current_avg = sum(volumes_in_base) / len(volumes_in_base)
        i += 1
    final_avg = sum(volumes_in_base)/len(volumes_in_base) if volumes_in_base else current_avg
    return signal_found, signal_idx, tail_count, final_avg

# ── Аналіз ціни ───────────────────────────────────────────────────────────────

def analyze_price_up(candles):
    try:
        best_pct = 0.0; best_max = 0.0; best_min_ts = None; best_max_ts = None
        running_min = float("inf"); running_min_ts = None
        for c in candles:
            high = float(c[2] or 0); low = float(c[3] or 0); ts = int(c[0])
            if 0 < low < running_min:
                running_min = low; running_min_ts = ts
            if running_min > 0 and high > 0 and running_min_ts and ts > running_min_ts:
                pct = (high - running_min) / running_min * 100
                if pct > best_pct:
                    best_pct = pct; best_max = high
                    best_min_ts = running_min_ts; best_max_ts = ts
        if best_pct <= 0: return 0.0, 0.0, "--:--", "--:--"
        return best_pct, best_max, ts_to_utc(best_min_ts), ts_to_utc(best_max_ts)
    except (ValueError, TypeError, IndexError, ZeroDivisionError) as e:
        print(f"Виняток analyze_price_up: {e}")
        return 0.0, 0.0, "--:--", "--:--"

def analyze_price_down(candles):
    try:
        best_pct = 0.0; best_min = 0.0; best_max_ts = None; best_min_ts = None
        running_max = 0.0; running_max_ts = None
        for c in candles:
            high = float(c[2] or 0); low = float(c[3] or 0); ts = int(c[0])
            if high > running_max:
                running_max = high; running_max_ts = ts
            if running_max > 0 and low > 0 and running_max_ts and ts > running_max_ts:
                pct = (running_max - low) / running_max * 100
                if pct > best_pct:
                    best_pct = pct; best_min = low
                    best_max_ts = running_max_ts; best_min_ts = ts
        if best_pct <= 0: return 0.0, 0.0, "--:--", "--:--"
        return best_pct, best_min, ts_to_utc(best_max_ts), ts_to_utc(best_min_ts)
    except (ValueError, TypeError, IndexError, ZeroDivisionError) as e:
        print(f"Виняток analyze_price_down: {e}")
        return 0.0, 0.0, "--:--", "--:--"

# ── Форматування ─────────────────────────────────────────────────────────────

def fmt_b1(name, growth_pct, max_price, min_time, max_time, tail_count, is_last):
    base = f"{name}+{growth_pct:.1f}%;ОКХ;max{fmt_price(max_price)}({min_time}-{max_time});V+10х"
    return base if is_last else f"{base}({tail_count}св)"

def fmt_b2(name, pct, price, start_time, end_time, is_up):
    p = fmt_price(price)
    if is_up: return f"{name}+{pct:.1f}%;ОКХ;max{p};{start_time}-{end_time}"
    else:     return f"{name}-{pct:.1f}%;ОКХ;min{p};{start_time}-{end_time}"

# ── Telegram ─────────────────────────────────────────────────────────────────

def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"Telegram не налаштовано:\n{text}"); return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=15)
        if resp.status_code == 200: print("Telegram: надіслано")
        else: print(f"Telegram помилка {resp.status_code}: {resp.text}")
    except (requests.RequestException, OSError) as e:
        print(f"Виняток send_telegram: {e}")

# ── Аналіз інструменту ───────────────────────────────────────────────────────

def analyze_instrument(candles, state_key, state, name,
                       signals_b1, signals_b2, found_b1_keys, stats):
    if len(candles) < 4: return
    try:
        price = float(candles[-1][4] or 0)
    except (ValueError, TypeError, IndexError):
        return
    if price <= 0 or price >= MAX_PRICE_USDT: return

    stats["passed_price"] += 1

    up_pct, up_price, up_min_t, up_max_t = analyze_price_up(candles)
    dn_pct, dn_price, dn_max_t, dn_min_t = analyze_price_down(candles)

    if up_pct >= GROWTH_THRESHOLD or dn_pct >= GROWTH_THRESHOLD:
        stats["passed_growth"] += 1

    # ── Блок 1 ──
    if up_pct >= GROWTH_THRESHOLD:
        saved_avg = state.get(state_key)
        sig_found, sig_idx, tail, final_avg = analyze_volumes(candles, saved_avg)
        state[state_key] = final_avg
        if sig_found:
            is_last = (sig_idx == len(candles) - 1)
            print(f"  [B1] {name}: +{up_pct:.1f}% | {up_min_t}-{up_max_t} | хвіст={tail}св")
            signals_b1.append({"name": name, "growth_pct": up_pct,
                "max_price": up_price, "min_time": up_min_t, "max_time": up_max_t,
                "tail_count": tail, "signal_is_last": is_last})
            found_b1_keys.add(state_key)
            return
    else:
        saved_avg = state.get(state_key)
        _, _, _, final_avg = analyze_volumes(candles, saved_avg)
        state[state_key] = final_avg

    # ── Блок 2 ──
    if state_key in found_b1_keys: return
    best_up = up_pct >= GROWTH_THRESHOLD
    best_dn = dn_pct >= GROWTH_THRESHOLD
    if not best_up and not best_dn: return
    if best_up:
        print(f"  [B2+] {name}: UP {up_pct:.1f}% | {up_min_t}-{up_max_t}")
        signals_b2.append({"name": name, "pct": up_pct, "price": up_price,
            "start_time": up_min_t, "end_time": up_max_t, "is_up": True})
    if best_dn:
        print(f"  [B2-] {name}: DN {dn_pct:.1f}% | {dn_max_t}-{dn_min_t}")
        signals_b2.append({"name": name, "pct": dn_pct, "price": dn_price,
            "start_time": dn_max_t, "end_time": dn_min_t, "is_up": False})

# ── main ─────────────────────────────────────────────────────────────────────

def main():
    now_utc = datetime.now(timezone.utc)
    now_str = now_utc.strftime("%Y-%m-%d  UTC=%H:%M")
    print(f"=== OKX_PAMP_bot старт | {now_str} ===")

    state = load_state()
    print(f"Записів у state.json: {len(state)}")

    signals_b1 = []; signals_b2 = []; found_b1_keys = set()
    stats = {"passed_price": 0, "passed_growth": 0}

    instruments = okx_get_instruments()
    print(f"OKX інструментів: {len(instruments)}")

    for inst_id in instruments:
        candles   = okx_get_candles(inst_id)
        time.sleep(REQUEST_DELAY)
        state_key = f"OKX:{inst_id}"
        name      = inst_id.replace("-USDT-SWAP", "")
        analyze_instrument(candles, state_key, state, name,
                           signals_b1, signals_b2, found_b1_keys, stats)

    print(f"Діагностика: пройшли ціну (<{MAX_PRICE_USDT}$): {stats['passed_price']} | "
          f"пройшли рух (>={GROWTH_THRESHOLD}%): {stats['passed_growth']}")

    # ── Формуємо сигнальні рядки ──
    signal_lines = []
    if signals_b1:
        signals_b1.sort(key=lambda x: x["growth_pct"], reverse=True)
        for s in signals_b1:
            line = fmt_b1(s["name"], s["growth_pct"], s["max_price"],
                          s["min_time"], s["max_time"],
                          s["tail_count"], s["signal_is_last"])
            signal_lines.append(line)
            print(f"  >> [B1] {line}")
    if signals_b1 and signals_b2:
        signal_lines.append("")
    if signals_b2:
        signals_b2.sort(key=lambda x: x["pct"], reverse=True)
        for s in signals_b2:
            line = fmt_b2(s["name"], s["pct"], s["price"],
                          s["start_time"], s["end_time"], s["is_up"])
            signal_lines.append(line)
            print(f"  >> [B2] {line}")

    # ── Логіка черги ──
    pending = state.get("pending", [])
    if not signal_lines:
        pending.append(now_str)
        state["pending"] = pending
        save_state(state)
        print(f"Сигналів немає → черга ({len(pending)} накопичено)")
    else:
        msg = "\n".join(pending + signal_lines)
        state["pending"] = []
        save_state(state)
        send_telegram(msg)
        print(f"Надіслано: {len(pending)} рядків черги + {len(signal_lines)} сигналів")

    print("=== Завершено ===")

if __name__ == "__main__":
    main()
