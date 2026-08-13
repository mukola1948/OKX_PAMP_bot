# =============================================================================
# scanner.py  |  OKX_PAMP_bot  |  OKX + MEXC + Gate.io  |  Спот + Ф'ючерси
#
# УНІФІКОВАНИЙ ФОРМАТ СВІЧКИ: [ts_мс, open, high[2], low[3], close[4], vol[5], vol_usdt[6]]
#   [6] = об'єм у USDT — для фільтру мінімального обороту
#
# ФІЛЬТРИ (по порядку у analyze_instrument):
#   1. Мінімум 4 свічки
#   2. Ціна > 0 і < 5 USDT
#   3. Об'єм останніх 3 свічок != 0 (не мертва пара)
#   4. Сума vol_usdt[6] >= 150,000 USDT за 12 год
#   5. Ріст або падіння >= 50%
#   6. Для блоку 1: аномальний об'єм >= 10х
#
# ЗМІНИ 10.08.2026 (узгоджено з ViTar, відповіді №1-15):
#   1. Повідомлення в Telegram тепер ГРУПУЮТЬСЯ по біржах і типу ринку в
#      порядку: OKX ф'ючерси → OKX спот → MEXC ф'ючерси → MEXC спот →
#      Gate спот → Gate ф'ючерси (функція market_priority()).
#   2. Доданий Gate-тригер: якщо символ Блоку 2 (рух ціни ≥50% UP) є на
#      ф'ючерсах Gate.io — сканер надсилає подію repository_dispatch у
#      приватний репозиторій OKX_PA3OM_3_bot, де "четвертий суб-бот"
#      (Gate-бот) відкриває позицію. Тригер НЕ надсилається, якщо:
#        а) серед 48 аналізованих свічок є хоч ОДНА свічка з діапазоном
#           (найвища-найнижча)/найнижча ≥37% (has_sharp_candle());
#        б) по цьому символу зараз діє "охолодження" — з моменту
#           попереднього тригера ціна ще не зросла на +60% і не впала
#           на -30% (gate_cooldown_check()/gate_cooldown_set()).
#      Охолодження й фільтр різких свічок впливають ЛИШЕ на Gate-тригер,
#      на сам Telegram-сигнал Блоку 2 вони не впливають (він надсилається
#      як і раніше).
# =============================================================================

import requests, json, os, time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ── Gate-тригер: доступ до приватного репозиторію OKX_PA3OM_3_bot ─────────────
# GATE_DISPATCH_TOKEN — Personal Access Token з правом "repo" саме на
# приватний репозиторій OKX_PA3OM_3_bot (зберігається як GitHub Secret
# у ЦЬОМУ, публічному репозиторії OKX_PAMP_bot).
# GATE_TARGET_OWNER — Ваш логін на GitHub (власник обох репозиторіїв).
GATE_DISPATCH_TOKEN = os.environ.get("GATE_DISPATCH_TOKEN", "")
GATE_TARGET_OWNER   = os.environ.get("GATE_TARGET_OWNER", "")
GATE_TARGET_REPO    = os.environ.get("GATE_TARGET_REPO", "OKX_PA3OM_3_bot")
GATE_EVENT_TYPE      = "gate_signal"

OKX_BASE_URL  = "https://www.okx.com"
MEXC_BASE_URL = "https://api.mexc.com"
GATE_BASE_URL = "https://api.gateio.ws"

STATE_FILE        = "state.json"
CANDLES_COUNT     = 48
MAX_PRICE_USDT    = 5.0
GROWTH_THRESHOLD  = 50.0
VOLUME_SPIKE_X    = 10.0
VOLUME_TAIL_X     = 5.0
HALF_CANDLES      = CANDLES_COUNT // 2
MAX_WORKERS       = 10
RETRY_DELAY       = 2.0
MIN_VOL_USDT_12H  = 150_000.0   # мінімальний USDT-оборот за 12 год

# Поріг "різкої" свічки для виключення Gate-тригера (діапазон
# high/low відносно low, у %). Узгоджено 10.08.2026 — досить ОДНІЄЇ
# такої свічки серед 48-ми, щоб Gate-тригер НЕ надсилався.
SHARP_CANDLE_RANGE_PCT = 37.0

# Пороги зняття охолодження Gate-тригера по символу (у %, від ціни
# тригера). Знімається тим порогом, який настане РАНІШЕ.
GATE_COOLDOWN_UP_PCT   = 60.0
GATE_COOLDOWN_DOWN_PCT = 30.0

LABEL_OKX   = "OKX"
LABEL_MEXC  = "MEXC"
LABEL_GATE  = "GATE"
SPOT_PREFIX = "Спот."

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

# ── OKX ───────────────────────────────────────────────────────────────────────

def okx_get_instruments(inst_type):
    try:
        resp = requests.get(f"{OKX_BASE_URL}/api/v5/public/instruments",
                            params={"instType": inst_type}, timeout=15)
        data = resp.json()
        if data.get("code") != "0":
            print(f"OKX {inst_type} помилка: {data.get('msg')}")
            return []
        now_ms = int(time.time() * 1000)
        result = []
        for i in data.get("data", []):
            inst_id = i.get("instId", "")
            if i.get("state") != "live":
                continue
            exp = i.get("expTime", "")
            if exp:
                try:
                    if (int(exp) - now_ms) / (1000 * 86400) < 7:
                        continue
                except (ValueError, TypeError):
                    pass
            if inst_type == "SWAP" and inst_id.endswith("-USDT-SWAP"):
                result.append(inst_id)
            elif inst_type == "SPOT" and inst_id.endswith("-USDT"):
                result.append(inst_id)
        return result
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"Виняток okx_get_instruments: {e}")
        return []

def okx_get_candles(inst_id):
    """
    OKX: [ts, open, high, low, close, vol, volCcy, volCcyQuote(USDT), confirm]
    Уніфікована: [ts, open, high[2], low[3], close[4], vol[5], vol_usdt[6]]
    vol_usdt[6] = volCcyQuote (індекс 7 у сирій свічці) = USDT об'єм
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
            raw = data["data"]
            raw.reverse()
            candles = []
            for c in raw:
                try:
                    candles.append([
                        c[0], c[1], c[2], c[3], c[4], c[5],
                        c[7] if len(c) > 7 else "0",  # volCcyQuote = USDT
                    ])
                except (IndexError, TypeError):
                    continue
            return candles
        except (requests.RequestException, ValueError, KeyError):
            return []
    return []

# ── MEXC Ф'ЮЧЕРСИ ─────────────────────────────────────────────────────────────

def mexc_fut_get_instruments():
    try:
        resp = requests.get(f"{MEXC_BASE_URL}/api/v1/contract/detail", timeout=15)
        if resp.status_code != 200:
            print(f"MEXC futures HTTP {resp.status_code}")
            return []
        data = resp.json()
        if not data.get("success"):
            return []
        now_s = int(time.time())
        result = []
        for item in data.get("data", []):
            if not (item.get("state") == 0 and item.get("futureType") == 1
                    and item.get("quoteCoin") == "USDT"):
                continue
            delivery = item.get("deliveryTime") or item.get("settleTime") or 0
            if delivery:
                try:
                    if (int(delivery) - now_s) / 86400 < 7:
                        continue
                except (ValueError, TypeError):
                    pass
            result.append(item["symbol"])
        print(f"MEXC ф'ючерси: {len(result)}")
        return result
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"Виняток mexc_fut_get_instruments: {e}")
        return []

def mexc_fut_get_candles(symbol):
    """
    MEXC ф'ючерси: масиви time, open, high, low, close, vol, amount(USDT)
    vol_usdt[6] = amount = USDT об'єм
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
            times   = d.get("time",   [])
            opens   = d.get("open",   [])
            highs   = d.get("high",   [])
            lows    = d.get("low",    [])
            closes  = d.get("close",  [])
            vols    = d.get("vol",    [])
            amounts = d.get("amount", [])
            if not times:
                return []
            candles = []
            for i in range(len(times)):
                try:
                    candles.append([
                        int(times[i]) * 1000,
                        str(opens[i]   if i < len(opens)   else 0),
                        str(highs[i]   if i < len(highs)   else 0),
                        str(lows[i]    if i < len(lows)    else 0),
                        str(closes[i]  if i < len(closes)  else 0),
                        str(vols[i]    if i < len(vols)    else 0),
                        str(amounts[i] if i < len(amounts) else 0),  # USDT [6]
                    ])
                except (IndexError, TypeError, ValueError):
                    continue
            return candles
        except (requests.RequestException, ValueError, KeyError):
            return []
    return []

# ── MEXC СПОТ ─────────────────────────────────────────────────────────────────

def mexc_spot_get_instruments():
    try:
        resp = requests.get(f"{MEXC_BASE_URL}/api/v3/exchangeInfo", timeout=20)
        if resp.status_code != 200:
            print(f"MEXC spot HTTP {resp.status_code}")
            return []
        data = resp.json()
        result = [
            s["symbol"] for s in data.get("symbols", [])
            if (s.get("status") == "ENABLED" and s.get("quoteAsset") == "USDT"
                and s.get("isSpotTradingAllowed", False))
        ]
        print(f"MEXC спот: {len(result)}")
        return result
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"Виняток mexc_spot_get_instruments: {e}")
        return []

def mexc_spot_get_candles(symbol):
    """
    MEXC спот Binance-формат: [ts,o,h,l,c,vol_base,close_ts,quote_vol_USDT,...]
    vol_usdt[6] = елемент [7] = quote asset volume (USDT)
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
            candles = []
            for c in data:
                try:
                    candles.append([
                        c[0], c[1], c[2], c[3], c[4], c[5],
                        str(c[7]) if len(c) > 7 else "0",  # quote vol USDT [6]
                    ])
                except (IndexError, TypeError):
                    continue
            return candles
        except (requests.RequestException, ValueError):
            return []
    return []

# ── Gate Ф'ЮЧЕРСИ ─────────────────────────────────────────────────────────────

def gate_fut_get_instruments():
    try:
        result = []
        offset = 0
        while True:
            resp = requests.get(
                f"{GATE_BASE_URL}/api/v4/futures/usdt/contracts",
                params={"limit": 100, "offset": offset}, timeout=15)
            if resp.status_code != 200:
                print(f"Gate futures HTTP {resp.status_code}")
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
    Gate ф'ючерси: {"t","o","h","l","c","v"(контракти)}
    vol_usdt[6] = v × c (контракти × ціна закриття ≈ USDT об'єм)
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
                    vol   = float(item.get("v", 0) or 0)
                    close = float(item.get("c", 0) or 0)
                    candles.append([
                        int(item["t"]) * 1000,
                        str(item.get("o", 0)), str(item.get("h", 0)),
                        str(item.get("l", 0)), str(item.get("c", 0)),
                        str(vol),
                        str(vol * close),   # USDT об'єм [6]
                    ])
                except (KeyError, TypeError, ValueError):
                    continue
            return candles
        except (requests.RequestException, ValueError):
            return []
    return []

# ── Gate СПОТ ─────────────────────────────────────────────────────────────────

def gate_spot_get_instruments():
    try:
        resp = requests.get(
            f"{GATE_BASE_URL}/api/v4/spot/currency_pairs", timeout=15)
        if resp.status_code != 200:
            print(f"Gate spot HTTP {resp.status_code}")
            return []
        data = resp.json()
        if not isinstance(data, list):
            return []
        result = [
            item["id"] for item in data
            if (item.get("trade_status") == "tradable"
                and item.get("quote") == "USDT"
                and not item.get("buy_disabled", False)
                and not item.get("sell_disabled", False))
        ]
        print(f"Gate спот: {len(result)}")
        return result
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"Виняток gate_spot_get_instruments: {e}")
        return []

def gate_spot_get_candles(currency_pair):
    """
    Gate спот API повертає свічки у двох форматах залежно від пари:
      Формат А (dict): {"t":ts_sec,"o":open,"h":high,"l":low,"c":close,"v":vol,"sum":usdt_vol}
      Формат Б (list): [timestamp, quote_volume(USDT), close, high, low, open, base_volume]
                       (порядок ВИПРАВЛЕНО 13.08.2026 — див. коментар нижче)
    Обробляємо обидва формати. vol_usdt[6] = USDT об'єм.
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
                    if isinstance(item, dict):
                        # Формат А: словник {"t","o","h","l","c","v","sum"}
                        ts       = int(item["t"]) * 1000
                        o        = str(item.get("o", 0))
                        h        = str(item.get("h", 0))
                        l        = str(item.get("l", 0))
                        c        = str(item.get("c", 0))
                        vol      = float(item.get("v", 0) or 0)
                        vol_usdt = float(item.get("sum", 0) or 0)
                        if vol_usdt == 0:
                            vol_usdt = vol * float(item.get("c", 0) or 0)
                    elif isinstance(item, list) and len(item) >= 6:
                        # Формат Б (масив) — РЕАЛЬНИЙ порядок полів Gate.io:
                        # [timestamp, quote_volume(USDT), close, high, low,
                        #  open, base_volume]. ВИПРАВЛЕНО 13.08.2026 — раніше
                        # тут помилково стояло [ts,o,h,l,c,vol,sum], через що
                        # vol_usdt бралось з base_volume (кількість монет),
                        # а не з реального USDT-обороту — фільтр мінімального
                        # обороту 150k через це міг пропускати неліквідні пари.
                        ts       = int(item[0]) * 1000
                        vol_usdt = float(item[1] or 0)
                        c        = str(item[2])
                        h        = str(item[3])
                        l        = str(item[4])
                        o        = str(item[5])
                        vol      = float(item[6]) if len(item) > 6 else 0.0
                    else:
                        continue
                    candles.append([ts, o, h, l, c, str(vol), str(vol_usdt)])
                except (KeyError, TypeError, ValueError, IndexError):
                    continue
            return candles
        except (requests.RequestException, ValueError):
            return []
    return []

# ── Допоміжні функції ─────────────────────────────────────────────────────────

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

# ── Групування повідомлень по біржах/типу ринку (додано 10.08.2026) ───────────

def market_priority(label, is_spot):
    """
    Порядок блоків у Telegram-повідомленні (узгоджено 10.08.2026):
    OKX ф'ючерси(0) → OKX спот(1) → MEXC ф'ючерси(2) → MEXC спот(3) →
    Gate спот(4) → Gate ф'ючерси(5) — саме для Gate порядок ф'ючерси/спот
    навмисно ПЕРЕВЕРНУТО відносно інших бірж, за прямою вказівкою ViTar.
    """
    if label == LABEL_OKX:  return 0 if not is_spot else 1
    if label == LABEL_MEXC: return 2 if not is_spot else 3
    if label == LABEL_GATE: return 4 if is_spot else 5
    return 9

# ── Gate-тригер: різкі свічки й охолодження (додано 10.08.2026) ───────────────

def has_sharp_candle(candles):
    """
    True якщо серед свічок є хоч ОДНА з діапазоном
    (найвища-найнижча ціна свічки)/найнижча ціна свічки >= SHARP_CANDLE_RANGE_PCT.
    Наявність такої свічки ВИКЛЮЧАЄ символ з Gate-тригера (не з Telegram-сигналу).
    """
    for c in candles:
        try:
            high = float(c[2] or 0); low = float(c[3] or 0)
            if low > 0 and (high - low) / low * 100 >= SHARP_CANDLE_RANGE_PCT:
                return True
        except (ValueError, TypeError, IndexError):
            continue
    return False

def gate_cooldown_check(state, base_symbol):
    """True якщо по base_symbol зараз діє охолодження Gate-тригера."""
    cooldown = state.get("gate_cooldown", {})
    return base_symbol in cooldown

def gate_cooldown_update_or_clear(state, base_symbol, current_price):
    """
    Якщо по символу є активне охолодження — перевіряє чи не настав час
    його зняти (ціна зросла на +60% чи впала на -30% від ціни тригера,
    що настане раніше). Знімає охолодження (видаляє запис), якщо так.
    Викликається для КОЖНОГО символу під охолодженням на кожному рані,
    незалежно від того спрацював зараз Блок 1/2 чи ні.
    """
    cooldown = state.setdefault("gate_cooldown", {})
    entry = cooldown.get(base_symbol)
    if not entry:
        return
    trigger_price = entry.get("price", 0.0)
    if trigger_price <= 0 or current_price <= 0:
        return
    change_pct = (current_price - trigger_price) / trigger_price * 100
    if change_pct >= GATE_COOLDOWN_UP_PCT or change_pct <= -GATE_COOLDOWN_DOWN_PCT:
        cooldown.pop(base_symbol, None)
        print(f"  [GATE cooldown] {base_symbol}: знято ({change_pct:+.1f}%)")

def gate_cooldown_set(state, base_symbol, trigger_price):
    cooldown = state.setdefault("gate_cooldown", {})
    cooldown[base_symbol] = {
        "price": trigger_price,
        "ts":    int(time.time() * 1000),
    }

def send_gate_dispatch(base_symbol, trigger_price):
    """
    Надсилає подію repository_dispatch у приватний репозиторій
    OKX_PA3OM_3_bot (owner/repo з GATE_TARGET_OWNER/GATE_TARGET_REPO),
    яка активує там Gate-суб-бота через окремий workflow
    (run_gate_entry.yml, on: repository_dispatch).
    """
    if not GATE_DISPATCH_TOKEN or not GATE_TARGET_OWNER:
        print("  [GATE dispatch] не налаштовано GATE_DISPATCH_TOKEN/GATE_TARGET_OWNER — пропуск")
        return
    url = f"https://api.github.com/repos/{GATE_TARGET_OWNER}/{GATE_TARGET_REPO}/dispatches"
    headers = {
        "Authorization": f"Bearer {GATE_DISPATCH_TOKEN}",
        "Accept":        "application/vnd.github+json",
    }
    payload = {
        "event_type": GATE_EVENT_TYPE,
        "client_payload": {
            "symbol":        base_symbol,
            "gate_contract": f"{base_symbol}_USDT",
            "price":         trigger_price,
        },
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        if resp.status_code == 204:
            print(f"  [GATE dispatch] надіслано: {base_symbol} @ {trigger_price}")
        else:
            print(f"  [GATE dispatch] помилка {resp.status_code}: {resp.text}")
    except (requests.RequestException, OSError) as e:
        print(f"  [GATE dispatch] виняток: {e}")

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

# ── Форматування ──────────────────────────────────────────────────────────────

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

# ── Telegram ──────────────────────────────────────────────────────────────────

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

# ── Паралельне завантаження ───────────────────────────────────────────────────

def fetch_all_candles(instruments, fetch_fn):
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

# ── Аналіз одного інструменту ─────────────────────────────────────────────────

def analyze_instrument(candles, state_key, state, label, name,
                       signals_b1, signals_b2, found_b1_keys, stats, is_spot,
                       gate_fut_set):
    # Фільтр 1: мінімум свічок
    if len(candles) < 4: return

    # Фільтр 2: ціна 0 < price < 5 USDT
    try:
        price = float(candles[-1][4] or 0)
    except (ValueError, TypeError, IndexError):
        return
    if price <= 0 or price >= MAX_PRICE_USDT: return

    # Фільтр 3: мертва пара (нульові об'єми останніх 3 свічок)
    try:
        if all(float(candles[-(i+1)][5] or 0) == 0 for i in range(3)):
            return
    except (IndexError, ValueError, TypeError):
        pass

    # Фільтр 4: мінімальний USDT-об'єм за 12 год >= 150,000 USDT
    # vol_usdt знаходиться у індексі [6] уніфікованої свічки
    try:
        total_vol_usdt = sum(float(c[6] or 0) for c in candles if len(c) > 6)
        if total_vol_usdt < MIN_VOL_USDT_12H:
            return
    except (ValueError, TypeError):
        return

    stats["passed_price"] += 1

    # Охолодження Gate-тригера перевіряємо/знімаємо для КОЖНОГО символу,
    # що дійшов до сюди (незалежно від того чи буде зараз новий сигнал) —
    # щоб +60%/-30% відслідковувались щорану, а не лише в момент сигналу.
    if name in state.get("gate_cooldown", {}):
        gate_cooldown_update_or_clear(state, name, price)

    up_pct, up_price, up_min_t, up_max_t = analyze_price_up(candles)
    dn_pct, dn_price, dn_max_t, dn_min_t = analyze_price_down(candles)

    if up_pct >= GROWTH_THRESHOLD or dn_pct >= GROWTH_THRESHOLD:
        stats["passed_growth"] += 1

    # ── Блок 1: памп з аномальним об'ємом ──
    if up_pct >= GROWTH_THRESHOLD:
        saved_avg = state.get(state_key)
        sig_found, sig_idx, tail, final_avg = analyze_volumes(candles, saved_avg)
        state[state_key] = final_avg
        if sig_found:
            is_last = (sig_idx == len(candles) - 1)
            prefix = SPOT_PREFIX if is_spot else ""
            print(f"  [B1/{label}] {prefix}{name}: +{up_pct:.1f}% | "
                  f"{up_min_t}-{up_max_t} | хвіст={tail}св | "
                  f"vol={total_vol_usdt:,.0f}$")
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

    # ── Блок 2: рух без перевірки аномальних об'ємів ──
    if state_key in found_b1_keys: return
    best_up = up_pct >= GROWTH_THRESHOLD
    best_dn = dn_pct >= GROWTH_THRESHOLD
    if not best_up and not best_dn: return

    prefix = SPOT_PREFIX if is_spot else ""
    if best_up:
        print(f"  [B2+/{label}] {prefix}{name}: UP {up_pct:.1f}% | "
              f"vol={total_vol_usdt:,.0f}$")
        signals_b2.append({"name": name, "label": label, "pct": up_pct,
            "price": up_price, "start_time": up_min_t, "end_time": up_max_t,
            "is_up": True, "is_spot": is_spot})

        # ── Gate-тригер (лише Блок 2, лише UP, додано 10.08.2026) ──
        # Кандидат лише якщо: символ є на ф'ючерсах Gate.io, немає жодної
        # різкої свічки (>=37%) серед 48-ми, і по символу зараз НЕ діє
        # охолодження.
        if name in gate_fut_set and not has_sharp_candle(candles) \
                and not gate_cooldown_check(state, name):
            gate_cooldown_set(state, name, price)
            send_gate_dispatch(name, price)
    if best_dn:
        print(f"  [B2-/{label}] {prefix}{name}: DN {dn_pct:.1f}% | "
              f"vol={total_vol_usdt:,.0f}$")
        signals_b2.append({"name": name, "label": label, "pct": dn_pct,
            "price": dn_price, "start_time": dn_max_t, "end_time": dn_min_t,
            "is_up": False, "is_spot": is_spot})

# ── Головна логіка ────────────────────────────────────────────────────────────

def main():
    now_utc = datetime.now(timezone.utc)
    now_str = now_utc.strftime("%Y-%m-%d  UTC=%H:%M")
    print(f"=== OKX_PAMP_bot старт | {now_str} ===")

    state = load_state()
    print(f"Записів у state.json: {len(state)}")

    signals_b1 = []; signals_b2 = []; found_b1_keys = set()
    stats = {"passed_price": 0, "passed_growth": 0}
    t0 = time.time()

    # Список ф'ючерсних інструментів Gate.io завантажуємо ПЕРШИМ, ще до
    # обробки решти бірж — потрібен для перевірки кандидатів на Gate-тригер
    # (додано 10.08.2026, раніше завантажувався лише при обробці самого
    # блоку Gate ф'ючерсів, тепер потрібен для ВСІХ бірж одразу).
    gate_fut_raw = gate_fut_get_instruments()
    gate_fut_set = {x.replace("_USDT", "") for x in gate_fut_raw}

    def process_market(instruments, fetch_fn, label, name_fn, key_prefix, is_spot):
        results = fetch_all_candles(instruments, fetch_fn)
        for ident, candles in results:
            analyze_instrument(candles, f"{key_prefix}:{ident}", state,
                               label, name_fn(ident),
                               signals_b1, signals_b2, found_b1_keys, stats,
                               is_spot, gate_fut_set)

    okx_swap = okx_get_instruments("SWAP")
    print(f"OKX SWAP: {len(okx_swap)}")
    process_market(okx_swap, okx_get_candles, LABEL_OKX,
                   lambda x: x.replace("-USDT-SWAP", ""), "OKX_SW", is_spot=False)

    okx_spot = okx_get_instruments("SPOT")
    print(f"OKX SPOT: {len(okx_spot)}")
    process_market(okx_spot, okx_get_candles, LABEL_OKX,
                   lambda x: x.replace("-USDT", ""), "OKX_SP", is_spot=True)

    mexc_fut = mexc_fut_get_instruments()
    process_market(mexc_fut, mexc_fut_get_candles, LABEL_MEXC,
                   lambda x: x.replace("_USDT", ""), "MEX_FW", is_spot=False)

    mexc_spt = mexc_spot_get_instruments()
    process_market(mexc_spt, mexc_spot_get_candles, LABEL_MEXC,
                   lambda x: x.replace("USDT", ""), "MEX_SP", is_spot=True)

    process_market(gate_fut_raw, gate_fut_get_candles, LABEL_GATE,
                   lambda x: x.replace("_USDT", ""), "GAT_FW", is_spot=False)

    gate_spt = gate_spot_get_instruments()
    process_market(gate_spt, gate_spot_get_candles, LABEL_GATE,
                   lambda x: x.replace("_USDT", ""), "GAT_SP", is_spot=True)

    print(f"Діагностика: пройшли ціну+об'єм({MIN_VOL_USDT_12H/1000:.0f}k$): "
          f"{stats['passed_price']} | пройшли рух: {stats['passed_growth']}")

    signal_lines = []
    if signals_b1:
        # Групування по біржах/типу ринку (додано 10.08.2026), у межах
        # групи — за спаданням сили сигналу, як і раніше.
        signals_b1.sort(key=lambda x: (market_priority(x["label"], x["is_spot"]),
                                        -x["growth_pct"]))
        for s in signals_b1:
            signal_lines.append(fmt_b1(
                s["name"], s["label"], s["growth_pct"], s["max_price"],
                s["min_time"], s["max_time"],
                s["tail_count"], s["signal_is_last"], s["is_spot"]))
    if signals_b1 and signals_b2:
        signal_lines.append("")
    if signals_b2:
        signals_b2.sort(key=lambda x: (market_priority(x["label"], x["is_spot"]),
                                        -x["pct"]))
        for s in signals_b2:
            signal_lines.append(fmt_b2(
                s["name"], s["label"], s["pct"], s["price"],
                s["start_time"], s["end_time"],
                s["is_up"], s["is_spot"]))

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
        print(f"Надіслано: {len(pending)} черги + {len(signal_lines)} сигналів")

    print(f"=== Завершено за {time.time()-t0:.1f}с ===")

if __name__ == "__main__":
    main()