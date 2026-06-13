"""
Робот: скачивает свечи, считает индикаторы, генерирует сигналы.
Каждый символ — отдельная asyncio-задача.

Логика:
  1. Загрузить последние 1000 свечей.
  2. Проверить текущий сигнал (только информационно при старте).
  3. Ждать закрытия следующей свечи (по таймеру).
  4. Подкачать одну закрытую свечу, пересчитать индикаторы.
  5. При появлении нового сигнала — отправить уведомление.
  6. В торговом режиме — открывать/закрывать сделки.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

import pandas as pd
import pandas_ta as ta

from bot.exchange import BinanceExchange
from bot.notifier import Notifier

logger = logging.getLogger(__name__)

# Длительность таймфреймов в секундах
TF_SECONDS: dict[str, int] = {
    "1m": 60,    "3m": 180,   "5m": 300,
    "15m": 900,  "30m": 1800,
    "1h": 3600,  "2h": 7200,  "4h": 14400,
    "6h": 21600, "8h": 28800, "12h": 43200,
    "1d": 86400,
}


def _next_candle_sleep(interval: str) -> float:
    """Секунды до закрытия текущей свечи (+ 5 сек буфер)."""
    tf = TF_SECONDS.get(interval, 3600)
    now = time.time()
    next_close = ((now // tf) + 1) * tf
    return max(5.0, next_close - now + 5)


# ─────────────────────────────────────────────────────────────────────────────
# Расчёт индикаторов и сигнала
# ─────────────────────────────────────────────────────────────────────────────

def _calc_signal(df: pd.DataFrame, s: dict) -> str | None:
    """
    Возвращает 'BUY', 'SELL' или None.
    s — словарь настроек с ключами: bb_length, bb_std, rsi_len,
        rsi_upper, rsi_lower, adx_len, adx_threshold.
    """
    try:
        c, h, lo = df["Close"], df["High"], df["Low"]

        adx_df = ta.adx(h, lo, c, length=s["adx_len"])
        if adx_df is None:
            return None
        adx = adx_df.filter(like="ADX_").iloc[:, 0]
        if adx.dropna().empty:
            return None

        bb = ta.bbands(c, length=s["bb_length"],
                       lower_std=s["bb_std"], upper_std=s["bb_std"])
        rsi = ta.rsi(c, length=s["rsi_len"])
        if bb is None or rsi is None or rsi.dropna().empty:
            return None

        bbl = float(bb.iloc[-1, 0])  # lower
        bbu = float(bb.iloc[-1, 2])  # upper
        last_c   = float(c.iloc[-1])
        last_rsi = float(rsi.iloc[-1])
        last_adx = float(adx.iloc[-1])

        if last_c <= bbl and last_rsi < s["rsi_lower"] and last_adx < s["adx_threshold"]:
            return "BUY"
        if last_c >= bbu and last_rsi > s["rsi_upper"] and last_adx < s["adx_threshold"]:
            return "SELL"
    except Exception as e:
        logger.error("Signal calc error: %s", e)
    return None


def _calc_bb_mid(df: pd.DataFrame, s: dict) -> float | None:
    try:
        bb = ta.bbands(df["Close"], length=s["bb_length"],
                       lower_std=s["bb_std"], upper_std=s["bb_std"])
        if bb is None:
            return None
        return float(bb.iloc[-1, 1])  # middle
    except Exception:
        return None


def _calc_atr(df: pd.DataFrame) -> float | None:
    try:
        atr = ta.atr(df["High"], df["Low"], df["Close"], length=14)
        return float(atr.iloc[-1]) if atr is not None else None
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# RobotManager
# ─────────────────────────────────────────────────────────────────────────────

class RobotManager:
    def __init__(self, exchange: BinanceExchange, notifier: Notifier):
        self._exchange = exchange
        self._notifier = notifier
        self._tasks: dict[str, asyncio.Task] = {}
        # symbol → { side, entry_price, stop_loss, size }
        self._positions: dict[str, dict] = {}

    # ── Управление задачами ──────────────────────────────────────────────────

    def is_running(self, symbol: str) -> bool:
        t = self._tasks.get(symbol)
        return t is not None and not t.done()

    def running_symbols(self) -> list[str]:
        return [s for s in self._tasks if self.is_running(s)]

    async def start(self, symbols: list[str], settings: dict) -> None:
        for sym in symbols:
            if self.is_running(sym):
                logger.info("[%s] Already running, skip", sym)
                continue
            task = asyncio.create_task(
                self._robot_loop(sym, dict(settings)),
                name=f"robot_{sym}",
            )
            self._tasks[sym] = task
            logger.info("[%s] Robot task started", sym)

    async def stop(self, symbols: list[str] | None = None) -> None:
        targets = symbols if symbols is not None else list(self._tasks.keys())
        for sym in targets:
            task = self._tasks.pop(sym, None)
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            logger.info("[%s] Robot task stopped", sym)

    async def stop_all(self) -> None:
        await self.stop()

    # ── Основной цикл одного символа ────────────────────────────────────────

    async def _robot_loop(self, symbol: str, settings: dict) -> None:
        interval = settings["interval"]
        mode = settings["mode"]
        logger.info(
            "[%s] Loop started | interval=%s mode=%s", symbol, interval, mode
        )

        # 1. Начальная загрузка 1000 свечей
        df = await self._download_initial(symbol, interval)
        if df is None:
            return

        # Начальная проверка сигнала (логируем, не отправляем уведомление,
        # чтобы не засорять канал старыми сигналами)
        last_signal = _calc_signal(df, settings)
        logger.info("[%s] Initial signal: %s", symbol, last_signal or "none")

        # 2. Бесконечный цикл по таймеру
        while True:
            try:
                sleep_sec = _next_candle_sleep(interval)
                logger.info("[%s] Next candle in %.0fs", symbol, sleep_sec)
                await asyncio.sleep(sleep_sec)

                # 3. Подкачать закрытую свечу
                candle = await self._exchange.get_last_closed_candle(symbol, interval)
                if candle is None:
                    logger.warning("[%s] Last candle unavailable, retry in 30s", symbol)
                    await asyncio.sleep(30)
                    continue

                # Проверяем, что это действительно новая свеча
                last_ts = df["Timestamp"].iloc[-1]
                if candle["Timestamp"] <= last_ts:
                    logger.debug("[%s] No new candle yet, waiting", symbol)
                    await asyncio.sleep(10)
                    continue

                # Добавляем и усекаем до 1000
                new_row = pd.DataFrame([candle])
                df = pd.concat([df, new_row], ignore_index=True)
                if len(df) > 1000:
                    df = df.iloc[-1000:].reset_index(drop=True)

                logger.info(
                    "[%s] Candle %s Close=%.4f",
                    symbol, candle["Timestamp"], candle["Close"],
                )

                # 4. Управление позицией (торговый режим)
                if mode == "trade" and symbol in self._positions:
                    await self._manage_position(symbol, df, settings)
                    last_signal = _calc_signal(df, settings)
                    continue  # не ищем новые входы пока есть позиция

                # 5. Расчёт сигнала
                signal = _calc_signal(df, settings)
                logger.info("[%s] Signal: %s (prev: %s)", symbol, signal, last_signal)

                if signal is not None and signal != last_signal:
                    await self._notifier.send_signal(symbol, signal)
                    if mode == "trade":
                        await self._handle_entry(symbol, signal, df, settings)

                last_signal = signal

            except asyncio.CancelledError:
                logger.info("[%s] Loop cancelled", symbol)
                raise
            except Exception as e:
                logger.error("[%s] Loop error: %s", symbol, e, exc_info=True)
                await self._notifier.send_error(f"{symbol}: {e}")
                await asyncio.sleep(60)

    # ── Вспомогательные методы ───────────────────────────────────────────────

    async def _download_initial(self, symbol: str, interval: str) -> pd.DataFrame | None:
        logger.info("[%s] Downloading 1000 candles...", symbol)
        try:
            df = await self._exchange.get_candles(symbol, interval, limit=1000)
            if df is None or df.empty:
                raise ValueError("Empty dataframe")
            logger.info("[%s] Downloaded %d candles", symbol, len(df))
            return df
        except Exception as e:
            logger.error("[%s] Initial download failed: %s", symbol, e)
            await self._notifier.send_error(f"{symbol}: ошибка загрузки свечей — {e}")
            return None

    async def _handle_entry(self, symbol: str, signal: str, df: pd.DataFrame, settings: dict) -> None:
        """Открываем позицию (торговый режим)."""
        atr = _calc_atr(df)
        if atr is None or atr == 0:
            logger.error("[%s] Cannot calculate ATR, skipping entry", symbol)
            return

        try:
            side = "BUY" if signal == "BUY" else "SELL"
            if side == "BUY":
                result = await self._exchange.open_long(
                    symbol, settings["trade_usdt"], settings.get("leverage", 5)
                )
                entry_price = float(result.get("_entry_price", 0))
                qty         = float(result.get("_quantity", 0))
                stop_loss   = entry_price - settings["sl_atr_mult"] * atr
                await self._exchange.place_stop_loss(symbol, "BUY", stop_loss)
            else:
                result = await self._exchange.open_short(
                    symbol, settings["trade_usdt"], settings.get("leverage", 5)
                )
                entry_price = float(result.get("_entry_price", 0))
                qty         = float(result.get("_quantity", 0))
                stop_loss   = entry_price + settings["sl_atr_mult"] * atr
                await self._exchange.place_stop_loss(symbol, "SELL", stop_loss)

            self._positions[symbol] = {
                "side":        "LONG" if side == "BUY" else "SHORT",
                "entry_price": entry_price,
                "stop_loss":   stop_loss,
                "size":        qty,
            }

            await self._notifier.send_trade_opened(
                symbol, side, entry_price, qty, stop_loss, settings["trade_usdt"]
            )

        except Exception as e:
            logger.error("[%s] Open position error: %s", symbol, e, exc_info=True)
            await self._notifier.send_error(f"{symbol}: ошибка открытия позиции — {e}")

    async def _manage_position(self, symbol: str, df: pd.DataFrame, settings: dict) -> None:
        """Проверяем условия выхода для открытой позиции."""
        pos = self._positions.get(symbol)
        if pos is None:
            return

        try:
            # Проверяем, жива ли позиция на бирже (мог сработать стоп)
            live = await self._exchange.get_position(symbol)
            if live is None:
                logger.info("[%s] Position closed externally (stop-loss)", symbol)
                pnl_est = (
                    (pos["stop_loss"] - pos["entry_price"]) * pos["size"]
                    if pos["side"] == "LONG"
                    else (pos["entry_price"] - pos["stop_loss"]) * pos["size"]
                )
                await self._notifier.send_trade_closed(
                    symbol, pos["side"], pos["entry_price"],
                    pos["stop_loss"], pnl_est, "STOP_LOSS"
                )
                del self._positions[symbol]
                return

            close_price = float(df["Close"].iloc[-1])
            bb_mid = _calc_bb_mid(df, settings)
            if bb_mid is None:
                return

            should_close = (
                (pos["side"] == "LONG"  and close_price >= bb_mid) or
                (pos["side"] == "SHORT" and close_price <= bb_mid)
            )

            if should_close:
                await self._exchange.close_position(symbol)
                if pos["side"] == "LONG":
                    pnl = (close_price - pos["entry_price"]) * pos["size"]
                else:
                    pnl = (pos["entry_price"] - close_price) * pos["size"]

                await self._notifier.send_trade_closed(
                    symbol, pos["side"], pos["entry_price"], close_price, pnl, "BB_MID"
                )
                logger.info("[%s] Position closed BB_MID pnl=%.2f", symbol, pnl)
                del self._positions[symbol]

        except Exception as e:
            logger.error("[%s] Manage position error: %s", symbol, e, exc_info=True)
