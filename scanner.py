# =============================================================================
# scanner.py  |  OKX_PAMP_bot  |  OKX + MEXC + Gate.io  |  Спот + Ф'ючерси
#
# РИНКИ:
#   OKX  Ф'ючерси (SWAP):  instType=SWAP,  свічки /api/v5/market/candles
#   OKX  Спот:             instType=SPOT,  свічки /api/v5/market/candles
#   MEXC Ф'ючерси:         /api/v1/contract/detail, свічки /api/v1/contract/kline
#   MEXC Спот:             /api/v3/exchangeInfo,    свічки /api/v3/klines (Binance-формат)
#   Gate Ф'ючерси:         /api/v4/futures/usdt/contracts, свічки /api/v4/futures/usdt/candlesticks
#   Gate Спот:             /api/v4/spot/currency_pairs,    свічки /api/v4/spot/candlesticks
#
# БЛОК 1: памп з об'ємами  — ціна < 5 USDT, ріст >= 50%, об'єм >= 10х
#   Формат ф'ючерс: LAB+63.2%;OKX;max7.7735(17:00-18:45);V+10х(6св)
#   Формат спот:    Спот.LAB+63.2%;OKX;max7.7735(17:00-18:45);V+10х(6св)
#
# БЛОК 2: рух без об'ємів — ціна < 5 USDT, ріст >= 50% АБО падіння >= 50%
#   Формат ф'ючерс: LAB+53.7%;MEXC;max0.16021;01:15-05:45
#   Формат спот:    Спот.LAB-53.7%;GATE;min0.16021;01:15-05:45
#
# ЧЕРГА: без сигналу — рядок часу у чергу; з сигналом — надсилаємо все
# ШВИДКІСТЬ: ThreadPoolExecutor(max_workers=10) — паралельні запити свічок
# =============================================================================

import requests, json, os, time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─────────────────────────────────────────────────────────────────────────────
# БЛОК НАЛАШТУВАНЬ
# ─────────────────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

OKX_BASE_URL  = "https://www.okx.com"
MEXC_BASE_URL = "https://api.mexc.com"
GATE_BASE_URL = "https://api.gateio.ws"

STATE_FILE       = "state.json"
CANDLES_COUNT    = 32                  # 32 × 15хв = 8 годин
MAX_PRICE_USDT   = 5.0
GROWTH_THRESHOLD = 50.0
VOLUME_SPIKE_X   = 10.0
VOLUME_TAIL_X    = 5.0
HALF_CANDLES     = CANDLES_COUNT // 2  # = 16
MAX_WORKERS      = 10                  # паралельних потоків
RETRY_DELAY      = 2.0                 # пауза при HTTP 429

LABEL_OKX  = "OKX"
LABEL_MEXC = "MEXC"
LABEL_GATE = "GATE"

# Префікс у повідомленні для спот-ринку
SPOT_PREFIX = "Спот."


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 1: state.json — атомарне збереження
# ─────────────────────────────────────────────────────────────────────────────

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
    """Атомарне збереження через .tmp → os.replace"""
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except (OSError, TypeError, ValueError) as e:
        print(f"Помилка збереження state.json: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 2: OKX — ф'ючерси і спот
# Однаковий endpoint свічок для обох ринків: /api/v5/market/candles
# instType=SWAP → пари BTC-USDT-SWAP
# instType=SPOT → пари BTC-USDT
# ─────────────────────────────────────────────────────────────────────────────

def okx_get_instruments(inst_type):
    """inst_type: 'SWAP' або 'SPOT'. Повертає список instId."""
    try:
        resp = requests.get(f"{OKX_BASE_URL}/api/v5/public/instruments",
                            params={"instType": inst_type}, timeout=15)
        data = resp.json()
        if data.get("code") != "0":
            print(f"OKX {inst_type} instruments помилка: {data.get('msg')}")
            return []
        if inst_type == "SWAP":
            return [i["instId"] for i in data.get("data", [])
                    if i.get("instId", "").endswith("-USDT-SWAP")]
        else:
            # SPOT: беремо лише пари з USDT як квотою
            return [i["instId"] for i in data.get("data", [])
                    if i.get("instId", "").endswith("-USDT")]
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"Виняток okx_get_instruments({inst_type}): {e}")
        return []

def okx_get_candles(inst_id):
    """
    Свічки OKX — однаковий endpoint для SWAP і SPOT.
    Повертає 32 свічки від [0]=найстаріша до [31]=найновіша.
    Формат: [ts_мс, open, high[2], low[3], close[4], vol[5]]
    """
    for _ in range(2):
        try:
            resp = requests.get(f"{OKX_BASE_URL}/api/v5/market/candles",
                                params={"instId": inst_id, "bar": "15m",
                                        "limit": str(CANDLES_COUNT)}, timeout=10)
            if resp.status_code == 429:
                time.sleep(RETRY_DELAY); continue
            data = resp.json()
            if data.get("code") != "0" or not data.get("data"):
                return []
            candles = data["data"]
            candles.reverse()
            return candles
        except (requests.RequestException, ValueError, KeyError):
            return []
    return []


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 3: MEXC — ф'ючерси
# Символ: "BTC_USDT", час у секундах → × 1000
# ─────────────────────────────────────────────────────────────────────────────

def mexc_fut_get_instruments():
    try:
        resp = requests.get(f"{MEXC_BASE_URL}/api/v1/contract/detail", timeout=15)
        if resp.status_code != 200:
            print(f"MEXC futures instruments HTTP {resp.status_code}")
            return []
        data = resp.json()
        if not data.get("success"):
            print(f"MEXC futures instruments помилка: {data}")
            return []
        result = [item["symbol"] for item in data.get("data", [])
                  if (item.get("state") == 0 and item.get("futureType") == 1
                      and item.get("quoteCoin") == "USDT")]
        print(f"MEXC ф'ючерси: {len(result)}")
        return result
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"Виняток mexc_fut_get_instruments: {e}")
        return []

def mexc_fut_get_candles(symbol):
    """
    MEXC ф'ючерси — окремі масиви time[], high[], low[], close[], vol[]
    Час у секундах → × 1000. Уніфікований формат: [ts_мс, open, high, low, close, vol]
    """
    for _ in range(2):
        try:
            resp = requests.get(
                f"{MEXC_BASE_URL}/api/v1/contract/kline/{symbol}",
                params={"interval": "Min15", "limit": CANDLES_COUNT}, timeout=10)
            if resp.status_code == 429:
                time.sleep(RETRY_DELAY); continue
            if resp.status_code != 200:
                return []
            data = resp.json()
            if not data.get("success") or not data.get("data"):
                return []
            d = data["data"]
            times  = d.get("time",  [])
            opens  = d.get("open",  [])
            highs  = d.get("high",  [])
            lows   = d.get("low",   [])
            closes = d.get("close", [])
            vols   = d.get("vol",   [])
            if not times:
                return []
            candles = []
            for i in range(len(times)):
                try:
                    candles.append([
                        int(times[i]) * 1000,
                        str(opens[i]  if i < len(opens)  else 0),
                        str(highs[i]  if i < len(highs)  else 0),
                        str(lows[i]   if i < len(lows)   else 0),
                        str(closes[i] if i < len(closes) else 0),
                        str(vols[i]   if i < len(vols)   else 0),
                    ])
                except (IndexError, TypeError, ValueError):
                    continue
            return candles
        except (requests.RequestException, ValueError, KeyError):
            return []
    return []


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 4: MEXC — спот
# Символ: "BTCUSDT" (без підкреслення)
# Свічки: Binance-формат [ts_мс, open, high, low, close, vol] — вже уніфікований
# ─────────────────────────────────────────────────────────────────────────────

def mexc_spot_get_instruments():
    """Повертає список символів активних USDT-спот пар на MEXC"""
    try:
        resp = requests.get(f"{MEXC_BASE_URL}/api/v3/exchangeInfo", timeout=20)
        if resp.status_code != 200:
            print(f"MEXC spot instruments HTTP {resp.status_code}")
            return []
        data = resp.json()
        result = []
        for sym in data.get("symbols", []):
            if (sym.get("status") == "ENABLED"
                    and sym.get("quoteAsset") == "USDT"
                    and sym.get("isSpotTradingAllowed", False)):
                result.append(sym["symbol"])  # наприклад "BTCUSDT"
        print(f"MEXC спот: {len(result)}")
        return result
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"Виняток mexc_spot_get_instruments: {e}")
        return []

def mexc_spot_get_candles(symbol):
    """
    MEXC спот — Binance-сумісний формат.
    Повертає 32 свічки від [0]=найстаріша до [31]=найновіша.
    Формат: [ts_мс, open, high[2], low[3], close[4], vol[5], ...]
    """
    for _ in range(2):
        try:
            resp = requests.get(
                f"{MEXC_BASE_URL}/api/v3/klines",
                params={"symbol": symbol, "interval": "15m",
                        "limit": CANDLES_COUNT}, timeout=10)
            if resp.status_code == 429:
                time.sleep(RETRY_DELAY); continue
            if resp.status_code != 200:
                return []
            data = resp.json()
            if not isinstance(data, list) or len(data) == 0:
                return []
            # Binance формат: [ts_мс, open, high, low, close, vol, ...]
            # вже від старих до нових, індекси збігаються з OKX
            return data
        except (requests.RequestException, ValueError):
            return []
    return []


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 5: Gate.io — ф'ючерси
# ─────────────────────────────────────────────────────────────────────────────

def gate_fut_get_instruments():
    try:
        result = []
        offset = 0
        while True:
            resp = requests.get(
                f"{GATE_BASE_URL}/api/v4/futures/usdt/contracts",
                params={"limit": 100, "offset": offset}, timeout=15)
            if resp.status_code != 200:
                print(f"Gate futures instruments HTTP {resp.status_code}")
                break
            data = resp.json()
            if not isinstance(data, list) or len(data) == 0:
                break
            for item in data:
                if not item.get("in_delisting", True):
                    result.append(item["name"])
            if len(data) < 100:
                break
            offset += 100
        print(f"Gate ф'ючерси: {len(result)}")
        return result
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"Виняток gate_fut_get_instruments: {e}")
        return []

def gate_fut_get_candles(contract):
    """
    Gate ф'ючерси — {"t": unix_sec, "o", "h", "l", "c", "v"}
    Уніфікований формат: [ts_мс, open, high, low, close, vol]
    """
    for _ in range(2):
        try:
            resp = requests.get(
                f"{GATE_BASE_URL}/api/v4/futures/usdt/candlesticks",
                params={"contract": contract, "interval": "15m",
                        "limit": CANDLES_COUNT}, timeout=10)
            if resp.status_code == 429:
                time.sleep(RETRY_DELAY); continue
            if resp.status_code != 200:
                return []
            data = resp.json()
            if not isinstance(data, list) or len(data) == 0:
                return []
            candles = []
            for item in data:
                try:
                    candles.append([
                        int(item["t"]) * 1000,
                        str(item.get("o", 0)), str(item.get("h", 0)),
                        str(item.get("l", 0)), str(item.get("c", 0)),
                        str(item.get("v", 0)),
                    ])
                except (KeyError, TypeError, ValueError):
                    continue
            return candles
        except (requests.RequestException, ValueError):
            return []
    return []


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 6: Gate.io — спот
# Символ: "BTC_USDT", свічки {"t": unix_sec, "h", "l", "c", "v"}
# Той самий формат що й у ф'ючерсах → та сама функція конвертації
# ─────────────────────────────────────────────────────────────────────────────

def gate_spot_get_instruments():
    """Повертає список торгових пар активного USDT-спот ринку Gate.io"""
    try:
        result = []
        # Gate spot не потребує пагінації — повертає всі одним запитом
        resp = requests.get(
            f"{GATE_BASE_URL}/api/v4/spot/currency_pairs",
            timeout=15)
        if resp.status_code != 200:
            print(f"Gate spot instruments HTTP {resp.status_code}")
            return []
        data = resp.json()
        if not isinstance(data, list):
            return []
        for item in data:
            # trade_status="tradable" означає активна пара
            if (item.get("trade_status") == "tradable"
                    and item.get("quote") == "USDT"):
                result.append(item["id"])  # наприклад "BTC_USDT"
        print(f"Gate спот: {len(result)}")
        return result
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"Виняток gate_spot_get_instruments: {e}")
        return []

def gate_spot_get_candles(currency_pair):
    """
    Gate спот — {"t": unix_sec, "h", "l", "c", "v"} — той самий формат що й ф'ючерси.
    Уніфікований формат: [ts_мс, open, high, low, close, vol]
    """
    for _ in range(2):
        try:
            resp = requests.get(
                f"{GATE_BASE_URL}/api/v4/spot/candlesticks",
                params={"currency_pair": currency_pair, "interval": "15m",
                        "limit": CANDLES_COUNT}, timeout=10)
            if resp.status_code == 429:
                time.sleep(RETRY_DELAY); continue
            if resp.status_code != 200:
                return []
            data = resp.json()
            if not isinstance(data, list) or len(data) == 0:
                return []
            candles = []
            for item in data:
                try:
                    candles.append([
                        int(item["t"]) * 1000,
                        str(item.get("o", 0)), str(item.get("h", 0)),
                        str(item.get("l", 0)), str(item.get("c", 0)),
                        str(item.get("v", 0)),
                    ])
                except (KeyError, TypeError, ValueError):
                    continue
            return candles
        except (requests.RequestException, ValueError):
            return []
    return []


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 7: Допоміжні функції
# ─────────────────────────────────────────────────────────────────────────────

def ts_to_utc(ts_ms):
    try:
        return datetime.fromtimestamp(
            int(ts_ms) / 1000, tz=timezone.utc
        ).strftime("%H:%M")
    except (ValueError, TypeError, OSError):
        return "--:--"

def fmt_price(p):
    if p >= 1.0:   return f"{p:.4f}"
    if p >= 0.01:  return f"{p:.5f}"
    return f"{p:.7f}"


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 8: Аналіз об'ємів — ковзне середнє з виключенням аномалій
# ─────────────────────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 9: Аналіз ціни — ріст і падіння
# running_min/running_max — екстремуми завжди хронологічно впорядковані
# ─────────────────────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 10: Форматування рядків сигналів
# is_spot=True → додаємо префікс "Спот." перед назвою монети
# ─────────────────────────────────────────────────────────────────────────────

def fmt_b1(name, label, growth_pct, max_price, min_time, max_time,
           tail_count, is_last, is_spot):
    prefix = SPOT_PREFIX if is_spot else ""
    base = (f"{prefix}{name}+{growth_pct:.1f}%;{label};"
            f"max{fmt_price(max_price)}({min_time}-{max_time});V+10х")
    return base if is_last else f"{base}({tail_count}св)"

def fmt_b2(name, label, pct, price, start_time, end_time, is_up, is_spot):
    prefix = SPOT_PREFIX if is_spot else ""
    p = fmt_price(price)
    if is_up:
        return f"{prefix}{name}+{pct:.1f}%;{label};max{p};{start_time}-{end_time}"
    else:
        return f"{prefix}{name}-{pct:.1f}%;{label};min{p};{start_time}-{end_time}"


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 11: Telegram
# ─────────────────────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 12: Паралельне завантаження свічок
# ─────────────────────────────────────────────────────────────────────────────

def fetch_all_candles(instruments, fetch_fn):
    """Паралельно завантажує свічки. Повертає список (ident, candles)."""
    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_ident = {executor.submit(fetch_fn, ident): ident
                           for ident in instruments}
        for future in as_completed(future_to_ident):
            ident = future_to_ident[future]
            try:
                candles = future.result()
            except (ValueError, TypeError, RuntimeError):
                candles = []
            results.append((ident, candles))
    return results


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 13: Аналіз одного інструменту
# is_spot — передається з main(), визначає префікс у повідомленні
# ─────────────────────────────────────────────────────────────────────────────

def analyze_instrument(candles, state_key, state, label, name,
                       signals_b1, signals_b2, found_b1_keys, stats, is_spot):
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
            prefix = "Спот." if is_spot else ""
            print(f"  [B1/{label}] {prefix}{name}: +{up_pct:.1f}% | "
                  f"{up_min_t}-{up_max_t} | хвіст={tail}св")
            signals_b1.append({
                "name": name, "label": label, "growth_pct": up_pct,
                "max_price": up_price, "min_time": up_min_t, "max_time": up_max_t,
                "tail_count": tail, "signal_is_last": is_last, "is_spot": is_spot,
            })
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

    prefix = "Спот." if is_spot else ""
    if best_up:
        print(f"  [B2+/{label}] {prefix}{name}: UP {up_pct:.1f}% | {up_min_t}-{up_max_t}")
        signals_b2.append({"name": name, "label": label, "pct": up_pct,
            "price": up_price, "start_time": up_min_t, "end_time": up_max_t,
            "is_up": True, "is_spot": is_spot})
    if best_dn:
        print(f"  [B2-/{label}] {prefix}{name}: DN {dn_pct:.1f}% | {dn_max_t}-{dn_min_t}")
        signals_b2.append({"name": name, "label": label, "pct": dn_pct,
            "price": dn_price, "start_time": dn_max_t, "end_time": dn_min_t,
            "is_up": False, "is_spot": is_spot})


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 14: Головна логіка
# Порядок: OKX SWAP → OKX SPOT → MEXC FUT → MEXC SPOT → GATE FUT → GATE SPOT
# ─────────────────────────────────────────────────────────────────────────────

def main():
    now_utc = datetime.now(timezone.utc)
    now_str = now_utc.strftime("%Y-%m-%d  UTC=%H:%M")
    print(f"=== OKX_PAMP_bot старт | {now_str} ===")

    state = load_state()
    print(f"Записів у state.json: {len(state)}")

    signals_b1 = []; signals_b2 = []; found_b1_keys = set()
    stats = {"passed_price": 0, "passed_growth": 0}
    t0 = time.time()

    # ── Допоміжна функція обробки одного ринку ──────────────────────────────
    def process_market(instruments, fetch_fn, label, name_fn,
                       key_prefix, is_spot):
        results = fetch_all_candles(instruments, fetch_fn)
        for ident, candles in results:
            name = name_fn(ident)
            state_key = f"{key_prefix}:{ident}"
            analyze_instrument(candles, state_key, state, label, name,
                               signals_b1, signals_b2, found_b1_keys,
                               stats, is_spot)

    # ── OKX SWAP (ф'ючерси) ──────────────────────────────────────────────────
    okx_swap = okx_get_instruments("SWAP")
    print(f"OKX SWAP: {len(okx_swap)}")
    process_market(okx_swap, okx_get_candles, LABEL_OKX,
                   lambda x: x.replace("-USDT-SWAP", ""),
                   "OKX_SW", is_spot=False)

    # ── OKX SPOT ─────────────────────────────────────────────────────────────
    okx_spot = okx_get_instruments("SPOT")
    print(f"OKX SPOT: {len(okx_spot)}")
    process_market(okx_spot, okx_get_candles, LABEL_OKX,
                   lambda x: x.replace("-USDT", ""),
                   "OKX_SP", is_spot=True)

    # ── MEXC Ф'ЮЧЕРСИ ────────────────────────────────────────────────────────
    mexc_fut = mexc_fut_get_instruments()
    process_market(mexc_fut, mexc_fut_get_candles, LABEL_MEXC,
                   lambda x: x.replace("_USDT", ""),
                   "MEX_FW", is_spot=False)

    # ── MEXC СПОТ ────────────────────────────────────────────────────────────
    mexc_spt = mexc_spot_get_instruments()
    process_market(mexc_spt, mexc_spot_get_candles, LABEL_MEXC,
                   lambda x: x.replace("USDT", ""),
                   "MEX_SP", is_spot=True)

    # ── Gate Ф'ЮЧЕРСИ ────────────────────────────────────────────────────────
    gate_fut = gate_fut_get_instruments()
    process_market(gate_fut, gate_fut_get_candles, LABEL_GATE,
                   lambda x: x.replace("_USDT", ""),
                   "GAT_FW", is_spot=False)

    # ── Gate СПОТ ────────────────────────────────────────────────────────────
    gate_spt = gate_spot_get_instruments()
    process_market(gate_spt, gate_spot_get_candles, LABEL_GATE,
                   lambda x: x.replace("_USDT", ""),
                   "GAT_SP", is_spot=True)

    print(f"Діагностика: пройшли ціну (<{MAX_PRICE_USDT}$): {stats['passed_price']} | "
          f"пройшли рух (>={GROWTH_THRESHOLD}%): {stats['passed_growth']}")

    # ── Формуємо сигнальні рядки ──────────────────────────────────────────────
    signal_lines = []
    if signals_b1:
        signals_b1.sort(key=lambda x: x["growth_pct"], reverse=True)
        for s in signals_b1:
            line = fmt_b1(s["name"], s["label"], s["growth_pct"], s["max_price"],
                          s["min_time"], s["max_time"],
                          s["tail_count"], s["signal_is_last"], s["is_spot"])
            signal_lines.append(line)
            print(f"  >> [B1] {line}")
    if signals_b1 and signals_b2:
        signal_lines.append("")
    if signals_b2:
        signals_b2.sort(key=lambda x: x["pct"], reverse=True)
        for s in signals_b2:
            line = fmt_b2(s["name"], s["label"], s["pct"], s["price"],
                          s["start_time"], s["end_time"],
                          s["is_up"], s["is_spot"])
            signal_lines.append(line)
            print(f"  >> [B2] {line}")

    # ── Логіка черги ──────────────────────────────────────────────────────────
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

    print(f"=== Завершено за {time.time()-t0:.1f}с ===")

if __name__ == "__main__":
    main()
