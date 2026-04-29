# =============================================================================
# scanner.py
# OKX_PAMP_bot — сканер пампів на ф'ючерсному ринку OKX (SWAP)
# Умови: ціна < 5 USDT | ріст min→max >= 50% за 6 год | аномальний об'єм >= 10х
# =============================================================================

import requests
import json
import os
import time
from datetime import datetime, timezone

# ─────────────────────────────────────────────────────────────────────────────
# БЛОК НАЛАШТУВАНЬ
# ─────────────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]   # токен Telegram-бота
TELEGRAM_CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]     # ID чату для сповіщень

OKX_BASE_URL      = "https://www.okx.com"              # базова адреса OKX API
STATE_FILE        = "state.json"                        # файл збереження середніх об'ємів

CANDLE_BAR        = "15m"                              # таймфрейм свічок
CANDLES_COUNT     = 24                                 # кількість свічок = 6 годин (24 × 15хв)
MAX_PRICE_USDT    = 5.0                                # максимальна ціна монети в USDT
GROWTH_THRESHOLD  = 50.0                               # мінімальний ріст ціни min→max у %
VOLUME_SPIKE_X    = 10.0                               # кратність аномального об'єму (10х)
VOLUME_TAIL_X     = 5.0                                # кратність "хвостових" свічок (5х)
HALF_CANDLES      = CANDLES_COUNT // 2                 # половина від загальної кількості свічок
REQUEST_DELAY     = 0.12                               # затримка між запитами (rate limit OKX)


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 1: ЗАВАНТАЖЕННЯ ТА ЗБЕРЕЖЕННЯ СТАНУ (збережені середні об'єми між запусками)
# ─────────────────────────────────────────────────────────────────────────────
def load_state():
    """Завантажує збережені середні об'єми з попереднього запуску"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state):
    """Зберігає оновлені середні об'єми для наступного запуску"""
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"Помилка збереження state.json: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 2: ОТРИМАННЯ ВСІХ SWAP-ІНСТРУМЕНТІВ З OKX
# Повертає список усіх активних безстрокових ф'ючерсних пар
# ─────────────────────────────────────────────────────────────────────────────
def get_all_swap_instruments():
    """Отримує список всіх активних USDT-SWAP інструментів з OKX"""
    url = f"{OKX_BASE_URL}/api/v5/public/instruments"
    params = {"instType": "SWAP"}
    try:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
        if data.get("code") != "0":
            print(f"Помилка отримання інструментів: {data}")
            return []
        instruments = []
        for item in data.get("data", []):
            inst_id = item.get("instId", "")
            # Беремо лише пари з USDT як розрахункова валюта
            if inst_id.endswith("-USDT-SWAP"):
                instruments.append(inst_id)
        return instruments
    except Exception as e:
        print(f"Виняток get_instruments: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 3: ОТРИМАННЯ ПОТОЧНОГО ТІКЕРА (для перевірки ціни < 5 USDT)
# ─────────────────────────────────────────────────────────────────────────────
def get_ticker_price(inst_id):
    """Повертає поточну ціну last для інструменту"""
    url = f"{OKX_BASE_URL}/api/v5/market/ticker"
    params = {"instId": inst_id}
    try:
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        if data.get("code") != "0" or not data.get("data"):
            return None
        return float(data["data"][0].get("last", 0) or 0)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 4: ОТРИМАННЯ 15-ХВИЛИННИХ СВІЧОК ЗА 6 ГОДИН
# OKX повертає свічки від НОВИХ до СТАРИХ: [0]=найновіша, [-1]=найстаріша
# Ми розвертаємо масив: [0]=найстаріша → [23]=найновіша
# Формат свічки: [ts, open, high, low, close, vol, volCcy, volCcyQuote, confirm]
# ─────────────────────────────────────────────────────────────────────────────
def get_candles(inst_id):
    """Отримує 24 свічки по 15 хв (6 годин) та повертає від найстарішої до найновішої"""
    url = f"{OKX_BASE_URL}/api/v5/market/candles"
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
        # Розвертаємо: тепер [0]=найстаріша, [-1]=найновіша
        candles.reverse()
        return candles
    except Exception as e:
        print(f"Виняток get_candles {inst_id}: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 5: АЛГОРИТМ КОВЗНОГО СЕРЕДНЬОГО ОБ'ЄМІВ З ВИКЛЮЧЕННЯМ АНОМАЛІЙ
#
# Логіка:
# 1. Базове середнє = збережене з попереднього запуску АБО об'єм свічки [0]
# 2. Рухаємось від [0] до [23]:
#    - якщо об'єм < середнє × 10 → включаємо в базу, перераховуємо середнє
#    - якщо об'єм >= середнє × 10 → СИГНАЛ (аномалія знайдена)
# 3. Після сигнальної свічки аналізуємо "хвіст":
#    - якщо наступна свічка >= середнє × 5 → хвостова, рахуємо, не включаємо в базу
#      (поки таких < половини від загального числа свічок)
#    - якщо хвостових >= половини → включаємо їх у базу і шукаємо нову аномалію
#    - якщо наступна свічка < середнє × 5 → нормальна, включаємо в базу
# ─────────────────────────────────────────────────────────────────────────────
def analyze_volumes(candles, saved_avg):
    """
    Аналізує об'єми свічок за алгоритмом ковзного середнього з аномаліями.
    Повертає: (signal_found, signal_candle_idx, tail_count, final_avg)
    """
    if not candles:
        return False, -1, 0, saved_avg

    # Ініціалізація середнього: збережене або об'єм першої свічки
    volumes_in_base = []
    if saved_avg is not None and saved_avg > 0:
        current_avg = saved_avg
        # Перша свічка також перевіряється за загальним правилом
        start_idx = 0
    else:
        # Перший запуск: базове середнє = об'єм свічки [0]
        first_vol = float(candles[0][5] or 0)
        current_avg = first_vol if first_vol > 0 else 1.0
        volumes_in_base.append(first_vol)
        start_idx = 1

    signal_found     = False
    signal_candle_idx = -1
    tail_count       = 0
    tail_candles_idx = []

    i = start_idx
    while i < len(candles):
        vol = float(candles[i][5] or 0)

        if not signal_found:
            # ── Режим пошуку аномалії ──
            if vol >= current_avg * VOLUME_SPIKE_X and current_avg > 0:
                # Знайдено аномальний об'єм
                signal_found      = True
                signal_candle_idx = i
                tail_count        = 1  # рахуємо саму сигнальну свічку
                tail_candles_idx  = [i]
            else:
                # Нормальна свічка → включаємо в базу
                volumes_in_base.append(vol)
                if volumes_in_base:
                    current_avg = sum(volumes_in_base) / len(volumes_in_base)
        else:
            # ── Режим аналізу хвоста після сигнальної свічки ──
            if vol >= current_avg * VOLUME_TAIL_X:
                # Хвостова свічка з підвищеним об'ємом
                tail_candles_idx.append(i)
                tail_count = len(tail_candles_idx)

                if tail_count >= HALF_CANDLES:
                    # Хвостових >= половини → включаємо всі в базу і шукаємо нову аномалію
                    for ti in tail_candles_idx:
                        volumes_in_base.append(float(candles[ti][5] or 0))
                    current_avg = sum(volumes_in_base) / len(volumes_in_base) if volumes_in_base else current_avg
                    # Скидаємо стан пошуку
                    signal_found      = False
                    signal_candle_idx = -1
                    tail_count        = 0
                    tail_candles_idx  = []
            else:
                # Нормальна свічка після хвоста → включаємо в базу
                volumes_in_base.append(vol)
                if volumes_in_base:
                    current_avg = sum(volumes_in_base) / len(volumes_in_base)

        i += 1

    # Підсумкове середнє для збереження
    final_avg = sum(volumes_in_base) / len(volumes_in_base) if volumes_in_base else current_avg

    return signal_found, signal_candle_idx, tail_count, final_avg


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 6: ПЕРЕВІРКА УМОВИ ЦІНИ (ріст min → max >= 50% за 6 год)
# Повертає: (відсоток_росту, ціна_макс, час_свічки_макс_UTC)
# ─────────────────────────────────────────────────────────────────────────────
def analyze_price(candles):
    """Розраховує ріст ціни від мінімуму до максимуму за весь період свічок"""
    try:
        # Знаходимо глобальний мінімум і максимум по всіх свічках
        min_price   = float("inf")
        max_price   = 0.0
        max_candle_ts = None

        for candle in candles:
            high = float(candle[2] or 0)
            low  = float(candle[3] or 0)
            ts   = int(candle[0])

            if low > 0 and low < min_price:
                min_price = low
            if high > max_price:
                max_price = high
                max_candle_ts = ts

        if min_price <= 0 or max_price <= 0:
            return 0.0, 0.0, None

        growth_pct = (max_price - min_price) / min_price * 100

        # Перетворюємо timestamp (мілісекунди) → UTC час
        max_time_utc = datetime.fromtimestamp(max_candle_ts / 1000, tz=timezone.utc)
        time_str = max_time_utc.strftime("%H:%M")

        return growth_pct, max_price, time_str

    except Exception as e:
        print(f"Виняток analyze_price: {e}")
        return 0.0, 0.0, None


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 7: ФОРМАТУВАННЯ РЯДКА СИГНАЛУ ДЛЯ ОДНОЇ МОНЕТИ
# Формат: DOGE+74.3%;max0.10200(14:30);V+10х(5св)
# ─────────────────────────────────────────────────────────────────────────────
def format_signal_line(inst_id, growth_pct, max_price, max_time, tail_count, signal_last):
    """Формує рядок повідомлення для однієї монети-сигналу"""

    # Скорочуємо назву: DOGE-USDT-SWAP → DOGE
    name = inst_id.replace("-USDT-SWAP", "")

    # Форматуємо ціну максимуму: до 5 значущих цифр
    if max_price >= 1:
        price_str = f"{max_price:.4f}"
    elif max_price >= 0.01:
        price_str = f"{max_price:.5f}"
    else:
        price_str = f"{max_price:.7f}"

    # Формуємо кратність об'єму (ціле число)
    vol_part = f"V+10х"

    if signal_last:
        # Сигнальна свічка — остання в запиті
        return f"{name}+{growth_pct:.1f}%;max{price_str}({max_time});{vol_part}"
    else:
        # Після сигнальної є ще хвостові свічки
        return f"{name}+{growth_pct:.1f}%;max{price_str}({max_time});{vol_part}({tail_count}св)"


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 8: НАДСИЛАННЯ ПОВІДОМЛЕННЯ У TELEGRAM (plain text — без parse_mode)
# ─────────────────────────────────────────────────────────────────────────────
def send_telegram(text):
    """Надсилає текстове повідомлення у Telegram без форматування"""
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text":    text,
    }
    try:
        resp = requests.post(url, data=data, timeout=15)
        if resp.status_code != 200:
            print(f"Telegram помилка: {resp.text}")
        else:
            print("Telegram: повідомлення надіслано")
    except Exception as e:
        print(f"Виняток Telegram: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 9: ГОЛОВНА ЛОГІКА — ОРКЕСТРАЦІЯ ВСІХ БЛОКІВ
# ─────────────────────────────────────────────────────────────────────────────
def main():
    now_utc    = datetime.now(timezone.utc)
    now_str    = now_utc.strftime("%Y-%m-%d  UTC=%H:%M")
    print(f"Запуск OKX_PAMP_bot | {now_str}")

    # ── Крок 1: завантажуємо збережені середні об'єми
    state = load_state()

    # ── Крок 2: отримуємо всі SWAP-інструменти
    instruments = get_all_swap_instruments()
    print(f"Інструментів SWAP: {len(instruments)}")
    if not instruments:
        print("Немає інструментів, завершення")
        return

    signals = []  # список знайдених сигналів

    # ── Крок 3: аналізуємо кожен інструмент
    for inst_id in instruments:

        # 3а. Перевірка ціни < 5 USDT (швидка відсівка)
        price = get_ticker_price(inst_id)
        time.sleep(REQUEST_DELAY)

        if price is None or price <= 0 or price >= MAX_PRICE_USDT:
            continue

        # 3б. Отримуємо свічки
        candles = get_candles(inst_id)
        time.sleep(REQUEST_DELAY)

        if len(candles) < 4:
            continue

        # 3в. Аналіз ціни: ріст min→max >= 50%
        growth_pct, max_price, max_time = analyze_price(candles)
        if growth_pct < GROWTH_THRESHOLD:
            continue

        print(f"  {inst_id}: ціна ОК ({price:.4f}), ріст {growth_pct:.1f}%")

        # 3г. Аналіз об'ємів за ковзним середнім
        saved_avg = state.get(inst_id, None)
        signal_found, signal_idx, tail_count, final_avg = analyze_volumes(candles, saved_avg)

        # 3д. Зберігаємо оновлене середнє незалежно від сигналу
        state[inst_id] = final_avg

        if not signal_found:
            continue

        # 3е. Перевіряємо: сигнальна свічка остання чи є хвіст
        signal_is_last = (signal_idx == len(candles) - 1)

        print(f"    СИГНАЛ! idx={signal_idx}, хвіст={tail_count}св, остання={signal_is_last}")

        signals.append({
            "inst_id":        inst_id,
            "growth_pct":     growth_pct,
            "max_price":      max_price,
            "max_time":       max_time,
            "tail_count":     tail_count,
            "signal_is_last": signal_is_last,
        })

    # ── Крок 4: зберігаємо оновлений стан
    save_state(state)

    # ── Крок 5: формуємо та надсилаємо повідомлення
    if not signals:
        # Нічого не знайдено — тихе повідомлення
        msg = now_str
        print(f"Сигналів не знайдено")
    else:
        # Сортуємо за відсотком росту (від більшого до меншого)
        signals.sort(key=lambda x: x["growth_pct"], reverse=True)
        lines = []
        for s in signals:
            line = format_signal_line(
                s["inst_id"],
                s["growth_pct"],
                s["max_price"],
                s["max_time"],
                s["tail_count"],
                s["signal_is_last"],
            )
            lines.append(line)
            print(f"  {line}")
        msg = "\n".join(lines)

    send_telegram(msg)
    print("Завершено")


if __name__ == "__main__":
    main()
