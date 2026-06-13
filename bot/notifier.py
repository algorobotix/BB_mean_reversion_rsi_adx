"""
Отправка уведомлений в Telegram-канал.
"""

import logging
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.enums import ParseMode

logger = logging.getLogger(__name__)


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


class Notifier:
    def __init__(self, bot: Bot, channel_id: str):
        self.bot = bot
        self.channel_id = channel_id

    async def _send(self, text: str) -> None:
        try:
            await self.bot.send_message(
                self.channel_id, text, parse_mode=ParseMode.MARKDOWN_V2
            )
        except Exception as e:
            logger.error("Telegram send failed: %s", e)

    @staticmethod
    def _esc(s: str) -> str:
        """Экранирование спецсимволов для MarkdownV2."""
        for ch in r"_*[]()~`>#+-=|{}.!":
            s = s.replace(ch, f"\\{ch}")
        return s

    async def send_signal(self, symbol: str, side: str) -> None:
        side_ru = "LONG 📈" if side == "BUY" else "SHORT 📉"
        text = (
            "🔔 *Обнаружен сигнал*\n\n"
            f"Монета: `{self._esc(symbol)}`\n"
            f"Направление: *{self._esc(side_ru)}*\n"
            f"Время: `{self._esc(_now_utc())}`"
        )
        logger.info("Signal notification → %s %s", symbol, side)
        await self._send(text)

    async def send_trade_opened(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        size: float,
        stop_loss: float,
        usdt: float,
    ) -> None:
        side_ru = "LONG" if side == "BUY" else "SHORT"
        text = (
            "✅ *Сделка открыта*\n\n"
            f"Дата/время: `{self._esc(_now_utc())}`\n"
            f"Монета: `{self._esc(symbol)}`\n"
            f"Направление: *{self._esc(side_ru)}*\n"
            f"Объём: `{self._esc(f'{usdt:.2f}')} USDT`\n"
            f"Цена входа: `{self._esc(f'{entry_price:.4f}')}`\n"
            f"Размер позиции: `{self._esc(f'{size:.6f}')}`\n"
            f"Стоп\\-лосс: `{self._esc(f'{stop_loss:.4f}')}`"
        )
        logger.info("Trade opened %s %s @ %.4f SL=%.4f", side_ru, symbol, entry_price, stop_loss)
        await self._send(text)

    async def send_trade_closed(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        exit_price: float,
        pnl: float,
        reason: str,
    ) -> None:
        emoji = "✅" if pnl >= 0 else "❌"
        pnl_str = f"{pnl:+.2f}"
        reason_map = {"BB_MID": "Достижение BB Middle", "STOP_LOSS": "Стоп\\-лосс"}
        reason_ru = reason_map.get(reason, self._esc(reason))
        text = (
            f"{emoji} *Сделка закрыта*\n\n"
            f"Дата/время: `{self._esc(_now_utc())}`\n"
            f"Монета: `{self._esc(symbol)}`\n"
            f"Направление: *{self._esc(side)}*\n"
            f"Цена входа: `{self._esc(f'{entry_price:.4f}')}`\n"
            f"Цена выхода: `{self._esc(f'{exit_price:.4f}')}`\n"
            f"PnL: `{self._esc(pnl_str)} USDT`\n"
            f"Причина: {reason_ru}"
        )
        logger.info(
            "Trade closed %s %s entry=%.4f exit=%.4f pnl=%+.2f reason=%s",
            side, symbol, entry_price, exit_price, pnl, reason,
        )
        await self._send(text)

    async def send_error(self, message: str) -> None:
        text = f"⚠️ *Ошибка робота*\n\n`{self._esc(message)}`"
        logger.warning("Error notification: %s", message)
        await self._send(text)

    async def send_info(self, message: str) -> None:
        text = f"ℹ️ {self._esc(message)}"
        await self._send(text)
