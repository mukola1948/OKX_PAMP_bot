# =============================================================================
# scanner.py
# OKX_PAMP_bot — сканер пампів на OKX і Binance Futures (USDT-SWAP / USDT-M)
#
# БЛОК 1 (памп з об'ємами):
#   1. Поточна ціна (close останньої свічки) < 5 USDT
#   2. Ріст ціни min→max >= 50% за 8 годин (32 свічки × 15хв)
#      алгоритм running_min: мінімум ЗАВЖДИ раніше максимуму
#   3. Аномальний об'єм >= 10х від ковзного середнього
#   Формат: LAB+63.2%;ОКХ;max7.7735(17:00-18:45);V+10х(6св)
#        або LAB+63.2%;БІН;max7.7735(17:00-18:45);V+10х(6св)
#
# БЛОК 2 (рух без перевірки об'ємів):
#   1. Поточна ціна < 5 USDT
#   2. Ріст min→max >= 50% АБО падіння max→min >= 50%
#      алгоритм running_min/running_max: екстремуми впорядковані хронологічно
#   3. Монети що вже є в блоці 1 — не дублюються
#   4. Якщо обидві умови (ріст І падіння) — надсилаємо обидва рядки
#   Формат ріст:    LAB+53.7%;ОКХ;max0.16021;01:15-05:45
#   Формат падіння: LAB-53.7%;БІН;min0.16021;01:15-05:45
#
# Збереження: state.json зберігає середні об'єми з префіксом біржі:
#   "OKX:LAYER-USDT-SWAP": 12345.6
#   "BIN:LAYERUSDT":        12345.6
# =============================================================================

import requests
import json
import os
import time
from datetime import datetime, timezone

# ─────────────────────────────────────────────────────────────────────────────
# БЛОК НАЛАШТУВАНЬ
# ─────────────────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

OKX_BASE_URL = "https://www.okx.com"
BIN_BASE_URL = "https://fapi.binance.com"      # Binance USD-M Futures публічний API

STATE_FILE       = "state.json"

CANDLE_BAR_OKX   = "15m"                       # таймфрейм для OKX
CANDLE_BAR_BIN   = "15m"                       # таймфрейм для Binance
CANDLES_COUNT    = 32                           # 32 свічки × 15хв = 8 годин
MAX_PRICE_USDT   = 5.0                          # максимально допустима ціна монети
GROWTH_THRESHOLD = 50.0                         # мінімальний ріст/падіння у відсотках
VOLUME_SPIKE_X   = 10.0                         # кратність аномального об'єму
VOLUME_TAIL_X    = 5.0                          # кратність хвостових свічок
HALF_CANDLES     = CANDLES_COUNT // 2           # половина від кількості свічок = 16
REQUEST_DELAY    = 0.12                         # затримка між HTTP-запитами (rate limit)

# Мітки бірж у повідомленнях
LABEL_OKX = "ОКХ"
LABEL_BIN = "БІН"


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 1: ЗБЕРЕЖЕННЯ ТА ЗАВАНТАЖЕННЯ СТАНУ МІЖ ЗАПУСКАМИ
# state.json: { "OKX:LAYER-USDT-SWAP": 12345.6, "BIN:LAYERUSDT": 12345.6, ... }
# ─────────────────────────────────────────────────────────────────────────────

def load_state():
    """Завантажує збережені середні об'єми з попереднього запуску"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            print(f"Помилка читання state.json: {e}")
            return {}
    return {}


def save_state(state):
    """Зберігає оновлені середні об'єми для наступного запуску"""
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except (OSError, TypeError, ValueError) as e:
        print(f"Помилка збереження state.json: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 2: OKX — ОТРИМАННЯ СПИСКУ ІНСТРУМЕНТІВ І СВІЧОК
# ─────────────────────────────────────────────────────────────────────────────

def okx_get_instruments():
    """Повертає список instId всіх активних USDT-SWAP інструментів на OKX"""
    url    = f"{OKX_BASE_URL}/api/v5/public/instruments"
    params = {"instType": "SWAP"}
    try:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
        if data.get("code") != "0":
            print(f"OKX instruments помилка: {data.get('msg')}")
            return []
        return [
            item["instId"] for item in data.get("data", [])
            if item.get("instId", "").endswith("-USDT-SWAP")
        ]
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"Виняток okx_get_instruments: {e}")
        return []


def okx_get_candles(inst_id):
    """
    Повертає 32 свічки 15м для OKX від найстарішої [0] до найновішої [31].
    Формат свічки: [ts_мс, open, high, low, close, vol, ...]
    """
    url    = f"{OKX_BASE_URL}/api/v5/market/candles"
    params = {"instId": inst_id, "bar": CANDLE_BAR_OKX, "limit": str(CANDLES_COUNT)}
    try:
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        if data.get("code") != "0" or not data.get("data"):
            return []
        candles = data["data"]
        candles.reverse()   # OKX повертає від нових до старих — розвертаємо
        return candles
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"Виняток okx_get_candles {inst_id}: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 3: BINANCE — ОТРИМАННЯ СПИСКУ ІНСТРУМЕНТІВ І СВІЧОК
# API: https://fapi.binance.com (USD-M Futures, публічний без ключів)
# Символ Binance: LAYER-USDT-SWAP → LAYERUSDT (прибираємо дефіси і -SWAP)
# ─────────────────────────────────────────────────────────────────────────────

def bin_get_instruments():
    """Повертає список символів всіх активних USDT-M Futures на Binance"""
    url = f"{BIN_BASE_URL}/fapi/v1/exchangeInfo"
    try:
        resp = requests.get(url, timeout=15)
        data = resp.json()
        result = []
        for sym in data.get("symbols", []):
            # Беремо лише активні лінійні безстрокові контракти з USDT
            if (sym.get("status") == "TRADING"
                    and sym.get("contractType") == "PERPETUAL"
                    and sym.get("quoteAsset") == "USDT"):
                result.append(sym["symbol"])   # наприклад "LAYERUSDT"
        return result
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"Виняток bin_get_instruments: {e}")
        return []


def bin_get_candles(symbol):
    """
    Повертає 32 свічки 15м для Binance від найстарішої [0] до найновішої [31].
    Binance вже повертає від старих до нових — розвертати не потрібно.
    Формат свічки: [ts_мс, open, high, low, close, vol, ...]
    Індекси збігаються з OKX: [0]=ts, [2]=high, [3]=low, [4]=close, [5]=vol
    """
    url    = f"{BIN_BASE_URL}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": CANDLE_BAR_BIN, "limit": CANDLES_COUNT}
    try:
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        if not isinstance(data, list) or len(data) == 0:
            return []
        return data   # вже від старих до нових
    except (requests.RequestException, ValueError) as e:
        print(f"Виняток bin_get_candles {symbol}: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 4: ДОПОМІЖНА ФУНКЦІЯ — TIMESTAMP В РЯДОК UTC HH:MM
# ─────────────────────────────────────────────────────────────────────────────

def ts_to_utc(ts_ms):
    """Перетворює timestamp у мілісекундах на рядок UTC HH:MM"""
    try:
        return datetime.fromtimestamp(
            int(ts_ms) / 1000, tz=timezone.utc
        ).strftime("%H:%M")
    except (ValueError, TypeError, OSError):
        return "--:--"


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 5: АЛГОРИТМ КОВЗНОГО СЕРЕДНЬОГО ОБ'ЄМІВ З ВИКЛЮЧЕННЯМ АНОМАЛІЙ
#
# Стартове середнє = збережене з попереднього запуску АБО об'єм свічки [0]
# Рух від [0] до [31] зліва направо:
#   vol < avg × 10  → нормальна: включаємо в базу, оновлюємо avg
#   vol >= avg × 10 → СИГНАЛ аномального об'єму
# Після сигналу — аналіз хвоста:
#   vol >= avg × 5  → хвостова: рахуємо, не включаємо поки їх < HALF_CANDLES
#   хвостових >= HALF_CANDLES → включаємо в базу, шукаємо нову аномалію
#   vol < avg × 5  → нормальна: включаємо в базу
# ─────────────────────────────────────────────────────────────────────────────

def analyze_volumes(candles, saved_avg):
    """
    Аналізує об'єми за алгоритмом ковзного середнього з виключенням аномалій.

    Повертає:
        signal_found      (bool)  — знайдено аномальний об'єм >= 10х
        signal_candle_idx (int)   — індекс сигнальної свічки (-1 якщо немає)
        tail_count        (int)   — кількість свічок з підвищеним об'ємом
        final_avg         (float) — підсумкове середнє для збереження
    """
    if not candles:
        return False, -1, 0, (saved_avg if saved_avg else 0.0)

    volumes_in_base = []

    if saved_avg is not None and saved_avg > 0:
        current_avg = saved_avg
        start_idx   = 0
    else:
        first_vol   = float(candles[0][5] or 0)
        current_avg = first_vol if first_vol > 0 else 1.0
        volumes_in_base.append(first_vol)
        start_idx   = 1

    signal_found      = False
    signal_candle_idx = -1
    tail_count        = 0
    tail_indices      = []

    i = start_idx
    while i < len(candles):
        vol = float(candles[i][5] or 0)

        if not signal_found:
            if current_avg > 0 and vol >= current_avg * VOLUME_SPIKE_X:
                signal_found      = True
                signal_candle_idx = i
                tail_count        = 1
                tail_indices      = [i]
            else:
                volumes_in_base.append(vol)
                current_avg = sum(volumes_in_base) / len(volumes_in_base)
        else:
            if vol >= current_avg * VOLUME_TAIL_X:
                tail_indices.append(i)
                tail_count = len(tail_indices)
                if tail_count >= HALF_CANDLES:
                    for ti in tail_indices:
                        volumes_in_base.append(float(candles[ti][5] or 0))
                    current_avg       = sum(volumes_in_base) / len(volumes_in_base)
                    signal_found      = False
                    signal_candle_idx = -1
                    tail_count        = 0
                    tail_indices      = []
            else:
                volumes_in_base.append(vol)
                current_avg = sum(volumes_in_base) / len(volumes_in_base)
        i += 1

    final_avg = (sum(volumes_in_base) / len(volumes_in_base)
                 if volumes_in_base else current_avg)
    return signal_found, signal_candle_idx, tail_count, final_avg


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 6: АНАЛІЗ ЦІНИ — РІСТ MIN→MAX ЗА 8 ГОДИН
# Алгоритм running_min одного проходу зліва направо:
#   — running_min оновлюється на кожній свічці
#   — для кожної свічки рахуємо ріст від running_min до high
#   — зберігаємо найкращий результат
# Гарантія: мінімум ЗАВЖДИ хронологічно раніше максимуму
# ─────────────────────────────────────────────────────────────────────────────

def analyze_price_up(candles):
    """
    Знаходить найкращий ріст де мінімум хронологічно раніше максимуму.

    Повертає:
        pct       (float) — найкращий відсоток росту min→max
        max_price (float) — ціна максимуму (high)
        min_time  (str)   — UTC HH:MM свічки з мінімумом (точка відліку)
        max_time  (str)   — UTC HH:MM свічки з максимумом
    """
    try:
        best_pct    = 0.0
        best_max    = 0.0
        best_min_ts = None
        best_max_ts = None

        running_min    = float("inf")
        running_min_ts = None

        for candle in candles:
            high = float(candle[2] or 0)
            low  = float(candle[3] or 0)
            ts   = int(candle[0])

            if 0 < low < running_min:
                running_min    = low
                running_min_ts = ts

            if (running_min > 0 and high > 0
                    and running_min_ts is not None
                    and ts > running_min_ts):
                pct = (high - running_min) / running_min * 100
                if pct > best_pct:
                    best_pct    = pct
                    best_max    = high
                    best_min_ts = running_min_ts
                    best_max_ts = ts

        if best_pct <= 0 or best_min_ts is None or best_max_ts is None:
            return 0.0, 0.0, "--:--", "--:--"
        return best_pct, best_max, ts_to_utc(best_min_ts), ts_to_utc(best_max_ts)

    except (ValueError, TypeError, IndexError, ZeroDivisionError) as e:
        print(f"Виняток analyze_price_up: {e}")
        return 0.0, 0.0, "--:--", "--:--"


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 7: АНАЛІЗ ЦІНИ — ПАДІННЯ MAX→MIN ЗА 8 ГОДИН
# Алгоритм running_max одного проходу зліва направо:
#   — running_max оновлюється на кожній свічці
#   — для кожної свічки рахуємо падіння від running_max до low
#   — зберігаємо найкращий результат
# Гарантія: максимум ЗАВЖДИ хронологічно раніше мінімуму
# ─────────────────────────────────────────────────────────────────────────────

def analyze_price_down(candles):
    """
    Знаходить найкраще падіння де максимум хронологічно раніше мінімуму.

    Повертає:
        pct       (float) — найкращий відсоток падіння max→min
        min_price (float) — ціна мінімуму (low)
        max_time  (str)   — UTC HH:MM свічки з максимумом (точка відліку)
        min_time  (str)   — UTC HH:MM свічки з мінімумом
    """
    try:
        best_pct    = 0.0
        best_min    = 0.0
        best_max_ts = None
        best_min_ts = None

        running_max    = 0.0
        running_max_ts = None

        for candle in candles:
            high = float(candle[2] or 0)
            low  = float(candle[3] or 0)
            ts   = int(candle[0])

            if high > running_max:
                running_max    = high
                running_max_ts = ts

            if (running_max > 0 and low > 0
                    and running_max_ts is not None
                    and ts > running_max_ts):
                pct = (running_max - low) / running_max * 100
                if pct > best_pct:
                    best_pct    = pct
                    best_min    = low
                    best_max_ts = running_max_ts
                    best_min_ts = ts

        if best_pct <= 0 or best_max_ts is None or best_min_ts is None:
            return 0.0, 0.0, "--:--", "--:--"
        return best_pct, best_min, ts_to_utc(best_max_ts), ts_to_utc(best_min_ts)

    except (ValueError, TypeError, IndexError, ZeroDivisionError) as e:
        print(f"Виняток analyze_price_down: {e}")
        return 0.0, 0.0, "--:--", "--:--"


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 8: ФОРМАТУВАННЯ ЦІНИ
# ─────────────────────────────────────────────────────────────────────────────

def fmt_price(price):
    """Форматує ціну залежно від її величини"""
    if price >= 1.0:
        return f"{price:.4f}"
    elif price >= 0.01:
        return f"{price:.5f}"
    else:
        return f"{price:.7f}"


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 9: ФОРМАТУВАННЯ РЯДКІВ СИГНАЛІВ
# Блок 1: LAB+63.2%;ОКХ;max7.7735(17:00-18:45);V+10х
#      або LAB+63.2%;ОКХ;max7.7735(17:00-18:45);V+10х(6св)
# Блок 2: LAB+53.7%;ОКХ;max0.16021;01:15-05:45
#         LAB-53.7%;БІН;min0.16021;01:15-05:45
# ─────────────────────────────────────────────────────────────────────────────

def format_line_block1(name, label, growth_pct, max_price, min_time, max_time,
                       tail_count, signal_is_last):
    """Формує рядок сигналу блоку 1 (памп з аномальним об'ємом)"""
    price_str = fmt_price(max_price)
    base = f"{name}+{growth_pct:.1f}%;{label};max{price_str}({min_time}-{max_time});V+10х"
    if signal_is_last:
        return base
    else:
        return f"{base}({tail_count}св)"


def format_line_block2(name, label, pct, price, start_time, end_time, is_up):
    """Формує рядок сигналу блоку 2 (рух ціни без перевірки об'ємів)"""
    price_str = fmt_price(price)
    if is_up:
        return f"{name}+{pct:.1f}%;{label};max{price_str};{start_time}-{end_time}"
    else:
        return f"{name}-{pct:.1f}%;{label};min{price_str};{start_time}-{end_time}"


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 10: НАДСИЛАННЯ ПОВІДОМЛЕННЯ У TELEGRAM (plain text)
# ─────────────────────────────────────────────────────────────────────────────

def send_telegram(text):
    """Надсилає текстове повідомлення у Telegram без форматування"""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"Telegram не налаштовано:\n{text}")
        return
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    try:
        resp = requests.post(url, data=data, timeout=15)
        if resp.status_code == 200:
            print("Telegram: надіслано успішно")
        else:
            print(f"Telegram помилка {resp.status_code}: {resp.text}")
    except (requests.RequestException, OSError) as e:
        print(f"Виняток send_telegram: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 11: УНІВЕРСАЛЬНИЙ АНАЛІЗ ОДНОГО ІНСТРУМЕНТУ
# Спільна логіка для OKX і Binance — один виклик для обох бірж
# ─────────────────────────────────────────────────────────────────────────────

def analyze_instrument(candles, state_key, state, label,
                       name, signals_b1, signals_b2, found_b1_keys):
    """
    Аналізує свічки одного інструменту і додає сигнали до списків.

    Аргументи:
        candles       — список 32 свічок від [0]=найстаріша до [31]=найновіша
        state_key     — ключ для state.json (наприклад "OKX:LAYER-USDT-SWAP")
        state         — словник збережених середніх об'ємів
        label         — мітка біржі у повідомленні ("ОКХ" або "БІН")
        name          — скорочена назва монети (наприклад "LAYER")
        signals_b1    — список сигналів блоку 1 (памп з об'ємами)
        signals_b2    — список сигналів блоку 2 (рух без об'ємів)
        found_b1_keys — множина ключів що вже є в блоці 1
    """
    if len(candles) < 4:
        return

    # Ціна = close останньої свічки — без окремого запиту тікера
    try:
        price = float(candles[-1][4] or 0)
    except (ValueError, TypeError, IndexError):
        return

    if price <= 0 or price >= MAX_PRICE_USDT:
        return

    # Аналіз ціни — один виклик, результат зберігаємо для обох блоків
    up_pct, up_price, up_min_time, up_max_time = analyze_price_up(candles)
    dn_pct, dn_price, dn_max_time, dn_min_time = analyze_price_down(candles)

    # ── БЛОК 1: памп з аномальним об'ємом ────────────────────────────────
    if up_pct >= GROWTH_THRESHOLD:
        saved_avg = state.get(state_key, None)
        signal_found, signal_idx, tail_count, final_avg = analyze_volumes(
            candles, saved_avg
        )
        state[state_key] = final_avg

        if signal_found:
            signal_is_last = (signal_idx == len(candles) - 1)
            print(f"  [B1/{label}] {name}: +{up_pct:.1f}% | "
                  f"{up_min_time}-{up_max_time} | хвіст={tail_count}св")
            signals_b1.append({
                "name":           name,
                "label":          label,
                "growth_pct":     up_pct,
                "max_price":      up_price,
                "min_time":       up_min_time,
                "max_time":       up_max_time,
                "tail_count":     tail_count,
                "signal_is_last": signal_is_last,
            })
            found_b1_keys.add(state_key)
            return   # не дублюємо в блок 2
    else:
        # Оновлюємо середнє навіть якщо ріст не досяг порогу
        saved_avg = state.get(state_key, None)
        _, _, _, final_avg = analyze_volumes(candles, saved_avg)
        state[state_key] = final_avg

    # ── БЛОК 2: рух ціни без перевірки об'ємів ───────────────────────────
    if state_key in found_b1_keys:
        return

    best_up = up_pct >= GROWTH_THRESHOLD
    best_dn = dn_pct >= GROWTH_THRESHOLD

    if not best_up and not best_dn:
        return

    # Якщо обидві умови — додаємо ОБИДВА рядки незалежно
    if best_up:
        print(f"  [B2+/{label}] {name}: UP {up_pct:.1f}% | {up_min_time}-{up_max_time}")
        signals_b2.append({
            "name":       name,
            "label":      label,
            "pct":        up_pct,
            "price":      up_price,
            "start_time": up_min_time,
            "end_time":   up_max_time,
            "is_up":      True,
        })

    if best_dn:
        print(f"  [B2-/{label}] {name}: DN {dn_pct:.1f}% | {dn_max_time}-{dn_min_time}")
        signals_b2.append({
            "name":       name,
            "label":      label,
            "pct":        dn_pct,
            "price":      dn_price,
            "start_time": dn_max_time,
            "end_time":   dn_min_time,
            "is_up":      False,
        })


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 12: ГОЛОВНА ЛОГІКА — ОРКЕСТРАЦІЯ ВСІХ БЛОКІВ
#
# Порядок роботи:
#   1. Завантажуємо state.json
#   2. OKX: отримуємо список інструментів → аналізуємо кожен
#   3. Binance: отримуємо список інструментів → аналізуємо кожен
#   4. Об'єднуємо сигнали обох бірж, сортуємо за відсотком
#   5. Зберігаємо state.json
#   6. Надсилаємо одне повідомлення у Telegram
# ─────────────────────────────────────────────────────────────────────────────

def main():
    now_utc = datetime.now(timezone.utc)
    now_str = now_utc.strftime("%Y-%m-%d  UTC=%H:%M")
    print(f"=== OKX_PAMP_bot старт | {now_str} ===")

    state = load_state()
    print(f"Завантажено середніх з state.json: {len(state)}")

    signals_b1   = []
    signals_b2   = []
    found_b1_keys = set()

    # ── OKX ──────────────────────────────────────────────────────────────────
    okx_instruments = okx_get_instruments()
    print(f"OKX інструментів: {len(okx_instruments)}")

    for inst_id in okx_instruments:
        candles   = okx_get_candles(inst_id)
        time.sleep(REQUEST_DELAY)
        state_key = f"OKX:{inst_id}"
        name      = inst_id.replace("-USDT-SWAP", "")
        analyze_instrument(
            candles, state_key, state, LABEL_OKX,
            name, signals_b1, signals_b2, found_b1_keys
        )

    # ── BINANCE ───────────────────────────────────────────────────────────────
    bin_instruments = bin_get_instruments()
    print(f"Binance інструментів: {len(bin_instruments)}")

    for symbol in bin_instruments:
        candles   = bin_get_candles(symbol)
        time.sleep(REQUEST_DELAY)
        state_key = f"BIN:{symbol}"
        # Назва монети: LAYERUSDT → LAYER
        name      = symbol.replace("USDT", "")
        analyze_instrument(
            candles, state_key, state, LABEL_BIN,
            name, signals_b1, signals_b2, found_b1_keys
        )

    # ── Зберігаємо state.json ─────────────────────────────────────────────────
    save_state(state)
    print(f"state.json збережено ({len(state)} записів)")

    # ── Формуємо повідомлення ─────────────────────────────────────────────────
    has_b1 = len(signals_b1) > 0
    has_b2 = len(signals_b2) > 0

    if not has_b1 and not has_b2:
        msg = now_str
        print("Сигналів не знайдено")
    else:
        lines = []

        if has_b1:
            signals_b1.sort(key=lambda x: x["growth_pct"], reverse=True)
            for s in signals_b1:
                line = format_line_block1(
                    s["name"], s["label"], s["growth_pct"], s["max_price"],
                    s["min_time"], s["max_time"],
                    s["tail_count"], s["signal_is_last"],
                )
                lines.append(line)
                print(f"  >> [B1] {line}")

        if has_b1 and has_b2:
            lines.append("")

        if has_b2:
            signals_b2.sort(key=lambda x: x["pct"], reverse=True)
            for s in signals_b2:
                line = format_line_block2(
                    s["name"], s["label"], s["pct"], s["price"],
                    s["start_time"], s["end_time"], s["is_up"],
                )
                lines.append(line)
                print(f"  >> [B2] {line}")

        msg = "\n".join(lines)

    send_telegram(msg)
    print("=== Завершено ===")


if __name__ == "__main__":
    main()
