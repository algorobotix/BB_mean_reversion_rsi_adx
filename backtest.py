# backtest.py
import warnings
import backtrader as bt
import pandas as pd
from candles import read_df_from_csv
from signal_settings import interval, rsi_len, bb_lenth, bb_std, adx_len

warnings.filterwarnings('ignore')


class SimpleMeanReversionStrategy(bt.Strategy):
    params = (
        ('rsi_len', rsi_len),
        ('rsi_lower', 30),
        ('rsi_upper', 70),
        ('bb_len', bb_lenth),
        ('bb_std', bb_std),
        ('adx_len', adx_len),
        ('adx_threshold', 30),
        ('atr_len', 14),
        ('sl_atr_mult', 2.5),  # Фиксированный стоп: entry ± sl_atr_mult * ATR
        ('trade_usdt', 150),   # Фиксированный размер позиции в USDT
        ('comm_rate', 0.001),
    )

    def __init__(self):
        self.rsi = bt.indicators.RSI(self.data.close, period=self.p.rsi_len)
        self.bb = bt.indicators.BollingerBands(self.data.close, period=self.p.bb_len, devfactor=self.p.bb_std)
        self.adx = bt.indicators.DirectionalMovementIndex(self.data, period=self.p.adx_len)
        self.atr = bt.indicators.ATR(self.data, period=self.p.atr_len)

        # Состояние позиции
        self.entry_price = 0.0
        self.sl_price = 0.0
        self.order = None
        self.entry_order_ref = None  # ref of entry order; used to skip exit-order callbacks
        self.initial_sl = 0.0
        self.exit_price = 0.0
        self.open_size = 0.0  # Фиксируем размер сделки при открытии
        self.trade_dir = ""  # LONG / SHORT
        self.entry_dt = None
        self.exit_reason = ""

        # Логирование
        self.trades_log = []

    def notify_order(self, order):
        """Фиксирует параметры входа ТОЛЬКО после реального исполнения ордера"""
        if order.status == order.Completed:
            if order.ref == self.entry_order_ref:
                # Only update position state for entry orders, not exit orders.
                # Exit orders (close) completing here would otherwise overwrite
                # entry_price, sl_price, and initial_sl with wrong values.
                self.entry_price = order.executed.price
                self.open_size = abs(order.executed.size)
                self.entry_dt = bt.num2date(self.data.datetime[0])

                sl_dist = self.p.sl_atr_mult * self.atr[0]
                self.initial_sl = self.entry_price - sl_dist if order.isbuy() else self.entry_price + sl_dist
                self.sl_price = self.initial_sl

                if order.isbuy():
                    self.trade_dir = "LONG"
                    print(f" LONG открыт: {self.entry_dt} @ {self.entry_price:.2f}, SL: {self.sl_price:.2f}")
                elif order.issell():
                    self.trade_dir = "SHORT"
                    print(f"📥 SHORT открыт: {self.entry_dt} @ {self.entry_price:.2f}, SL: {self.sl_price:.2f}")
            else:
                # Exit order completed: capture actual execution price for trade log.
                # trade.price in notify_trade is the entry average, not the exit price.
                self.exit_price = order.executed.price

        if order.status in [order.Completed, order.Canceled, order.Margin]:
            self.order = None

    def notify_trade(self, trade):
        if trade.isclosed:
            exit_dt = bt.num2date(self.data.datetime[0])
            pnl = trade.pnlcomm

            if self.exit_reason == "FIXED_SL":
                print(f"📤 {self.trade_dir} ЗАКРЫТ ПО СТОПУ:")
                print(f"   Entry: {self.entry_price:.2f}, SL: {self.sl_price:.2f}, Exit: {self.exit_price:.2f}")
                print(f"   Разница Exit-SL: {abs(self.exit_price - self.sl_price):.2f}")

            self.trades_log.append({
                'Entry Time': self.entry_dt,
                'Exit Time': exit_dt,
                'Direction': self.trade_dir,
                'Entry Price': round(self.entry_price, 5),
                'Exit Price': round(self.exit_price, 5),
                'Size': self.open_size,
                'PnL (USD)': round(pnl, 4),
                'PnL (%)': round((pnl / (self.entry_price * self.open_size)) * 100, 4) if self.entry_price and self.open_size else 0,
                'Exit Reason': self.exit_reason,
                'SL': round(self.sl_price, 5)
            })

            self.reset_state()

    def next(self):
        if self.order:
            return

        if self.position:
            if self.position.size > 0:
                self.manage_long()
            else:
                self.manage_short()
        else:
            self.check_entry()

    def check_entry(self):
        c = self.data.close[0]
        # LONG
        size = round(self.p.trade_usdt / c, 6)
        if c < self.bb.bot[0] and self.rsi[0] < self.p.rsi_lower and self.adx.adx[0] < self.p.adx_threshold:
            self.exit_reason = "ENTER_LONG"
            self.order = self.buy(size=size)
            self.entry_order_ref = self.order.ref
        # SHORT
        elif c > self.bb.top[0] and self.rsi[0] > self.p.rsi_upper and self.adx.adx[0] < self.p.adx_threshold:
            self.exit_reason = "ENTER_SHORT"
            self.order = self.sell(size=size)
            self.entry_order_ref = self.order.ref

    def manage_long(self):
        l, c = self.data.low[0], self.data.close[0]

        if l <= self.sl_price:
            self.exit_reason = "FIXED_SL"
            self.order = self.close(exectype=bt.Order.Stop, price=self.sl_price)
            return

        if c >= self.bb.mid[0]:
            self.exit_reason = "BB_MID"
            self.order = self.close()
            return

    def manage_short(self):
        h, c = self.data.high[0], self.data.close[0]

        if h >= self.sl_price:
            self.exit_reason = "FIXED_SL"
            self.order = self.close(exectype=bt.Order.Stop, price=self.sl_price)
            return

        if c <= self.bb.mid[0]:
            self.exit_reason = "BB_MID"
            self.order = self.close()
            return

    def reset_state(self):
        self.entry_price = 0.0
        self.sl_price = 0.0
        self.exit_reason = ""
        self.exit_price = 0.0
        self.entry_dt = None
        self.trade_dir = ""
        self.open_size = 0
        self.initial_sl = 0.0
        self.order = None
        self.entry_order_ref = None


def plot_tradingview(
    df: pd.DataFrame,
    trades_log: list,
    equity: pd.Series,
    symbol: str,
    tf: str,
    bb_len: int = 20,
    bb_std_val: float = 2.0,
    rsi_len: int = 14,
    adx_len: int = 14,
    save_path: str = None,
):
    """TradingView-style interactive chart using Plotly (dark theme)."""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    df = df.copy()

    # ── Bollinger Bands ──────────────────────────────────────────────────────
    df['bb_mid'] = df['close'].rolling(bb_len).mean()
    _bb_std = df['close'].rolling(bb_len).std()
    df['bb_upper'] = df['bb_mid'] + bb_std_val * _bb_std
    df['bb_lower'] = df['bb_mid'] - bb_std_val * _bb_std

    # ── RSI (Wilder EWM) ─────────────────────────────────────────────────────
    delta = df['close'].diff()
    avg_gain = delta.clip(lower=0).ewm(com=rsi_len - 1, min_periods=rsi_len, adjust=False).mean()
    avg_loss = (-delta).clip(lower=0).ewm(com=rsi_len - 1, min_periods=rsi_len, adjust=False).mean()
    df['rsi'] = 100 - 100 / (1 + avg_gain / avg_loss.replace(0, 1e-9))

    # ── ADX (Wilder EWM) ─────────────────────────────────────────────────────
    hi, lo, cl = df['high'], df['low'], df['close']
    tr = pd.concat([hi - lo, (hi - cl.shift()).abs(), (lo - cl.shift()).abs()], axis=1).max(axis=1)
    plus_dm = hi.diff().clip(lower=0)
    minus_dm = (-lo.diff()).clip(lower=0)
    plus_dm_s = plus_dm.where(plus_dm > minus_dm, 0.0)
    minus_dm_s = minus_dm.where(minus_dm > plus_dm, 0.0)
    _ew = dict(alpha=1 / adx_len, min_periods=adx_len, adjust=False)
    atr_s = tr.ewm(**_ew).mean()
    plus_di = 100 * plus_dm_s.ewm(**_ew).mean() / atr_s
    minus_di = 100 * minus_dm_s.ewm(**_ew).mean() / atr_s
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-9)
    df['adx'] = dx.ewm(**_ew).mean()

    # ── Subplots ─────────────────────────────────────────────────────────────
    BG, GRID, TEXT = '#131722', '#1e2130', '#d1d4dc'

    fig = make_subplots(
        rows=4, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.025,
        row_heights=[0.50, 0.17, 0.17, 0.16],
        subplot_titles=(
            f'{symbol} {tf} — Price + Bollinger Bands',
            f'RSI ({rsi_len})',
            f'ADX ({adx_len})',
            'Equity Curve',
        ),
    )

    # ── Candlesticks ─────────────────────────────────────────────────────────
    fig.add_trace(go.Candlestick(
        x=df.index,
        open=df['open'], high=df['high'], low=df['low'], close=df['close'],
        name='Price',
        increasing=dict(line=dict(color='#26a69a', width=1), fillcolor='#26a69a'),
        decreasing=dict(line=dict(color='#ef5350', width=1), fillcolor='#ef5350'),
        whiskerwidth=0,
    ), row=1, col=1)

    # ── Bollinger Bands ──────────────────────────────────────────────────────
    fig.add_trace(go.Scatter(
        x=df.index, y=df['bb_upper'],
        line=dict(color='#2962ff', width=1),
        name='BB Upper', showlegend=False,
    ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=df.index, y=df['bb_lower'],
        line=dict(color='#2962ff', width=1),
        fill='tonexty', fillcolor='rgba(41,98,255,0.07)',
        name='BB Lower', showlegend=False,
    ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=df.index, y=df['bb_mid'],
        line=dict(color='#ff9800', width=1, dash='dot'),
        name='BB Mid', showlegend=False,
    ), row=1, col=1)

    # ── Trade markers ────────────────────────────────────────────────────────
    if trades_log:
        tdf = pd.DataFrame(trades_log)
        tdf['Entry Time'] = pd.to_datetime(tdf['Entry Time'])
        tdf['Exit Time'] = pd.to_datetime(tdf['Exit Time'])

        longs = tdf[tdf['Direction'] == 'LONG']
        shorts = tdf[tdf['Direction'] == 'SHORT']

        if not longs.empty:
            fig.add_trace(go.Scatter(
                x=longs['Entry Time'], y=longs['Entry Price'],
                mode='markers',
                marker=dict(symbol='triangle-up', size=11, color='#26a69a',
                            line=dict(color='#fff', width=0.5)),
                name='Long entry',
            ), row=1, col=1)

        if not shorts.empty:
            fig.add_trace(go.Scatter(
                x=shorts['Entry Time'], y=shorts['Entry Price'],
                mode='markers',
                marker=dict(symbol='triangle-down', size=11, color='#ef5350',
                            line=dict(color='#fff', width=0.5)),
                name='Short entry',
            ), row=1, col=1)

        for exits, color, label in [
            (tdf[tdf['PnL (USD)'] > 0], '#26a69a', 'Exit (win)'),
            (tdf[tdf['PnL (USD)'] <= 0], '#ef5350', 'Exit (loss)'),
        ]:
            if not exits.empty:
                hover = exits.apply(
                    lambda r: f"{r['Direction']} {r['Exit Reason']}<br>PnL: ${r['PnL (USD)']:.2f}", axis=1
                )
                fig.add_trace(go.Scatter(
                    x=exits['Exit Time'], y=exits['Exit Price'],
                    mode='markers',
                    marker=dict(symbol='x', size=9, color=color,
                                line=dict(color=color, width=2)),
                    name=label,
                    hovertext=hover, hoverinfo='text',
                ), row=1, col=1)

        # ── Stop-loss levels (one segment per trade: Entry→Exit at SL price) ──
        sl_x, sl_y, sl_hover = [], [], []
        for _, r in tdf.iterrows():
            sl_x  += [r['Entry Time'], r['Exit Time'], None]
            sl_y  += [r['SL'], r['SL'], None]
            sl_hover += [
                f"{r['Direction']}  SL: {r['SL']:.2f}<br>"
                f"Entry: {r['Entry Price']:.2f}  →  dist: "
                f"{abs(r['Entry Price'] - r['SL']):.2f}",
                f"{r['Direction']}  SL: {r['SL']:.2f}", None,
            ]
        fig.add_trace(go.Scatter(
            x=sl_x, y=sl_y,
            mode='lines',
            line=dict(color='rgba(255,82,82,0.55)', width=1, dash='dot'),
            name='Stop Loss',
            hovertext=sl_hover, hoverinfo='text',
        ), row=1, col=1)

    # ── RSI ──────────────────────────────────────────────────────────────────
    fig.add_trace(go.Scatter(
        x=df.index, y=df['rsi'],
        line=dict(color='#9c27b0', width=1.5),
        name=f'RSI {rsi_len}',
    ), row=2, col=1)
    for lvl, col in [(70, '#ef5350'), (50, 'rgba(255,255,255,0.2)'), (30, '#26a69a')]:
        fig.add_hline(y=lvl, line=dict(color=col, dash='dash', width=0.8), row=2, col=1)

    # ── ADX ──────────────────────────────────────────────────────────────────
    fig.add_trace(go.Scatter(
        x=df.index, y=df['adx'],
        line=dict(color='#ff9800', width=1.5),
        name=f'ADX {adx_len}',
    ), row=3, col=1)
    fig.add_hline(y=30, line=dict(color='rgba(255,255,255,0.35)', dash='dash', width=0.8), row=3, col=1)

    # ── Equity Curve ─────────────────────────────────────────────────────────
    if equity is not None and len(equity) > 0:
        initial = equity.iloc[0]
        eq_color = '#26a69a' if equity.iloc[-1] >= initial else '#ef5350'
        fig.add_trace(go.Scatter(
            x=equity.index, y=equity.values,
            line=dict(color=eq_color, width=1.5),
            name='Equity',
        ), row=4, col=1)
        fig.add_hline(
            y=initial,
            line=dict(color='rgba(255,255,255,0.25)', dash='dash', width=0.8),
            row=4, col=1,
        )

    # ── Global layout ─────────────────────────────────────────────────────────
    ax_style = dict(gridcolor=GRID, showgrid=True, zeroline=False,
                    tickfont=dict(color=TEXT, size=10))
    for r in range(1, 5):
        fig.update_xaxes(ax_style, row=r, col=1)
        fig.update_yaxes(ax_style, row=r, col=1)

    fig.update_layout(
        template='plotly_dark',
        paper_bgcolor=BG,
        plot_bgcolor=BG,
        title=dict(
            text=f'<b>{symbol}</b>  Mean Reversion  BB+RSI+ADX  |  {tf}',
            font=dict(color=TEXT, size=15),
        ),
        xaxis_rangeslider_visible=False,
        legend=dict(
            bgcolor='rgba(19,23,34,0.85)',
            font=dict(color=TEXT, size=11),
            orientation='h', yanchor='bottom', y=1.01, x=0,
        ),
        height=920,
        margin=dict(l=60, r=30, t=80, b=40),
        hovermode='x unified',
        hoverlabel=dict(bgcolor='#1e2130', font_color=TEXT),
    )

    # ── X-axis: initial view ends at last trade, spans 500 bars ─────────────
    bar_width = df.index[-1] - df.index[-2]
    x_data_end = str(df.index[-1] + bar_width * 3)

    if trades_log:
        last_exit = max(pd.to_datetime(t['Exit Time']) for t in trades_log)
        anchor = df.index.searchsorted(last_exit)          # position in df
        start_pos = max(0, anchor - 490)
        end_pos   = min(len(df) - 1, anchor + 10)
        x_view_start = str(df.index[start_pos])
        x_view_end   = str(df.index[end_pos] + bar_width * 3)
    else:
        n_show = min(500, len(df))
        x_view_start = str(df.index[-n_show])
        x_view_end   = x_data_end

    fig.update_xaxes(
        range=[x_view_start, x_view_end],
        autorange=False,
    )

    if save_path:
        out = save_path if save_path.endswith('.html') else save_path.replace('.png', '.html')
        fig.write_html(out, config={'scrollZoom': True})
        print(f"📊 График сохранён: {out}")

    fig.show(config={'scrollZoom': True})


def run_backtest(df: pd.DataFrame, symbol: str, plot: bool = False):
    df_test = df.copy()
    df_test.rename(columns={
        'Timestamp': 'datetime', 'Open': 'open', 'High': 'high',
        'Low': 'low', 'Close': 'close', 'Volume': 'volume'
    }, inplace=True)
    df_test['datetime'] = pd.to_datetime(df_test['datetime'])
    df_test.set_index('datetime', inplace=True)
    df_test = df_test.iloc[:-1]  # Защита от look-ahead bias

    print(f" Всего свечей: {len(df_test)}")

    cerebro = bt.Cerebro()
    data = bt.feeds.PandasData(dataname=df_test)
    cerebro.adddata(data)
    cerebro.addstrategy(SimpleMeanReversionStrategy)

    cerebro.broker.setcash(1500.0)
    cerebro.broker.setcommission(commission=0.001)

    cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe', riskfreerate=0.0, annualize=True)
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trades')

    results = cerebro.run()
    strat = results[0]

    dd = strat.analyzers.drawdown.get_analysis()
    sharpe = strat.analyzers.sharpe.get_analysis()
    trades = strat.analyzers.trades.get_analysis()

    final_value = cerebro.broker.getvalue()
    max_dd = dd.max.drawdown / 100.0 if dd.max.drawdown else 0.0
    sharpe_val = sharpe.get('sharperatio', 0.0) or 0.0

    total_trades = trades.get('total', {}).get('total', 0)
    won = trades.get('won', {}).get('total', 0)
    win_rate = won / max(total_trades, 1)

    trades_df = pd.DataFrame(strat.trades_log)

    if plot:
        equity_series = None
        if strat.trades_log:
            _eq = pd.DataFrame(strat.trades_log)[['Exit Time', 'PnL (USD)']]
            _eq['Exit Time'] = pd.to_datetime(_eq['Exit Time'])
            _eq = _eq.sort_values('Exit Time')
            equity_series = pd.Series(
                (1500.0 + _eq['PnL (USD)'].cumsum()).values,
                index=_eq['Exit Time'].values,
            )

        plot_tradingview(
            df_test, strat.trades_log, equity_series,
            symbol, interval,
            bb_len=bb_lenth, bb_std_val=bb_std,
            rsi_len=rsi_len, adx_len=adx_len,
            save_path=f"backtest_{symbol}_{interval}.html",
        )

    return {
        'final_value': final_value,
        'max_dd': max_dd,
        'sharpe': sharpe_val,
        'total_trades': total_trades,
        'win_rate': win_rate,
        'trades_df': trades_df
    }


if __name__ == '__main__':
    SYMBOL = 'ETHUSDT'
    print(f"📂 1. Загрузка данных для {SYMBOL} (таймфрейм: {interval})...")
    df_main = read_df_from_csv(SYMBOL, interval=interval)

    print(f"\n🚀 2. Запуск бэктеста...")
    metrics = run_backtest(df_main, symbol=SYMBOL, plot=True)

    print("\n Итоговые метрики:")
    print(f"  Финальный депозит: ${metrics['final_value']:.2f}")
    print(f"  Макс. просадка:   {metrics['max_dd']:.2%}")
    print(f"  Коэф. Шарпа:      {metrics['sharpe']:.2f}")
    print(f"  Всего сделок:     {metrics['total_trades']}")
    print(f"  Винрейт:          {metrics['win_rate']:.2%}")

    if not metrics['trades_df'].empty:
        print("\n📈 Список всех сделок:")
        print(metrics['trades_df'].to_string(index=False))

        long_df = metrics['trades_df'][metrics['trades_df']['Direction'] == 'LONG']
        short_df = metrics['trades_df'][metrics['trades_df']['Direction'] == 'SHORT']
        print(f"\n📊 LONG: {len(long_df)} сделок | Винрейт: {long_df['PnL (USD)'].apply(lambda x: x > 0).mean():.2%}")
        print(f"📊 SHORT: {len(short_df)} сделок | Винрейт: {short_df['PnL (USD)'].apply(lambda x: x > 0).mean():.2%}")

        csv_path = f"trades_{SYMBOL}_{interval}.csv"
        metrics['trades_df'].to_csv(csv_path, index=False, encoding='utf-8-sig')
        print(f"\n✅ Все сделки сохранены в: {csv_path}")
    else:
        print("\n⚠️ Сделок не было.")

