# =============================================================================
# scanner.py
# OKX_PAMP_bot — сканер пампів на ф'ючерсному ринку OKX (USDT-SWAP)
#
# БЛОК 1 (памп з об'ємами):
#   1. Поточна ціна (close останньої свічки) < 5 USDT
#   2. Ріст ціни min→max >= 50% за 8 годин (32 свічки × 15хв)
#      де мінімум хронологічно РАНІШЕ максимуму
#   3. Аномальний об'єм >= 10х від ковзного середнього
#   Формат: DOGE+74.3%;max0.10200(03:45-14:30);V+10х
#        або DOGE+74.3%;max0.10200(03:45-14:30);V+10х(5св)
#
# БЛОК 2 (рух без перевірки об'ємів):
#   1. Поточна ціна (close останньої свічки) < 5 USDT
#   2. Ріст min→max >= 50% АБО падіння max→min >= 50% за 8 годин
#      де екстремуми хронологічно впорядковані
#   3. Монети що вже є в блоці 1 — не дублюються
#   Формат ріст:    BSB+52.7%;max0.61520;03:45-14:30
#   Формат падіння: BSB-52.7%;min0.61520;03:45-14:30
#
# Оптимізація швидкості:
#   - ціна береться з close останньої свічки (без окремого HTTP-запиту тікера)
#   - результати analyze_price_up() зберігаються і повторно не викликаються
#   - один запит свічок використовується для обох блоків
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
CANDLES_COUNT    = 32                      # 32 свічки × 15хв = 8 годин
MAX_PRICE_USDT   = 5.0                     # максимально допустима ціна монети в USDT
GROWTH_THRESHOLD = 50.0                    # мінімальний ріст/падіння у відсотках
VOLUME_SPIKE_X   = 10.0                    # кратність аномального об'єму для сигналу
VOLUME_TAIL_X    = 5.0                     # кратність хвостових свічок після аномалії
HALF_CANDLES     = CANDLES_COUNT // 2      # половина від кількості свічок = 16
REQUEST_DELAY    = 0.12                    # затримка між HTTP-запитами (rate limit OKX)


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 1: ЗБЕРЕЖЕННЯ ТА ЗАВАНТАЖЕННЯ СТАНУ МІЖ ЗАПУСКАМИ
# state.json зберігає: { "DOGE-USDT-SWAP": 12345.6, ... }
# де значення — останнє ковзне середнє об'єму пари з попереднього запуску
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
# Публічний endpoint — API ключі не потрібні
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
# БЛОК 3: ОТРИМАННЯ 15-ХВИЛИННИХ СВІЧОК ЗА 8 ГОДИН
# OKX повертає від нових до старих — розвертаємо:
#   [0] = найстаріша свічка, [31] = найновіша свічка
# Формат свічки OKX:
#   [0]=timestamp(мс), [1]=open, [2]=high, [3]=low, [4]=close,
#   [5]=vol(контракти), [6]=volCcy, [7]=volCcyQuote(USDT), [8]=confirm
#
# ВАЖЛИВО: ціна монети береться з close([4]) ОСТАННЬОЇ свічки [31]
#   без окремого HTTP-запиту тікера — це вдвічі прискорює роботу бота
# ─────────────────────────────────────────────────────────────────────────────

def get_candles(inst_id):
    """Повертає 32 свічки 15м від найстарішої [0] до найновішої [31]"""
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
        candles.reverse()   # тепер [0]=найстаріша, [31]=найновіша
        return candles
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"Виняток get_candles {inst_id}: {e}")
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
#   vol < avg × 10  → нормальна свічка: включаємо в базу, оновлюємо avg
#   vol >= avg × 10 → СИГНАЛ: аномальний об'єм знайдено
# Після сигналу — аналіз хвоста:
#   vol >= avg × 5  → хвостова: рахуємо, НЕ включаємо поки їх < HALF_CANDLES
#   хвостових >= HALF_CANDLES → включаємо всі в базу, скидаємо сигнал,
#                                шукаємо нову аномалію далі
#   vol < avg × 5  → нормальна після хвоста: включаємо в базу
# ─────────────────────────────────────────────────────────────────────────────

def analyze_volumes(candles, saved_avg):
    """
    Аналізує об'єми за алгоритмом ковзного середнього з виключенням аномалій.

    Аргументи:
        candles   — список свічок від [0]=найстаріша до [31]=найновіша
        saved_avg — збережене середнє з попереднього запуску (None = перший запуск)

    Повертає:
        signal_found      (bool)  — знайдено аномальний об'єм >= 10х
        signal_candle_idx (int)   — індекс сигнальної свічки (-1 якщо немає)
        tail_count        (int)   — кількість свічок з підвищеним об'ємом
        final_avg         (float) — підсумкове середнє для збереження у state.json
    """
    if not candles:
        return False, -1, 0, (saved_avg if saved_avg else 0.0)

    volumes_in_base = []

    if saved_avg is not None and saved_avg > 0:
        # Є збережене середнє — використовуємо як стартову базу
        current_avg = saved_avg
        start_idx   = 0
    else:
        # Перший запуск: базове середнє = об'єм першої свічки [0]
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
            # ── Режим пошуку аномалії ──
            if current_avg > 0 and vol >= current_avg * VOLUME_SPIKE_X:
                signal_found      = True
                signal_candle_idx = i
                tail_count        = 1
                tail_indices      = [i]
            else:
                volumes_in_base.append(vol)
                current_avg = sum(volumes_in_base) / len(volumes_in_base)
        else:
            # ── Режим аналізу хвоста після аномалії ──
            if vol >= current_avg * VOLUME_TAIL_X:
                tail_indices.append(i)
                tail_count = len(tail_indices)
                if tail_count >= HALF_CANDLES:
                    # Хвіст занадто довгий — включаємо в базу і шукаємо нову аномалію
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
# Алгоритм одного проходу з running_min зліва направо.
# На кожній свічці: оновлюємо running_min, рахуємо ріст від running_min до high.
# Зберігаємо найкращий результат. Гарантія: мінімум ЗАВЖДИ раніше максимуму.
# ─────────────────────────────────────────────────────────────────────────────

def analyze_price_up(candles):
    """
    Знаходить найкращий ріст де мінімум хронологічно РАНІШЕ максимуму.
    Алгоритм одного проходу зліва направо з running_min:
      — running_min оновлюється на кожній свічці
      — для кожної свічки рахуємо ріст від running_min до high цієї свічки
      — зберігаємо найкращий результат (максимальний відсоток)
    Це дозволяє знайти ріст навіть якщо після максимуму ціна впала
    нижче початкового мінімуму (новий глобальний мінімум зміщується вправо).

    Повертає:
        pct       (float) — найкращий відсоток росту min→max
        max_price (float) — ціна максимуму найкращого росту (high)
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

            # Оновлюємо поточний мінімум (ліва точка відліку)
            if 0 < low < running_min:
                running_min    = low
                running_min_ts = ts

            # Рахуємо ріст від running_min до high поточної свічки
            # Максимум повинен бути ПІЗНІШЕ мінімуму (суворо >)
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
# Алгоритм одного проходу з running_max зліва направо.
# На кожній свічці: оновлюємо running_max, рахуємо падіння від running_max до low.
# Зберігаємо найкращий результат. Гарантія: максимум ЗАВЖДИ раніше мінімуму.
# ─────────────────────────────────────────────────────────────────────────────

def analyze_price_down(candles):
    """
    Знаходить найкраще падіння де максимум хронологічно РАНІШЕ мінімуму.
    Алгоритм одного проходу зліва направо з running_max:
      — running_max оновлюється на кожній свічці
      — для кожної свічки рахуємо падіння від running_max до low цієї свічки
      — зберігаємо найкращий результат (максимальний відсоток)
    Це дозволяє знайти падіння навіть якщо перед мінімумом ціна зросла
    вище початкового максимуму (новий глобальний максимум зміщується вправо).

    Повертає:
        pct       (float) — найкращий відсоток падіння max→min
        min_price (float) — ціна мінімуму найкращого падіння (low)
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

            # Оновлюємо поточний максимум (ліва точка відліку)
            if high > running_max:
                running_max    = high
                running_max_ts = ts

            # Рахуємо падіння від running_max до low поточної свічки
            # Мінімум повинен бути ПІЗНІШЕ максимуму (суворо >)
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
# БЛОК 8: ФОРМАТУВАННЯ ЦІНИ ДО ПОТРІБНОЇ КІЛЬКОСТІ ЗНАКІВ
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
# БЛОК 9: ФОРМАТУВАННЯ РЯДКА СИГНАЛУ БЛОКУ 1 (памп з об'ємами)
# Формат (сигнальна свічка остання):  DOGE+74.3%;max0.10200(03:45-14:30);V+10х
# Формат (є хвостові свічки після):   DOGE+74.3%;max0.10200(03:45-14:30);V+10х(5св)
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
# БЛОК 10: ФОРМАТУВАННЯ РЯДКА СИГНАЛУ БЛОКУ 2 (рух без об'ємів)
# Ріст:    BSB+52.7%;max0.61520;03:45-14:30
# Падіння: BSB-52.7%;min0.61520;03:45-14:30
# де перший час = точка відліку (min для росту, max для падіння)
#    другий час = кінцева точка  (max для росту, min для падіння)
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
# БЛОК 11: НАДСИЛАННЯ ПОВІДОМЛЕННЯ У TELEGRAM (plain text без parse_mode)
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
# БЛОК 12: ГОЛОВНА ЛОГІКА — ОРКЕСТРАЦІЯ ВСІХ БЛОКІВ
#
# Порядок роботи:
#   1. Завантажуємо state.json (збережені середні об'єми)
#   2. Отримуємо список всіх USDT-SWAP інструментів (1 запит)
#   3. Для кожного інструменту — ОДИН запит свічок:
#      а) ціна = close останньої свічки [31][4] — без окремого запиту тікера
#      б) якщо ціна >= 5 USDT — пропускаємо
#      в) результати analyze_price_up() зберігаємо — не викликаємо двічі
#      г) БЛОК 1: перевірка росту >= 50% + аналіз об'ємів
#      д) БЛОК 2: перевірка росту АБО падіння >= 50% (якщо не в блоці 1)
#   4. Зберігаємо оновлений state.json
#   5. Формуємо і надсилаємо повідомлення у Telegram
# ─────────────────────────────────────────────────────────────────────────────

def main():
    now_utc = datetime.now(timezone.utc)
    now_str = now_utc.strftime("%Y-%m-%d  UTC=%H:%M")
    print(f"=== OKX_PAMP_bot старт | {now_str} ===")

    # ── 1. Завантажуємо збережені середні об'єми
    state = load_state()
    print(f"Завантажено середніх з state.json: {len(state)}")

    # ── 2. Отримуємо список інструментів (1 запит на весь цикл)
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

        # 3а. Один запит свічок — використовується для всього аналізу
        candles = get_candles(inst_id)
        time.sleep(REQUEST_DELAY)

        if len(candles) < 4:
            continue

        # 3б. Ціна = close останньої свічки — БЕЗ окремого запиту тікера
        # Це вдвічі скорочує кількість HTTP-запитів і прискорює цикл
        try:
            price = float(candles[-1][4] or 0)
        except (ValueError, TypeError, IndexError):
            continue

        if price <= 0 or price >= MAX_PRICE_USDT:
            continue

        # 3в. Аналіз ціни — викликаємо ОДИН РАЗ, результат зберігаємо
        up_pct, up_price, up_min_time, up_max_time = analyze_price_up(candles)
        dn_pct, dn_price, dn_max_time, dn_min_time = analyze_price_down(candles)

        # ── БЛОК 1: памп з аномальним об'ємом ──────────────────────────────
        if up_pct >= GROWTH_THRESHOLD:
            saved_avg = state.get(inst_id, None)
            signal_found, signal_idx, tail_count, final_avg = analyze_volumes(
                candles, saved_avg
            )
            state[inst_id] = final_avg

            if signal_found:
                signal_is_last = (signal_idx == len(candles) - 1)
                print(f"  [B1] {inst_id}: +{up_pct:.1f}% | "
                      f"{up_min_time}-{up_max_time} | хвіст={tail_count}св")
                signals_b1.append({
                    "inst_id":        inst_id,
                    "growth_pct":     up_pct,
                    "max_price":      up_price,
                    "min_time":       up_min_time,
                    "max_time":       up_max_time,
                    "tail_count":     tail_count,
                    "signal_is_last": signal_is_last,
                })
                found_b1_ids.add(inst_id)
                continue   # монета вже в блоці 1 — не дублюємо в блок 2
        else:
            # Ріст не досяг порогу — оновлюємо середнє все одно
            saved_avg = state.get(inst_id, None)
            _, _, _, final_avg = analyze_volumes(candles, saved_avg)
            state[inst_id] = final_avg

        # ── БЛОК 2: рух ціни без перевірки об'ємів ─────────────────────────
        if inst_id in found_b1_ids:
            continue

        best_up = up_pct >= GROWTH_THRESHOLD
        best_dn = dn_pct >= GROWTH_THRESHOLD

        if not best_up and not best_dn:
            continue

        # Якщо обидві умови виконуються одночасно — додаємо ОБИДВА рядки
        # для цієї монети незалежно один від одного
        if best_up:
            print(f"  [B2+] {inst_id}: UP {up_pct:.1f}% | {up_min_time}-{up_max_time}")
            signals_b2.append({
                "inst_id":    inst_id,
                "pct":        up_pct,
                "price":      up_price,
                "start_time": up_min_time,   # точка відліку = мінімум
                "end_time":   up_max_time,   # кінцева точка = максимум
                "is_up":      True,
            })

        if best_dn:
            print(f"  [B2-] {inst_id}: DN {dn_pct:.1f}% | {dn_max_time}-{dn_min_time}")
            signals_b2.append({
                "inst_id":    inst_id,
                "pct":        dn_pct,
                "price":      dn_price,
                "start_time": dn_max_time,   # точка відліку = максимум
                "end_time":   dn_min_time,   # кінцева точка = мінімум
                "is_up":      False,
            })

    # ── 4. Зберігаємо оновлений state.json
    save_state(state)
    print(f"state.json збережено ({len(state)} пар)")

    # ── 5. Формуємо і надсилаємо повідомлення у Telegram
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
