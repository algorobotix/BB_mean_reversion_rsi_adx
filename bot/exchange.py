"""
Обёртка над Binance Futures REST API.
  - Публичные методы (validate_symbol, get_candles, get_last_closed_candle) —
    не требуют API-ключей.
  - Торговые методы (open_long, open_short, close_position, place_stop_loss) —
    требуют BINANCE_API_KEY / BINANCE_API_SECRET в .env.
"""

import hashlib
import hmac
import logging
import time
from urllib.parse import urlencode

import aiohttp
import pandas as pd

logger = logging.getLogger(__name__)

_FAPI = "https://fapi.binance.com"
_TIMEOUT = aiohttp.ClientTimeout(total=15)


class BinanceExchange:
    def __init__(self, api_key: str = "", api_secret: str = ""):
        self._api_key = api_key
        self._api_secret = api_secret
        self._futures_symbols: set[str] = set()

    # ─────────────────────────────────────────────
    # Авторизация
    # ─────────────────────────────────────────────

    def _sign(self, params: dict) -> str:
        payload = urlencode(params)
        return hmac.new(
            self._api_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _auth_headers(self) -> dict:
        return {"X-MBX-APIKEY": self._api_key}

    # ─────────────────────────────────────────────
    # Публичные запросы
    # ─────────────────────────────────────────────

    async def get_futures_symbols(self) -> set[str]:
        """Кэшированный список всех активных USDT-фьючерсов."""
        if self._futures_symbols:
            return self._futures_symbols
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.get(f"{_FAPI}/fapi/v1/exchangeInfo") as resp:
                data = await resp.json()
        self._futures_symbols = {
            s["symbol"]
            for s in data.get("symbols", [])
            if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"
        }
        logger.debug("Binance Futures: %d symbols cached", len(self._futures_symbols))
        return self._futures_symbols

    async def validate_symbol(self, symbol: str) -> bool:
        symbols = await self.get_futures_symbols()
        return symbol.upper() in symbols

    async def get_candles(self, symbol: str, interval: str, limit: int = 1000) -> pd.DataFrame | None:
        """Скачивает последние `limit` свечей через существующий feed/candles."""
        from feed.candles import get_df_async
        try:
            df = await get_df_async(symbol, interval)
            return df
        except Exception as e:
            logger.error("get_candles %s %s: %s", symbol, interval, e)
            return None

    async def get_last_closed_candle(self, symbol: str, interval: str) -> pd.Series | None:
        """
        Запрашивает 2 последних свечи и возвращает предпоследнюю (закрытую).
        Последняя свеча ещё открыта, поэтому нужна именно вторая с конца.
        """
        params = {"symbol": symbol, "interval": interval, "limit": 2}
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.get(f"{_FAPI}/fapi/v1/klines", params=params) as resp:
                if resp.status != 200:
                    logger.error("klines %s: HTTP %s", symbol, resp.status)
                    return None
                data = await resp.json()

        if not data or len(data) < 2:
            return None

        c = data[-2]  # закрытая свеча
        return pd.Series({
            "Timestamp": pd.to_datetime(int(c[0]), unit="ms"),
            "Open":  float(c[1]),
            "High":  float(c[2]),
            "Low":   float(c[3]),
            "Close": float(c[4]),
            "Volume": float(c[5]),
        })

    async def get_current_price(self, symbol: str) -> float:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.get(
                f"{_FAPI}/fapi/v1/ticker/price", params={"symbol": symbol}
            ) as resp:
                data = await resp.json()
        return float(data["price"])

    # ─────────────────────────────────────────────
    # Авторизованные торговые запросы
    # ─────────────────────────────────────────────

    async def _signed_get(self, path: str, extra: dict) -> dict | list:
        params = {**extra, "timestamp": int(time.time() * 1000)}
        params["signature"] = self._sign(params)
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.get(
                f"{_FAPI}{path}", params=params, headers=self._auth_headers()
            ) as resp:
                return await resp.json()

    async def _signed_post(self, path: str, extra: dict) -> dict:
        params = {**extra, "timestamp": int(time.time() * 1000)}
        params["signature"] = self._sign(params)
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(
                f"{_FAPI}{path}", params=params, headers=self._auth_headers()
            ) as resp:
                return await resp.json()

    async def get_balance(self) -> float:
        data = await self._signed_get("/fapi/v2/balance", {})
        for asset in (data if isinstance(data, list) else []):
            if asset.get("asset") == "USDT":
                return float(asset["availableBalance"])
        return 0.0

    async def get_position(self, symbol: str) -> dict | None:
        data = await self._signed_get("/fapi/v2/positionRisk", {"symbol": symbol})
        for pos in (data if isinstance(data, list) else []):
            if pos.get("symbol") == symbol and float(pos.get("positionAmt", 0)) != 0:
                return pos
        return None

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        return await self._signed_post(
            "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}
        )

    async def _place_market_order(self, symbol: str, side: str, quantity: float) -> dict:
        result = await self._signed_post("/fapi/v1/order", {
            "symbol":   symbol,
            "side":     side,          # "BUY" | "SELL"
            "type":     "MARKET",
            "quantity": f"{quantity:.6f}",
        })
        logger.info("Market order %s %s qty=%.6f → %s", side, symbol, quantity, result)
        return result

    async def place_stop_loss(self, symbol: str, entry_side: str, stop_price: float) -> dict:
        """STOP_MARKET closePosition=true (автоматически закрывает позицию)."""
        close_side = "SELL" if entry_side == "BUY" else "BUY"
        result = await self._signed_post("/fapi/v1/order", {
            "symbol":        symbol,
            "side":          close_side,
            "type":          "STOP_MARKET",
            "stopPrice":     f"{stop_price:.4f}",
            "closePosition": "true",
        })
        logger.info("Stop-loss placed %s @ %.4f → %s", symbol, stop_price, result)
        return result

    async def _cancel_all_orders(self, symbol: str) -> dict:
        return await self._signed_post("/fapi/v1/allOpenOrders", {"symbol": symbol})

    async def open_long(self, symbol: str, usdt_amount: float, leverage: int) -> dict:
        await self.set_leverage(symbol, leverage)
        price = await self.get_current_price(symbol)
        qty = round((usdt_amount * leverage) / price, 6)
        result = await self._place_market_order(symbol, "BUY", qty)
        result["_entry_price"] = price
        result["_quantity"] = qty
        return result

    async def open_short(self, symbol: str, usdt_amount: float, leverage: int) -> dict:
        await self.set_leverage(symbol, leverage)
        price = await self.get_current_price(symbol)
        qty = round((usdt_amount * leverage) / price, 6)
        result = await self._place_market_order(symbol, "SELL", qty)
        result["_entry_price"] = price
        result["_quantity"] = qty
        return result

    async def close_position(self, symbol: str) -> dict:
        pos = await self.get_position(symbol)
        if pos is None:
            return {"msg": "no open position"}
        amt = float(pos["positionAmt"])
        side = "SELL" if amt > 0 else "BUY"
        return await self._place_market_order(symbol, side, abs(amt))
