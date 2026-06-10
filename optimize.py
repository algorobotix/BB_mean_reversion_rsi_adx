"""
optimize.py — Оптимизатор стратегии: Backtrader + Optuna

Использование:
    python optimize.py [--symbol BTCUSDT] [--trials 200] [--jobs 1]
                       [--sampler tpe] [--objective composite]
                       [--walk-forward] [--wf-folds 5] [--out-of-sample 0.20]

Примеры:
    python optimize.py --trials 5 --objective composite --walk-forward --wf-folds 2
    python optimize.py --symbol ETHUSDT --trials 300 --sampler tpe --objective sharpe
    python optimize.py --symbol BTCUSDT --trials 500 --storage sqlite:///study.db
"""

import argparse
import contextlib
import gc
import io
import json
import logging
import math
import warnings
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import backtrader as bt
import optuna
from optuna.samplers import TPESampler, CmaEsSampler, NSGAIISampler, RandomSampler
from optuna.pruners import MedianPruner

warnings.filterwarnings("ignore")
logging.getLogger("backtrader").setLevel(logging.CRITICAL)
optuna.logging.set_verbosity(optuna.logging.WARNING)


# ─────────────────────────────────────────────────────────────────────────────
# Подавление вывода Backtrader во время оптимизации
# ─────────────────────────────────────────────────────────────────────────────

@contextlib.contextmanager
def suppress_output():
    with contextlib.redirect_stdout(io.StringIO()):
        with contextlib.redirect_stderr(io.StringIO()):
            yield


# ─────────────────────────────────────────────────────────────────────────────
# Цели оптимизации
# ─────────────────────────────────────────────────────────────────────────────

OBJECTIVES = {
    "sharpe": lambda m: m["sharpe"],
    "sortino": lambda m: m["sortino"],
    "calmar": lambda m: m["calmar"],
    "sqn": lambda m: m["sqn"],
    "profit_factor": lambda m: m["profit_factor"],
    "return_pct": lambda m: m["return_pct"],
    "composite": lambda m: (
        0.35 * m["sharpe"]
        + 0.20 * m["sortino"]
        + 0.20 * m["calmar"]
        + 0.15 * m["profit_factor"]
        + 0.10 * m["sqn"]
    ) * (1.0 - m["max_dd"]),
    "risk_adjusted": lambda m: (
        (m["return_pct"] / 100.0) / max(m["max_dd"], 0.01) * m["win_rate"]
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# Вычисление метрик
# ─────────────────────────────────────────────────────────────────────────────

def compute_metrics(cerebro: bt.Cerebro, strat, initial_cash: float, trades_df: pd.DataFrame) -> dict:
    """Вычисляет полный набор метрик из cerebro + strat + trades_df."""
    final_value = cerebro.broker.getvalue()
    net_profit = final_value - initial_cash
    return_pct = (net_profit / initial_cash) * 100.0

    dd_a = strat.analyzers.drawdown.get_analysis()
    sh_a = strat.analyzers.sharpe.get_analysis()
    tr_a = strat.analyzers.trades.get_analysis()
    sq_a = strat.analyzers.sqn.get_analysis()

    max_dd = (dd_a.max.drawdown / 100.0) if dd_a.max.drawdown else 0.0
    sharpe = sh_a.get("sharperatio") or 0.0
    sqn = sq_a.get("sqn") or 0.0

    total = tr_a.get("total", {}).get("total", 0)
    won = tr_a.get("won", {}).get("total", 0)
    lost = tr_a.get("lost", {}).get("total", 0)
    win_rate = won / max(total, 1)

    if trades_df.empty or "PnL (USD)" not in trades_df.columns:
        profit_factor = sortino = calmar = avg_win = avg_loss = expectancy = recovery_factor = 0.0
    else:
        pnl = trades_df["PnL (USD)"]
        gross_profit = pnl[pnl > 0].sum()
        gross_loss = abs(pnl[pnl < 0].sum())
        profit_factor = gross_profit / max(gross_loss, 1e-9)

        avg_win = float(pnl[pnl > 0].mean()) if won > 0 else 0.0
        avg_loss = float(pnl[pnl < 0].mean()) if lost > 0 else 0.0
        expectancy = (win_rate * avg_win) + ((1 - win_rate) * abs(avg_loss))

        neg_pnl = pnl[pnl < 0]
        ds_std = float(neg_pnl.std()) if len(neg_pnl) > 1 else 1e-9
        mean_pnl = float(pnl.mean())
        sortino = (mean_pnl / ds_std) * math.sqrt(252) if ds_std > 0 else 0.0

        calmar = return_pct / (max_dd * 100.0) if max_dd > 0 else 0.0
        recovery_factor = net_profit / (abs(avg_loss) * max(lost, 1)) if avg_loss != 0 else 0.0

    return {
        "final_value": round(final_value, 4),
        "net_profit": round(net_profit, 4),
        "return_pct": round(return_pct, 4),
        "sharpe": round(float(sharpe), 4),
        "sortino": round(sortino, 4),
        "calmar": round(calmar, 4),
        "sqn": round(float(sqn), 4),
        "max_dd": round(max_dd, 4),
        "profit_factor": round(profit_factor, 4),
        "total_trades": total,
        "win_rate": round(win_rate, 4),
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "expectancy": round(expectancy, 4),
        "recovery_factor": round(recovery_factor, 4),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Запуск одного бэктеста с заданными параметрами
# ─────────────────────────────────────────────────────────────────────────────

def run_backtest_with_params(df: pd.DataFrame, params: dict, initial_cash: float = 1500.0) -> dict:
    """Запускает один бэктест; возвращает dict метрик. Не рисует, не печатает."""
    from backtest import SimpleMeanReversionStrategy

    df_bt = df.copy()
    df_bt.rename(columns={
        "Timestamp": "datetime", "Open": "open",
        "High": "high", "Low": "low", "Close": "close", "Volume": "volume",
    }, inplace=True)
    df_bt["datetime"] = pd.to_datetime(df_bt["datetime"])
    df_bt.set_index("datetime", inplace=True)
    df_bt = df_bt.iloc[:-1]  # защита от look-ahead bias

    if len(df_bt) < 200:
        return _empty_metrics(initial_cash)

    cerebro = bt.Cerebro()
    cerebro.adddata(bt.feeds.PandasData(dataname=df_bt))
    cerebro.addstrategy(SimpleMeanReversionStrategy, **params)
    cerebro.broker.setcash(initial_cash)
    cerebro.broker.setcommission(commission=0.001)

    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe",
                        riskfreerate=0.0, annualize=True, timeframe=bt.TimeFrame.Days)
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")
    cerebro.addanalyzer(bt.analyzers.SQN, _name="sqn")

    results = cerebro.run(maxcpus=1)
    strat = results[0]

    trades_df = pd.DataFrame(strat.trades_log) if strat.trades_log else pd.DataFrame()
    metrics = compute_metrics(cerebro, strat, initial_cash, trades_df)
    return metrics


def _empty_metrics(initial_cash: float) -> dict:
    return {
        "final_value": initial_cash, "net_profit": 0.0, "return_pct": 0.0,
        "sharpe": 0.0, "sortino": 0.0, "calmar": 0.0, "sqn": 0.0,
        "max_dd": 0.0, "profit_factor": 0.0, "total_trades": 0,
        "win_rate": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
        "expectancy": 0.0, "recovery_factor": 0.0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Optuna objective
# ─────────────────────────────────────────────────────────────────────────────

def make_objective(df_train: pd.DataFrame, args):
    """Возвращает замыкание-objective для Optuna."""

    def objective(trial: optuna.Trial) -> float:
        params = {
            "bb_len":        trial.suggest_int("bb_len", 10, 50),
            "bb_std":        trial.suggest_float("bb_std", 1.0, 3.5, step=0.25),
            "rsi_len":       trial.suggest_int("rsi_len", 5, 30),
            "rsi_lower":     trial.suggest_int("rsi_lower", 15, 45),
            "rsi_upper":     trial.suggest_int("rsi_upper", 55, 85),
            "adx_len":       trial.suggest_int("adx_len", 7, 28),
            "adx_threshold": trial.suggest_int("adx_threshold", 15, 45),
            "sl_atr_mult":   trial.suggest_float("sl_atr_mult", 1.0, 5.0, step=0.25),
        }

        # Доменное ограничение: rsi_lower должен быть значимо меньше rsi_upper
        if params["rsi_lower"] >= params["rsi_upper"] - 10:
            raise optuna.exceptions.TrialPruned()

        try:
            with suppress_output():
                metrics = run_backtest_with_params(df_train, params, args.initial_cash)
        except Exception:
            raise optuna.exceptions.TrialPruned()

        # Отсекаем нежизнеспособные параметры
        if metrics["total_trades"] < args.min_trades:
            raise optuna.exceptions.TrialPruned()
        if metrics["max_dd"] > 0.85:
            raise optuna.exceptions.TrialPruned()
        if metrics["return_pct"] < -90.0:
            raise optuna.exceptions.TrialPruned()

        score = OBJECTIVES[args.objective](metrics)
        if not math.isfinite(score):
            raise optuna.exceptions.TrialPruned()

        for k, v in metrics.items():
            if isinstance(v, (int, float)) and math.isfinite(float(v)):
                trial.set_user_attr(k, v)

        return score

    return objective


# ─────────────────────────────────────────────────────────────────────────────
# Walk-forward validation
# ─────────────────────────────────────────────────────────────────────────────

def walk_forward_validation(
    df: pd.DataFrame,
    best_params: dict,
    n_folds: int = 5,
    test_ratio: float = 0.20,
    initial_cash: float = 1500.0,
) -> pd.DataFrame:
    """
    Expanding-window walk-forward.
    Каждый фолд: тренировка на первых X% данных, тест на следующих test_ratio%.
    """
    rows = []
    n = len(df)
    min_train_frac = 0.40

    for fold in range(n_folds):
        train_end_frac = min_train_frac + fold * ((1.0 - min_train_frac - test_ratio) / max(n_folds - 1, 1))
        test_end_frac = train_end_frac + test_ratio
        if test_end_frac > 1.0:
            break

        test_start = int(n * train_end_frac)
        test_end = int(n * test_end_frac)
        df_test = df.iloc[test_start:test_end].reset_index(drop=True)

        if len(df_test) < 200:
            continue

        ts_col = "Timestamp" if "Timestamp" in df_test.columns else df_test.index.name or "index"
        try:
            t_start = str(df_test["Timestamp"].iloc[0]) if "Timestamp" in df_test.columns else str(df_test.index[0])
            t_end = str(df_test["Timestamp"].iloc[-1]) if "Timestamp" in df_test.columns else str(df_test.index[-1])
        except Exception:
            t_start = t_end = "unknown"

        try:
            with suppress_output():
                m = run_backtest_with_params(df_test, best_params, initial_cash)
        except Exception as e:
            rows.append({"fold": fold, "test_start": t_start, "test_end": t_end,
                         "error": str(e), "return_pct": 0.0, "sharpe": 0.0,
                         "win_rate": 0.0, "total_trades": 0, "max_dd": 0.0})
            continue

        m["fold"] = fold
        m["test_start"] = t_start
        m["test_end"] = t_end
        rows.append(m)

    return pd.DataFrame(rows)


# ─────────────────────────────────────────────────────────────────────────────
# Детектор переобучения
# ─────────────────────────────────────────────────────────────────────────────

def detect_overfitting(train_m: dict, oos_m: dict, wf_df: pd.DataFrame) -> dict:
    """Многосигнальная проверка на переобучение."""
    flags = []

    def degradation(is_v, oos_v):
        return (is_v - oos_v) / max(abs(is_v), 1e-9) if abs(is_v) > 1e-9 else 0.0

    sh_deg = degradation(train_m["sharpe"], oos_m["sharpe"])
    wr_deg = degradation(train_m["win_rate"], oos_m["win_rate"])
    re_deg = degradation(train_m["return_pct"], oos_m["return_pct"])
    pf_deg = degradation(train_m["profit_factor"], oos_m["profit_factor"])

    if sh_deg > 0.50:
        flags.append(f"Sharpe деградировал на {sh_deg:.0%} (IS→OOS), порог 50%")
    if wr_deg > 0.30:
        flags.append(f"WinRate деградировал на {wr_deg:.0%} (IS→OOS), порог 30%")
    if re_deg > 0.50:
        flags.append(f"Return% деградировал на {re_deg:.0%} (IS→OOS), порог 50%")
    if pf_deg > 0.50:
        flags.append(f"ProfitFactor деградировал на {pf_deg:.0%} (IS→OOS), порог 50%")
    if train_m["sharpe"] > 0 and oos_m["sharpe"] < 0:
        flags.append("Sharpe стал отрицательным на OOS — сильный сигнал переобучения")
    if oos_m["total_trades"] < 3:
        flags.append("Слишком мало сделок на OOS — нельзя сделать вывод")

    wf_consistency = None
    wf_sharpe_vals = []
    if not wf_df.empty and "return_pct" in wf_df.columns:
        positive_folds = int((wf_df["return_pct"] > 0).sum())
        total_folds = len(wf_df)
        wf_consistency = positive_folds / max(total_folds, 1)
        if "sharpe" in wf_df.columns:
            wf_sharpe_vals = wf_df["sharpe"].dropna().tolist()
            wf_sh_std = float(wf_df["sharpe"].std())
            if wf_sh_std > 1.5:
                flags.append(f"Высокая вариативность Sharpe по фолдам (std={wf_sh_std:.2f}) — нестабильная стратегия")
        if wf_consistency < 0.50:
            flags.append(f"Только {positive_folds}/{total_folds} walk-forward фолдов прибыльны")

    n_flags = len(flags)
    verdict = "OVERFIT" if n_flags >= 2 else ("BORDERLINE" if n_flags == 1 else "OK")

    return {
        "verdict": verdict,
        "flags": flags,
        "sharpe_degradation": round(sh_deg, 4),
        "winrate_degradation": round(wr_deg, 4),
        "return_degradation": round(re_deg, 4),
        "pf_degradation": round(pf_deg, 4),
        "wf_consistency": wf_consistency,
        "wf_sharpe_vals": wf_sharpe_vals,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Сохранение результатов
# ─────────────────────────────────────────────────────────────────────────────

def save_results(
    study: optuna.Study,
    best_params: dict,
    train_m: dict,
    oos_m: dict,
    wf_df: pd.DataFrame,
    overfit: dict,
    symbol: str,
    args,
    out_dir: Path,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    # best_params.json
    with open(out_dir / "best_params.json", "w", encoding="utf-8") as f:
        json.dump(
            {"params": best_params, "train_metrics": train_m,
             "oos_metrics": oos_m, "overfit": overfit},
            f, indent=2, default=str,
        )

    # all_trials.csv
    try:
        trials_df = study.trials_dataframe(attrs=("number", "value", "params", "user_attrs", "state"))
        trials_df.to_csv(out_dir / "all_trials.csv", index=False, encoding="utf-8-sig")
    except Exception as e:
        print(f"  [warn] Не удалось сохранить all_trials.csv: {e}")

    # top20_trials.csv
    completed = [t for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE]
    top20 = sorted(completed, key=lambda t: t.value or -999, reverse=True)[:20]
    top20_rows = []
    for t in top20:
        row = {"trial": t.number, "score": t.value}
        row.update(t.params)
        row.update({f"m_{k}": v for k, v in (t.user_attrs or {}).items()})
        top20_rows.append(row)
    pd.DataFrame(top20_rows).to_csv(out_dir / "top20_trials.csv", index=False, encoding="utf-8-sig")

    # walk_forward.csv
    if not wf_df.empty:
        wf_df.to_csv(out_dir / "walk_forward.csv", index=False, encoding="utf-8-sig")

    # report.txt
    ts_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(out_dir / "report.txt", "w", encoding="utf-8") as f:
        lines = [
            f"OPTIMIZATION REPORT — {symbol} — {ts_str}",
            "=" * 60,
            f"Trials completed : {len(completed)}/{args.trials}",
            f"Objective        : {args.objective}",
            f"Sampler          : {args.sampler}",
            f"OOS fraction     : {args.out_of_sample:.0%}",
            "",
            "BEST PARAMETERS:",
        ]
        for k, v in best_params.items():
            lines.append(f"  {k}: {v}")

        def fmt_metrics(m_dict, label):
            lines.append(f"\n{label}:")
            keys_order = ["return_pct", "sharpe", "sortino", "calmar", "sqn",
                          "profit_factor", "max_dd", "total_trades", "win_rate",
                          "avg_win", "avg_loss", "expectancy", "final_value"]
            for k in keys_order:
                if k in m_dict:
                    lines.append(f"  {k:20s}: {m_dict[k]}")

        fmt_metrics(train_m, "IN-SAMPLE METRICS")
        fmt_metrics(oos_m, "OUT-OF-SAMPLE METRICS")

        lines += [
            "",
            f"OVERFITTING CHECK: {overfit['verdict']}",
        ]
        if overfit["flags"]:
            for flag in overfit["flags"]:
                lines.append(f"  ⚠  {flag}")
        else:
            lines.append("  ✓  Признаков переобучения не обнаружено")

        if wf_df is not None and not wf_df.empty and "return_pct" in wf_df.columns:
            lines += [
                "",
                "WALK-FORWARD SUMMARY:",
                wf_df[["fold", "return_pct", "sharpe", "win_rate", "total_trades"]].to_string(index=False),
            ]

        lines.append(f"\nРезультаты сохранены: {out_dir.resolve()}")
        f.write("\n".join(lines))


# ─────────────────────────────────────────────────────────────────────────────
# Визуализация
# ─────────────────────────────────────────────────────────────────────────────

def plot_results(study: optuna.Study, wf_df: pd.DataFrame, out_dir: Path) -> None:
    try:
        from plotly.subplots import make_subplots
        import plotly.graph_objects as go

        optuna.visualization.plot_optimization_history(study).write_html(str(out_dir / "opt_history.html"))
        optuna.visualization.plot_param_importances(study).write_html(str(out_dir / "param_importance.html"))
        optuna.visualization.plot_parallel_coordinate(study).write_html(str(out_dir / "parallel_coords.html"))
        optuna.visualization.plot_slice(study).write_html(str(out_dir / "param_slices.html"))
        optuna.visualization.plot_contour(study).write_html(str(out_dir / "contour.html"))

        if not wf_df.empty and "return_pct" in wf_df.columns:
            fig = make_subplots(rows=2, cols=1,
                                subplot_titles=("Walk-Forward Return %", "Walk-Forward Sharpe"))
            fig.add_trace(go.Bar(x=wf_df["fold"], y=wf_df["return_pct"], name="Return %"), row=1, col=1)
            if "sharpe" in wf_df.columns:
                fig.add_trace(go.Bar(x=wf_df["fold"], y=wf_df["sharpe"], name="Sharpe"), row=2, col=1)
            fig.update_layout(title="Walk-Forward Validation", showlegend=True)
            fig.write_html(str(out_dir / "walk_forward.html"))

        print(f"  Графики сохранены в: {out_dir}/")
    except Exception as e:
        print(f"  [warn] Ошибка при построении графиков: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Вспомогательные функции
# ─────────────────────────────────────────────────────────────────────────────

def build_sampler(name: str):
    seed = 42
    if name == "tpe":
        return TPESampler(n_startup_trials=20, multivariate=True, seed=seed)
    elif name == "cmaes":
        return CmaEsSampler(seed=seed)
    elif name == "nsga2":
        return NSGAIISampler(seed=seed)
    else:
        return RandomSampler(seed=seed)


def print_comparison_table(default_params: dict, best_params: dict,
                            default_m: dict, best_train_m: dict, oos_m: dict) -> None:
    print("\n" + "=" * 75)
    print("СРАВНЕНИЕ: ДЕФОЛТНЫЕ vs ЛУЧШИЕ ПАРАМЕТРЫ")
    print("=" * 75)
    print(f"{'Параметр':<20} {'Дефолт':>12} {'Оптимум':>12}")
    print("-" * 45)
    all_keys = sorted(set(default_params) | set(best_params))
    for k in all_keys:
        dv = default_params.get(k, "—")
        bv = best_params.get(k, "—")
        marker = " ◄" if dv != bv else ""
        print(f"  {k:<18} {str(dv):>12} {str(bv):>12}{marker}")

    print("\n" + "=" * 75)
    print(f"{'Метрика':<22} {'Дефолт (IS)':>14} {'Оптим (IS)':>14} {'Оптим (OOS)':>14}")
    print("-" * 65)
    metrics_to_show = [
        ("return_pct", "Return %"),
        ("sharpe", "Sharpe"),
        ("sortino", "Sortino"),
        ("calmar", "Calmar"),
        ("sqn", "SQN"),
        ("max_dd", "Max DD"),
        ("profit_factor", "Profit Factor"),
        ("win_rate", "Win Rate"),
        ("total_trades", "Trades"),
        ("expectancy", "Expectancy"),
    ]
    for key, label in metrics_to_show:
        dv = default_m.get(key, "—")
        bv = best_train_m.get(key, "—")
        ov = oos_m.get(key, "—")
        print(f"  {label:<20} {str(dv):>14} {str(bv):>14} {str(ov):>14}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Оптимизатор торговой стратегии: Backtrader + Optuna",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--symbol", default="BTCUSDT", help="Торговая пара")
    p.add_argument("--trials", type=int, default=200, help="Количество trials Optuna")
    p.add_argument("--jobs", type=int, default=1, help="Параллельных воркеров (1 = безопасно)")
    p.add_argument("--sampler", default="tpe", choices=["tpe", "cmaes", "nsga2", "random"])
    p.add_argument("--objective", default="composite", choices=list(OBJECTIVES))
    p.add_argument("--walk-forward", action="store_true", dest="walk_forward")
    p.add_argument("--wf-folds", type=int, default=5, dest="wf_folds")
    p.add_argument("--out-of-sample", type=float, default=0.20, dest="out_of_sample")
    p.add_argument("--initial-cash", type=float, default=1500.0, dest="initial_cash")
    p.add_argument("--min-trades", type=int, default=10, dest="min_trades")
    p.add_argument("--no-plots", action="store_true", dest="no_plots")
    p.add_argument("--study-name", default=None, dest="study_name")
    p.add_argument("--storage", default=None, help="sqlite:///study.db — для сохранения/возобновления")
    return p.parse_args()


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    warnings.filterwarnings("ignore")

    # ── Загрузка данных ──────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  ОПТИМИЗАЦИЯ СТРАТЕГИИ: {args.symbol}")
    print(f"{'='*60}")
    print(f"  Trials     : {args.trials}")
    print(f"  Sampler    : {args.sampler}")
    print(f"  Objective  : {args.objective}")
    print(f"  OOS split  : {args.out_of_sample:.0%}")
    print(f"{'='*60}\n")

    from candles import read_df_from_csv
    from signal_settings import interval

    print(f"Загрузка данных для {args.symbol}...")
    df_full = read_df_from_csv(args.symbol, interval=interval)
    print(f"Всего баров: {len(df_full)}")

    # ── Train / OOS split ────────────────────────────────────────────────────
    split_idx = int(len(df_full) * (1.0 - args.out_of_sample))
    df_train = df_full.iloc[:split_idx].reset_index(drop=True)
    df_oos = df_full.iloc[split_idx:].reset_index(drop=True)
    print(f"Train: {len(df_train)} баров | OOS: {len(df_oos)} баров")

    # ── Дефолтные параметры для сравнения ───────────────────────────────────
    from signal_settings import bb_lenth, bb_std, rsi_len, adx_len
    default_params = {
        "bb_len": bb_lenth, "bb_std": bb_std,
        "rsi_len": rsi_len, "rsi_lower": 30, "rsi_upper": 70,
        "adx_len": adx_len, "adx_threshold": 30, "sl_atr_mult": 2.5,
    }
    print("\nЗапуск бэктеста с дефолтными параметрами для сравнения...")
    with suppress_output():
        default_m = run_backtest_with_params(df_train, default_params, args.initial_cash)
    print(f"  Дефолт IS: Sharpe={default_m['sharpe']:.3f}, Return={default_m['return_pct']:.1f}%, "
          f"Trades={default_m['total_trades']}, MaxDD={default_m['max_dd']:.2%}")

    # ── Optuna study ──────────────────────────────────────────────────────────
    study_name = args.study_name or f"{args.symbol}_{args.objective}_{datetime.now().strftime('%Y%m%d%H%M')}"
    study = optuna.create_study(
        study_name=study_name,
        direction="maximize",
        sampler=build_sampler(args.sampler),
        pruner=MedianPruner(n_startup_trials=15, n_warmup_steps=5),
        storage=args.storage,
        load_if_exists=True,
    )

    objective_fn = make_objective(df_train, args)

    print(f"\nЗапуск оптимизации: {args.trials} trials...")
    study.optimize(
        objective_fn,
        n_trials=args.trials,
        n_jobs=args.jobs,
        show_progress_bar=True,
        gc_after_trial=True,
    )

    # ── Лучший trial ─────────────────────────────────────────────────────────
    if not study.best_trial:
        print("Все trials были pruned. Попробуйте уменьшить --min-trades.")
        return

    best_trial = study.best_trial
    best_params = best_trial.params
    print(f"\nЛучший trial #{best_trial.number} — Score: {best_trial.value:.4f}")
    print("Лучшие параметры:")
    for k, v in best_params.items():
        print(f"  {k}: {v}")

    # ── IS метрики лучших параметров ─────────────────────────────────────────
    print("\nПересчёт метрик на train-множестве...")
    with suppress_output():
        best_train_m = run_backtest_with_params(df_train, best_params, args.initial_cash)

    # ── OOS оценка ───────────────────────────────────────────────────────────
    print("Оценка на out-of-sample данных...")
    with suppress_output():
        oos_m = run_backtest_with_params(df_oos, best_params, args.initial_cash)

    # ── Walk-forward ──────────────────────────────────────────────────────────
    wf_df = pd.DataFrame()
    if args.walk_forward:
        print(f"\nWalk-forward validation ({args.wf_folds} фолдов)...")
        wf_df = walk_forward_validation(
            df_full, best_params,
            n_folds=args.wf_folds,
            test_ratio=args.out_of_sample,
            initial_cash=args.initial_cash,
        )
        if not wf_df.empty and "return_pct" in wf_df.columns:
            print(wf_df[["fold", "return_pct", "sharpe", "win_rate", "total_trades"]].to_string(index=False))

    # ── Детектор переобучения ─────────────────────────────────────────────────
    overfit = detect_overfitting(best_train_m, oos_m, wf_df)
    print(f"\nПроверка переобучения: {overfit['verdict']}")
    for flag in overfit["flags"]:
        print(f"  ⚠  {flag}")
    if not overfit["flags"]:
        print("  ✓  Признаков переобучения не обнаружено")

    # ── Таблица сравнения ─────────────────────────────────────────────────────
    print_comparison_table(default_params, best_params, default_m, best_train_m, oos_m)

    # ── Важность параметров ───────────────────────────────────────────────────
    try:
        importances = optuna.importance.get_param_importances(study)
        print("\nВажность параметров (Optuna FAnova):")
        for k, v in sorted(importances.items(), key=lambda x: -x[1]):
            bar = "█" * int(v * 30)
            print(f"  {k:<20} {bar} {v:.3f}")
    except Exception:
        pass

    # ── Сохранение ────────────────────────────────────────────────────────────
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(f"optimization_results/{args.symbol}_{ts}")
    save_results(study, best_params, best_train_m, oos_m, wf_df, overfit, args.symbol, args, out_dir)

    # ── Графики ───────────────────────────────────────────────────────────────
    if not args.no_plots:
        plot_results(study, wf_df, out_dir)

    # ── Итоговая рекомендация ────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("ИТОГОВАЯ РЕКОМЕНДАЦИЯ")
    print("=" * 60)
    completed_count = len([t for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE])
    print(f"Завершено trials: {completed_count}/{args.trials}")
    print(f"Лучший Score ({args.objective}): {best_trial.value:.4f}")

    if overfit["verdict"] == "OK":
        print("✓  Параметры выглядят робастно. Рекомендуется forward test.")
    elif overfit["verdict"] == "BORDERLINE":
        print("⚠  Пограничный результат. Рекомендуется больше trials или строже ограничения.")
    else:
        print("✗  Обнаружено переобучение. НЕ использовать в live trading.")
        print("   Рекомендации: больше данных, меньше параметров, другая логика стратегии.")

    print(f"\nРезультаты: {out_dir.resolve()}")
    return best_params, best_train_m, oos_m


if __name__ == "__main__":
    main()
