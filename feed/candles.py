"""
Асинхронная загрузка свечей с Binance Futures.

Rate limits (Binance Futures):
  - 2400 weight/minute rolling window
  - GET /fapi/v1/klines  limit=1000 → weight 10
  - Максимум: 240 klines-запросов/мин = 4/сек
  - Таргет: 3 req/sec (75% бюджета)

На 429 → ждём Retry-After + exponential backoff.
На 418 → IP забанен, ждём дольше.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows cp1251 терминал не умеет ✗/✓ — принудительно utf-8
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import asyncio
import os
import sys
import time
import aiohttp
import requests
import pandas as pd
from datetime import datetime

# Windows ProactorEventLoop + aiohttp/SSL не совместимы — переключаем на SelectorEventLoop
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from config.settings import top_by_cap


# ─────────────────────────────────────────────────────────────────────────────
# Константы Binance
# ─────────────────────────────────────────────────────────────────────────────

_KLINES_URL      = "https://fapi.binance.com/fapi/v1/klines"
_EXCHANGE_URL    = "https://fapi.binance.com/fapi/v1/exchangeInfo"
_KLINES_WEIGHT   = 10          # вес одного запроса klines (limit=1000)
_WEIGHT_BUDGET   = 2400        # лимит веса в минуту
_TARGET_RPS      = 3.0         # запросов/сек (75% от 4 req/sec)
_MAX_CONCURRENT  = 5           # максимум параллельных соединений
_MAX_RETRIES     = 5           # попыток на один батч
_BATCH_LIMIT     = 1000        # свечей за один запрос


# ─────────────────────────────────────────────────────────────────────────────
# Token-bucket rate limiter
# ─────────────────────────────────────────────────────────────────────────────

class _RateLimiter:
    """
    Token bucket: rate токенов/сек, burst — максимальный запас.
    acquire() блокирует до тех пор, пока не появится токен.
    """

    def __init__(self, rate: float, burst: int):
        self._rate   = rate
        self._burst  = burst
        self._tokens = float(burst)
        self._last   = 0.0
        self._lock   = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = asyncio.get_event_loop().time()
            elapsed = now - self._last
            self._last = now
            self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
            if self._tokens >= 1.0:
                self._tokens -= 1.0
            else:
                wait = (1.0 - self._tokens) / self._rate
                await asyncio.sleep(wait)
                self._tokens = 0.0
                self._last = asyncio.get_event_loop().time()


# Глобальный синглтон — создаётся один раз при первом async-вызове
_limiter: _RateLimiter | None = None
_semaphore: asyncio.Semaphore | None = None


def _get_limiter() -> _RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = _RateLimiter(rate=_TARGET_RPS, burst=_MAX_CONCURRENT)
    return _limiter


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
    return _semaphore


# ─────────────────────────────────────────────────────────────────────────────
# Вспомогательные функции
# ─────────────────────────────────────────────────────────────────────────────

def date_to_ms(date_str: str | None) -> int | None:
    if date_str is None:
        return None
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            return int(datetime.strptime(date_str.strip(), fmt).timestamp() * 1000)
        except ValueError:
            continue
    raise ValueError(
        f"Не удалось распарсить дату: '{date_str}'. "
        "Ожидаемые форматы: 'YYYY-MM-DD' или 'YYYY-MM-DD HH:MM:SS'"
    )


def _candles_to_df(candles: list) -> pd.DataFrame:
    columns = [
        'Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume',
        'Close time', 'Quote asset volume', 'Number of trades',
        'Taker buy base asset volume', 'Taker buy quote asset volume', 'Ignore',
    ]
    df = pd.DataFrame(candles, columns=columns)
    df = df[['Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume']].copy()
    df['Timestamp'] = pd.to_datetime(df['Timestamp'], unit='ms')
    df[['Open', 'High', 'Low', 'Close', 'Volume']] = (
        df[['Open', 'High', 'Low', 'Close', 'Volume']].astype(float)
    )
    return df.sort_values('Timestamp').reset_index(drop=True)


# ─────────────────────────────────────────────────────────────────────────────
# Один HTTP-запрос с retry
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_batch(
    session: aiohttp.ClientSession,
    symbol: str,
    interval: str,
    start_time: int | None = None,
    end_time: int | None = None,
    limit: int = _BATCH_LIMIT,
) -> list | None:
    """
    Один запрос к Binance /fapi/v1/klines.
    При 429/418 — ждёт Retry-After и повторяет.
    При сетевых ошибках — exponential backoff до _MAX_RETRIES.
    """
    params = {'symbol': symbol, 'interval': interval, 'limit': limit}
    if start_time is not None:
        params['startTime'] = start_time
    if end_time is not None:
        params['endTime'] = end_time

    backoff = 1.0
    for attempt in range(1, _MAX_RETRIES + 1):
        await _get_limiter().acquire()
        try:
            async with _get_semaphore():
                async with session.get(_KLINES_URL, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        return await resp.json()

                    if resp.status == 429:
                        retry_after = float(resp.headers.get('Retry-After', 60))
                        print(f"  [429] rate limit — ждём {retry_after:.0f}с ({symbol})")
                        await asyncio.sleep(retry_after)
                        backoff = 1.0
                        continue

                    if resp.status == 418:
                        # IP заблокирован — ждём намного дольше
                        retry_after = float(resp.headers.get('Retry-After', 120))
                        print(f"  [418] IP ban — ждём {retry_after:.0f}с ({symbol})")
                        await asyncio.sleep(retry_after)
                        backoff = 1.0
                        continue

                    text = await resp.text()
                    print(f"  [HTTP {resp.status}] {symbol}: {text[:200]}")
                    return None

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if attempt == _MAX_RETRIES:
                print(f"  [error] {symbol} — {e} (попытка {attempt}/{_MAX_RETRIES})")
                return None
            wait = backoff * (2 ** (attempt - 1))
            print(f"  [retry {attempt}/{_MAX_RETRIES}] {symbol} — {e}, ждём {wait:.1f}с")
            await asyncio.sleep(wait)

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Загрузка всех свечей для одного символа
# ─────────────────────────────────────────────────────────────────────────────

async def download_candles_async(
    session: aiohttp.ClientSession,
    symbol: str,
    interval: str,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = _BATCH_LIMIT,
) -> list:
    """
    Загружает все свечи для символа пагинацией батчей по 1000.
    Батчи одного символа — строго последовательны (каждый батч
    зависит от timestamp последнего батча).
    """
    start_ms = date_to_ms(start_date)
    end_ms   = date_to_ms(end_date)

    # Случай 1: нет дат → последние `limit` свечей
    if start_ms is None and end_ms is None:
        candles = await _fetch_batch(session, symbol, interval, limit=limit)
        return candles or []

    # Случай 2: только start → вперёд от start до сейчас
    if start_ms is not None and end_ms is None:
        all_candles: list = []
        current_start = start_ms
        while True:
            batch = await _fetch_batch(session, symbol, interval,
                                       start_time=current_start, limit=_BATCH_LIMIT)
            if not batch:
                break
            all_candles.extend(batch)
            if len(batch) < _BATCH_LIMIT:
                break
            current_start = batch[-1][6] + 1   # close_time последней свечи + 1мс
        return all_candles

    # Случай 3: только end → назад от end
    if start_ms is None and end_ms is not None:
        all_candles = []
        current_end = end_ms
        while True:
            batch = await _fetch_batch(session, symbol, interval,
                                       end_time=current_end, limit=_BATCH_LIMIT)
            if not batch:
                break
            all_candles = batch + all_candles
            if len(batch) < _BATCH_LIMIT:
                break
            current_end = batch[0][0] - 1      # open_time первой свечи - 1мс
        return all_candles

    # Случай 4: start + end
    all_candles = []
    current_start = start_ms
    end_ms_adjusted = end_ms + 24 * 60 * 60 * 1000   # включаем последний день целиком
    while current_start < end_ms_adjusted:
        batch = await _fetch_batch(session, symbol, interval,
                                   start_time=current_start,
                                   end_time=end_ms_adjusted,
                                   limit=_BATCH_LIMIT)
        if not batch:
            break
        all_candles.extend(batch)
        current_start = batch[-1][6] + 1
        if current_start >= end_ms_adjusted:
            break
    return all_candles


# ─────────────────────────────────────────────────────────────────────────────
# Параллельная загрузка нескольких символов
# ─────────────────────────────────────────────────────────────────────────────

async def download_many_symbols_async(
    symbols: list[str],
    interval: str,
    start_date: str | None = None,
    end_date: str | None = None,
    filepath: str = 'klines',
    save: bool = True,
) -> dict[str, pd.DataFrame]:
    """
    Загружает символы параллельно (до _MAX_CONCURRENT одновременно).
    Возвращает {symbol: DataFrame}.
    """
    results: dict[str, pd.DataFrame] = {}

    connector = aiohttp.TCPConnector(limit=_MAX_CONCURRENT, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as session:

        async def _one(sym: str) -> tuple[str, pd.DataFrame | None]:
            candles = await download_candles_async(
                session, sym, interval, start_date, end_date
            )
            if not candles:
                print(f"✗ {sym}: данные не получены")
                return sym, None
            df = _candles_to_df(candles)
            print(f"✓ {sym}: {len(df):,} свечей")
            if save:
                save_df_to_csv(df, sym, interval, start_date, end_date, filepath)
            return sym, df

        tasks = [asyncio.create_task(_one(s)) for s in symbols]
        for coro in asyncio.as_completed(tasks):
            sym, df = await coro
            if df is not None:
                results[sym] = df

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Публичные обёртки (sync + async)
# ─────────────────────────────────────────────────────────────────────────────

async def get_df_async(
    symbol: str,
    interval: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> pd.DataFrame | None:
    connector = aiohttp.TCPConnector(limit=_MAX_CONCURRENT, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as session:
        candles = await download_candles_async(session, symbol, interval, start_date, end_date)
    if not candles:
        print(f"✗ Не удалось получить данные для {symbol}")
        return None
    return _candles_to_df(candles)


def get_df(
    symbol: str,
    interval: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> pd.DataFrame | None:
    """Синхронная обёртка над get_df_async (для обратной совместимости)."""
    return asyncio.run(get_df_async(symbol, interval, start_date, end_date))


def download_candles(
    symbol: str,
    interval: str,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = _BATCH_LIMIT,
) -> list:
    """Синхронная обёртка над download_candles_async (для обратной совместимости)."""
    async def _run():
        connector = aiohttp.TCPConnector(limit=_MAX_CONCURRENT, ttl_dns_cache=300)
        async with aiohttp.ClientSession(connector=connector) as session:
            return await download_candles_async(session, symbol, interval,
                                                start_date, end_date, limit)
    return asyncio.run(_run())


# ─────────────────────────────────────────────────────────────────────────────
# Топ-N монет (CoinGecko + Binance) — остаётся синхронным (2 запроса)
# ─────────────────────────────────────────────────────────────────────────────

def get_binance_top_by_cap() -> list:
    cg_url = "https://api.coingecko.com/api/v3/coins/markets"
    cg_params = {
        'vs_currency': 'usd',
        'order': 'market_cap_desc',
        'per_page': 100,
        'page': 1,
    }

    print("Запрашиваем данные с CoinGecko...")
    try:
        cg_resp = requests.get(cg_url, params=cg_params, timeout=10)
    except requests.RequestException as e:
        print(f"✗ CoinGecko недоступен: {e}")
        return []

    if cg_resp.status_code != 200:
        print(f"✗ CoinGecko вернул ошибку: {cg_resp.status_code}\n  {cg_resp.text[:300]}")
        return []

    try:
        cg_data = cg_resp.json()
    except Exception as e:
        print(f"✗ Ошибка парсинга JSON CoinGecko: {e}")
        return []

    if not isinstance(cg_data, list):
        print(f"✗ Ожидали список, получили: {type(cg_data)}")
        return []

    print(f"✓ Получено {len(cg_data)} монет с CoinGecko")
    cg_symbols = {coin['symbol'].upper() for coin in cg_data}

    print("Запрашиваем данные с Binance...")
    try:
        bn_resp = requests.get(_EXCHANGE_URL, timeout=10)
    except requests.RequestException as e:
        print(f"✗ Binance недоступен: {e}")
        return []

    if bn_resp.status_code != 200:
        print(f"✗ Binance вернул ошибку: {bn_resp.status_code}")
        return []

    bn_data = bn_resp.json()
    bn_symbols = {
        s['baseAsset'].upper()
        for s in bn_data['symbols']
        if s['quoteAsset'] == 'USDT' and s['status'] == 'TRADING'
    }
    print(f"✓ {len(bn_symbols)} активных USDT-пар на Binance Futures")

    common = cg_symbols & bn_symbols
    print(f"✓ Найдено {len(common)} общих монет")

    return [c for c in cg_data if c['symbol'].upper() in common][:top_by_cap]


# ─────────────────────────────────────────────────────────────────────────────
# CSV helpers
# ─────────────────────────────────────────────────────────────────────────────

def save_df_to_csv(
    df: pd.DataFrame,
    symbol: str,
    interval: str,
    start_date: str | None = None,
    end_date: str | None = None,
    filepath: str = 'klines',
) -> str | None:
    if df is None or len(df) == 0:
        print("✗ DataFrame пустой!")
        return None

    os.makedirs(filepath, exist_ok=True)
    if start_date and end_date:
        s = str(start_date).replace(':', '-').replace(' ', '_')
        e = str(end_date).replace(':', '-').replace(' ', '_')
        filename = f"{symbol}_{interval}_{s}_{e}.csv"
    else:
        filename = f"{symbol}_{interval}.csv"

    full_path = os.path.join(filepath, filename)
    try:
        df.to_csv(full_path, index=False, encoding='utf-8-sig',
                  date_format='%Y-%m-%d %H:%M:%S', float_format='%.8f')
        size_kb = os.path.getsize(full_path) / 1024
        print(f"  Сохранён: {full_path}  ({len(df):,} строк, {size_kb:.1f} KB)")
        return full_path
    except Exception as e:
        print(f"✗ Ошибка сохранения {full_path}: {e}")
        return None


def read_df_from_csv(
    symbol: str,
    interval: str = '1h',
    start_date: str | None = None,
    end_date: str | None = None,
    filepath: str = 'klines',
) -> pd.DataFrame:
    base_dir = Path(filepath)
    matches = list(base_dir.glob(f"{symbol}_{interval}_*.csv"))

    if not matches:
        raise FileNotFoundError(f"Нет файла для {symbol} {interval} в папке {filepath}")

    file_path = matches[0]
    print(f"✓ Чтение: {file_path}")

    df = pd.read_csv(file_path, parse_dates=['Timestamp'], encoding='utf-8-sig')

    if start_date or end_date:
        df = df.copy()
        if start_date:
            df = df[df['Timestamp'] >= pd.to_datetime(start_date)]
        if end_date:
            df = df[df['Timestamp'] <= pd.to_datetime(end_date)]

    if len(df) == 0:
        print("✗ DataFrame пуст после фильтрации!")
        return pd.DataFrame(columns=['Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])

    print(f"✓ Загружено строк: {len(df):,}")
    return df


# ─────────────────────────────────────────────────────────────────────────────
# CLI: скачать топ-N монет параллельно
# ─────────────────────────────────────────────────────────────────────────────

async def _cli_main():
    from config.settings import interval, start_date, end_date

    coins = get_binance_top_by_cap()
    if not coins:
        print("✗ Не удалось получить список монет")
        return

    symbols = []
    print(f"\nТоп-{top_by_cap} монет по капитализации:")
    for coin in coins:
        rank   = coin.get('market_cap_rank', '?')
        symbol = coin['symbol'].upper() + 'USDT'
        name   = coin['name']
        mcap   = coin['market_cap'] / 1_000_000_000
        price  = coin['current_price']
        print(f"  {rank:3}. {symbol:12s} {name:20s}  ${mcap:.3f}B  ${price:.4f}")
        symbols.append(symbol)

    print(f"\nСкачиваем {len(symbols)} символов асинхронно "
          f"({_TARGET_RPS} req/s, {_MAX_CONCURRENT} потоков)...\n")

    t0 = time.monotonic()
    results = await download_many_symbols_async(
        symbols, interval,
        start_date=start_date,
        end_date=end_date,
        filepath='klines',
        save=True,
    )
    elapsed = time.monotonic() - t0

    ok  = sum(1 for df in results.values() if df is not None)
    err = len(symbols) - ok
    print(f"\n{'='*50}")
    print(f"Готово за {elapsed:.1f}с | Успешно: {ok} | Ошибок: {err}")
    print(f"{'='*50}")


if __name__ == '__main__':
    asyncio.run(_cli_main())
