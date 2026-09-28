"""Data feeds. No keys.

- GeckoTerminal: discovery (new pools, trending pools). The free tier here
  measured ~10 calls/min (2026-09-23), so it is only used for discovery.
- DexScreener: tracking. /token-pairs returns every pool of a token (bonding
  curve and graduated AMM) with market cap, liquidity, txns and creation time;
  300 calls/min.
- Coinbase Exchange: /products, for new listings.
"""
import json, time, urllib.request, urllib.error
from collections import deque
from datetime import datetime

GT = "https://api.geckoterminal.com/api/v2"
DS = "https://api.dexscreener.com"
UA = {"User-Agent": "coin-launch-agent/1.0", "Accept": "application/json"}


class RateLimited(Exception):
    pass


class Limiter:
    def __init__(self, per_min):
        self.per_min, self.calls = per_min, deque()

    def left(self):
        now = time.time()
        while self.calls and now - self.calls[0] > 60:
            self.calls.popleft()
        return self.per_min - len(self.calls)

    def take(self):
        """Claim a call slot if one is free; never sleeps."""
        if self.left() <= 0:
            return False
        self.calls.append(time.time())
        return True


gt_limit, ds_limit = Limiter(8), Limiter(200)


# One keep-alive session (2026-09-28). The watcher makes ~3 DexScreener calls a second; with a
# fresh urllib connection each, TLS handshakes were most of its ~8% constant CPU.
import requests
_session = requests.Session()
_session.headers.update(UA)


def get(url, limiter):
    if not limiter.take():
        raise RateLimited(url)
    r = _session.get(url, timeout=15)
    if r.status_code == 404:
        return None
    if r.status_code == 429:
        limiter.calls.extend([time.time()] * limiter.per_min)   # back off a full minute
        raise RateLimited(url)
    r.raise_for_status()
    return r.json()


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else None


def gt_parse(p, net):
    a, r = p["attributes"], p["relationships"]
    lp = a.get("launchpad_details") or {}
    return {
        "net": net,
        "token": r["base_token"]["data"]["id"].split("_", 1)[1],
        "symbol": (a.get("name") or "").split(" / ")[0].strip(),
        "created": iso(a.get("pool_created_at")),
        "mc": num(a.get("market_cap_usd")) or num(a.get("fdv_usd")),
        "launchpad": bool(lp),
        "grad_pct": num(lp.get("graduation_percentage")),
        "graduated": bool(lp.get("completed")),
    }


def gt_new_pools(net, page=1):
    d = get(f"{GT}/networks/{net}/new_pools?page={page}", gt_limit)
    return [gt_parse(p, net) for p in (d or {}).get("data", [])]


def gt_trending(net, duration="5m"):
    d = get(f"{GT}/networks/{net}/trending_pools?duration={duration}&page=1", gt_limit)
    return [gt_parse(p, net) for p in (d or {}).get("data", [])]


def ds_token_pairs(net, token):
    """Every pool of a token, as DexScreener sees it."""
    d = get(f"{DS}/token-pairs/v1/{net}/{token}", ds_limit) or []
    out = []
    for p in d:
        if p.get("baseToken", {}).get("address", "").lower() != token.lower():
            continue   # pools where our token is the quote side
        tx = (p.get("txns") or {}).get("h1") or {}
        out.append({
            "pair": p["pairAddress"], "dex": p.get("dexId", ""), "url": p.get("url", ""),
            "symbol": p["baseToken"].get("symbol", ""), "name": p["baseToken"].get("name", ""),
            "info": p.get("info"),   # websites / socials, for jev_shadow
            "created": (p.get("pairCreatedAt") or 0) / 1000 or None,
            "mc": num(p.get("marketCap")) or num(p.get("fdv")),
            "liq": num((p.get("liquidity") or {}).get("usd")) or 0.0,
            "buys": tx.get("buys", 0), "sells": tx.get("sells", 0),
            "vol": num((p.get("volume") or {}).get("h1")) or 0.0,
        })
    return out


def coinbase_products():
    with urllib.request.urlopen(urllib.request.Request(
            "https://api.exchange.coinbase.com/products", headers=UA), timeout=15) as r:
        return {p["id"]: p for p in json.load(r)}
