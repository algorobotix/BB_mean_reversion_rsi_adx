import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import pandas as pd


def analyze(csv_path: str):
    df = pd.read_csv(csv_path, parse_dates=['Entry Time', 'Exit Time'])
    df['Duration (h)'] = (df['Exit Time'] - df['Entry Time']).dt.total_seconds() / 3600

    trail = df[df['Exit Reason'] == 'FIXED_SL'].copy()
    trail['initial_dist'] = abs(trail['Entry Price'] - trail['SL'])
    trail['exit_vs_entry'] = trail['Exit Price'] - trail['Entry Price']

    short_stops = trail[trail['Duration (h)'] <= 1]
    print(f"Stopped out on entry bar (<=1h): {len(short_stops)} / {len(trail)} = "
          f"{len(short_stops)/max(len(trail), 1):.1%}")
    print(f"  Avg PnL: ${short_stops['PnL (USD)'].mean():.2f}")
    print()

    bb = df[df['Exit Reason'] == 'BB_MID']
    print('=== BB_MID exits ===')
    print(f"  Count: {len(bb)} | Avg PnL: ${bb['PnL (USD)'].mean():.2f} | "
          f"Avg duration: {bb['Duration (h)'].mean():.1f}h")
    print()

    print('=== Trailing stop distance analysis ===')
    trail['initial_dist_pct'] = trail['initial_dist'] / trail['Entry Price'] * 100
    print(f"  Avg initial SL distance: ${trail['initial_dist'].mean():.2f} "
          f"({trail['initial_dist_pct'].mean():.2f}%)")
    print(f"  Min: ${trail['initial_dist'].min():.2f} | Max: ${trail['initial_dist'].max():.2f}")
    print()

    print('=== Entry bar stop-outs sample ===')
    print(short_stops[['Entry Price', 'Exit Price', 'SL', 'PnL (USD)', 'Duration (h)']].head(15).to_string())
    print()

    wins = df[df['PnL (USD)'] > 0]['PnL (USD)']
    losses = df[df['PnL (USD)'] <= 0]['PnL (USD)']
    print('=== Risk/Reward ===')
    print(f"  Win/Loss ratio (avg): {wins.mean() / abs(losses.mean()):.3f}")
    print(f"  Required WR to break even: {1 / (1 + wins.mean() / abs(losses.mean())):.1%}")
    print()

    long_trail = trail[trail['Direction'] == 'LONG']
    short_trail = trail[trail['Direction'] == 'SHORT']
    long_past_sl = long_trail[long_trail['Exit Price'] < long_trail['SL']]
    short_past_sl = short_trail[short_trail['Exit Price'] > short_trail['SL']]
    print('=== Exits worse than SL (gap past original stop) ===')
    print(f"  LONG  exits below SL: {len(long_past_sl)} / {len(long_trail)} = "
          f"{len(long_past_sl)/max(1, len(long_trail)):.1%}")
    print(f"  SHORT exits above SL: {len(short_past_sl)} / {len(short_trail)} = "
          f"{len(short_past_sl)/max(1, len(short_trail)):.1%}")


if __name__ == '__main__':
    p = argparse.ArgumentParser(description='Анализ сделок из CSV')
    p.add_argument('csv', nargs='?', default='trades_BTCUSDT_1h.csv',
                   help='Путь к CSV-файлу со сделками')
    args = p.parse_args()
    analyze(args.csv)
