"""
Диагностика сетевого доступа из Python.
Запуск: python diagnose_network.py
"""
import sys
import socket
import urllib.request
import asyncio

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

print("=" * 55)
print("ДИАГНОСТИКА СЕТИ")
print("=" * 55)

# ── 1. DNS-резолюция напрямую через socket ────────────────
print("\n[1] DNS-резолюция через socket:")
hosts = ['api.coingecko.com', 'fapi.binance.com', '8.8.8.8', 'google.com']
for host in hosts:
    try:
        ip = socket.gethostbyname(host)
        print(f"  OK  {host} -> {ip}")
    except socket.gaierror as e:
        print(f"  ERR {host} -> {e}")

# ── 2. Proxy из системных настроек ───────────────────────
print("\n[2] Системные proxy-настройки:")
proxies = urllib.request.getproxies()
if proxies:
    for k, v in proxies.items():
        print(f"  {k}: {v}")
else:
    print("  Прокси не настроены")

# ── 3. HTTP-запрос через requests ────────────────────────
print("\n[3] requests.get():")
try:
    import requests
    for url in ['https://fapi.binance.com/fapi/v1/exchangeInfo',
                'https://api.coingecko.com/api/v3/ping']:
        try:
            r = requests.get(url, timeout=10)
            print(f"  OK  {url} -> HTTP {r.status_code}  {r.text[:60]}")
        except Exception as e:
            print(f"  ERR {url} -> {e.__class__.__name__}: {str(e)[:80]}")
except ImportError:
    print("  requests не установлен")

# ── 4. Async HTTP через aiohttp ───────────────────────────
print("\n[4] aiohttp async GET:")
async def test_aiohttp():
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            for url in ['https://fapi.binance.com/fapi/v1/exchangeInfo']:
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        text = await resp.text()
                        print(f"  OK  {url} -> HTTP {resp.status}  {text[:60]}")
                except Exception as e:
                    print(f"  ERR {url} -> {e.__class__.__name__}: {str(e)[:80]}")
    except ImportError:
        print("  aiohttp не установлен")

asyncio.run(test_aiohttp())

# ── 5. Переменные окружения ───────────────────────────────
print("\n[5] Env-переменные HTTP_PROXY / HTTPS_PROXY / NO_PROXY:")
import os
for var in ['HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY',
            'http_proxy', 'https_proxy', 'no_proxy']:
    val = os.environ.get(var)
    if val:
        print(f"  {var} = {val}")
if not any(os.environ.get(v) for v in ['HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy']):
    print("  Не заданы")

print("\n" + "=" * 55)
