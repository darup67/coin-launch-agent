#!/usr/bin/env python3
"""Coinbase / Robinhood-listed token board (2026-09-29).

The user trades only through Coinbase and Robinhood, so this is now the coin detector's default view:

  A. FAST AND STEADY: the token gained quickly over the last 6-24 h, the gain came from many up-hours (not one spike)
     and the price path is close to a straight rising line.
  B. HOLDING: after that run the price did not give much back inside the hold window (small drawdown, still near its high).

A token is shown ONLY if it is listed on Coinbase (online spot product) or Robinhood (tradable crypto) and, for Solana/Base
tokens, the listing is matched by CONTRACT ADDRESS (Coinbase's /currencies gives the address per network), never by ticker,
because pump.fun is full of look-alike tickers (PUMP, RAY, "NVIDIA", ...). Market cap moves are read as price moves
(supply assumed fixed); the market cap shown comes from DexScreener where the token has an on-chain address.

  listed.py scan         score the universe, write data/listed_board.json, print the board
  listed.py venues       rebuild the venue allowlist (cached 24 h) and print its size
  listed.py chains       which blockchains Coinbase and Robinhood support (the project only draws tokens from these)
  listed.py check ADDR   is this Solana mint / Base contract supported on Coinbase or Robinhood?
  listed.py prune-junk   one-time: back up then delete pump.fun / DEX-watcher data (nothing there can pass the venue filter)

Data: trade-core's 15-minute bar store (already refreshed every 15 min for the whole universe), so a scan makes no price
API calls. Thresholds live in config.json under "listed".
"""
import json, math, os, sys, time, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
VENUES = os.path.join(DATA, "venues.json")
BOARD = os.path.join(DATA, "listed_board.json")
STATE = os.path.join(DATA, "listed_state.json")
CG_CACHE = os.path.join(DATA, "cg_platforms.json")
RH_FILE = os.path.expanduser("~/flip-notifier/robinhood-crypto.json")
UA = {"User-Agent": "Mozilla/5.0"}
sys.path.insert(0, os.path.expanduser("~/trade-core"))
os.makedirs(DATA, exist_ok=True)

DEFAULTS = {
    "fast_gain_6h": 0.05,        # +5% in 6 h ...
    "fast_gain_24h": 0.10,       # ... or +10% in 24 h
    "min_up_hours": 0.55,        # share of the last 24 hourly returns that are up
    "min_r2": 0.50,              # R^2 of log price vs time over 24 h (a rising straight line scores 1.0)
    "max_spike_share": 0.45,     # the best single hour may be at most this share of the 24 h gain
    "hold_hours": 12,            # hold window
    "max_drawdown": 0.06,        # largest peak-to-trough fall inside the hold window
    "max_off_high": 0.04,        # now vs the 24 h high
    "min_usd_vol_24h": 500_000,  # thin coins make clean-looking charts
}
# Not "tokens" in the sense of the request: stablecoins, wrapped / staked assets, gold.
CHAIN_ALIAS = {"binance-smart-chain": "bsc", "arbitrum-one": "arbitrum", "the-open-network": "ton", "polygon-pos": "polygon",
               "optimistic-ethereum": "optimism", "avalanche": "avacchain", "ethereum-classic": "ethereumclassic"}
WRAPPED = {"solana:so11111111111111111111111111111111111111112", "base:0x4200000000000000000000000000000000000006"}   # wrapped SOL / WETH: the chain's own coin
EXCLUDE = {"USDC", "USDT", "USDG", "PYUSD", "USDF", "USDS", "DAI", "EURC", "PAXG", "XAUT", "WBTC", "CBBTC", "WETH", "STETH", "WSTETH",
           "CBETH", "JITOSOL", "MSOL", "BSOL", "USDE", "FDUSD", "RLUSD", "USD1"}


def load(p, d):
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def save(p, o):
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(o, f)
    os.replace(tmp, p)


def get(url, timeout=25):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return json.load(r)


def cfg():
    c = load(os.path.join(HERE, "config.json"), {}).get("listed", {})
    return {**DEFAULTS, **{k: v for k, v in c.items() if not k.startswith("_")}}


def key(chain, addr):
    # Coinbase's /currencies returns some Solana mints lower-cased (RAY did), and a base58 mint that differs from another only by
    # case is not realistic (58^44 combinations), so every address is compared lower-cased. `canon` restores the real case for links.
    return f"{chain}:{addr.lower()}"


# ------------------------------------------------------------------ venues (who is really listed)
def _cg_platforms():
    """CoinGecko id/symbol/name -> solana & base addresses, cached a week (3.8 MB download). Only used to find the
    address of Robinhood-only assets; Coinbase assets get theirs from Coinbase itself."""
    c = load(CG_CACHE, {})
    if c and time.time() - c.get("at", 0) < 7 * 86400:
        return c["coins"]
    coins = []
    for x in get("https://api.coingecko.com/api/v3/coins/list?include_platform=true", 60):
        p = {k: v for k, v in (x.get("platforms") or {}).items() if k and v}
        if p:
            coins.append({"sym": (x.get("symbol") or "").upper(), "name": (x.get("name") or "").lower(), "p": p})
    save(CG_CACHE, {"at": time.time(), "coins": coins})
    return coins


def venues(refresh=False):
    v = load(VENUES, {})
    if v and not refresh and time.time() - v.get("at", 0) < 86400:
        return v
    cb_cur = get("https://api.exchange.coinbase.com/currencies")
    cb_prod = get("https://api.exchange.coinbase.com/products")
    tradable = {p["base_currency"] for p in cb_prod
                if p.get("quote_currency") in ("USD", "USDC") and p.get("status") == "online" and not p.get("trading_disabled")
                and not p.get("cancel_only") and not p.get("post_only")}
    coinbase = {}
    addr = {}
    chains = {}                                        # symbol -> blockchains the asset lives on
    cb_nets = {}
    pretrade = {}                                      # Coinbase enabled the asset (deposits) but no spot product trades yet: a classic listing precursor
    for c in cb_cur:
        if c.get("status") == "online" and c["id"] not in tradable and (c.get("details") or {}).get("type") == "crypto" \
                and c["id"] not in EXCLUDE:
            pretrade[c["id"]] = {"name": c.get("name"), "addr": {n["id"]: n["contract_address"] for n in c.get("supported_networks", [])
                                                                  if n.get("id") in ("solana", "base") and n.get("contract_address")}}
        if c.get("status") != "online" or c["id"] not in tradable:
            continue
        coinbase[c["id"]] = c.get("name")
        chains[c["id"]] = sorted({n["id"] for n in c.get("supported_networks", []) if n.get("status", "online") == "online"})
        for n in chains[c["id"]]:
            cb_nets[n] = cb_nets.get(n, 0) + 1
        for n in c.get("supported_networks", []):
            if n.get("id") in ("solana", "base") and n.get("contract_address") and n.get("status", "online") == "online":
                addr[key(n["id"], n["contract_address"])] = {"symbol": c["id"], "coinbase": True, "robinhood": False}
    rh_raw = load(RH_FILE, {}).get("coins", {})
    robinhood = {s: {"name": (d.get("name") or "").replace(" to US Dollar", ""), "halted": d.get("halted_regions", [])} for s, d in rh_raw.items()}
    for s, d in robinhood.items():
        hit = [a for a, m in addr.items() if m["symbol"] == s]
        if hit:                                        # already found through Coinbase: mark the second venue
            for a in hit:
                addr[a]["robinhood"] = True
            continue
        if s in coinbase:                              # listed on Coinbase without a Solana/Base address (an L1 or Ethereum coin)
            continue
        try:                                           # Robinhood-only: find the address by ticker AND name, never ticker alone
            cands = [x for x in _cg_platforms() if x["sym"] == s and (x["name"] == d["name"].lower() or d["name"].lower() in x["name"] or x["name"] in d["name"].lower())]
        except Exception:
            cands = []
        for x in cands:
            chains.setdefault(s, [])
            for chain, a in x["p"].items():
                chains[s] = sorted(set(chains[s]) | {chain})
                if chain in ("solana", "base"):
                    addr[key(chain, a)] = {"symbol": s, "coinbase": False, "robinhood": True}
    for s in list(chains):                             # CoinGecko spells some chains differently from Coinbase
        chains[s] = sorted({CHAIN_ALIAS.get(n, n) for n in chains[s]})
    rh_nets = {}
    for s in robinhood:
        for n in chains.get(s, []):
            rh_nets[n] = rh_nets.get(n, 0) + 1
    # The chains this project may draw tokens from: every network Coinbase lists as online (its own API, authoritative) plus
    # Robinhood's own chain. Robinhood publishes no per-asset network list, and CoinGecko lists every bridged deployment of
    # a token (PulseChain, Scroll, ...), which would over-count, so Robinhood-only assets count only when their chain is in
    # this set. Override with config listed.allowed_chains to narrow it.
    allowed = sorted(set(cb_nets) | {"robinhood"})
    canon = {}
    try:
        for x in _cg_platforms():
            for chain, a in x["p"].items():
                if key(chain, a) in addr:
                    canon[key(chain, a)] = a
    except Exception:
        pass
    out = {"at": time.time(), "coinbase": coinbase, "robinhood": robinhood, "addr": addr, "canon": canon, "chains": chains,
           "coinbase_networks": cb_nets, "robinhood_networks": rh_nets, "allowed_chains": allowed}
    save(VENUES, out)
    return out


def supported(chain, address):
    """{'symbol','coinbase','robinhood'} if this on-chain token is listed on Coinbase or Robinhood, else None."""
    v = venues()
    if chain not in ("solana", "base") or chain not in allowed_chains(v):
        return None
    return v["addr"].get(key(chain, address))


def allowed_chains(v=None):
    """Blockchains tokens may come from: Coinbase's supported networks + chains of Robinhood-listed assets, or the config override."""
    v = v or venues()
    override = load(os.path.join(HERE, "config.json"), {}).get("listed", {}).get("allowed_chains")
    return set(override) if override else set(v.get("allowed_chains", []))


# ------------------------------------------------------------------ scoring
def metrics(bars, c):
    """A/B metrics for one token from its 15-minute bars. None if there is not a full 24 h of data."""
    if len(bars) < 100:
        return None
    # hourly closes counted back from the newest bar by timestamp (a missing 15-minute bar falls back to the one before it)
    by_t = {b["t"]: b["c"] for b in bars}
    t0 = bars[-1]["t"]
    h = []
    for k in range(24, -1, -1):
        t = t0 - k * 3_600_000
        for back in (0, 900_000, 1_800_000):
            if t - back in by_t:
                h.append(by_t[t - back]); break
        else:
            return None
    b96 = bars[-96:]
    return core(h, max(b["h"] for b in b96), sum(b["c"] * b["v"] for b in b96), bars[-1]["c"], bars[-1]["t"], c)


def core(h, hi24, vol, last, last_t, c):
    """h = 25 hourly closes, oldest first. Shared by the listed universe (15 m bars) and the radar (hourly OHLCV)."""
    r1 = [h[i] / h[i - 1] - 1 for i in range(1, len(h))]                # 24 hourly returns
    ret_24 = h[-1] / h[0] - 1
    ret_6 = h[-1] / h[-7] - 1
    up = sum(1 for r in r1 if r > 0) / len(r1)
    ys = [math.log(x) for x in h]
    n = len(ys); xs = list(range(n)); mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs); sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys)); syy = sum((y - my) ** 2 for y in ys)
    slope = sxy / sxx if sxx else 0
    r2 = (sxy * sxy / (sxx * syy)) if sxx and syy else 0
    gain_sum = sum(r for r in r1 if r > 0)
    spike = (max(r1) / gain_sum) if gain_sum > 0 else 1.0
    hh = h[-(c["hold_hours"] + 1):]
    peak, mdd = hh[0], 0.0
    for x in hh:
        peak = max(peak, x); mdd = max(mdd, 1 - x / peak)
    return {"price": last, "ret_6h": ret_6, "ret_24h": ret_24, "up_hours": up, "r2": r2 if slope > 0 else 0.0, "spike_share": spike,
            "max_drawdown": mdd, "off_high": 1 - last / hi24, "usd_vol_24h": vol, "slope_h": slope, "last_bar": last_t}


def grade(m, c):
    fast = m["ret_24h"] >= c["fast_gain_24h"] or m["ret_6h"] >= c["fast_gain_6h"]
    steady = m["up_hours"] >= c["min_up_hours"] and m["r2"] >= c["min_r2"] and m["spike_share"] <= c["max_spike_share"] and m["ret_24h"] > 0
    A = fast and steady and m["usd_vol_24h"] >= c["min_usd_vol_24h"]
    B = m["max_drawdown"] <= c["max_drawdown"] and m["off_high"] <= c["max_off_high"] and m["usd_vol_24h"] >= c["min_usd_vol_24h"]
    # 0-100: how far inside every bar the token sits (each term capped at 1)
    s = (min(m["ret_24h"] / (2 * c["fast_gain_24h"]), 1) + min(m["up_hours"] / 0.75, 1) + min(m["r2"] / 0.9, 1)
         + (1 - min(m["spike_share"] / 0.8, 1)) + (1 - min(m["max_drawdown"] / (2 * c["max_drawdown"]), 1))
         + (1 - min(m["off_high"] / (2 * c["max_off_high"]), 1))) / 6
    return A, B, round(100 * max(s, 0))


def _mcaps(rows):
    """Market cap from DexScreener for tokens with a known address, batched 30 per call."""
    by = {}
    for r in rows:
        if r.get("chain") and r.get("address"):
            by.setdefault(r["chain"], []).append(r)
    for chain, rs in by.items():
        for i in range(0, len(rs), 30):
            chunk = rs[i:i + 30]
            try:
                pairs = get(f"https://api.dexscreener.com/tokens/v1/{chain}/" + ",".join(r["address"] for r in chunk))
            except Exception:
                continue
            best = {}
            for p in pairs if isinstance(pairs, list) else []:
                a = (p.get("baseToken") or {}).get("address", "")
                if a and (a not in best or (p.get("liquidity") or {}).get("usd", 0) > (best[a].get("liquidity") or {}).get("usd", 0)):
                    best[a] = p
            for r in chunk:
                p = best.get(r["address"])
                if p:
                    r["mcap"] = p.get("marketCap") or p.get("fdv")


# ------------------------------------------------------------------ loose ends: tokens that GET listed, and tokens that might
LISTINGS = os.path.join(DATA, "listings.json")
_last_gt = [0.0]


def gt(path):
    """GeckoTerminal (free, ~30 calls/min): paced, one retry on 429. Returns parsed JSON or None."""
    for attempt in (0, 1):
        wait = _last_gt[0] + 2.2 - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_gt[0] = time.time()
        try:
            return get("https://api.geckoterminal.com/api/v2" + path, 20)
        except urllib.error.HTTPError as e:
            if e.code != 429:
                return None
            time.sleep(8)
        except Exception:
            return None
    return None


def origin(chain, address):
    """Where a newly listed token came from: age of its first pool, whether it launched on pump.fun, size."""
    out = {}
    t = gt(f"/networks/{chain}/tokens/{address}")
    a = ((t or {}).get("data") or {}).get("attributes") or {}
    if a:
        out.update({"name": a.get("name"), "fdv": a.get("fdv_usd"), "mcap": a.get("market_cap_usd")})
    p = gt(f"/networks/{chain}/tokens/{address}/pools?page=1")
    pools = (p or {}).get("data") or []
    born = sorted(x["attributes"]["pool_created_at"] for x in pools if x["attributes"].get("pool_created_at"))
    if born:
        out["first_pool"] = born[0]
        out["age_days"] = round((time.time() - time.mktime(time.strptime(born[0][:19], "%Y-%m-%dT%H:%M:%S")) + time.timezone) / 86400, 1)
    dexes = {(x.get("relationships") or {}).get("dex", {}).get("data", {}).get("id", "") for x in pools}
    out["pump_fun"] = address.endswith("pump") or any("pump" in d for d in dexes)
    return out


def listing_watch(v):
    """Diff Coinbase / Robinhood against the last scan. New Coinbase or Robinhood assets, and assets Coinbase has enabled
    but does not trade yet (deposits open), become events, enriched by contract address with the token's origin (a pump.fun
    or DEX launch has an age and a first pool). First run only seeds the baseline. Events are kept 30 days."""
    st = load(LISTINGS, {})
    now = time.time()
    cur = {"coinbase": sorted(v["coinbase"]), "robinhood": sorted(v["robinhood"]), "pretrade": sorted(v.get("pretrade", {}))}
    if not st.get("seeded"):
        save(LISTINGS, {"seeded": now, "events": [], **cur})
        return []
    events = [e for e in st.get("events", []) if now - e["t"] < 30 * 86400]
    for kind, label in (("coinbase", "Coinbase listed"), ("pretrade", "Coinbase deposits open, not trading yet"), ("robinhood", "Robinhood listed")):
        for sym in sorted(set(cur[kind]) - set(st.get(kind, []))):
            if sym in EXCLUDE:
                continue
            if kind == "coinbase" and any(e["symbol"] == sym and e["kind"] == "pretrade" for e in events):
                label = "Coinbase listed (deposits were open first)"
            ad = (v.get("pretrade", {}).get(sym) or {}).get("addr") or {a.split(":", 1)[0]: a.split(":", 1)[1] for a, m in v["addr"].items() if m["symbol"] == sym}
            chain, address = next(iter(ad.items()), (None, None))
            e = {"t": now, "kind": kind, "label": label, "symbol": sym, "chain": chain, "address": address}
            if chain in ("solana", "base") and address:
                try:
                    e["origin"] = origin(chain, address)
                except Exception:
                    pass
            h = next((x for x in load(HITS, {}).values() if x["token"] == address), None) if address else None
            if h:
                e["watcher_hit"] = {"t": h.get("t"), "mc": h.get("mc")}
            if kind == "coinbase":
                try:
                    e["price0"] = float(get(f"https://api.exchange.coinbase.com/products/{sym}-USD/ticker", 15)["price"])
                except Exception:
                    pass
            events.append(e)
    save(LISTINGS, {"seeded": st["seeded"], "events": events, **cur})
    return events


def other_cex():
    """Base-asset symbols on other exchanges (Kraken, Bitstamp, OKX, Binance.US, Upbit, Gemini), cached a day. A token that is on
    several other exchanges but not on Coinbase / Robinhood is the likeliest to be listed next."""
    c = load(os.path.join(DATA, "other_cex.json"), {})
    if c and time.time() - c.get("at", 0) < 86400:
        return {k: set(x) for k, x in c["ex"].items()}
    ex = {}

    def safe(name, fn):
        try:
            ex[name] = sorted({x.upper() for x in fn()})
        except Exception:
            pass
    safe("Kraken", lambda: [(p.get("wsname") or "").split("/")[0] for p in get("https://api.kraken.com/0/public/AssetPairs")["result"].values()])
    safe("Bitstamp", lambda: [p["name"].split("/")[0] for p in get("https://www.bitstamp.net/api/v2/trading-pairs-info/")])
    safe("OKX", lambda: [p["instId"].split("-")[0] for p in get("https://www.okx.com/api/v5/public/instruments?instType=SPOT")["data"]])
    safe("Binance.US", lambda: [p["baseAsset"] for p in get("https://api.binance.us/api/v3/exchangeInfo")["symbols"]])
    safe("Upbit", lambda: [m["market"].split("-")[1] for m in get("https://api.upbit.com/v1/market/all")])
    safe("Gemini", lambda: [x[:-3] for x in get("https://api.gemini.com/v1/symbols") if x.endswith("usd")])
    save(os.path.join(DATA, "other_cex.json"), {"at": time.time(), "ex": ex})
    return {k: set(x) for k, x in ex.items()}


def radar(v, c):
    """SEPARATE from the original watcher (whose parameters are untouched). Tokens NOT on Coinbase / Robinhood yet that could plausibly be
    listed later: heavily traded on Solana / Base DEXs (top pools by 24 h volume), real liquidity, more than a few days old, not a
    stablecoin. Each gets the same A/B test from hourly candles and a list of OTHER exchanges it trades on, verified by contract
    address through CoinGecko (never ticker alone). Shown apart from the main board, clearly marked not buyable on Coinbase/Robinhood
    today; runs hourly."""
    r = c.get("radar", {})
    ok = allowed_chains(v)
    cg = {}
    for x in _cg_platforms():
        for chain, a in x["p"].items():
            cg.setdefault(key(chain, a), x)
    cex = other_cex()
    cands = {}
    for chain in ("solana", "base"):
        if chain not in ok:
            continue
        for page in range(1, r.get("pages", 4) + 1):
            d = gt(f"/networks/{chain}/pools?page={page}&sort=h24_volume_usd_desc&include=base_token")
            for p in (d or {}).get("data", []):
                a = p["attributes"]
                addr = p["relationships"]["base_token"]["data"]["id"].split("_", 1)[1]
                sym = (a.get("name") or "").split(" / ")[0].strip().upper()
                liq, vol = float(a.get("reserve_in_usd") or 0), float((a.get("volume_usd") or {}).get("h24") or 0)
                cap = float(a.get("market_cap_usd") or a.get("fdv_usd") or 0)
                born = a.get("pool_created_at")
                age = (time.time() - time.mktime(time.strptime(born[:19], "%Y-%m-%dT%H:%M:%S")) + time.timezone) / 86400 if born else 0
                price = float(a.get("base_token_price_usd") or 0)
                if (sym in EXCLUDE or key(chain, addr) in WRAPPED or supported(chain, addr) or liq < r.get("min_liquidity", 1_000_000) or vol < r.get("min_volume_24h", 2_000_000)
                        or age < r.get("min_age_days", 3) or cap < r.get("min_cap", 20_000_000) or 0.97 < price < 1.03 or price <= 0):
                    continue
                k = key(chain, addr)
                if k not in cands or vol > cands[k]["vol24"]:
                    cands[k] = {"symbol": sym, "chain": chain, "address": addr, "pool": a["address"], "vol24": vol, "liq": liq, "mcap": cap, "age_days": round(age, 1)}
    rows = []
    for k, x in sorted(cands.items(), key=lambda kv: -kv[1]["vol24"])[:r.get("max_scored", 14)]:
        d = gt(f"/networks/{x['chain']}/pools/{x['pool']}/ohlcv/hour?aggregate=1&limit=26")
        bars = sorted(((d or {}).get("data") or {}).get("attributes", {}).get("ohlcv_list", []))
        if len(bars) < 25:
            continue
        bars = bars[-25:]
        m = core([b[4] for b in bars], max(b[2] for b in bars[1:]), sum(b[5] for b in bars[1:]), bars[-1][4], bars[-1][0] * 1000, c)
        A, B, score = grade(m, {**c, "min_usd_vol_24h": 0})
        cgx = cg.get(k)
        on = sorted(n for n, syms in cex.items() if cgx and cgx["sym"] in syms) if cgx else []
        rows.append({**x, "A": A, "B": B, "clean": A and B, "score": score, "other_exchanges": on, "cg_verified": bool(cgx),
                     **{q: (round(z, 4) if isinstance(z, float) else z) for q, z in m.items() if q in ("ret_6h", "ret_24h", "up_hours", "r2", "max_drawdown", "off_high", "spike_share")},
                     "links": {"DexScreener": f"https://dexscreener.com/{x['chain']}/{x['address']}"}})
    rows.sort(key=lambda z: (-int(z["clean"]), -len(z["other_exchanges"]), -z["score"]))
    return {"updated": time.time(), "candidates": len(cands), "rows": rows}


HITS = os.path.join(DATA, "watcher_hits.json")


def watcher_check(v):
    """The ORIGINAL coin watcher (agent.py, unchanged: >= $150k within 4 h of a first pool, same liquidity / buys / honeypot filters,
    Solana + Base) is the source of pump.fun and DEX candidates. This checks every token it tracks or has ever flagged against the
    Coinbase / Robinhood address lists. A hit that later gets listed becomes a 'listing' event with the watcher's record attached."""
    hits = load(HITS, {})
    tracked = load(os.path.join(DATA, "tracked.json"), {}).get("tokens", [])
    seen = {**{f"{h['net']}:{h['token']}": h for h in hits.values()}, **{f"{t['net']}:{t['token']}": {**t, "hit": False} for t in tracked}}
    sup_hits, sup_tracked, ev = [], [], load(LISTINGS, {})
    events = ev.get("events", [])
    for k, t in seen.items():
        m = supported(t["net"], t["token"])
        if not m:
            continue
        row = {"symbol": t.get("symbol"), "chain": t["net"], "address": t["token"], "coinbase": m["coinbase"], "robinhood": m["robinhood"],
               "links": links(m["symbol"], m["coinbase"], m["robinhood"], t["net"], t["token"])}
        (sup_hits if k in {f"{h['net']}:{h['token']}" for h in hits.values()} else sup_tracked).append(row)
        if k in {f"{h['net']}:{h['token']}" for h in hits.values()} and not any(e.get("address") == t["token"] and e["kind"] == "watcher_hit_listed" for e in events):
            h = next(h for h in hits.values() if h["token"] == t["token"])
            events.append({"t": time.time(), "kind": "watcher_hit_listed", "label": "Watcher hit is now listed", "symbol": m["symbol"], "chain": t["net"],
                           "address": t["token"], "watcher_hit": {"t": h.get("t"), "mc": h.get("mc")}})
    if events != ev.get("events", []):
        ev["events"] = events
        save(LISTINGS, ev)
    return {"tracked": len(tracked), "hits": len(hits), "listed_hits": sup_hits, "listed_tracked": sup_tracked}


def links(sym, cb, rh, chain=None, addr=None):
    out = {}
    if cb:
        out["Coinbase"] = f"https://www.coinbase.com/advanced-trade/spot/{sym}-USD"
    if rh:
        out["Robinhood"] = f"https://robinhood.com/us/en/crypto/{sym}/"
    if chain and addr:
        out["DexScreener"] = f"https://dexscreener.com/{chain}/{addr}"
    return out


def scan(quiet=False):
    import trade_core
    c = cfg()
    v = venues()
    uni = load(os.path.expanduser("~/flip-notifier/crypto-universe.json"), {}).get("symbols", [])
    by_addr = {}
    for a, m in v["addr"].items():
        by_addr.setdefault(m["symbol"], a)
    rows, off_chain = [], []
    ok_chains = allowed_chains(v)
    for s in uni:
        sym = s["name"]
        if sym in EXCLUDE:
            continue
        cb, rh = sym in v["coinbase"], sym in v["robinhood"]
        if not (cb or rh):
            continue
        ch = v.get("chains", {}).get(sym, [])
        if not (set(ch) & ok_chains):                  # blockchain unknown or not one Coinbase/Robinhood support: not included
            off_chain.append(sym)
            continue
        m = metrics(trade_core.bars(s["tv"], days=3), c)
        if not m or time.time() * 1000 - m["last_bar"] > 90 * 60_000:      # stale data is not scored
            continue
        A, B, score = grade(m, c)
        r = {"symbol": sym, "coinbase": cb, "robinhood": rh, "halted": (v["robinhood"].get(sym) or {}).get("halted", []),
             "chains": ch[:6], "A": A, "B": B, "clean": A and B, "score": score, **{k: (round(x, 4) if isinstance(x, float) else x) for k, x in m.items()}}
        a = by_addr.get(sym)
        if a:
            r["chain"], r["address"] = a.split(":", 1)
            r["address"] = v.get("canon", {}).get(a, r["address"])
        rows.append(r)
    _mcaps([r for r in rows if r["clean"] or r["A"] or r["B"]])
    st = load(STATE, {})
    now = time.time()
    for r in rows:
        if r["clean"]:
            st.setdefault(r["symbol"], now)
            r["clean_since"] = st[r["symbol"]]
        else:
            st.pop(r["symbol"], None)
    save(STATE, st)
    try:
        events = listing_watch(v)
    except Exception as e:
        events = load(LISTINGS, {}).get("events", []); print("listing watch error", repr(e), file=sys.stderr)
    try:
        wc = watcher_check(v)
    except Exception as e:
        wc = None; print("watcher check error", repr(e), file=sys.stderr)
    events = load(LISTINGS, {}).get("events", events)
    rd = load(BOARD, {}).get("radar")
    if c.get("radar", {}).get("show", True) and (not rd or time.time() - rd["updated"] > 55 * 60):
        try:
            rd = radar(v, c)
        except Exception as e:
            print("radar error", repr(e), file=sys.stderr)
    for r in rows:
        r["links"] = links(r["symbol"], r["coinbase"], r["robinhood"], r.get("chain"), r.get("address"))
    rows.sort(key=lambda r: (-int(r["clean"]), -r["score"]))
    out = {"updated": now, "universe": len(rows), "thresholds": c, "excluded_chain_unverified": sorted(off_chain),
           "listings": [e for e in events if time.time() - e["t"] < 14 * 86400], "watcher": wc, "radar": rd,
           "chains": {"coinbase": len(v.get("coinbase_networks", {})), "robinhood": len(v.get("robinhood_networks", {})), "allowed": len(ok_chains)},
           "counts": {"clean": sum(r["clean"] for r in rows), "A_only": sum(r["A"] and not r["B"] for r in rows), "B_only": sum(r["B"] and not r["A"] for r in rows)},
           "rows": rows}
    save(BOARD, out)
    if not quiet:
        show(out)
    return out


def _ago(t):
    d = time.time() - t
    return f"{d / 3600:.1f}h ago" if d < 86400 else f"{d / 86400:.1f}d ago"


def show_extras(b):
    """Watcher check, new listings, and the separate radar."""
    w = b.get("watcher")
    if w is not None:
        print(f"\nOriginal watcher (unchanged parameters): tracking {w['tracked']} tokens, {w['hits']} hits so far; "
              f"{len(w['listed_hits']) + len(w['listed_tracked'])} of them listed on Coinbase or Robinhood")
        for r in w["listed_hits"] + w["listed_tracked"]:
            print(f"  {r['symbol']} ({r['chain']})  " + " · ".join(f"{k} {u}" for k, u in r["links"].items()))
    ls = b.get("listings") or []
    print("\nNew listings and listing signals (last 14 days): " + ("none yet" if not ls else ""))
    for e in sorted(ls, key=lambda e: -e["t"])[:8]:
        o = e.get("origin") or {}
        bits = [e["label"], _ago(e["t"])]
        if o.get("pump_fun"):
            bits.append("launched on pump.fun")
        if o.get("age_days") is not None:
            bits.append(f"token {o['age_days']}d old")
        if e.get("watcher_hit"):
            bits.append("was a watcher hit")
        print(f"  {e['symbol']:<9} " + " · ".join(bits))
    rd = b.get("radar")
    if rd and rd.get("rows") is not None:
        rows = rd["rows"]
        clean = [r for r in rows if r["clean"]]
        print(f"\nRadar (separate; NOT on Coinbase or Robinhood today, so not buyable there): {len(clean)} of {len(rows)} scored pass A and B · {rd['candidates']} unlisted "
              f"heavy-volume Solana/Base tokens screened · {int((time.time() - rd['updated']) / 60)}m ago")
        for r in (clean or rows)[:5]:
            on = ", ".join(r["other_exchanges"]) or ("no other exchange found" if r["cg_verified"] else "exchange match unverified")
            tag = "A+B" if r["clean"] else ("A" if r["A"] else "B" if r["B"] else "-")
            print(f"  {r['symbol']:<9} {tag:<3} 24h {_pct(r['ret_24h'])}  worst dip {r['max_drawdown']:.1%}  mcap ${r['mcap']:,.0f}  liq ${r['liq']:,.0f}  {r['age_days']}d old  also on: {on}")
            print("    🔗 " + " · ".join(f"{k} {u}" for k, u in r["links"].items()))


def _pct(x):
    return f"{100 * x:+.1f}%"


def show(b=None, top=8):
    b = b or load(BOARD, None)
    if not b or time.time() - b["updated"] > 45 * 60:
        print("Listed-coin board: no fresh scan (com.dhruv.coinlaunch.listed runs every 15 min)")
        return
    c = b["thresholds"]
    cn = b["counts"]
    print(f"Coinbase + Robinhood tokens that are (A) rising fast and steadily and (B) holding: {cn['clean']} of {b['universe']} scored · "
          f"{cn['A_only']} running but not holding · {cn['B_only']} holding but not running · {int((time.time() - b['updated']) / 60)}m ago")
    print(f"  A = +{c['fast_gain_6h']:.0%}/6h or +{c['fast_gain_24h']:.0%}/24h, up in ≥{c['min_up_hours']:.0%} of hours, straight-line R² ≥ {c['min_r2']}, no single hour > {c['max_spike_share']:.0%} of the gain")
    print(f"  B = worst dip in the last {c['hold_hours']}h ≤ {c['max_drawdown']:.0%} and price within {c['max_off_high']:.0%} of its 24h high · market cap moves = price moves\n")
    ch = b.get("chains", {})
    if ch:
        print(f"  Chains: {ch['allowed']} blockchains (Coinbase supports {ch['coinbase']}, Robinhood-listed assets sit on {ch['robinhood']}); "
              f"excluded, chain not verifiable: {', '.join(b.get('excluded_chain_unverified', [])) or 'none'}")
    clean = [r for r in b["rows"] if r["clean"]][:top]
    if not clean:
        print("  Nothing passes both tests right now.")
        near = [r for r in b["rows"] if r["A"] or r["B"]][:5]
        for r in near:
            tag = "running, not holding" if r["A"] else "holding, not running"
            print(f"  {r['symbol']:<9} {tag:<22} 24h {_pct(r['ret_24h'])}  6h {_pct(r['ret_6h'])}  worst dip {r['max_drawdown']:.1%}  off high {r['off_high']:.1%}  score {r['score']}")
    for r in clean:
        venue = "+".join(n for n, on in (("Coinbase", r["coinbase"]), ("Robinhood", r["robinhood"])) if on)
        mc = f"  mcap ${r['mcap']:,.0f}" if r.get("mcap") else ""
        since = f"  clean for {(time.time() - r['clean_since']) / 3600:.1f}h" if r.get("clean_since") else ""
        halt = f"  (Robinhood halted in {', '.join(r['halted'])})" if r["halted"] and r["robinhood"] else ""
        print(f"  {r['symbol']:<9} score {r['score']:>3}  24h {_pct(r['ret_24h'])}  6h {_pct(r['ret_6h'])}  up-hours {r['up_hours']:.0%}  R² {r['r2']:.2f}  "
              f"worst dip {r['max_drawdown']:.1%}  off high {r['off_high']:.1%}{mc}{since}  [{venue}]{halt}")
        print("    🔗 " + " · ".join(f"{k} {u}" for k, u in r["links"].items()))
    show_extras(b)


# ------------------------------------------------------------------ junk pruning
JUNK = ["data/pre", "data/pump", "data/dataset.parquet", "data/plus50.json", "data/plus50_state.json", "data/live.json", "data/tracked.json",
        "data/scores.json", "data/train.log", "data/alert.txt", "models/plus50", "models/plus50_meta.json", "models/pre_meta.json", "results"]


def prune_junk():
    """Back up (local tgz, deleted by the daily sweep after 7 days) then delete everything the pump.fun / DEX watcher collected:
    none of it can pass the Coinbase/Robinhood filter. The git archive/ folder is history and stays untouched."""
    import subprocess, shutil
    keep = [p for p in JUNK if os.path.exists(os.path.join(HERE, p))]
    tgz = os.path.join(DATA, f"_pruned_{time.strftime('%Y-%m-%d')}.tgz")
    subprocess.run(["/usr/bin/tar", "czf", tgz] + keep, cwd=HERE, check=True)
    freed = 0
    for p in keep:
        full = os.path.join(HERE, p)
        freed += sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(full) for f in fs) if os.path.isdir(full) else os.path.getsize(full)
        shutil.rmtree(full) if os.path.isdir(full) else os.remove(full)
    print(f"backup {tgz} ({os.path.getsize(tgz) / 1e6:.1f} MB, removed by the sweep after 7 days); deleted {len(keep)} items, {freed / 1e6:.0f} MB freed")


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a or a[0] == "scan":
        scan()
    elif a[0] == "venues":
        v = venues(refresh=True)
        print(f"Coinbase online spot assets {len(v['coinbase'])} · Robinhood {len(v['robinhood'])} · on-chain addresses {len(v['addr'])}")
    elif a[0] == "chains":
        v = venues(refresh="--refresh" in a)
        print("Coinbase supported networks (assets on each):", dict(sorted(v["coinbase_networks"].items(), key=lambda kv: -kv[1])))
        print("Chains of Robinhood-listed assets:", dict(sorted(v["robinhood_networks"].items(), key=lambda kv: -kv[1])))
        print(f"Allowed: {len(allowed_chains(v))} blockchains")
    elif a[0] == "check" and len(a) > 1:
        for ch in ("solana", "base"):
            m = supported(ch, a[1])
            if m:
                print(ch, m); break
        else:
            print("not listed on Coinbase or Robinhood")
    elif a[0] == "prune-junk":
        prune_junk()
    else:
        print(__doc__)
