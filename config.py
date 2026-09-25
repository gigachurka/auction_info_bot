"""Конфигурация бота. Значения берутся из .env / переменных окружения."""

import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _get_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# Telegram bot token (обязательно)
TOKEN = os.environ.get("BOT_TOKEN", "")

# Запасной курс USD -> EUR. Основной курс берётся в реальном времени (exchange.py),
# это значение используется только при недоступности Yahoo Finance.
USD_TO_EUR = _get_float("USD_TO_EUR",1.14)

# Сбор за штат отправки по умолчанию (USD)
DEFAULT_STATE_FEE_USD = _get_float("DEFAULT_STATE_FEE_USD", 100)

# Сервисный сбор компании (USD)
SERVICE_FEE_USD = _get_float("SERVICE_FEE_USD", 0)

# Прокси для Chrome (Copart из РФ без VPN обычно режется Incapsula).
# Примеры:
#   PROXY_URL=socks5://127.0.0.1:1080
#   PROXY_URL=http://127.0.0.1:7890
# Если используете системный VPN (WireGuard/OpenVPN) — оставьте пустым,
# просто включите VPN до запуска бота.
PROXY_URL = (os.environ.get("PROXY_URL") or "").strip()

# PostgreSQL для кэша справочника марок/моделей (модуль car_finder.py).
# Пример: postgresql://user:password@localhost:5432/parcerbot
# Если не задан — справочник кэшируется в памяти до перезапуска.
DATABASE_URL = (os.environ.get("DATABASE_URL") or "").strip()