"""
SQLite-хранилище настроек пользователей (aiosqlite, async).
"""

import json
import logging
from datetime import datetime
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "data" / "settings.db"

DEFAULT_SETTINGS: dict = {
    "symbols": [],
    "interval": "1h",
    "mode": "signal",        # 'signal' | 'trade'
    "bb_length": 20,
    "bb_std": 2.0,
    "rsi_len": 14,
    "rsi_upper": 70,
    "rsi_lower": 30,
    "adx_len": 14,
    "adx_threshold": 30,
    "sl_atr_mult": 2.5,
    "trade_usdt": 100.0,
    "leverage": 5,
    "is_running": False,
}

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS user_settings (
    user_id       INTEGER PRIMARY KEY,
    symbols       TEXT    DEFAULT '[]',
    interval      TEXT    DEFAULT '1h',
    mode          TEXT    DEFAULT 'signal',
    bb_length     INTEGER DEFAULT 20,
    bb_std        REAL    DEFAULT 2.0,
    rsi_len       INTEGER DEFAULT 14,
    rsi_upper     INTEGER DEFAULT 70,
    rsi_lower     INTEGER DEFAULT 30,
    adx_len       INTEGER DEFAULT 14,
    adx_threshold INTEGER DEFAULT 30,
    sl_atr_mult   REAL    DEFAULT 2.5,
    trade_usdt    REAL    DEFAULT 100.0,
    leverage      INTEGER DEFAULT 5,
    is_running    INTEGER DEFAULT 0,
    updated_at    TEXT
)
"""

_UPSERT = """
INSERT INTO user_settings (
    user_id, symbols, interval, mode,
    bb_length, bb_std, rsi_len, rsi_upper, rsi_lower,
    adx_len, adx_threshold, sl_atr_mult, trade_usdt, leverage,
    is_running, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(user_id) DO UPDATE SET
    symbols       = excluded.symbols,
    interval      = excluded.interval,
    mode          = excluded.mode,
    bb_length     = excluded.bb_length,
    bb_std        = excluded.bb_std,
    rsi_len       = excluded.rsi_len,
    rsi_upper     = excluded.rsi_upper,
    rsi_lower     = excluded.rsi_lower,
    adx_len       = excluded.adx_len,
    adx_threshold = excluded.adx_threshold,
    sl_atr_mult   = excluded.sl_atr_mult,
    trade_usdt    = excluded.trade_usdt,
    leverage      = excluded.leverage,
    is_running    = excluded.is_running,
    updated_at    = excluded.updated_at
"""


async def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(_CREATE_TABLE)
        await db.commit()
    logger.info("Database initialised: %s", DB_PATH)


async def reset_running_state() -> None:
    """Сбрасываем is_running при старте бота (задачи в памяти не сохраняются)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE user_settings SET is_running = 0")
        await db.commit()


async def get_settings(user_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM user_settings WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()

    if row is None:
        return {**DEFAULT_SETTINGS, "user_id": user_id}

    data = dict(row)
    data["symbols"] = json.loads(data.get("symbols") or "[]")
    data["is_running"] = bool(data.get("is_running", 0))
    return data


async def save_settings(user_id: int, settings: dict) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(_UPSERT, (
            user_id,
            json.dumps(settings.get("symbols", []), ensure_ascii=False),
            settings.get("interval", "1h"),
            settings.get("mode", "signal"),
            int(settings.get("bb_length", 20)),
            float(settings.get("bb_std", 2.0)),
            int(settings.get("rsi_len", 14)),
            int(settings.get("rsi_upper", 70)),
            int(settings.get("rsi_lower", 30)),
            int(settings.get("adx_len", 14)),
            int(settings.get("adx_threshold", 30)),
            float(settings.get("sl_atr_mult", 2.5)),
            float(settings.get("trade_usdt", 100.0)),
            int(settings.get("leverage", 5)),
            1 if settings.get("is_running") else 0,
            datetime.utcnow().isoformat(),
        ))
        await db.commit()
    logger.debug("Settings saved for user %s", user_id)


async def update_field(user_id: int, field: str, value) -> None:
    s = await get_settings(user_id)
    s[field] = value
    await save_settings(user_id, s)
