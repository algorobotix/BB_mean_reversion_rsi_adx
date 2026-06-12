"""
Стратегия: Возврат к средней по полосам Боллинджера с фильтрами ADX и RSI

Суть: Ловим отскок цены к среднему после резких импульсов. Покупаем, когда цена
«перерастянута» вниз, продаём, когда улетела слишком высоко. Работаем в боковике.

Условия входа (Лонг):
- Close < BB Lower
- RSI < rsi_lower_level
- ADX < adx_threshold (нет сильного тренда)

Условия входа (Шорт): зеркально.

Выход: касание BB Middle (50%) + трейлинг-стоп на остаток.
Стоп-лосс: entry ± sl_atr_mult * ATR.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas_ta as ta

from config.settings import (
    bb_lenth, bb_std, rsi_len, rsi_lower_level, rsi_upper_level,
    adx_len, adx_threshold,
)


def _enough(series, n=2):
    return series is not None and hasattr(series, 'iloc') and series.dropna().shape[0] >= n


def _rolling_slope(series, window=10):
    if len(series) < window or window < 2:
        return np.nan
    window_vals = series.iloc[-window:]
    return (window_vals.iloc[-1] - window_vals.iloc[0]) / (window - 1 + 1e-9)


def get_signals(df, symbol):
    c = df['Close']
    h, l = df['High'], df['Low']

    adx = ta.adx(h, l, c, length=adx_len).filter(like='ADX_').iloc[:, 0]
    if not _enough(adx, 1):
        return None

    bb = ta.bbands(c, length=bb_lenth, lower_std=bb_std, upper_std=bb_std)
    rsi = ta.rsi(c, length=rsi_len)
    if bb is None or not _enough(rsi, 1):
        return None

    bbl = bb.iloc[:, 0]
    bbu = bb.iloc[:, 2]

    buy = (c.iloc[-1] <= bbl.iloc[-1]) and (rsi.iloc[-1] < rsi_lower_level) and (adx.iloc[-1] < adx_threshold)
    sell = (c.iloc[-1] >= bbu.iloc[-1]) and (rsi.iloc[-1] > rsi_upper_level) and (adx.iloc[-1] < adx_threshold)

    if buy:
        print(f"{symbol} -> Покупка")
        return 'Покупка'
    if sell:
        print(f"{symbol} -> Продажа")
        return 'Продажа'

    print(f"{symbol} -> No signal")
    return None


if __name__ == '__main__':
    from config.settings import interval
    from feed.candles import get_binance_top_by_cap, read_df_from_csv

    coins = get_binance_top_by_cap()
    for coin in coins:
        symbol = coin['symbol'].upper() + 'USDT'
        try:
            df = read_df_from_csv(symbol, interval=interval)
            get_signals(df, symbol)
        except FileNotFoundError:
            print(f"{symbol} -> нет данных")
