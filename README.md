# BB Mean Reversion — RSI + ADX

Алгоритмическая торговая стратегия на основе возврата к среднему (mean reversion) с фильтрами RSI и ADX. Разработана для крипто-фьючерсов Binance, таймфрейм 1h.

Включает **Telegram-бота** (aiogram 3.x) для управления роботом в реальном времени: выбор монет, таймфрейм, сигнальный / торговый режим, импорт параметров из оптимизатора.

---

## Что делает стратегия

Стратегия ищет моменты, когда цена **чрезмерно отклонилась** от скользящего среднего и вероятен откат обратно к центру. Торгует в обе стороны (лонг и шорт) только на боковых рынках.

### Логика входа

| Направление | Условия |
|-------------|---------|
| **LONG**  | `Close < BB_lower` AND `RSI < 30` AND `ADX < 30` |
| **SHORT** | `Close > BB_upper` AND `RSI > 70` AND `ADX < 30` |

### Логика выхода

| Причина    | Описание |
|------------|---------|
| `BB_MID`   | Цена закрытия достигла средней линии Боллинджера — закрытие по рынку |
| `FIXED_SL` | Цена пробила стоп-лосс — закрытие стоп-ордером |

### Параметры по умолчанию (задаются через Telegram-бот или `config/settings.py`)

| Параметр        | Значение | Описание |
|-----------------|----------|---------|
| `bb_lenth`      | 20       | Период Bollinger Bands |
| `bb_std`        | 2.0      | Множитель стандартного отклонения BB |
| `rsi_len`       | 14       | Период RSI |
| `adx_len`       | 14       | Период ADX |
| `adx_threshold` | 30       | Максимальный ADX для входа (боковик) |
| `interval`      | `1h`     | Таймфрейм |
| `top_by_cap`    | 100      | Количество монет по капитализации |
| `commission`    | 0.1%     | Комиссия на сторону |

---

## Что показывает график

После бэктеста открывается интерактивный HTML-график в стиле TradingView (тёмная тема).

### Панель 1 — Цена
- **Свечи** — зелёные (бычьи) / красные (медвежьи)
- **Синие линии** — верхняя и нижняя полоса Боллинджера с полупрозрачной заливкой
- **Оранжевая пунктирная линия** — средняя линия BB (цель выхода)
- **▲ Зелёный треугольник вверх** — открытие лонга
- **▼ Красный треугольник вниз** — открытие шорта
- **× Зелёный крестик** — прибыльный выход
- **× Красный крестик** — убыточный выход
- **Красная пунктирная линия** — уровень стоп-лосса (от входа до выхода)

### Панель 2 — RSI (14)
- Линия RSI с уровнями 30 / 50 / 70

### Панель 3 — ADX (14)
- Линия ADX с порогом 30

### Панель 4 — Кривая капитала
- Накопленный P&L с начального депозита ($1 500)

> **Навигация:** колесо мыши — зум, перетащить — прокрутка, двойной клик — сброс.  
> При наведении на маркеры выхода — всплывает направление, причина и P&L сделки.

---

## Структура проекта

```
.
├── main.py                      # Сканер сигналов по топ-N монетам (CLI)
├── main_bot.py                  # Точка входа Telegram-бота + робота
├── diagnose_network.py          # Утилита диагностики сетевого доступа
├── requirements.txt             # Зависимости проекта
├── .env.example                 # Шаблон переменных окружения
│
├── bot/
│   ├── database.py              # SQLite-хранилище настроек (aiosqlite, async)
│   ├── exchange.py              # Обёртка Binance Futures REST API
│   ├── notifier.py              # Отправка уведомлений в Telegram-канал
│   ├── robot.py                 # RobotManager: цикл по символам, сигналы, торговля
│   ├── tg_bot.py                # Telegram-бот (aiogram 3.x): меню, FSM, хендлеры
│   └── analytics_import.py     # Парсер результатов оптимизатора для импорта в бот
│
├── config/
│   └── settings.py              # Параметры стратегии для CLI-режима
│
├── feed/
│   └── candles.py               # Асинхронный загрузчик Binance Futures + CSV-хелперы
│
├── strategy/
│   ├── signals.py               # get_signals() — сканер сигналов (live)
│   └── backtest.py              # SimpleMeanReversionStrategy (Backtrader) + TradingView-график
│
├── optimization/
│   └── optimizer.py             # Оптимизатор Optuna с walk-forward валидацией
│
├── analysis/
│   └── analyze_trades.py        # Анализ лога сделок (CSV)
│
├── klines/                      # Исторические OHLCV CSV — 1h, ~55 пар, 2022–2026 (не в git)
└── data/                        # Дополнительные CSV — 15m и другие TF (не в git)
```

---

## Быстрый старт

### 1. Клонировать репозиторий

```bash
git clone https://github.com/algorobotix/BB_mean_reversion_rsi_adx.git
cd BB_mean_reversion_rsi_adx
```

### 2. Создать виртуальное окружение

```bash
# Windows
python -m venv .venv
.venv\Scripts\activate

# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Установить зависимости

```bash
pip install -r requirements.txt
```

### 4. Скачать исторические данные

Данные скачиваются с Binance Futures API асинхронно и сохраняются в папку `klines/`.  
Настройте `config/settings.py` (период, таймфрейм, количество монет), затем запустите:

```bash
python feed/candles.py
```

Скрипт получит топ-N монет по капитализации (CoinGecko + Binance) и параллельно скачает OHLCV-историю для каждой.

Для одного символа или произвольного таймфрейма — напрямую из Python:

```python
from feed.candles import download_many_symbols_async
import asyncio

async def run():
    results = await download_many_symbols_async(
        ['BTCUSDT', 'ETHUSDT'], interval='15m',
        start_date='2026-01-01', end_date='2026-06-12',
        filepath='klines_15m', save=True,
    )

asyncio.run(run())
```

### 5. Запустить бэктест

```bash
python strategy/backtest.py
```

По умолчанию запускается на `ETHUSDT`. Чтобы сменить символ — измените переменную `SYMBOL` в конце `backtest.py`:

```python
if __name__ == '__main__':
    SYMBOL = 'BTCUSDT'   # любой символ из папки klines/
```

После запуска:
- В терминале — метрики (депозит, просадка, Шарп, винрейт)
- В браузере — интерактивный график
- В папке проекта — `backtest_SYMBOL_1h.html` и `trades_SYMBOL_1h.csv`

### 6. Сканировать сигналы

```bash
# Сигналы из локальных CSV
python main.py

# Скачать свежие данные и сразу сканировать
python main.py --download

# Только одна монета
python main.py --symbol BTCUSDT
```

---

## Telegram-бот

`main_bot.py` запускает робота в паре с Telegram-ботом в одном event loop.

### Настройка

```bash
# Скопировать шаблон и заполнить значения
cp .env.example .env
```

Переменные в `.env`:

| Переменная          | Описание |
|---------------------|---------|
| `BOT_TOKEN`         | Токен от @BotFather |
| `TG_CHANNEL_ID`     | ID или @username канала для сигналов |
| `BINANCE_API_KEY`   | Ключ Binance Futures (только для торгового режима) |
| `BINANCE_API_SECRET`| Секрет Binance Futures (только для торгового режима) |

### Запуск

```bash
python main_bot.py
```

### Возможности бота

| Пункт меню            | Описание |
|-----------------------|---------|
| 🪙 Выбор монет        | Топ-10/20/50/100 по капитализации или ввод тикеров вручную |
| ⏱ Таймфрейм          | 1m, 5m, 15m, 30m, 1h, 4h, 1d |
| ⚙️ Режим работы       | **Сигнальный** — только уведомления; **Торговый** — открывает сделки на Binance |
| 📊 Параметры стратегии| BB / RSI / ADX / SL×ATR / Trade USDT / Leverage |
| 📥 Импорт из аналитики| Загружает лучшие параметры из `optimization_results/` |
| ▶️ / ⏹ Старт / Стоп  | Запуск и остановка робота |
| 📈 Статус             | Текущее состояние всех символов |

Настройки каждого пользователя сохраняются в SQLite (`data/settings.db`).

---

## Оптимизация параметров

`optimization/optimizer.py` перебирает комбинации параметров через Optuna и проверяет на out-of-sample данных.

```bash
# Базовый запуск (200 итераций, BTCUSDT)
python optimization/optimizer.py --symbol BTCUSDT --trials 200

# С walk-forward валидацией
python optimization/optimizer.py --symbol ETHUSDT --trials 500 --walk-forward --wf-folds 5

# Сохранить study в БД для продолжения
python optimization/optimizer.py --trials 1000 --storage sqlite:///study.db
```

**Доступные цели оптимизации** (`--objective`):

| Значение       | Описание |
|----------------|---------|
| `composite`    | Взвешенная сумма Sharpe + Sortino + Calmar + PF + SQN *(по умолчанию)* |
| `sharpe`       | Коэффициент Шарпа |
| `sortino`      | Коэффициент Сортино |
| `calmar`       | Calmar ratio |
| `sqn`          | System Quality Number |
| `profit_factor`| Profit Factor |

Результаты сохраняются в `optimization_results/SYMBOL_TIMESTAMP/`:
- `best_params.json` — лучшие параметры и метрики
- `report.txt` — текстовый отчёт с детектором переобучения
- `all_trials.csv` — все итерации
- `*.html` — интерактивные графики Optuna

---

## Анализ сделок

```bash
python analysis/analyze_trades.py trades_BTCUSDT_1h.csv
```

Читает CSV-лог сделок и выводит статистику по стоп-лоссам, win/loss ratio, средним длительностям.

---

## Результаты бэктеста (дефолтные параметры)

| Символ  | Период    | Сделок | Винрейт | Итог    | Макс. просадка | Шарп  |
|---------|-----------|--------|---------|---------|----------------|-------|
| BTCUSDT | 2022–2026 | 369    | 46.1%   | $1 235  | 18.3%          | −1.89 |
| ETHUSDT | 2022–2026 | 297    | 50.8%   | $1 339  | 19.6%          | −0.31 |

> Начальный депозит: $1 500. Стратегия убыточна на дефолтных параметрах — используйте `optimization/optimizer.py` для подбора.

---

## Требования

- Python 3.10+
- Интернет-соединение для скачивания данных (Binance Futures API, CoinGecko API)
- Для Telegram-бота: `aiogram>=3.7`, `aiosqlite>=0.20`, `python-dotenv>=1.0` (входят в `requirements.txt`)
