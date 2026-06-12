"""
Точка входа: сканирование топ-N монет и вывод торговых сигналов.

Использование:
    python main.py                  # сигналы из локальных CSV (klines/)
    python main.py --download       # скачать свежие данные, затем сигналы
    python main.py --symbol BTCUSDT # только одна монета
"""

import argparse

from config.settings import interval, start_date, end_date
from feed.candles import get_binance_top_by_cap, get_df, read_df_from_csv, save_df_to_csv
from strategy.signals import get_signals


def parse_args():
    p = argparse.ArgumentParser(description='BB Mean Reversion — сканер сигналов')
    p.add_argument('--symbol', default=None, help='Конкретная пара, например BTCUSDT')
    p.add_argument('--download', action='store_true', help='Скачать данные перед анализом')
    p.add_argument('--filepath', default='klines', help='Папка с CSV-данными')
    return p.parse_args()


def main():
    args = parse_args()

    if args.symbol:
        symbols = [args.symbol]
    else:
        coins = get_binance_top_by_cap()
        symbols = [c['symbol'].upper() + 'USDT' for c in coins]

    print(f"\nСканирование {len(symbols)} монет на таймфрейме {interval}...\n")

    buy_signals, sell_signals = [], []

    for symbol in symbols:
        try:
            if args.download:
                df = get_df(symbol=symbol, interval=interval,
                            start_date=start_date, end_date=end_date)
                if df is not None:
                    save_df_to_csv(df, symbol=symbol, interval=interval,
                                   start_date=start_date, end_date=end_date,
                                   filepath=args.filepath)
            else:
                df = read_df_from_csv(symbol, interval=interval, filepath=args.filepath)
        except FileNotFoundError:
            print(f"{symbol} -> нет данных (используйте --download)")
            continue

        if df is None or df.empty:
            continue

        signal = get_signals(df, symbol)
        if signal == 'Покупка':
            buy_signals.append(symbol)
        elif signal == 'Продажа':
            sell_signals.append(symbol)

    print(f"\n{'='*40}")
    print(f"Покупка  ({len(buy_signals)}): {', '.join(buy_signals) or '—'}")
    print(f"Продажа  ({len(sell_signals)}): {', '.join(sell_signals) or '—'}")
    print(f"{'='*40}")


if __name__ == '__main__':
    main()
