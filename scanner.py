# =============================================================================
# scanner.py
# OKX_PAMP_bot — сканер пампів на ф'ючерсному ринку OKX (USDT-SWAP)
#
# БЛОК 1 (памп з об'ємами):
#   1. Поточна ціна < 5 USDT
#   2. Ріст ціни min→max >= 50% за 6 годин (24 свічки × 15хв)
#   3. Аномальний об'єм >= 10х від ковзного середнього
#   Формат: DOGE+74.3%;max0.10200(03:45-14:30);V+10х
#
# БЛОК 2 (рух без перевірки об'ємів):
#   1. Поточна ціна < 5 USDT
#   2. Ріст min→max >= 50% АБО падіння max→min >= 50% за 6 годин
#   3. Монети що вже є в блоці 1 — не дублюються
#   Формат ріст:    BSB+52.7%;max0.61520;03:45-14:30
#   Формат падіння: BSB-52.7%;min0.61520;03:45-14:30
#
# Збереження: середній об'єм кожної пари зберігається у state.json між запусками
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

OKX_BASE_URL     = "https://www.okx.com"

STATE_FILE       = "state.json"

CANDLE_BAR       = "15m"
CANDLES_COUNT    = 24                      # 24 свічки × 15хв = 6 годин
MAX_PRICE_USDT   = 5.0                     # максимально допустима ціна монети
GROWTH_THRESHOLD = 50.0                    # мінімальний ріст/падіння у відсотках
VOLUME_SPIKE_X   = 10.0                    # кратність аномального об'єму
VOLUME_TAIL_X    = 5.0                     # кратність хвостових свічок
HALF_CANDLES     = CANDLES_COUNT // 2      # половина від кількості свічок = 12
REQUEST_DELAY    = 0.12                    # затримка між запитами (rate limit OKX)


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 1: ЗБЕРЕЖЕННЯ ТА ЗАВАНТАЖЕННЯ СТАНУ МІЖ ЗАПУСКАМИ
# state.json зберігає: { "DOGE-USDT-SWAP": 12345.6, ... }
# де значення — останнє ковзне середнє об'єму пари
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
# БЛОК 2: ОТРИМАННЯ СПИСКУ ВСІХ АКТИВНИХ USDT-SWAP ІНСТРУМЕНТІВ
# ─────────────────────────────────────────────────────────────────────────────

def get_all_swap_instruments():
    """Повертає список instId всіх активних USDT-SWAP інструментів на OKX"""
    url    = f"{OKX_BASE_URL}/api/v5/public/instruments"
    params = {"instType": "SWAP"}
    try:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
        if data.get("code") != "0":
            print(f"Помилка API instruments: {data.get('msg')}")
            return []
        result = []
        for item in data.get("data", []):
            inst_id = item.get("instId", "")
            if inst_id.endswith("-USDT-SWAP"):
                result.append(inst_id)
        return result
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"Виняток get_all_swap_instruments: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 3: ОТРИМАННЯ ПОТОЧНОЇ ЦІНИ (для відсівки за ціною < 5 USDT)
# ─────────────────────────────────────────────────────────────────────────────

def get_ticker_price(inst_id):
    """Повертає поточну ціну last або None при помилці"""
    url    = f"{OKX_BASE_URL}/api/v5/market/ticker"
    params = {"instId": inst_id}
    try:
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        if data.get("code") != "0" or not data.get("data"):
            return None
        val = data["data"][0].get("last", 0)
        return float(val) if val else None
    except (requests.RequestException, ValueError, KeyError, TypeError, IndexError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 4: ОТРИМАННЯ 15-ХВИЛИННИХ СВІЧОК ЗА 6 ГОДИН
# OKX повертає від нових до старих — розвертаємо:
# [0]=найстаріша, [23]=найновіша
# Формат свічки: [ts, open, high, low, close, vol, volCcy, volCcyQuote, confirm]
# ─────────────────────────────────────────────────────────────────────────────

def get_candles(inst_id):
    """Повертає 24 свічки 15м від найстарішої [0] до найновішої [23]"""
    url    = f"{OKX_BASE_URL}/api/v5/market/candles"
    params = {
        "instId": inst_id,
        "bar":    CANDLE_BAR,
        "limit":  str(CANDLES_COUNT),
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        if data.get("code") != "0" or not data.get("data"):
            return []
        candles = data["data"]
        candles.reverse()   # тепер [0]=найстаріша, [23]=найновіша
        return candles
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"Виняток get_candles {inst_id}: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 5: ДОПОМІЖНА ФУНКЦІЯ — TIMESTAMP В РЯДОК UTC HH:MM
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
# БЛОК 6: АЛГОРИТМ КОВЗНОГО СЕРЕДНЬОГО ОБ'ЄМІВ З ВИКЛЮЧЕННЯМ АНОМАЛІЙ
#
# Стартове середнє = збережене з попереднього запуску АБО об'єм свічки [0]
# Рух від [0] до [23]:
#   obj < avg × 10  → нормальна, включаємо в базу, оновлюємо середнє
#   obj >= avg × 10 → СИГНАЛ аномального об'єму
# Після сигналу — аналіз хвоста:
#   obj >= avg × 5  → хвостова, рахуємо, не включаємо поки їх < HALF_CANDLES
#   хвостових >= HALF_CANDLES → включаємо всі в базу, шукаємо нову аномалію
#   obj < avg × 5  → нормальна після хвоста, включаємо в базу
# ─────────────────────────────────────────────────────────────────────────────

def analyze_volumes(candles, saved_avg):
    """
    Аналізує об'єми за алгоритмом ковзного середнього з виключенням аномалій.

    Повертає:
        signal_found      (bool)  — знайдено аномальний об'єм
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
# БЛОК 7: АНАЛІЗ ЦІНИ — РІСТ MIN→MAX ЗА 6 ГОДИН
# Повертає: відсоток росту, ціну max, час свічки з min, час свічки з max
# ─────────────────────────────────────────────────────────────────────────────

def analyze_price_up(candles):
    """
    Розраховує ріст від глобального мінімуму до глобального максимуму.

    Повертає:
        growth_pct (float) — відсоток росту min→max
        max_price  (float) — ціна максимуму (high)
        min_time   (str)   — UTC час свічки з мінімумом HH:MM (точка відліку)
        max_time   (str)   — UTC час свічки з максимумом HH:MM
    """
    try:
        min_price     = float("inf")
        max_price     = 0.0
        min_candle_ts = None
        max_candle_ts = None

        for candle in candles:
            high = float(candle[2] or 0)
            low  = float(candle[3] or 0)
            ts   = int(candle[0])

            if 0 < low < min_price:
                min_price     = low
                min_candle_ts = ts
            if high > max_price:
                max_price     = high
                max_candle_ts = ts

        if min_price <= 0 or max_price <= 0 or min_price == float("inf"):
            return 0.0, 0.0, "--:--", "--:--"

        growth_pct = (max_price - min_price) / min_price * 100
        min_time   = ts_to_utc(min_candle_ts)
        max_time   = ts_to_utc(max_candle_ts)

        return growth_pct, max_price, min_time, max_time

    except (ValueError, TypeError, IndexError, ZeroDivisionError) as e:
        print(f"Виняток analyze_price_up: {e}")
        return 0.0, 0.0, "--:--", "--:--"


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 8: АНАЛІЗ ЦІНИ — ПАДІННЯ MAX→MIN ЗА 6 ГОДИН
# Повертає: відсоток падіння, ціну min, час свічки з max, час свічки з min
# ─────────────────────────────────────────────────────────────────────────────

def analyze_price_down(candles):
    """
    Розраховує падіння від глобального максимуму до глобального мінімуму.

    Повертає:
        drop_pct   (float) — відсоток падіння max→min
        min_price  (float) — ціна мінімуму (low)
        max_time   (str)   — UTC час свічки з максимумом HH:MM (точка відліку)
        min_time   (str)   — UTC час свічки з мінімумом HH:MM
    """
    try:
        max_price     = 0.0
        min_price     = float("inf")
        max_candle_ts = None
        min_candle_ts = None

        for candle in candles:
            high = float(candle[2] or 0)
            low  = float(candle[3] or 0)
            ts   = int(candle[0])

            if high > max_price:
                max_price     = high
                max_candle_ts = ts
            if 0 < low < min_price:
                min_price     = low
                min_candle_ts = ts

        if max_price <= 0 or min_price <= 0 or min_price == float("inf"):
            return 0.0, 0.0, "--:--", "--:--"

        drop_pct = (max_price - min_price) / max_price * 100
        max_time = ts_to_utc(max_candle_ts)
        min_time = ts_to_utc(min_candle_ts)

        return drop_pct, min_price, max_time, min_time

    except (ValueError, TypeError, IndexError, ZeroDivisionError) as e:
        print(f"Виняток analyze_price_down: {e}")
        return 0.0, 0.0, "--:--", "--:--"


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 9: ФОРМАТУВАННЯ ЦІНИ ДО ПОТРІБНОЇ КІЛЬКОСТІ ЗНАКІВ
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
# БЛОК 10: ФОРМАТУВАННЯ РЯДКА СИГНАЛУ БЛОКУ 1 (памп з об'ємами)
# Формат: DOGE+74.3%;max0.10200(03:45-14:30);V+10х
#      або DOGE+74.3%;max0.10200(03:45-14:30);V+10х(5св)
# де 03:45 = час свічки з мінімумом (точка відліку росту)
#    14:30 = час свічки з максимумом
# ─────────────────────────────────────────────────────────────────────────────

def format_line_block1(inst_id, growth_pct, max_price, min_time, max_time,
                       tail_count, signal_is_last):
    """Формує рядок сигналу для блоку 1 (памп з аномальним об'ємом)"""
    name      = inst_id.replace("-USDT-SWAP", "")
    price_str = fmt_price(max_price)
    base      = f"{name}+{growth_pct:.1f}%;max{price_str}({min_time}-{max_time});V+10х"
    if signal_is_last:
        return base
    else:
        return f"{base}({tail_count}св)"


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 11: ФОРМАТУВАННЯ РЯДКА СИГНАЛУ БЛОКУ 2 (рух без об'ємів)
# Ріст:    BSB+52.7%;max0.61520;03:45-14:30
# Падіння: BSB-52.7%;min0.61520;03:45-14:30
# де перший час = точка відліку (min для росту, max для падіння)
#    другий час = кінцева точка (max для росту, min для падіння)
# ─────────────────────────────────────────────────────────────────────────────

def format_line_block2(inst_id, pct, price, start_time, end_time, is_up):
    """Формує рядок сигналу для блоку 2 (рух ціни без перевірки об'ємів)"""
    name      = inst_id.replace("-USDT-SWAP", "")
    price_str = fmt_price(price)
    if is_up:
        return f"{name}+{pct:.1f}%;max{price_str};{start_time}-{end_time}"
    else:
        return f"{name}-{pct:.1f}%;min{price_str};{start_time}-{end_time}"


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 12: НАДСИЛАННЯ ПОВІДОМЛЕННЯ У TELEGRAM (plain text)
# ─────────────────────────────────────────────────────────────────────────────

def send_telegram(text):
    """Надсилає текстове повідомлення у Telegram без форматування"""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"Telegram не налаштовано. Повідомлення:\n{text}")
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
# БЛОК 13: ГОЛОВНА ЛОГІКА — ОРКЕСТРАЦІЯ ВСІХ БЛОКІВ
# Порядок роботи:
#   1. Завантажуємо state.json
#   2. Отримуємо список інструментів
#   3. Для кожного інструменту:
#      а) відсівка за ціною < 5 USDT
#      б) отримуємо свічки (один запит для обох блоків)
#      в) БЛОК 1: перевірка росту + аналіз об'ємів
#      г) БЛОК 2: перевірка росту АБО падіння (якщо не в блоці 1)
#   4. Зберігаємо state.json
#   5. Формуємо і надсилаємо повідомлення
# ─────────────────────────────────────────────────────────────────────────────

def main():
    now_utc = datetime.now(timezone.utc)
    now_str = now_utc.strftime("%Y-%m-%d  UTC=%H:%M")
    print(f"=== OKX_PAMP_bot старт | {now_str} ===")

    # ── 1. Завантажуємо збережені середні об'єми
    state = load_state()
    print(f"Завантажено середніх з state.json: {len(state)}")

    # ── 2. Отримуємо список інструментів
    instruments = get_all_swap_instruments()
    print(f"Інструментів USDT-SWAP: {len(instruments)}")
    if not instruments:
        print("Список порожній — завершення")
        return

    signals_b1   = []      # сигнали блоку 1 (памп з об'ємами)
    signals_b2   = []      # сигнали блоку 2 (рух без об'ємів)
    found_b1_ids = set()   # instId що вже є в блоці 1 — для уникнення дублів

    # ── 3. Аналізуємо кожен інструмент
    for inst_id in instruments:

        # 3а. Відсівка за ціною < 5 USDT
        price = get_ticker_price(inst_id)
        time.sleep(REQUEST_DELAY)

        if price is None or price <= 0 or price >= MAX_PRICE_USDT:
            continue

        # 3б. Отримуємо свічки — один запит, використовуємо в обох блоках
        candles = get_candles(inst_id)
        time.sleep(REQUEST_DELAY)

        if len(candles) < 4:
            continue

        # ── БЛОК 1: памп з аномальним об'ємом ──────────────────────────────

        growth_pct, max_price, min_time, max_time = analyze_price_up(candles)

        if growth_pct >= GROWTH_THRESHOLD:
            saved_avg = state.get(inst_id, None)
            signal_found, signal_idx, tail_count, final_avg = analyze_volumes(
                candles, saved_avg
            )
            state[inst_id] = final_avg

            if signal_found:
                signal_is_last = (signal_idx == len(candles) - 1)
                print(f"  [B1] {inst_id}: +{growth_pct:.1f}% | "
                      f"{min_time}-{max_time} | хвіст={tail_count}св")
                signals_b1.append({
                    "inst_id":        inst_id,
                    "growth_pct":     growth_pct,
                    "max_price":      max_price,
                    "min_time":       min_time,
                    "max_time":       max_time,
                    "tail_count":     tail_count,
                    "signal_is_last": signal_is_last,
                })
                found_b1_ids.add(inst_id)
                continue   # не дублюємо в блок 2
        else:
            # Ріст не досяг порогу — оновлюємо середнє все одно
            saved_avg = state.get(inst_id, None)
            _, _, _, final_avg = analyze_volumes(candles, saved_avg)
            state[inst_id] = final_avg

        # ── БЛОК 2: рух ціни без перевірки об'ємів ─────────────────────────

        if inst_id in found_b1_ids:
            continue

        up_pct, up_price, up_min_time, up_max_time = analyze_price_up(candles)
        dn_pct, dn_price, dn_max_time, dn_min_time = analyze_price_down(candles)

        best_up = up_pct >= GROWTH_THRESHOLD
        best_dn = dn_pct >= GROWTH_THRESHOLD

        if not best_up and not best_dn:
            continue

        # Якщо обидві умови — беремо більшу за абсолютним значенням
        if best_up and best_dn:
            is_up = up_pct >= dn_pct
        elif best_up:
            is_up = True
        else:
            is_up = False

        if is_up:
            pct        = up_pct
            sig_price  = up_price
            start_time = up_min_time   # точка відліку = мінімум
            end_time   = up_max_time   # кінець = максимум
        else:
            pct        = dn_pct
            sig_price  = dn_price
            start_time = dn_max_time   # точка відліку = максимум
            end_time   = dn_min_time   # кінець = мінімум

        direction = "UP" if is_up else "DN"
        print(f"  [B2] {inst_id}: {direction} {pct:.1f}% | {start_time}-{end_time}")

        signals_b2.append({
            "inst_id":    inst_id,
            "pct":        pct,
            "price":      sig_price,
            "start_time": start_time,
            "end_time":   end_time,
            "is_up":      is_up,
        })

    # ── 4. Зберігаємо оновлений state.json
    save_state(state)
    print(f"state.json збережено ({len(state)} пар)")

    # ── 5. Формуємо і надсилаємо повідомлення
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
                    s["inst_id"], s["growth_pct"], s["max_price"],
                    s["min_time"], s["max_time"],
                    s["tail_count"], s["signal_is_last"],
                )
                lines.append(line)
                print(f"  >> [B1] {line}")

        if has_b1 and has_b2:
            lines.append("")   # порожній рядок між блоками

        if has_b2:
            signals_b2.sort(key=lambda x: x["pct"], reverse=True)
            for s in signals_b2:
                line = format_line_block2(
                    s["inst_id"], s["pct"], s["price"],
                    s["start_time"], s["end_time"], s["is_up"],
                )
                lines.append(line)
                print(f"  >> [B2] {line}")

        msg = "\n".join(lines)

    send_telegram(msg)
    print("=== Завершено ===")


if __name__ == "__main__":
    main()
