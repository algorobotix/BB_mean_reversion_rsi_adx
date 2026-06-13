"""
Telegram-бот для настройки стратегии (aiogram 3.x).

Меню:
  Главное меню
    ├── 🪙 Выбор монет    → Топ-10/20/50/100 | Ввести тикер вручную
    ├── ⏱ Таймфрейм      → 1m/5m/15m/30m/1h/4h/1d
    ├── ⚙️ Режим работы   → Сигнальный | Торговый
    ├── 📊 Параметры      → BB/RSI/ADX/SL/Trade USDT/Leverage
    ├── 📥 Импорт         → из optimization_results/ (params + символ)
    ├── ▶️/⏹ Старт/Стоп
    └── 📈 Статус
"""

import asyncio
import logging

from aiogram import Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.analytics_import import (
    fmt_f, fmt_pct, result_short_line, scan_results, verdict_emoji,
)
from bot.database import get_settings, save_settings, update_field
from bot.exchange import BinanceExchange
from bot.robot import RobotManager

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# FSM-состояния
# ─────────────────────────────────────────────────────────────────────────────

class S(StatesGroup):
    waiting_ticker      = State()
    waiting_param_value = State()
    viewing_analytics   = State()


# ─────────────────────────────────────────────────────────────────────────────
# Клавиатуры
# ─────────────────────────────────────────────────────────────────────────────

def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def kb_main(running: bool) -> InlineKeyboardMarkup:
    run_row = (
        [_btn("⏹ Остановить робота", "stop_robot")]
        if running
        else [_btn("▶️ Запустить робота", "start_robot")]
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("🪙 Выбор монет",         "menu_coins")],
        [_btn("⏱ Таймфрейм",           "menu_interval")],
        [_btn("⚙️ Режим работы",        "menu_mode")],
        [_btn("📊 Параметры стратегии", "menu_params")],
        [_btn("📥 Импорт из аналитики", "menu_import")],
        run_row,
        [_btn("📈 Статус", "menu_status")],
    ])


def kb_coins() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("Топ-10 по капитализации",  "coins_top_10")],
        [_btn("Топ-20 по капитализации",  "coins_top_20")],
        [_btn("Топ-50 по капитализации",  "coins_top_50")],
        [_btn("Топ-100 по капитализации", "coins_top_100")],
        [_btn("✏️ Ввести тикер вручную",  "coins_custom")],
        [_btn("◀️ Назад", "menu_back")],
    ])


def kb_interval() -> InlineKeyboardMarkup:
    tfs = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
    rows = []
    for i in range(0, len(tfs), 4):
        rows.append([_btn(tf, f"iv_{tf}") for tf in tfs[i:i + 4]])
    rows.append([_btn("◀️ Назад", "menu_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_mode() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("📡 Сигнальный (только уведомления)", "mode_signal")],
        [_btn("💰 Торговый (открывать сделки)",     "mode_trade")],
        [_btn("◀️ Назад", "menu_back")],
    ])


def kb_params() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("BB Period",       "p_bb_length"),   _btn("BB Std Dev",       "p_bb_std")],
        [_btn("RSI Period",      "p_rsi_len"),      _btn("RSI Upper",        "p_rsi_upper")],
        [_btn("RSI Lower",       "p_rsi_lower"),    _btn("ADX Period",       "p_adx_len")],
        [_btn("ADX Threshold",   "p_adx_threshold"), _btn("SL × ATR",        "p_sl_atr_mult")],
        [_btn("Trade USDT",      "p_trade_usdt"),   _btn("Leverage",         "p_leverage")],
        [_btn("◀️ Назад", "menu_back")],
    ])


def kb_status() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("🔄 Обновить", "menu_status")],
        [_btn("◀️ Назад",    "menu_back")],
    ])


# ─────────────────────────────────────────────────────────────────────────────
# Мета-данные параметров
# ─────────────────────────────────────────────────────────────────────────────

PARAM_LABEL: dict[str, str] = {
    "bb_length":     "BB Period (период Bollinger Bands)",
    "bb_std":        "BB Std Dev (число стандартных отклонений)",
    "rsi_len":       "RSI Period",
    "rsi_upper":     "RSI Upper (уровень перекупленности)",
    "rsi_lower":     "RSI Lower (уровень перепроданности)",
    "adx_len":       "ADX Period",
    "adx_threshold": "ADX Threshold (порог силы тренда)",
    "sl_atr_mult":   "Stop-Loss = entry ± N × ATR",
    "trade_usdt":    "Размер сделки (USDT, до левериджа)",
    "leverage":      "Кредитное плечо (x)",
}

PARAM_TYPE: dict[str, type] = {
    "bb_length": int, "rsi_len": int, "rsi_upper": int, "rsi_lower": int,
    "adx_len": int, "adx_threshold": int, "leverage": int,
    "bb_std": float, "sl_atr_mult": float, "trade_usdt": float,
}


# ─────────────────────────────────────────────────────────────────────────────
# Регистрация хендлеров
# ─────────────────────────────────────────────────────────────────────────────

def setup_bot_handlers(dp: Dispatcher, robot: RobotManager, exchange: BinanceExchange) -> None:

    # ── /start ───────────────────────────────────────────────────────────────

    @dp.message(Command("start"))
    async def cmd_start(msg: Message, state: FSMContext) -> None:
        await state.clear()
        s = await get_settings(msg.from_user.id)
        running = any(robot.is_running(sym) for sym in s.get("symbols", []))
        await msg.answer(
            "👋 *AlgoRobotix — BB Mean Reversion*\n\n"
            "Настройте параметры и запустите робота\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=kb_main(running),
        )

    # ── Назад в меню ─────────────────────────────────────────────────────────

    @dp.callback_query(F.data == "menu_back")
    async def back(cb: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        s = await get_settings(cb.from_user.id)
        running = any(robot.is_running(sym) for sym in s.get("symbols", []))
        await cb.message.edit_text(
            "📋 *Главное меню*",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=kb_main(running),
        )

    # ══════════════════════════════════════════════════════════════════════════
    # МОНЕТЫ
    # ══════════════════════════════════════════════════════════════════════════

    @dp.callback_query(F.data == "menu_coins")
    async def coins_menu(cb: CallbackQuery) -> None:
        s = await get_settings(cb.from_user.id)
        syms = s.get("symbols", [])
        current = ", ".join(syms) if syms else "—"
        await cb.message.edit_text(
            f"🪙 *Выбор монет*\n\nТекущий список: `{current}`",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=kb_coins(),
        )

    @dp.callback_query(F.data.startswith("coins_top_"))
    async def select_top_coins(cb: CallbackQuery) -> None:
        n = int(cb.data.replace("coins_top_", ""))
        await cb.message.edit_text(f"⏳ Загружаю топ\\-{n} монет…",
                                   parse_mode=ParseMode.MARKDOWN_V2)
        try:
            from feed.candles import get_binance_top_by_cap
            loop = asyncio.get_event_loop()
            coins = await loop.run_in_executor(None, get_binance_top_by_cap)
            if not coins:
                await cb.message.edit_text(
                    "❌ Не удалось получить список монет\\. Попробуйте позже\\.",
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=kb_coins(),
                )
                return
            selected = [c["symbol"].upper() + "USDT" for c in coins[:n]]
            await update_field(cb.from_user.id, "symbols", selected)

            preview = "\n".join(f"  • {s}" for s in selected[:10])
            if len(selected) > 10:
                preview += f"\n  …и ещё {len(selected) - 10}"

            await cb.message.edit_text(
                f"✅ *Выбраны топ\\-{n} монет:*\n{preview}",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=kb_coins(),
            )
        except Exception as e:
            logger.error("Top coins error: %s", e)
            await cb.message.edit_text(
                f"❌ Ошибка: `{e}`",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=kb_coins(),
            )

    @dp.callback_query(F.data == "coins_custom")
    async def ask_ticker(cb: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(S.waiting_ticker)
        await cb.message.edit_text(
            "✏️ Введите тикер\\(ы\\) через пробел или запятую:\n"
            "`BTCUSDT` или `BTCUSDT, ETHUSDT, SOLUSDT`\n\n"
            "_Тикеры будут проверены на Binance Futures\\._",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    @dp.message(S.waiting_ticker)
    async def process_ticker(msg: Message, state: FSMContext) -> None:
        raw = msg.text.replace(",", " ").split()
        tickers = [t.strip().upper() for t in raw if t.strip()]

        if not tickers:
            await msg.answer("❌ Введите хотя бы один тикер.")
            return

        await msg.answer("⏳ Проверяю тикеры на Binance Futures…")
        valid, invalid = [], []
        for t in tickers:
            sym = t if t.endswith("USDT") else t + "USDT"
            if await exchange.validate_symbol(sym):
                valid.append(sym)
            else:
                invalid.append(sym)

        lines = []
        if invalid:
            lines.append("⚠️ Не найдены: " + ", ".join(f"`{s}`" for s in invalid))
        if valid:
            s = await get_settings(msg.from_user.id)
            merged = list(dict.fromkeys(s.get("symbols", []) + valid))
            await update_field(msg.from_user.id, "symbols", merged)
            lines.append("✅ Добавлены: " + ", ".join(f"`{s}`" for s in valid))
            lines.append(f"Всего монет: {len(merged)}")
        else:
            lines.append("❌ Ни один тикер не найден\\.")

        await msg.answer(
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=kb_coins(),
        )
        await state.clear()

    # ══════════════════════════════════════════════════════════════════════════
    # ТАЙМФРЕЙМ
    # ══════════════════════════════════════════════════════════════════════════

    @dp.callback_query(F.data == "menu_interval")
    async def interval_menu(cb: CallbackQuery) -> None:
        s = await get_settings(cb.from_user.id)
        cur = s.get("interval", "1h")
        await cb.message.edit_text(
            f"⏱ *Таймфрейм*\n\nТекущий: `{cur}`\n\nВыберите:",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=kb_interval(),
        )

    @dp.callback_query(F.data.startswith("iv_"))
    async def select_interval(cb: CallbackQuery) -> None:
        iv = cb.data.replace("iv_", "")
        await update_field(cb.from_user.id, "interval", iv)
        await cb.message.edit_text(
            f"✅ Таймфрейм: `{iv}`",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=kb_interval(),
        )
        await cb.answer(f"Таймфрейм: {iv}")

    # ══════════════════════════════════════════════════════════════════════════
    # РЕЖИМ
    # ══════════════════════════════════════════════════════════════════════════

    @dp.callback_query(F.data == "menu_mode")
    async def mode_menu(cb: CallbackQuery) -> None:
        s = await get_settings(cb.from_user.id)
        cur = "Сигнальный" if s.get("mode") == "signal" else "Торговый"
        await cb.message.edit_text(
            f"⚙️ *Режим работы*\n\nТекущий: *{cur}*",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=kb_mode(),
        )

    @dp.callback_query(F.data.startswith("mode_"))
    async def select_mode(cb: CallbackQuery) -> None:
        mode = cb.data.replace("mode_", "")
        await update_field(cb.from_user.id, "mode", mode)
        name = "Сигнальный" if mode == "signal" else "Торговый"
        await cb.message.edit_text(
            f"✅ Режим: *{name}*",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=kb_mode(),
        )
        await cb.answer(f"Режим: {name}")

    # ══════════════════════════════════════════════════════════════════════════
    # ПАРАМЕТРЫ СТРАТЕГИИ
    # ══════════════════════════════════════════════════════════════════════════

    @dp.callback_query(F.data == "menu_params")
    async def params_menu(cb: CallbackQuery) -> None:
        s = await get_settings(cb.from_user.id)
        text = (
            "📊 *Параметры стратегии*\n\n"
            f"BB Period: `{s['bb_length']}` | BB Std: `{s['bb_std']}`\n"
            f"RSI Period: `{s['rsi_len']}` | Upper: `{s['rsi_upper']}` | Lower: `{s['rsi_lower']}`\n"
            f"ADX Period: `{s['adx_len']}` | Threshold: `{s['adx_threshold']}`\n"
            f"SL × ATR: `{s['sl_atr_mult']}` | Trade: `{s['trade_usdt']}` USDT | Leverage: `{s.get('leverage', 5)}x`\n\n"
            "Нажмите параметр для изменения:"
        )
        await cb.message.edit_text(
            text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb_params()
        )

    @dp.callback_query(F.data.startswith("p_"))
    async def select_param(cb: CallbackQuery, state: FSMContext) -> None:
        param = cb.data[2:]  # убираем "p_"
        s = await get_settings(cb.from_user.id)
        cur = s.get(param, "—")
        label = PARAM_LABEL.get(param, param)
        await state.update_data(param=param)
        await state.set_state(S.waiting_param_value)
        await cb.message.edit_text(
            f"✏️ *{label}*\n\nТекущее значение: `{cur}`\n\nВведите новое:",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    @dp.message(S.waiting_param_value)
    async def process_param(msg: Message, state: FSMContext) -> None:
        data = await state.get_data()
        param = data.get("param")
        if not param:
            await state.clear()
            return
        try:
            typ = PARAM_TYPE.get(param, float)
            value = typ(msg.text.strip())
            await update_field(msg.from_user.id, param, value)
            label = PARAM_LABEL.get(param, param)
            await msg.answer(
                f"✅ *{label}*: `{value}`",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=kb_params(),
            )
        except ValueError:
            await msg.answer("❌ Неверное значение\\. Ожидается число\\.",
                             parse_mode=ParseMode.MARKDOWN_V2,
                             reply_markup=kb_params())
        await state.clear()

    # ══════════════════════════════════════════════════════════════════════════
    # ЗАПУСК / ОСТАНОВКА РОБОТА
    # ══════════════════════════════════════════════════════════════════════════

    @dp.callback_query(F.data == "start_robot")
    async def start_robot(cb: CallbackQuery) -> None:
        s = await get_settings(cb.from_user.id)
        symbols = s.get("symbols", [])
        if not symbols:
            await cb.answer("⚠️ Сначала выберите монеты!", show_alert=True)
            return

        await cb.message.edit_text(f"⏳ Запускаю робота для {len(symbols)} монет…")
        await robot.start(symbols, s)
        await update_field(cb.from_user.id, "is_running", True)

        mode_name = "Сигнальный" if s["mode"] == "signal" else "Торговый"
        preview = ", ".join(symbols[:5]) + (f" +{len(symbols)-5}" if len(symbols) > 5 else "")
        await cb.message.edit_text(
            f"✅ *Робот запущен\\!*\n\n"
            f"Монеты: `{preview}`\n"
            f"Таймфрейм: `{s['interval']}`\n"
            f"Режим: `{mode_name}`",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=kb_main(True),
        )
        logger.info("User %s started robot for %s", cb.from_user.id, symbols)

    @dp.callback_query(F.data == "stop_robot")
    async def stop_robot(cb: CallbackQuery) -> None:
        s = await get_settings(cb.from_user.id)
        await robot.stop(s.get("symbols", []))
        await update_field(cb.from_user.id, "is_running", False)
        await cb.message.edit_text(
            "⏹ *Робот остановлен\\.*",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=kb_main(False),
        )
        await cb.answer("Робот остановлен")
        logger.info("User %s stopped robot", cb.from_user.id)

    # ══════════════════════════════════════════════════════════════════════════
    # СТАТУС
    # ══════════════════════════════════════════════════════════════════════════

    # ══════════════════════════════════════════════════════════════════════════
    # ИМПОРТ ИЗ АНАЛИТИКИ
    # ══════════════════════════════════════════════════════════════════════════

    _PAGE_SIZE = 5  # результатов на одну страницу

    def _esc_md(s: str) -> str:
        for ch in r"_*[]()~`>#+-=|{}.!":
            s = s.replace(ch, f"\\{ch}")
        return s

    def _kb_results_list(results: list, page: int) -> InlineKeyboardMarkup:
        """Клавиатура со списком результатов (с пагинацией)."""
        total = len(results)
        start = page * _PAGE_SIZE
        page_items = results[start : start + _PAGE_SIZE]

        rows = []
        for local_i, r in enumerate(page_items):
            global_i = start + local_i
            rows.append([_btn(result_short_line(r), f"ai_pick_{global_i}")])

        nav = []
        if page > 0:
            nav.append(_btn("◀️ Пред.", f"ai_page_{page - 1}"))
        total_pages = (total - 1) // _PAGE_SIZE + 1
        nav.append(_btn(f"{page + 1}/{total_pages}", "ai_noop"))
        if start + _PAGE_SIZE < total:
            nav.append(_btn("След. ▶️", f"ai_page_{page + 1}"))
        if nav:
            rows.append(nav)

        rows.append([_btn("◀️ Назад", "menu_back")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def _kb_result_detail(idx: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [_btn("📊 Импортировать только параметры", f"ai_do_params_{idx}")],
            [_btn("📊➕🪙 Параметры + добавить монету в список", f"ai_do_all_{idx}")],
            [_btn("◀️ К списку", "ai_page_0")],
            [_btn("◀️ Главное меню", "menu_back")],
        ])

    def _render_result_detail(r: dict, idx: int) -> str:
        """Полное описание одного результата оптимизации (MarkdownV2)."""
        v = r["overfit"].get("verdict", "?")
        flags = r["overfit"].get("flags", [])
        p = r["params"]
        tr = r["train"]
        oos = r["oos"]

        lines = [
            f"📋 *Результат оптимизации*",
            f"",
            f"Символ: `{_esc_md(r['symbol'])}`",
            f"Дата: `{_esc_md(r['timestamp'].strftime('%Y\\-%m\\-%d %H:%M:%S'))}`",
            f"Переобучение: {verdict_emoji(v)} *{_esc_md(v)}*",
        ]
        if flags:
            lines.append("")
            lines.append("*Предупреждения:*")
            for f in flags:
                lines.append(f"  ⚠️ {_esc_md(f)}")

        lines += [
            "",
            "*Параметры стратегии:*",
            f"  BB Period: `{p.get('bb_length', '—')}` | BB Std: `{p.get('bb_std', '—')}`",
            f"  RSI Period: `{p.get('rsi_len', '—')}` | Lower: `{p.get('rsi_lower', '—')}` | Upper: `{p.get('rsi_upper', '—')}`",
            f"  ADX Period: `{p.get('adx_len', '—')}` | Threshold: `{p.get('adx_threshold', '—')}`",
            f"  SL × ATR: `{p.get('sl_atr_mult', '—')}`",
            "",
            f"{'Метрика':<18} {'IS':>10} {'OOS':>10}",
            f"{'─'*40}",
            f"{'Return %':<18} {_esc_md(fmt_pct(tr.get('return_pct'))):>10} {_esc_md(fmt_pct(oos.get('return_pct'))):>10}",
            f"{'Sharpe':<18} {_esc_md(fmt_f(tr.get('sharpe'))):>10} {_esc_md(fmt_f(oos.get('sharpe'))):>10}",
            f"{'Win Rate':<18} {_esc_md(fmt_pct(float(tr.get('win_rate', 0)) * 100)):>10} {_esc_md(fmt_pct(float(oos.get('win_rate', 0)) * 100)):>10}",
            f"{'Max DD':<18} {_esc_md(fmt_pct(float(tr.get('max_dd', 0)) * 100)):>10} {_esc_md(fmt_pct(float(oos.get('max_dd', 0)) * 100)):>10}",
            f"{'Profit Factor':<18} {_esc_md(fmt_f(tr.get('profit_factor'))):>10} {_esc_md(fmt_f(oos.get('profit_factor'))):>10}",
            f"{'Trades':<18} {str(tr.get('total_trades', '—')):>10} {str(oos.get('total_trades', '—')):>10}",
        ]
        return "\n".join(lines)

    @dp.callback_query(F.data == "menu_import")
    async def import_menu(cb: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(S.viewing_analytics)
        results = scan_results()
        if not results:
            await cb.message.edit_text(
                "📥 *Импорт из аналитики*\n\n"
                "Результатов оптимизации не найдено\\.\n"
                "Запустите `optimization/optimizer\\.py` для нужного символа\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [_btn("◀️ Назад", "menu_back")]
                ]),
            )
            return
        await state.update_data(results=results)
        await cb.message.edit_text(
            f"📥 *Импорт из аналитики*\n\nНайдено результатов: *{len(results)}*\nВыберите:",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_kb_results_list(results, page=0),
        )

    @dp.callback_query(F.data.startswith("ai_page_"))
    async def analytics_page(cb: CallbackQuery, state: FSMContext) -> None:
        page = int(cb.data.replace("ai_page_", ""))
        data = await state.get_data()
        results = data.get("results") or scan_results()
        await state.update_data(results=results)
        await cb.message.edit_text(
            f"📥 *Импорт из аналитики*\n\nНайдено результатов: *{len(results)}*\nВыберите:",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_kb_results_list(results, page=page),
        )

    @dp.callback_query(F.data == "ai_noop")
    async def analytics_noop(cb: CallbackQuery) -> None:
        await cb.answer()

    @dp.callback_query(F.data.startswith("ai_pick_"))
    async def analytics_pick(cb: CallbackQuery, state: FSMContext) -> None:
        idx = int(cb.data.replace("ai_pick_", ""))
        data = await state.get_data()
        results = data.get("results") or scan_results()
        if idx >= len(results):
            await cb.answer("Результат не найден", show_alert=True)
            return
        r = results[idx]
        await cb.message.edit_text(
            _render_result_detail(r, idx),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_kb_result_detail(idx),
        )

    async def _do_import(cb: CallbackQuery, state: FSMContext, add_symbol: bool) -> None:
        idx_str = cb.data.split("_")[-1]
        idx = int(idx_str)
        data = await state.get_data()
        results = data.get("results") or scan_results()
        if idx >= len(results):
            await cb.answer("Результат не найден", show_alert=True)
            return
        r = results[idx]

        s = await get_settings(cb.from_user.id)
        # Импортируем только параметры стратегии (не трогаем символы/режим/TF)
        importable = {
            "bb_length", "bb_std", "rsi_len", "rsi_upper",
            "rsi_lower", "adx_len", "adx_threshold", "sl_atr_mult",
        }
        for k, v in r["params"].items():
            if k in importable:
                s[k] = v

        if add_symbol:
            sym = r["symbol"]
            if sym not in s["symbols"]:
                s["symbols"].append(sym)

        await save_settings(cb.from_user.id, s)
        await state.clear()

        added_note = f"\nМонета `{_esc_md(r['symbol'])}` добавлена в список\\." if add_symbol else ""
        await cb.message.edit_text(
            f"✅ *Параметры импортированы\\!*{added_note}\n\n"
            f"BB: `{s['bb_length']}` / `{s['bb_std']}` | "
            f"RSI: `{s['rsi_len']}` \\(`{s['rsi_lower']}`\\-`{s['rsi_upper']}`\\)\n"
            f"ADX: `{s['adx_len']}` / `<{s['adx_threshold']}` | SL×ATR: `{s['sl_atr_mult']}`",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [_btn("◀️ Главное меню", "menu_back")]
            ]),
        )
        logger.info(
            "User %s imported params from %s (add_symbol=%s)",
            cb.from_user.id, r["folder_name"], add_symbol,
        )

    @dp.callback_query(F.data.startswith("ai_do_params_"))
    async def analytics_import_params(cb: CallbackQuery, state: FSMContext) -> None:
        await _do_import(cb, state, add_symbol=False)

    @dp.callback_query(F.data.startswith("ai_do_all_"))
    async def analytics_import_all(cb: CallbackQuery, state: FSMContext) -> None:
        await _do_import(cb, state, add_symbol=True)

    # ══════════════════════════════════════════════════════════════════════════
    # СТАТУС
    # ══════════════════════════════════════════════════════════════════════════

    @dp.callback_query(F.data == "menu_status")
    async def show_status(cb: CallbackQuery) -> None:
        s = await get_settings(cb.from_user.id)
        syms = s.get("symbols", [])
        running_set = set(robot.running_symbols())
        mode_name = "Сигнальный" if s.get("mode") == "signal" else "Торговый"
        is_on = bool(running_set & set(syms))

        status = "🟢 Работает" if is_on else "🔴 Остановлен"
        sym_lines = "\n".join(
            f"  {'🟢' if sym in running_set else '⚪'} {sym}" for sym in syms
        ) or "  —"

        text = (
            f"📈 *Статус*\n\n"
            f"Состояние: {status}\n"
            f"Таймфрейм: `{s.get('interval', '1h')}`\n"
            f"Режим: `{mode_name}`\n\n"
            f"*Монеты \\({len(syms)}\\):*\n{sym_lines}\n\n"
            f"*Стратегия:* BB\\({s['bb_length']}, {s['bb_std']}\\) "
            f"RSI\\({s['rsi_len']}, {s['rsi_lower']}\\-{s['rsi_upper']}\\) "
            f"ADX\\({s['adx_len']}, \\<{s['adx_threshold']}\\)"
        )
        await cb.message.edit_text(
            text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb_status()
        )
