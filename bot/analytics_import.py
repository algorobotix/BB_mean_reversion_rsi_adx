"""
Сканер результатов оптимизации из папки optimization_results/.

Структура папки (создаётся optimizer.py):
    optimization_results/
        BTCUSDT_20260613_120045/
            best_params.json   ← читаем это
            all_trials.csv
            top20_trials.csv
            report.txt
            walk_forward.csv   (опционально)

best_params.json содержит:
    {
        "params": { bb_len, bb_std, rsi_len, rsi_lower, rsi_upper,
                    adx_len, adx_threshold, sl_atr_mult },
        "train_metrics": { sharpe, return_pct, win_rate, max_dd, ... },
        "oos_metrics":   { ... },
        "overfit":       { verdict, flags, ... }
    }
"""

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

OPT_DIR = Path(__file__).parent.parent / "optimization_results"

# Оптимизатор называет параметр bb_len, а бот хранит bb_length
_RENAME = {"bb_len": "bb_length"}


def _map_params(raw: dict) -> dict:
    return {_RENAME.get(k, k): v for k, v in raw.items()}


def scan_results() -> list[dict]:
    """
    Возвращает список найденных результатов оптимизации,
    отсортированных от новейшего к старейшему.
    """
    if not OPT_DIR.exists():
        return []

    results = []
    for folder in OPT_DIR.iterdir():
        if not folder.is_dir():
            continue
        json_path = folder / "best_params.json"
        if not json_path.exists():
            continue
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Cannot read %s: %s", json_path, e)
            continue

        # Имя папки: BTCUSDT_20260613_120045
        parts = folder.name.split("_")
        symbol = parts[0]
        ts_raw = "_".join(parts[1:3]) if len(parts) >= 3 else ""
        try:
            ts = datetime.strptime(ts_raw, "%Y%m%d_%H%M%S")
        except ValueError:
            ts = datetime.min

        results.append({
            "folder_name": folder.name,
            "symbol":       symbol,
            "timestamp":    ts,
            "params":       _map_params(data.get("params", {})),
            "train":        data.get("train_metrics", {}),
            "oos":          data.get("oos_metrics", {}),
            "overfit":      data.get("overfit", {}),
        })

    return sorted(results, key=lambda r: r["timestamp"], reverse=True)


def verdict_emoji(verdict: str) -> str:
    return {"OK": "✅", "BORDERLINE": "⚠️", "OVERFIT": "❌"}.get(verdict, "❓")


def fmt_pct(v) -> str:
    try:
        return f"{float(v):+.1f}%"
    except (TypeError, ValueError):
        return "—"


def fmt_f(v, decimals: int = 2) -> str:
    try:
        return f"{float(v):.{decimals}f}"
    except (TypeError, ValueError):
        return "—"


def result_short_line(r: dict) -> str:
    """Одна строка для списка (без MarkdownV2 — используется в кнопке)."""
    v = r["overfit"].get("verdict", "?")
    oos = r["oos"]
    return (
        f"{verdict_emoji(v)} {r['symbol']} "
        f"{r['timestamp'].strftime('%m-%d %H:%M')} | "
        f"Sharpe {fmt_f(oos.get('sharpe'))} | "
        f"OOS {fmt_pct(oos.get('return_pct'))}"
    )
