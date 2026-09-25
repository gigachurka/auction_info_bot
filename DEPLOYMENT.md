# Развёртывание бота на сервере

Инструкция по установке и настройке Telegram-бота на сервере (Ubuntu/Debian Linux).

---

## 1. Состав проекта

Для работы нужны только эти файлы:

| Файл | Назначение |
|------|------------|
| `bot.py` | Основной файл бота (точка входа) |
| `car_finder.py` | Модуль подбора авто (FSM: марка → модель → лот, кэш в PostgreSQL) |
| `parser.py` | Парсер Copart |
| `iaai_parser.py` | Парсер IAAI |
| `customs.py` | Расчёт растаможки РБ |
| `chrome_utils.py` | Автоопределение версии Chrome |
| `config.py` | Загрузка настроек из `.env` |
| `requirements.txt` | Список зависимостей Python |
| `.env.example` | Шаблон файла с настройками |
| `iaai_cookies.json` | Cookies сессии IAAI (опционально, см. п.6) |

Файл `.env` (с реальными значениями) вы создаёте сами на сервере — **в репозиторий его коммитить нельзя**.

---

## 2. Требования к серверу

- **ОС:** Ubuntu 22.04 / Debian 12 (или аналог)
- **Python:** 3.10+
- **Google Chrome:** обязателен, т.к. парсинг идёт через браузер (Selenium + undetected-chromedriver)
- **ОЗУ:** минимум 1 ГБ (Chrome прожорлив), рекомендуется 2 ГБ

### Установка системных пакетов

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip wget curl
```

### Установка Google Chrome

```bash
wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt install -y ./google-chrome-stable_current_amd64.deb
google-chrome --version   # проверка: должна вывести версию, например 149.x.x.x
```

> **Важно:** `chrome_utils.py` сам определяет версию Chrome и подбирает драйвер.
> Ничего вручную прописывать не нужно. Если Chrome обновится — драйвер подстроится автоматически.

---

## 3. Загрузка проекта на сервер

Вариант через git:
```bash
git clone <url-вашего-репозитория> parcerbot
cd parcerbot
```

Или скопируйте файлы вручную (scp/SFTP) в папку, например `/home/<user>/parcerbot`.

---

## 4. Установка зависимостей Python

```bash
cd parcerbot
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

---

## 5. Настройка `.env` (ГЛАВНОЕ)

Скопируйте шаблон и заполните своими значениями:

```bash
cp .env.example .env
nano .env
```

Содержимое `.env`:

```env
# ===== Telegram =====
# Токен бота от @BotFather (ОБЯЗАТЕЛЬНО)
BOT_TOKEN=8661655083:AAFmr-XXXXXXXXXXXXXXXXXXXXXXXXXXXX

# ===== Расчёт растаможки =====
# Курс конвертации цены авто USD -> EUR (таблица таможни в евро)
USD_TO_EUR=0.92

# Сбор за штат отправки по умолчанию, USD
DEFAULT_STATE_FEE_USD=100

# Сервисный сбор компании, USD
SERVICE_FEE_USD=0

# ===== Модуль подбора авто (car_finder) =====
# PostgreSQL для кэша справочника марок/моделей.
# Если не задан — кэш работает только в памяти (до перезапуска).
DATABASE_URL=postgresql://user:password@localhost:5432/parcerbot

# Как часто обновлять справочник марок/моделей с сайтов аукционов (дней)
CATALOG_TTL_DAYS=7
```

### Что куда вписывать

| Переменная | Обязательно | Что это | Пример |
|------------|:-----------:|---------|--------|
| `BOT_TOKEN` | **Да** | Токен Telegram-бота. Получить у [@BotFather](https://t.me/BotFather): `/newbot` или `/token` | `8661655083:AAF...` |
| `USD_TO_EUR` | Нет | Курс доллара к евро для пересчёта таможни. Таблица пошлин в евро, цена авто в долларах | `0.92` |
| `DEFAULT_STATE_FEE_USD` | Нет | Сбор за доставку из штата (пока единый для всех штатов) | `100` |
| `SERVICE_FEE_USD` | Нет | Наценка/сбор вашей компании, добавляется к стоимости доставки | `0` |
| `DATABASE_URL` | Нет | PostgreSQL 连接串，用于 car_finder 的 марки/модели 缓存。不配则用内存缓存 | `postgresql://user:pass@localhost:5432/parcerbot` |
| `CATALOG_TTL_DAYS` | Нет | Справочник марок/моделей 多久强制刷新一次（天） | `7` |

> Если необязательные переменные не указать — возьмутся значения по умолчанию из таблицы выше.
> Бот **не запустится** без `BOT_TOKEN` и выдаст понятную ошибку.

---

## 6. Сессия IAAI (для парсинга IAAI)

IAAI защищён Incapsula/CAPTCHA. Бот использует **постоянный профиль Chrome**
`chrome_profiles/iaai/` — куки и fingerprint сохраняются на диске, поэтому
проверку нужно пройти **один раз**:

- При первом запросе к IAAI откроется видимое окно Chrome — пройдите CAPTCHA
  вручную (до 3 мин). После этого профиль «прогрет» и бот работает headless.
- Если IAAI снова просит проверку (сменился IP/прокси, протухла сессия) —
  окно откроется автоматически ещё раз. Файл `iaai_cookies.json` служит
  флагом «профиль прогрет»; его удаление заставит бота снова открыть
  видимое окно.
- Каталог `chrome_profiles/` содержит сессионные данные — не коммитьте его
  (уже в `.gitignore`) и не удаляйте между рестартами.
- Для **только Copart** это не нужно.

---

## 7. Запуск

Проверочный запуск (вручную):
```bash
source venv/bin/activate
python bot.py
```
В логах должно появиться `Starting bot...` и `Run polling for bot @...`.
Остановить — `Ctrl+C`.

---

## 8. Автозапуск через systemd (рекомендуется)

Чтобы бот работал постоянно и перезапускался после сбоев/перезагрузки.

Создайте сервис:
```bash
sudo nano /etc/systemd/system/parcerbot.service
```

Вставьте (замените `<user>` и пути на свои):
```ini
[Unit]
Description=ParcerBot Telegram Bot
After=network.target

[Service]
Type=simple
User=<user>
WorkingDirectory=/home/<user>/parcerbot
ExecStart=/home/<user>/parcerbot/venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Активируйте:
```bash
sudo systemctl daemon-reload
sudo systemctl enable parcerbot
sudo systemctl start parcerbot
```

Управление:
```bash
sudo systemctl status parcerbot     # статус
sudo systemctl restart parcerbot    # перезапуск (например, после правки .env)
sudo systemctl stop parcerbot       # остановить
journalctl -u parcerbot -f          # смотреть логи в реальном времени
```

> После любой правки `.env` нужно перезапустить сервис: `sudo systemctl restart parcerbot`.

---

## 9. Обновление настроек растаможки

- **Курс, сборы** — меняются в `.env`, затем перезапуск сервиса.
- **Индивидуальные сборы по штатам** — пока единый сбор `DEFAULT_STATE_FEE_USD`.
  Когда появятся цены по конкретным штатам, их вписывают в словарь `STATE_FEES_USD`
  в файле `customs.py`, например: `STATE_FEES_USD = {"CT": 120, "CA": 150}`.

---

## 10. Возможные проблемы

| Симптом | Причина / решение |
|---------|-------------------|
| `BOT_TOKEN не задан` | Не создан/не заполнен `.env`. См. п.5 |
| `session not created: ... only supports Chrome version X` | Версия драйвера не совпала с Chrome. Обычно решается само (`chrome_utils.py`). Убедитесь, что Chrome установлен: `google-chrome --version` |
| `cannot connect to chrome` | На сервере нет Chrome или не хватает памяти. Установите Chrome (п.2), добавьте swap |
| IAAI не парсится | Устарели cookies. См. п.6 |
| Бот молчит | Проверьте логи: `journalctl -u parcerbot -f` |

---

## Краткая шпаргалка (по шагам)

```bash
# 1. система + chrome
sudo apt update && sudo apt install -y python3 python3-venv python3-pip wget
wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt install -y ./google-chrome-stable_current_amd64.deb

# 2. проект
cd ~/parcerbot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 3. настройки
cp .env.example .env && nano .env      # вписать BOT_TOKEN

# 4. запуск (тест)
python bot.py
```
