# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Скачать исторические данные (Binance Futures API → klines/*.csv)
python feed/candles.py

# Запустить бэктест (по умолчанию ETHUSDT, меняется переменной SYMBOL в конце файла)
python strategy/backtest.py

# Сканирование сигналов по топ-N монетам
python main.py
python main.py --download          # скачать данные и сразу сканировать
python main.py --symbol BTCUSDT    # только одна монета

# Оптимизация параметров (Optuna + walk-forward)
python optimization/optimizer.py --symbol BTCUSDT --trials 200
python optimization/optimizer.py --symbol ETHUSDT --trials 500 --walk-forward --wf-folds 5
python optimization/optimizer.py --trials 1000 --storage sqlite:///study.db

# Запуск Telegram-бота + робота (требует .env)
python main_bot.py

# Анализ лога сделок
python analysis/analyze_trades.py trades_BTCUSDT_1h.csv
```

### Зависимости

```bash
pip install -r requirements.txt
```

Python 3.10+. Виртуальное окружение: `.venv/`.

### Переменные окружения

Скопировать `.env.example` → `.env` и заполнить:
- `BOT_TOKEN` — токен от @BotFather
- `TG_CHANNEL_ID` — ID/username канала для сигналов
- `BINANCE_API_KEY`, `BINANCE_API_SECRET` — только для торгового режима

---

## Архитектура

### Два режима работы

**CLI-режим** (`main.py`, `strategy/backtest.py`) — синхронный, читает CSV из `klines/`, не требует `.env`.

**Bot-режим** (`main_bot.py`) — асинхронный event loop (aiogram + asyncio), бот и робот работают в одном процессе. При старте инициализируется SQLite БД и сбрасывается `is_running` у всех пользователей (задачи в памяти не выживают между перезапусками).

### Потоки данных

```
Binance Futures API (fapi.binance.com)
    └── feed/candles.py (aiohttp async) → klines/*.csv (OHLCV 1h, ~55 пар)
            └── strategy/signals.py (get_signals)     ← main.py (CLI)
            └── strategy/backtest.py (Backtrader)     ← ручной запуск
            └── bot/exchange.py (get_candles)         ← bot/robot.py (live)
```

### Пакеты

| Пакет | Назначение |
|-------|-----------|
| `bot/exchange.py` | `BinanceExchange` — публичные + торговые методы Binance Futures REST. Публичные (свечи, символы) работают без ключей. Торговые методы подписывают запросы HMAC-SHA256. |
| `bot/robot.py` | `RobotManager` — каждый символ — отдельный asyncio.Task. Цикл: ждать закрытия свечи → скачать → пересчитать → сигнал → действие. Позиции хранятся in-memory в `_positions`. |
| `bot/database.py` | SQLite через aiosqlite. Одна таблица `user_settings`. Настройки каждого Telegram-пользователя независимы. `symbols` хранится как JSON-строка. |
| `bot/tg_bot.py` | Telegram-бот (aiogram 3.x), FSM для диалогов. |
| `bot/notifier.py` | Отправка форматированных сообщений в канал. |
| `bot/analytics_import.py` | Парсер `optimization_results/` для импорта лучших параметров в бот. |
| `strategy/backtest.py` | `SimpleMeanReversionStrategy` (Backtrader). Выход: `BB_MID` (цена достигла средней BB) или `FIXED_SL` (стоп-лосс ATR×mult). Генерирует HTML-график (Plotly, TradingView-стиль) и `trades_*.csv`. |
| `strategy/signals.py` | `get_signals()` — синхронный, для CLI-сканера. |
| `optimization/optimizer.py` | Optuna (TPE/CMA-ES/NSGA-II). Цель по умолчанию — `composite` (взвешенная сумма Sharpe + Sortino + Calmar + PF + SQN). Результаты → `optimization_results/SYMBOL_TIMESTAMP/`. |
| `feed/candles.py` | Асинхронный загрузчик OHLCV с Binance Futures. `download_many_symbols_async` — параллельная загрузка. Список топ-N монет через CoinGecko + фильтрация по Binance Futures. |
| `config/settings.py` | Параметры стратегии для CLI-режима (не используются ботом — у него настройки в SQLite). |

### Логика стратегии

Вход: `Close < BB_lower AND RSI < 30 AND ADX < 30` (LONG) / зеркально (SHORT).  
Выход: цена закрылась за средней BB (`BB_MID`) или сработал стоп (`FIXED_SL` = `entry ± sl_atr_mult × ATR`).  
Стратегия торгует только на боковых рынках (`ADX < adx_threshold`).

### Windows-специфика

В `main_bot.py` явно устанавливается `WindowsSelectorEventLoopPolicy` — обязательно для aiohttp/SSL на Windows.

### Файлы данных (не в git)

- `klines/` — исторические CSV (1h, ~55 пар, 2022–2026)
- `data/` — CSV для других таймфреймов + `settings.db` (SQLite)
- `optimization_results/` — результаты оптимизатора
- `logs/` — лог робота (`robot.log`)
