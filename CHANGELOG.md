# Changelog

## [0.3.0] — 2026-06-13

### Telegram-бот + торговый робот

#### Добавлено

- `main_bot.py` — единая точка входа: Telegram-бот и робот запускаются в одном `asyncio` event loop
- `bot/` — новый пакет:
  - `database.py` — асинхронное SQLite-хранилище настроек пользователей (`aiosqlite`); хранит символы, таймфрейм, режим, все параметры стратегии, плечо, размер сделки
  - `exchange.py` — обёртка Binance Futures REST API: публичные методы (котировки, свечи, список символов) и авторизованные торговые методы (open long/short, stop-loss, close position)
  - `notifier.py` — отправка уведомлений в Telegram-канал: сигналы, открытие/закрытие сделок, ошибки
  - `robot.py` — `RobotManager`: каждый символ — отдельная `asyncio.Task`; загрузка 1000 свечей, ожидание закрытия свечи по таймеру, расчёт BB/RSI/ADX, управление позицией (BB Middle выход, стоп-лосс)
  - `tg_bot.py` — Telegram-бот (aiogram 3.x): главное меню, FSM-диалоги, выбор монет (топ-10/20/50/100 или вручную), таймфрейм, режим (сигнальный / торговый), параметры стратегии, статус
  - `analytics_import.py` — парсер `optimization_results/`: загружает `best_params.json`, позволяет импортировать параметры и символ одной кнопкой в боте
- `requirements.txt` — зафиксированы все зависимости проекта
- `.env.example` — шаблон переменных окружения (реальные ключи в `.env`, который не попадает в git)

#### Безопасность

- `.env` добавлен в `.gitignore` — токены и API-ключи не попадают в репозиторий
- `logs/` добавлен в `.gitignore` — логи не отслеживаются

#### Изменено

- `README.md` — добавлены разделы: Telegram-бот, установка через `requirements.txt`, обновлена структура проекта

---

## [0.2.0] — 2026-06-12

### Рефакторинг: переход на пакетную структуру

Плоские файлы в корне проекта перемещены в тематические пакеты:

| Старый файл        | Новое расположение                  |
|--------------------|-------------------------------------|
| `signal_settings.py` | `config/settings.py`              |
| `candles.py`         | `feed/candles.py`                 |
| `signals.py`         | `strategy/signals.py`             |
| `backtest.py`        | `strategy/backtest.py`            |
| `optimize.py`        | `optimization/optimizer.py`       |
| `analyze_trades.py`  | `analysis/analyze_trades.py`      |

### Новое в `feed/candles.py`

- Загрузка свечей переведена на **`aiohttp` + `asyncio`** — параллельное скачивание нескольких символов одновременно
- Добавлен **token-bucket rate limiter** (`_RateLimiter`): 3 req/s, burst 5 — соблюдает лимиты Binance Futures (2400 weight/min)
- Добавлен **`asyncio.Semaphore`** — не более 5 параллельных соединений
- Обработка ответов `429` (rate limit) и `418` (IP ban) с автоматическим `Retry-After`
- Exponential backoff при сетевых ошибках (до 5 попыток)
- Новая функция `download_many_symbols_async()` — параллельная загрузка списка символов
- Синхронные обёртки `get_df()` и `download_candles()` сохранены для обратной совместимости
- Фикс для Windows: принудительное переключение на `WindowsSelectorEventLoopPolicy` (совместимость `aiohttp` + SSL)

### Обновлён `main.py`

- Переписан как полноценная точка входа: сканирует топ-N монет и выводит сигналы
- Поддержка флагов: `--symbol`, `--download`, `--filepath`

### Добавлено

- `diagnose_network.py` — утилита диагностики: DNS, proxy, `requests`, `aiohttp`, env-переменные
- `config/__init__.py`, `feed/__init__.py`, `strategy/__init__.py`, `optimization/__init__.py`, `analysis/__init__.py`

### Обновлено

- `README.md` — приведён в соответствие с новой структурой пакетов и командами запуска
- `.gitignore` — добавлена маска `klines_*/` для папок с данными произвольных таймфреймов

### Проверено

- Асинхронная загрузка топ-50 монет, таймфрейм `15m`, период 2026-06-01 — 2026-06-12: **50/50 символов**, 1 128 свечей каждый, без ошибок и без 429

---

## [0.1.0] — 2026-06-11

### Первоначальный коммит

- Стратегия BB Mean Reversion с фильтрами RSI + ADX (Backtrader)
- Загрузчик свечей Binance Futures (`candles.py`)
- Оптимизатор Optuna с walk-forward валидацией (`optimize.py`)
- TradingView-график на Plotly (`backtest.py`)
- Сканер сигналов (`signals.py`)
- Исторические данные: ~55 пар, 1h, 2022-01-01 — 2026-05-19
