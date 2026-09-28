"""pump.fun data for the explosion model: graduated coins and their 5-minute candles.

- /coins?complete=true lists graduated coins, newest first. It pages back about
  1.5–2 days (offset stops near 2000), so the backfill caches every coin it sees
  in data/pump/coins.json and the training set grows each night.
- /v1/coins/{mint}/candles?interval=5m returns up to 1000 bars (≈3.5 days) from
  launch, in USD price; price × 1B supply = market cap. Bars with no trades are
  omitted. Measured 2026-09-23: no rate limit on candles, ~1 req/s on /coins.
"""
import json, os, time, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "data", "pump")
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
SUPPLY = 1e9


# Keep-alive sessions, one per thread (the scorers and backfill fetch from thread pools). A fresh
# TLS connection per candle request was most of their CPU (2026-09-28).
import threading, requests
_local = threading.local()


def _session():
    s = getattr(_local, "s", None)
    if s is None:
        s = _local.s = requests.Session()
        s.headers.update(UA)
    return s


def get(url, tries=4):
    for i in range(tries):
        try:
            r = _session().get(url, timeout=20)
            if r.status_code == 429:
                time.sleep(3 * (i + 1))
                continue
            if r.status_code in (400, 404):
                return None
            r.raise_for_status()
            return r.json()
        except Exception:
            if i == tries - 1:
                raise
        time.sleep(1 + i)
    return None


def graduated(max_pages=60):
    """Every graduated coin the API will page to, merged into the local cache."""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, "coins.json")
    coins = {}
    if os.path.exists(path):
        with open(path) as f:
            coins = json.load(f)
    for page in range(max_pages):
        for attempt in range(4):   # the list endpoint rate-limits bursts; an empty list is the real end
            d = get("https://frontend-api-v3.pump.fun/coins?offset=%d&limit=50&includeNsfw=true"
                    "&complete=true&sort=created_timestamp&order=DESC" % (page * 50))
            if isinstance(d, list):
                break
            time.sleep(15)
        if not isinstance(d, list) or not d:
            break
        for c in d:
            coins[c["mint"]] = {k: c.get(k) for k in ("mint", "symbol", "created_timestamp",
                                                       "ath_market_cap", "ath_market_cap_timestamp")}
        time.sleep(1.1)
    with open(path, "w") as f:
        json.dump(coins, f)
    return coins


def candles_5m(mint, cache=True):
    """5-minute bars as [(t_open_ms, open, high, low, close, vol_usd)] in market-cap dollars.
    Only cached once the coin is past the training window, so the file never changes later."""
    path = os.path.join(CACHE, "candles", mint + ".json")
    if cache and os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    d = get(f"https://swap-api.pump.fun/v1/coins/{mint}/candles?interval=5m&limit=1000") or []
    bars = [(int(c["timestamp"]), float(c["open"]) * SUPPLY, float(c["high"]) * SUPPLY,
             float(c["low"]) * SUPPLY, float(c["close"]) * SUPPLY, float(c["volume"])) for c in d]
    bars.sort()
    return bars


def save_candles(mint, bars):
    os.makedirs(os.path.join(CACHE, "candles"), exist_ok=True)
    with open(os.path.join(CACHE, "candles", mint + ".json"), "w") as f:
        json.dump(bars, f)
