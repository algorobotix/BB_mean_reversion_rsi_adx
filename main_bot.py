"""
Точка входа: запуск Telegram-бота + робота в одном event loop.

Запуск:
    python main_bot.py

Переменные окружения (.env):
    BOT_TOKEN         — токен Telegram-бота
    TG_CHANNEL_ID     — ID/username канала для уведомлений (например @my_channel)
    BINANCE_API_KEY   — ключ Binance Futures (только для торгового режима)
    BINANCE_API_SECRET — секрет Binance Futures (только для торгового режима)
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

# Windows + aiohttp/SSL: SelectorEventLoop обязателен
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

load_dotenv()

BOT_TOKEN         = os.getenv("BOT_TOKEN", "")
TG_CHANNEL_ID     = os.getenv("TG_CHANNEL_ID", "")
BINANCE_API_KEY   = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")


def _setup_logging() -> None:
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)

    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(logs_dir / "robot.log", encoding="utf-8"),
    ]
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)
    # Сторонние библиотеки — только WARNING
    for lib in ("aiohttp", "aiogram", "asyncio"):
        logging.getLogger(lib).setLevel(logging.WARNING)


async def main() -> None:
    _setup_logging()
    logger = logging.getLogger(__name__)

    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN не задан в .env")
        sys.exit(1)
    if not TG_CHANNEL_ID:
        logger.warning("TG_CHANNEL_ID не задан — уведомления в канал не будут отправляться")

    from bot.database import init_db, reset_running_state
    from bot.exchange import BinanceExchange
    from bot.notifier import Notifier
    from bot.robot import RobotManager
    from bot.tg_bot import setup_bot_handlers

    await init_db()
    await reset_running_state()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN_V2),
    )
    dp = Dispatcher(storage=MemoryStorage())

    exchange = BinanceExchange(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)
    notifier = Notifier(bot=bot, channel_id=TG_CHANNEL_ID)
    robot    = RobotManager(exchange=exchange, notifier=notifier)

    setup_bot_handlers(dp, robot, exchange)

    logger.info("AlgoRobotix Bot запущен")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await robot.stop_all()
        await bot.session.close()
        logger.info("Бот остановлен")


if __name__ == "__main__":
    asyncio.run(main())
