"""Курс USD -> EUR в реальном времени через yfinance.

Курс берётся с Yahoo Finance (тикер USDEUR=X = сколько EUR за 1 USD),
кэшируется на час, чтобы не дёргать сеть на каждый расчёт. При любой
ошибке (нет интернета / Yahoo недоступен) используется fallback из .env.
"""

import logging
import threading
import time

logger = logging.getLogger(__name__)

# Время жизни кэша курса (секунды)
_CACHE_TTL = 3600  # 1 час при успехе
_RETRY_TTL = 300   # 5 минут — не дёргать сеть после неудачи

_cache = {"rate": None, "ts": 0.0}
_last_fail_ts = 0.0
_lock = threading.Lock()


def _fetch_rate():
    """Запрашивает свежий курс USD->EUR с Yahoo Finance."""
    try:
        import yfinance as yf
        data = yf.Ticker("USDEUR=X").history(period="1d")
        if not data.empty:
            return float(data["Close"].iloc[-1])
        logger.warning("yfinance вернул пустые данные по USDEUR=X")
    except Exception as e:
        logger.warning(f"yfinance не смог получить курс USD->EUR: {e}")
    return None


def get_usd_to_eur(fallback: float) -> float:
    """Возвращает актуальный курс «сколько EUR за 1 USD».

    Логика:
    1. Если в кэше есть свежее значение (моложе _CACHE_TTL) — отдаём его.
    2. Иначе пробуем получить новый курс с Yahoo Finance.
    3. Если не вышло — отдаём последний известный курс, а если его нет —
       fallback (значение USD_TO_EUR из .env).
    """
    global _last_fail_ts
    now = time.time()
    with _lock:
        if _cache["rate"] and (now - _cache["ts"] < _CACHE_TTL):
            return _cache["rate"]
        # Недавно была неудача — не дёргаем сеть, отдаём что есть
        if (now - _last_fail_ts) < _RETRY_TTL:
            return _cache["rate"] or fallback

    rate = _fetch_rate()
    if rate and rate > 0:
        with _lock:
            _cache["rate"] = rate
            _cache["ts"] = now
        logger.info(f"Актуальный курс USD->EUR: {rate:.4f}")
        return rate

    with _lock:
        _last_fail_ts = now
        if _cache["rate"]:
            return _cache["rate"]

    logger.warning(f"Курс USD->EUR не получен, используется fallback из .env: {fallback}")
    return fallback


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("USD->EUR:", get_usd_to_eur(0.92))
