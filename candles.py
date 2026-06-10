#candles.py
from pathlib import Path
from datetime import datetime
import os
import requests
import pandas as pd
import numpy as np
import pandas_ta as ta
import time
from signal_settings import *
symbols_lst = []



def get_binance_top_50_by_cap():
    global symbols_lst
    # 1. Получаем топ-100 с CoinGecko (с запасом)
    cg_url = "https://api.coingecko.com/api/v3/coins/markets"
    cg_params = {
        'vs_currency': 'usd',
        'order': 'market_cap_desc',
        'per_page': 100,
        'page': 1
    }

    print("Запрашиваем данные с CoinGecko...")
    cg_response = requests.get(cg_url, params=cg_params, timeout=10)

    # Проверяем статус ответа
    if cg_response.status_code != 200:
        print(f"✗ CoinGecko вернул ошибку: {cg_response.status_code}")
        print(f"  Ответ: {cg_response.text[:300]}")  # первые 300 символов
        return []

    # Пробуем распарсить JSON
    try:
        cg_data = cg_response.json()
    except requests.exceptions.JSONDecodeError as e:
        print(f"✗ Ошибка парсинга JSON: {e}")
        print(f"  Ответ сервера: {cg_response.text[:300]}")
        return []

    # Проверяем, что это список
    if not isinstance(cg_data, list):
        print(f"✗ Ожидали список, получили: {type(cg_data)}")
        print(f"  Данные: {cg_data}")
        return []

    print(f"✓ Получено {len(cg_data)} монет с CoinGecko")
    cg_symbols = {coin['symbol'].upper() for coin in cg_data}

    # 2. Получаем список торговых пар с Binance Futures
    print("Запрашиваем данные с Binance...")
    binance_url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
    binance_response = requests.get(binance_url, timeout=10)

    if binance_response.status_code != 200:
        print(f"✗ Binance вернул ошибку: {binance_response.status_code}")
        return []

    binance_data = binance_response.json()
    binance_symbols = {
        s['baseAsset'].upper()
        for s in binance_data['symbols']
        if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING'
    }
    print(f"✓ Получено {len(binance_symbols)} активных пар USDT на Binance Futures")

    # 3. Пересекаем и берём топ-50
    common = cg_symbols & binance_symbols
    print(f"✓ Найдено {len(common)} общих монет")

    top = [
        coin for coin in cg_data
        if coin['symbol'].upper() in common
    ][:top_by_cap]

    return top

def date_to_ms(date_str):
    """
    Конвертирует строку даты в миллисекунды (timestamp).
    Поддерживает форматы: '2026-01-01' или '2026-01-01 13:00:00'
    Возвращает None, если date_str is None.
    """
    if date_str is None:
        return None

    formats = ['%Y-%m-%d %H:%M:%S', '%Y-%m-%d']
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    raise ValueError(
        f"Не удалось распарсить дату: '{date_str}'. Ожидаемые форматы: 'YYYY-MM-DD' или 'YYYY-MM-DD HH:MM:SS'")


def download_candles_batch(symbol, interval, start_time=None, end_time=None, limit=1000):
    """Один запрос к Binance API за порцией свечей"""
    bin_url = "https://fapi.binance.com/fapi/v1/klines"
    params = {
        'symbol': symbol,
        'interval': interval,
        'limit': limit,
    }
    if start_time is not None:
        params['startTime'] = start_time
    if end_time is not None:
        params['endTime'] = end_time

    try:
        response = requests.get(bin_url, params=params, timeout=10)
        if response.status_code == 200:
            return response.json()
        else:
            print(f"✗ Ошибка {response.status_code}: {response.text[:200]}")
            return None
    except requests.RequestException as e:
        print(f"✗ Сетевая ошибка: {e}")
        return None


def download_candles_flexible(symbol, interval, start_date=None, end_date=None, limit=1000):
    """
    Универсальная загрузка свечей с гибкой обработкой дат.

    Параметры:
    - start_date: None или строка '2026-01-01' / '2026-01-01 13:00:00'
    - end_date: None или строка в тех же форматах
    - limit: макс. свечей за один запрос (для случая без дат)

    Возвращает список свечей в хронологическом порядке (старая → новая).
    """

    # Конвертируем даты в мс (или оставляем None)
    start_ms = date_to_ms(start_date) if start_date else None
    end_ms = date_to_ms(end_date) if end_date else None

    # Случай 1: нет ни start, ни end → берём последние `limit` свечей
    if start_ms is None and end_ms is None:
        candles = download_candles_batch(symbol, interval, limit=limit)
        return candles if candles else []

    # Случай 2: есть только start → грузим от start до сейчас порциями
    if start_ms is not None and end_ms is None:
        all_candles = []
        current_start = start_ms
        while True:
            candles = download_candles_batch(symbol, interval, start_time=current_start, limit=1000)
            if not candles:
                break
            all_candles.extend(candles)
            # Если получили меньше 1000 — достигли конца
            if len(candles) < 1000:
                break
            # Сдвигаем точку старта: последняя свеча + 1 мс
            last_close = candles[-1][6]
            current_start = last_close + 1
            time.sleep(0.1)  # защита от рейт-лимита
        return all_candles

    # Случай 3: есть только end → грузим "назад" от end
    if start_ms is None and end_ms is not None:
        all_candles = []
        current_end = end_ms
        while True:
            # Запрашиваем 1000 свечей ДО current_end
            candles = download_candles_batch(symbol, interval, end_time=current_end, limit=1000)
            if not candles:
                break
            # Вставляем в начало списка, чтобы сохранить хронологию
            all_candles = candles + all_candles
            if len(candles) < 1000:
                break
            # Сдвигаем end: первая свеча - 1 мс
            first_open = candles[0][0]
            current_end = first_open - 1
            time.sleep(0.1)
        return all_candles

    # Случай 4: есть и start, и end → грузим диапазон
    if start_ms is not None and end_ms is not None:
        all_candles = []
        current_start = start_ms
        # Добавляем 1 день к end, чтобы включить последний день полностью
        end_ms_adjusted = end_ms + 24 * 60 * 60 * 1000

        while current_start < end_ms_adjusted:
            candles = download_candles_batch(
                symbol, interval,
                start_time=current_start,
                end_time=end_ms_adjusted,
                limit=1000
            )
            if not candles:
                break
            all_candles.extend(candles)
            last_close = candles[-1][6]
            current_start = last_close + 1
            if current_start >= end_ms_adjusted:
                break
            time.sleep(0.1)
        return all_candles

    # Фолбэк (не должен достигаться)
    return []


def get_df(symbol, interval, start_date=None, end_date=None):
    """Получает свечи и возвращает отформатированный DataFrame"""

    candles = download_candles_flexible(symbol, interval, start_date, end_date)

    if not candles:
        print(f"✗ Не удалось получить данные для {symbol}")
        return None

    # Создаём DataFrame
    columns = [
        'Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume',
        'Close time', 'Quote asset volume', 'Number of trades',
        'Taker buy base asset volume', 'Taker buy quote asset volume',
        'Ignore'
    ]
    df = pd.DataFrame(candles, columns=columns)

    # Оставляем только нужные колонки
    cols_to_save = ['Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume']
    df = df[cols_to_save].copy()

    # Преобразуем типы данных
    df['Timestamp'] = pd.to_datetime(df['Timestamp'], unit='ms')
    numeric_cols = ['Open', 'High', 'Low', 'Close', 'Volume']
    df[numeric_cols] = df[numeric_cols].astype(float)

    # Сортируем по времени (на случай, если порции пришли не по порядку)
    df = df.sort_values('Timestamp').reset_index(drop=True)

    # Фильтруем незакрытые свечи (опционально, для бэктеста)
    # current_ms = int(time.time() * 1000)
    # df = df[df['Close time'] <= current_ms].reset_index(drop=True)

    return df

def save_df_to_csv(df: pd.DataFrame, symbol, interval, start_date=None, end_date=None, filepath='klines'):
    """Сохраняет DataFrame в CSV"""
    if df is None or len(df) == 0:
        print("✗ DataFrame пустой!")
        return None

    os.makedirs(filepath, exist_ok=True)
    # Заменяем двоеточия и пробелы на дефисы для совместимости с Windows
    if start_date and end_date:
        start_str = str(start_date).replace(':', '-').replace(' ', '_')
        end_str = str(end_date).replace(':', '-').replace(' ', '_')
        filename = f"{symbol}_{interval}_{start_str}_{end_str}.csv"
    else:
        filename = f"{symbol}_{interval}.csv"
    full_path = os.path.join(filepath, filename)

    try:
        df.to_csv(
            full_path,
            index=False,
            encoding='utf-8-sig',
            date_format='%Y-%m-%d %H:%M:%S',
            float_format='%.8f'
        )

        file_size = os.path.getsize(full_path) / 1024
        print(f"✓ Файл сохранён: {full_path}")
        print(f"  Строк: {len(df):,} | Размер: {file_size:.1f} KB")
        return full_path

    except Exception as e:
        print(f"✗ Ошибка сохранения: {e}")
        return None


def read_df_from_csv(
        symbol: str,
        interval: str = '1h',
        start_date: str = None,
        end_date: str = None,
        filepath: str = 'klines'
) -> pd.DataFrame:


    """
    Читает DataFrame из CSV файла.

    Args:
        symbol: Символ (например, 'BTCUSDT')
        interval: Таймфрейм (например, '1h')
        start_date: Начальная дата (опционально, формат: 'YYYY-MM-DD' или 'YYYY-MM-DD HH:MM:SS')
        end_date: Конечная дата (опционально, формат: 'YYYY-MM-DD' или 'YYYY-MM-DD HH:MM:SS')
        filepath: Путь к папке с файлами

    Returns:
        pd.DataFrame с данными
    """

    base_dir = Path(filepath)

    # Ищем все файлы, соответствующие шаблону symbol_interval_*.csv
    pattern = f"{symbol}_{interval}_*.csv"
    matching_files = list(base_dir.glob(pattern))

    if not matching_files:
        raise FileNotFoundError(f"Не найдены файлы для {symbol} {interval} в папке {filepath}")

    # Если файлов несколько, выбираем наиболее подходящий
    # (с самыми широкими датами или первым найденным)
    file_path = matching_files[0]

    print(f"✓ Чтение файла: {file_path}")

    try:
        df = pd.read_csv(
            file_path,
            parse_dates=['Timestamp'],
            encoding='utf-8-sig'
        )
    except Exception as e:
        print(f"✗ Ошибка чтения файла: {e}")
        raise

    # Фильтрация по датам, если указаны
    if start_date or end_date:
        df = df.copy()  # Чтобы избежать SettingWithCopyWarning

        if start_date:
            start_dt = pd.to_datetime(start_date)
            df = df[df['Timestamp'] >= start_dt]
            print(f"  Отфильтровано с: {start_dt}")

        if end_date:
            end_dt = pd.to_datetime(end_date)
            df = df[df['Timestamp'] <= end_dt]
            print(f"  Отфильтровано до: {end_dt}")

    if len(df) == 0:
        print("✗ DataFrame пуст после фильтрации!")
        return pd.DataFrame(columns=['Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])

    print(f"✓ Загружено строк: {len(df):,}")
    return df


# ВСПОМОГАТЕЛЬНОЕ
def _enough(series, n=2):
    return series is not None and hasattr(series, "iloc") and series.dropna().shape[0] >= n

def _rolling_slope(series, window=10):
    """
    Грубая оценка наклона: (последнее - первое)/window.
    Используем как индикатор направления (положительный/отрицательный).
    """
    if len(series) < window or window < 2:
        return np.nan
    window_vals = series.iloc[-window:]
    return (window_vals.iloc[-1] - window_vals.iloc[0]) / (window - 1 + 1e-9)

'''
Стратегия:  Возврат к средней по полосам боллинджера с фильтрами ADX и RSI

Суть: Ловим отскок цены к среднему после резких импульсов. Покупаем, когда цена «перерастянута» вниз, и продаем, когда она улетела слишком высоко вверх. Работаем в боковике.

Где торгуем:
- Binance (фьючерсы или спот)
- Топ-50 монет по капитализации
- Таймфрейм: 1 час
- Лонг и Шорт

Индикаторы:
RSI (9-14) — показывает, когда цена «перегрета» или «перепродана»
Bollinger Bands (20, 2.0) — показывает границы нормального движения цены
ADX (14) — фильтр: не входим, если на рынке сильный тренд
ATR (14) — для расчёта стопов

Вход в сделку (Лонг):
- Цена закрылась ниже нижней полосы Боллинджера
- RSI упал ниже 20 (зона перепроданности)
- ADX ниже 25 (рынок не в сильном тренде)
- Входим на открытии следующей свечи

Для Шорта — всё зеркально: цена выше верхней полосы, RSI > 80, ADX < 25.

Риск-менеджмент:
- Стоп-лосс: сразу после входа ставим на 1.5 × ATR от цены входа
- Перенос стопа в Безубыток: когда цена прошла в нашу сторону X × ATR (параметр для подбора), переносим стоп на точку входа + комиссии (~0.3%)
- трейлинг стоп (описан дальше)

Выход из позиции:
- 50% позиции закрываем, когда цена касается средней линии Боллинджера. В этот момент активируем трейлинг для остатка:
- Фиксируем расстояние между ценой закрытия первой части и текущим стопом
- Дальше стоп двигается за ценой, сохраняя это расстояние
- Если цена разворачивается — остаток закрывается по стопу
- Если цена идёт дальше — стоп подтягивается
- Остаток 50% закрываем, когда цена достигает противоположной полосы Боллинджера ИЛИ срабатывает трейлинг-стоп

Что хотим оптимизировать:
- Множитель для перевода в безубыток (break_even_atr_multiplier)
- Уровни RSI для входа
- Порог ADX для фильтрации тренда
- мультипликатор ATR для первоначального стопа
'''


def get_signals(df, symbol):

    c = df['Close']; h, l = df['High'], df['Low']; v = df.get('Volume')
    adx = ta.adx(h, l, c, length=adx_len).filter(like='ADX_').iloc[:, 0]

    if not _enough(adx, 1):
        return None
    bb = ta.bbands(c, length=bb_lenth, lower_std=bb_std, upper_std=bb_std)
    rsi = ta.rsi(c, length=rsi_len)
    if bb is None or not _enough(rsi, 1):
        return None
    bbl, bbu, bbm = bb.iloc[:, 0], bb.iloc[:, 2],bb.iloc[:, 1]

    # Сами сигналы
    buy = (c.iloc[-1] <= bbl.iloc[-1]) and (rsi.iloc[-1] < rsi_lower_level) and adx.iloc[-1] < adx_threshold
    sell = (c.iloc[-1] >= bbu.iloc[-1]) and (rsi.iloc[-1] > rsi_upper_level) and adx.iloc[-1] < adx_threshold


    if buy:
        print (symbol, " сигнал -> Покупка")
        return "Покупка"
    if sell:
        print (symbol, " сигнал -> Продажа")
        return "Продажа"
    print (symbol, ' -> No signal')
    return None

# ==================== ЗАПУСК ====================

if __name__ == "__main__":
    # global symbols_lst, interval, start_date, end_date
    symbols_lst = []

    coins = get_binance_top_50_by_cap()
    print(f"len_coins = {len(coins)}")

    if coins:
        print(f"\nТоп-{top_by_cap} монет по капитализации (доступные на Binance Futures):")

        for coin in coins:
            rank = coin.get('market_cap_rank', 'N/A')
            symbol = coin['symbol'].upper() + 'USDT'
            symbols_lst.append(symbol)
            name = coin['name']
            market_cap = coin['market_cap']
            price = coin['current_price']
            print(f"{rank:3d}. {symbol:6s} {name:20s} bln${market_cap / 1000000000:.4f} ${price:.4f}")

    else:
        print("✗ Не удалось получить данные")


    # Для конкретного символа:
    #
    # for symbol in symbols_lst:
    #     if symbol == 'JSTUSDT':
    #         df = get_df(symbol=symbol, interval=interval, start_date=start_date, end_date=end_date)
    #         get_signals(df, symbol = symbol)
    #         # save_df_to_csv(df=df, symbol=symbol, start_date=start_date, end_date=end_date, interval=interval)

    # Для списка символов:

    for symbol in symbols_lst:
        # df = get_df(symbol=symbol, interval=interval, start_date=start_date, end_date=end_date)
        df = read_df_from_csv(symbol,interval=interval)
        get_signals(df, symbol = symbol)
        # save_df_to_csv(df=df, symbol=symbol, start_date=start_date, end_date=end_date, interval=interval)