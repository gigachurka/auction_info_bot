"""
Расчёт таможенных платежей для ввоза авто в РБ (физические лица).

Источник ставок: таблица "Ставки таможенных пошлин для физических лиц".
Таблица в евро, цена авто на аукционе в долларах, поэтому используем
конвертацию USD -> EUR (курс настраивается через USD_TO_EUR).

Итоговая стоимость растаможки = таможенная пошлина + сбор за штат отправки.
Сбор за штат пока заглушка (DEFAULT_STATE_FEE_USD), будет уточняться позже.
"""

import re
from datetime import datetime

from config import USD_TO_EUR, DEFAULT_STATE_FEE_USD
from exchange import get_usd_to_eur

# --- Настраиваемые параметры ---------------------------------------------
# USD_TO_EUR и DEFAULT_STATE_FEE_USD задаются через .env (см. config.py).

# Индивидуальные сборы по штатам (USD). Будет заполняться позже.
# Пример: {"CT": 120, "CA": 150}
STATE_FEES_USD = {}


# --- Парсинг входных данных -----------------------------------------------

def parse_engine_cc(engine_str) -> int:
    """Извлекает объём двигателя в см3 из строки.

    Поддерживает форматы:
    - "2.5L", "2.5 L", "2.5L 4 Cylinder"  -> литры -> см3 (x1000)
    - "1998cc", "1998 cc", "1998 cm3"     -> см3 напрямую
    - "2000"                              -> трактуется как см3
    Возвращает 0, если объём определить не удалось.
    """
    if not engine_str or not isinstance(engine_str, str):
        return 0

    text = engine_str.lower().strip()

    # Формат в литрах: число с буквой L (например "2.5l")
    liters_match = re.search(r'(\d+(?:[.,]\d+)?)\s*l\b', text)
    if liters_match:
        liters = float(liters_match.group(1).replace(',', '.'))
        return int(round(liters * 1000))

    # Формат в см3 / cc
    cc_match = re.search(r'(\d{3,5})\s*(?:cc|cm3|см3|куб)', text)
    if cc_match:
        return int(cc_match.group(1))

    # Просто число
    num_match = re.search(r'(\d+(?:[.,]\d+)?)', text)
    if num_match:
        value = float(num_match.group(1).replace(',', '.'))
        # Если число маленькое (< 20) — это литры, иначе уже см3
        if value < 20:
            return int(round(value * 1000))
        return int(round(value))

    return 0


def parse_year(title_or_year) -> int:
    """Извлекает год выпуска из заголовка ("2019 TESLA MODEL 3") или числа."""
    if title_or_year is None:
        return 0
    if isinstance(title_or_year, int):
        return title_or_year
    match = re.search(r'\b(19|20)\d{2}\b', str(title_or_year))
    if match:
        return int(match.group(0))
    return 0


def parse_state(location_str) -> str:
    """Извлекает код штата из локации Copart/IAAI.

    Примеры:
    - "CT - HARTFORD SPRINGFIELD" -> "CT"
    - "Hartford, CT"              -> "CT"
    Возвращает "" если не найдено.
    """
    if not location_str or not isinstance(location_str, str):
        return ""
    text = location_str.strip()

    # Формат "CT - ..." (код штата в начале)
    head_match = re.match(r'\s*([A-Z]{2})\b', text)
    if head_match:
        return head_match.group(1)

    # Формат "..., CT" (код штата в конце)
    tail_match = re.search(r'\b([A-Z]{2})\b\s*$', text)
    if tail_match:
        return tail_match.group(1)

    return ""


def get_vehicle_age(year: int) -> int:
    """Возраст авто в годах (текущий год - год выпуска)."""
    if not year:
        return 0
    age = datetime.now().year - year
    return max(age, 0)


# --- Расчёт таможенной пошлины --------------------------------------------

def _duty_under_3_years(value_eur: float, engine_cc: int) -> float:
    """Авто до 3 лет: процент от стоимости, но не менее X евро за см3."""
    if value_eur <= 8500:
        percent, min_per_cc = 0.54, 2.5
    elif value_eur <= 16700:
        percent, min_per_cc = 0.48, 3.5
    elif value_eur <= 42300:
        percent, min_per_cc = 0.48, 5.5
    elif value_eur <= 84500:
        percent, min_per_cc = 0.48, 7.5
    elif value_eur <= 169000:
        percent, min_per_cc = 0.48, 15.0
    else:
        percent, min_per_cc = 0.48, 20.0

    by_value = value_eur * percent
    by_volume = engine_cc * min_per_cc
    return max(by_value, by_volume)


def _rate_per_cc_3_to_5_years(engine_cc: int) -> float:
    """Ставка евро/см3 для авто от 3 до 5 лет."""
    if engine_cc <= 1000:
        return 1.5
    elif engine_cc <= 1500:
        return 1.7
    elif engine_cc <= 1800:
        return 2.5
    elif engine_cc <= 2300:
        return 2.7
    elif engine_cc <= 3000:
        return 3.0
    else:
        return 3.6


def _rate_per_cc_over_5_years(engine_cc: int) -> float:
    """Ставка евро/см3 для авто старше 5 лет."""
    if engine_cc <= 1000:
        return 3.0
    elif engine_cc <= 1500:
        return 3.2
    elif engine_cc <= 1800:
        return 3.5
    elif engine_cc <= 2300:
        return 4.8
    elif engine_cc <= 3000:
        return 5.0
    else:
        return 5.7


def calculate_customs_duty_eur(value_eur: float, engine_cc: int, age_years: int) -> float:
    """Таможенная пошлина в евро по таблице для физлиц."""
    if engine_cc <= 0:
        return 0.0

    if age_years < 3:
        return _duty_under_3_years(value_eur, engine_cc)
    elif age_years <= 5:
        return engine_cc * _rate_per_cc_3_to_5_years(engine_cc)
    else:
        return engine_cc * _rate_per_cc_over_5_years(engine_cc)


def get_state_fee_usd(state: str) -> int:
    """Сбор за штат отправки (USD). Пока заглушка $100 для всех."""
    return STATE_FEES_USD.get(state, DEFAULT_STATE_FEE_USD)


def calculate_total_customs(price_usd: float, engine_str, title_or_year,
                            location_str, usd_to_eur: float = None) -> dict:
    """Главная функция расчёта.

    Принимает сырые данные из парсера, возвращает детальный расчёт.
    Курс USD->EUR по умолчанию берётся в реальном времени (yfinance),
    с откатом на значение USD_TO_EUR из .env при сбое сети.

    Возвращает словарь:
        engine_cc, year, age_years, state, state_fee_usd,
        value_eur, duty_eur, duty_usd, total_usd
    """
    if usd_to_eur is None:
        usd_to_eur = get_usd_to_eur(USD_TO_EUR)

    engine_cc = parse_engine_cc(engine_str)
    year = parse_year(title_or_year)
    age_years = get_vehicle_age(year)
    state = parse_state(location_str)

    value_eur = price_usd * usd_to_eur
    duty_eur = calculate_customs_duty_eur(value_eur, engine_cc, age_years)
    duty_usd = duty_eur / usd_to_eur if usd_to_eur else 0.0

    state_fee_usd = get_state_fee_usd(state)
    total_usd = duty_usd + state_fee_usd

    return {
        "engine_cc": engine_cc,
        "year": year,
        "age_years": age_years,
        "state": state,
        "state_fee_usd": state_fee_usd,
        "value_eur": round(value_eur, 2),
        "duty_eur": round(duty_eur, 2),
        "duty_usd": round(duty_usd, 2),
        "total_usd": round(total_usd, 2),
    }


if __name__ == "__main__":
    # Быстрый тест
    examples = [
        # price_usd, engine, title, location
        (10000, "2.5L", "2019 TESLA MODEL 3", "CT - HARTFORD SPRINGFIELD"),
        (5000, "1.6L", "2024 TOYOTA COROLLA", "CA - LOS ANGELES"),
        (8000, "3.5L", "2010 NISSAN PATHFINDER", "TX - DALLAS"),
    ]
    for price, eng, ttl, loc in examples:
        res = calculate_total_customs(price, eng, ttl, loc)
        print(f"\n{ttl} | {eng} | {loc} | ${price}")
        for k, v in res.items():
            print(f"  {k}: {v}")
