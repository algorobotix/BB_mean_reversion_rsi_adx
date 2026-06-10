# BB Mean Reversion — RSI + ADX

Алгоритмическая торговая стратегия на основе возврата к среднему (mean reversion) с фильтрами RSI и ADX. Разработана для крипто-фьючерсов Binance, таймфрейм 1h.

---

## Что делает стратегия

Стратегия ищет моменты, когда цена **чрезмерно отклонилась** от скользящего среднего и вероятен откат обратно к центру. Торгует в обе стороны (лонг и шорт) только на боковых рынках.

### Логика входа

| Направление | Условия |
|-------------|---------|
| **LONG** | `Close < BB_lower` AND `RSI < 30` AND `ADX < 30` |
| **SHORT** | `Close > BB_upper` AND `RSI > 70` AND `ADX < 30` |

### Логика выхода

| Причина | Описание |
|---------|---------|
| `BB_MID` | Цена закрытия достигла средней линии Боллинджера — закрытие по рынку |
| `FIXED_SL` | Цена пробила стоп-лосс — закрытие стоп-ордером |

### Параметры по умолчанию (`signal_settings.py`)

| Параметр | Значение | Описание |
|----------|----------|---------|
| `bb_lenth` | 20 | Период Bollinger Bands |
| `bb_std` | 2.0 | Множитель стандартного отклонения BB |
| `rsi_len` | 14 | Период RSI |
| `adx_len` | 14 | Период ADX |
| `adx_threshold` | 30 | Максимальный ADX для входа (боковик) |
| `interval` | `1h` | Таймфрейм |
| `sl_atr_mult` | 2.5 | Множитель ATR для стоп-лосса |
| `trade_usdt` | 150 | Размер позиции в USDT |
| `commission` | 0.1% | Комиссия на сторону |

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
├── backtest.py          # Стратегия Backtrader + TradingView-график (Plotly)
├── optimize.py          # Оптимизатор Optuna с walk-forward валидацией
├── candles.py           # Загрузчик данных с Binance Futures и CSV-ридер
├── signal_settings.py   # Все настраиваемые параметры стратегии
├── analyze_trades.py    # Анализ лога сделок после бэктеста
├── works.ipynb          # Исследовательский notebook
├── bb_mean_reversion_rsi_adx.pine  # Эквивалент стратегии на Pine Script (TradingView)
├── main.py              # Заготовка под живой бот (пока пустая)
├── signals.py           # Заготовка под сканер сигналов (пока пустая)
└── klines/              # Исторические OHLCV CSV (не в git — создаётся локально)
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
pip install backtrader pandas numpy plotly requests pandas_ta optuna
```

### 4. Скачать исторические данные

Данные скачиваются с Binance Futures API и сохраняются в папку `klines/`.  
Откройте `signal_settings.py` и при необходимости измените `start_date`, `end_date`, `interval`.

Затем запустите `candles.py`:

```bash
python candles.py
```

Скрипт получит топ-100 монет по капитализации и скачает для каждой OHLCV-историю. Для одного символа вручную:

```python
# В Python-консоли или notebook
from candles import get_df, save_df_to_csv
from signal_settings import interval, start_date, end_date

df = get_df('BTCUSDT', interval, start_date, end_date)
save_df_to_csv(df, 'BTCUSDT', interval, start_date, end_date)
```

### 5. Запустить бэктест

```bash
python backtest.py
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

---

## Оптимизация параметров

`optimize.py` перебирает комбинации параметров через Optuna и проверяет на out-of-sample данных.

```bash
# Базовый запуск (200 итераций, BTCUSDT)
python optimize.py

# С walk-forward валидацией
python optimize.py --symbol ETHUSDT --trials 500 --walk-forward --wf-folds 5

# Сохранить study в БД для продолжения
python optimize.py --trials 1000 --storage sqlite:///study.db
```

**Доступные цели оптимизации** (`--objective`):

| Значение | Описание |
|----------|---------|
| `composite` | Взвешенная сумма Sharpe + Sortino + Calmar + PF + SQN *(по умолчанию)* |
| `sharpe` | Коэффициент Шарпа |
| `sortino` | Коэффициент Сортино |
| `calmar` | Calmar ratio |
| `sqn` | System Quality Number |
| `profit_factor` | Profit Factor |

Результаты сохраняются в `optimization_results/SYMBOL_TIMESTAMP/`:
- `best_params.json` — лучшие параметры и метрики
- `report.txt` — текстовый отчёт с детектором переобучения
- `all_trials.csv` — все итерации
- `*.html` — интерактивные графики Optuna

---

## Анализ сделок

```bash
python analyze_trades.py
```

Читает `trades_BTCUSDT_1h.csv` и выводит статистику по стоп-лоссам, win/loss ratio, средним длительностям сделок.

---

## Сканер сигналов (live)

```python
from candles import read_df_from_csv, get_signals
from signal_settings import interval

df = read_df_from_csv('BTCUSDT', interval=interval)
signal = get_signals(df, 'BTCUSDT')   # → "Покупка" / "Продажа" / None
```

---

## Результаты бэктеста (дефолтные параметры)

| Символ | Период | Сделок | Винрейт | Итог | Макс. просадка | Шарп |
|--------|--------|--------|---------|------|----------------|------|
| BTCUSDT | 2022–2026 | 369 | 46.1% | $1 235 | 18.3% | −1.89 |
| ETHUSDT | 2022–2026 | 297 | 50.8% | $1 339 | 19.6% | −0.31 |

> Начальный депозит: $1 500. Стратегия убыточна на дефолтных параметрах — используйте `optimize.py` для подбора.

---

## Требования

- Python 3.10+
- Интернет-соединение для скачивания данных (Binance Futures API, CoinGecko API)
