# Changelog

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
