#!/usr/bin/env python3
"""Pre-graduation detector (2026-09-29): score brand-new pump.fun coins while they are still on the
bonding curve for "most likely to graduate AND explode".

  score = P(graduate) x P(explode | graduate)

  Stage A  P(graduate): does the coin reach a $75k market cap (the graduation-era cutoff the whole
           agent uses) within 4h of launch? Trained on OUR OWN live snapshots, because pump.fun's
           listing only reaches back ~55 minutes, so coins that never graduated can't be backfilled.
           Until enough labeled snapshots exist, stage A is absent and the board says so.
  Stage B  P(explode | graduate): the plus50 event (reaches $150k within 4h, and is up 50%+ one hour
           later) given the coin's PRE-graduation features. Trainable now from the ~3,500 cached
           graduates and their 5-minute candles from launch.

Discovery: the newest-first listing (usd_market_cap for the newest ~700 coins, one page per 2.7 s), keeping
coins with real traction (>= $5k). Each is then tracked by mint through the candles endpoint (no rate
limit), snapshotted every tick with candle features + static features, and labeled later from candles.

  prelaunch.py tick          every 5 min (com.dhruv.coinlaunch.pre): discover, snapshot, score, resolve
  prelaunch.py train         nightly: fit stage B (and stage A once >= MIN_A_POS positives exist)
  prelaunch.py board         print the current board
"""
import json, math, os, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import numpy as np

import ml, onchain, pump

HERE = os.path.dirname(os.path.abspath(__file__))
PRE = os.path.join(HERE, "data", "pre")
COINS = os.path.join(PRE, "coins.json")            # tracked coins: static meta + snapshot bookkeeping
SNAPS = os.path.join(PRE, "snapshots.jsonl")       # LEGACY single file (migrated into SNAP_DIR on first use)
SNAP_DIR = os.path.join(PRE, "snaps")              # one gzip file per UTC day, one row per (coin, tick): features + ids
LABELS = os.path.join(PRE, "labels.json")          # mint -> {"grad": 0/1, "explode": 0/1/None, "resolved_at": ms}
BOARD = os.path.join(PRE, "board.json")
MODELS = os.path.join(HERE, "models")
MODEL_A, MODEL_B = os.path.join(MODELS, "pre_a"), os.path.join(MODELS, "pre_b")
META = os.path.join(MODELS, "pre_meta.json")
REPORT = os.path.join(HERE, "results", "prelaunch_report.md")

MIN_TRACTION_USD = 5_000         # a coin needs this much market cap to be followed
GRAD_MC = 75_000                 # "graduated" = reaches this (matches ml.MIN_MC used across the agent)
EXPLODE_ENTRY_MC = 150_000
HORIZON_H = 4                    # graduation must happen within 4h of launch
MAX_AGE_TRACK_MIN = 120          # snapshot coins only while <= 2h old
MIN_AGE_MIN = 6
LIST_PAGES = 12                  # ~600 newest coins ~ 18 min of launches (pump.fun allows ~1 request / 2.7 s, erratically)
MIN_A_POS = 60                   # labeled positives before stage A trains
MIN_AUC = 0.60                   # a stage is used only if it beats coin-flip on held-out coins
os.makedirs(PRE, exist_ok=True)


def log(msg):
    print(f"{datetime.now():%m-%d %H:%M:%S} {msg}", flush=True)


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


# ---------------------------------------------------------------- snapshot storage
import glob, gzip, shutil, zlib


def _day(t_ms):
    return time.strftime("%Y-%m-%d", time.gmtime(t_ms / 1000))


def write_snaps(rows):
    """One small gzip per scan, written to a temp file and renamed into place, so a crash can never leave a torn
    file (appending to a shared gzip lets one torn write make the rest of the day unreadable). Older days are
    packed into a single tighter file by pack_old(). Layout: snaps/<day>/<HHMMSS>.jsonl.gz and snaps/<day>.jsonl.gz."""
    by = {}
    for r in rows:
        by.setdefault(_day(r["t"]), []).append(r)
    stamp = time.strftime("%H%M%S", time.gmtime())
    for day, rs in by.items():
        d = os.path.join(SNAP_DIR, day)
        os.makedirs(d, exist_ok=True)
        path, i = os.path.join(d, stamp + ".jsonl.gz"), 0
        while os.path.exists(path):
            i += 1
            path = os.path.join(d, f"{stamp}-{i}.jsonl.gz")
        tmp = path + ".tmp"
        with gzip.open(tmp, "wb", compresslevel=6) as f:
            f.write(("\n".join(json.dumps(r) for r in rs) + "\n").encode())
        os.replace(tmp, path)


def _read_gz(path):
    try:
        with gzip.open(path, "rt") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and "mint" in r and "t" in r:      # partial or foreign rows never reach training
                    yield r
    except (EOFError, OSError, zlib.error):
        return          # a damaged file never blocks the rest


def iter_snaps():
    migrate_legacy()
    for path in sorted(glob.glob(os.path.join(SNAP_DIR, "*.jsonl.gz")) + glob.glob(os.path.join(SNAP_DIR, "*", "*.jsonl.gz"))):
        yield from _read_gz(path)


def pack_old():
    """Merge each finished day's per-scan files into one file at the highest compression (~2x tighter again)."""
    today = _day(time.time() * 1000)
    for d in sorted(glob.glob(os.path.join(SNAP_DIR, "*-*-*"))):
        if not os.path.isdir(d) or os.path.basename(d) >= today:
            continue
        files = sorted(glob.glob(os.path.join(d, "*.jsonl.gz")))
        rows = [r for f in files for r in _read_gz(f)]
        out = d + ".jsonl.gz"
        tmp = out + ".tmp"
        if os.path.exists(out):                              # merge with an existing packed file for the same day
            rows = list(_read_gz(out)) + rows
        with gzip.open(tmp, "wb", compresslevel=9) as f:
            f.write(("\n".join(json.dumps(r) for r in rows) + "\n").encode())
        os.replace(tmp, out)
        shutil.rmtree(d)


def migrate_legacy():
    """One-time: the old single snapshots.jsonl becomes per-day files."""
    if not os.path.exists(SNAPS):
        return
    by = {}
    with open(SNAPS) as f:
        for line in f:
            try:
                r = json.loads(line)
                by.setdefault(_day(r["t"]), []).append(r)
            except (ValueError, KeyError):
                pass
    for day, rs in by.items():
        d = os.path.join(SNAP_DIR, day)
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, "legacy.jsonl.gz.tmp")
        with gzip.open(tmp, "wb", compresslevel=6) as f:
            f.write(("\n".join(json.dumps(r) for r in rs) + "\n").encode())
        os.replace(tmp, os.path.join(d, "legacy.jsonl.gz"))
    os.remove(SNAPS)


# ---------------------------------------------------------------- features
CREATOR_GRAD = None


def creator_grads():
    """creator -> number of cached graduated coins (a free 'this dev has shipped a winner' feature)."""
    global CREATOR_GRAD
    if CREATOR_GRAD is None:
        CREATOR_GRAD = {}
        for c in load(os.path.join(pump.CACHE, "coins.json"), {}).values():
            if c.get("creator"):
                CREATOR_GRAD.setdefault(c["creator"], []).append(c.get("created_timestamp", 0))
    return CREATOR_GRAD


def static_features(meta, created_ms):
    prior = sum(1 for t in creator_grads().get(meta.get("creator"), []) if t < created_ms)
    desc, name, sym = meta.get("description") or "", meta.get("name") or "", meta.get("symbol") or ""
    return {"has_twitter": int(bool(meta.get("twitter"))), "has_website": int(bool(meta.get("website"))),
            "has_telegram": int(bool(meta.get("telegram"))), "name_len": len(name), "sym_len": len(sym),
            "desc_len": len(desc), "creator_prior_grads": prior, "name_upper_share": (sum(ch.isupper() for ch in name) / len(name)) if name else 0.0}


def candle_features(g, k, created_ms):
    """Features from bars closed up to index k (market-cap dollars). Same function for live and history."""
    c, h, v = g["close"], g["high"], g["vol"]
    lc = np.log(np.maximum(c[: k + 1], 1.0))
    base = math.log(max(g["first_open"], 1.0))
    t_close = g["t0"] + (k + 1) * ml.BAR_MS

    def ret(n):
        return lc[-1] - (lc[-1 - n] if k - n >= 0 else base)

    ath_i = int(np.argmax(h[: k + 1]))
    d = np.diff(lc) if k >= 1 else np.zeros(1)
    last6 = d[-6:] if len(d) else np.zeros(1)
    over5k = np.nonzero(c[: k + 1] >= MIN_TRACTION_USD)[0]
    return {
        "age_min": (t_close - created_ms) / 60000, "log_mc": float(lc[-1]),
        "ret_5m": ret(1), "ret_10m": ret(2), "ret_15m": ret(3), "ret_30m": ret(6), "ret_since_launch": float(lc[-1] - base),
        "drawdown": float(lc[-1] - math.log(max(h[ath_i], 1.0))), "bars_since_ath": k - ath_i,
        "log_ath": math.log(max(h[ath_i], 1.0)),
        "log_vol_5m": math.log1p(v[k]), "log_vol_15m": math.log1p(v[max(0, k - 2): k + 1].sum()),
        "log_vol_total": math.log1p(v[: k + 1].sum()),
        "vol_accel": float(v[max(0, k - 2): k + 1].sum() / (v[max(0, k - 8): max(1, k - 2)].sum() / 2 + 1)),
        "active_frac": float((v[max(0, k - 5): k + 1] > 0).mean()), "rv_30m": float(np.std(last6)),
        "up_frac": float((last6 > 0).mean()), "max_bar_jump": float(d.max()) if len(d) else 0.0,
        "mins_since_5k": float(((k - over5k[0]) * 5) if len(over5k) else -1), "log_first_close": math.log(max(c[0], 1.0)),
        "hour_utc": (t_close // 3_600_000) % 24,
    }


CANDLE_KEYS = list(candle_features({"close": np.array([6e3, 7e3]), "high": np.array([6e3, 7e3]), "vol": np.array([1.0, 1.0]),
                                    "t0": 0, "first_open": 3e3}, 1, 0).keys())
STATIC_KEYS = list(static_features({}, 0).keys())
FEATURES = CANDLE_KEYS + STATIC_KEYS
# Free on-chain features (onchain.py), available for live snapshots only (no history to backfill): used by stage A.
ONCHAIN_KEYS = ["curve_progress", "curve_sol", "n_trades", "n_trades_5m", "n_trades_15m", "fail_share", "first_slot_txs",
                "unique_slots", "creator_share", "creator_sold", "mint_authority", "freeze_authority", "n_extensions",
                "creator_txs", "creator_wallet_age_h", "oc_age_min", "curve_std", "curve_k_ratio"]
FEATURES_A = FEATURES + ONCHAIN_KEYS


# ---------------------------------------------------------------- discovery
def _page(url):
    """One listing page. pump.fun sustains about one request per 2.5-3 s (measured 2026-09-29: 9/10 ok at
    2.5 s spacing, 5/10 at 1.8 s), so a 429 is simply retried after a short pause."""
    import urllib.request, urllib.error
    for i in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=pump.UA), timeout=20) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            time.sleep(2.8 if e.code == 429 else 2.0 * (i + 1))
        except Exception:
            time.sleep(1.5)
    return None


def list_newest(budget_s=150):
    """Newest ~600 coins (~18 min of launches) with their current usd_market_cap. pump.fun's throttling is
    erratic (about 11 of 14 pages succeed at 2.7 s spacing), so failed pages are skipped, not fatal: ticks
    overlap (5 min vs ~18 min of coverage), so a coin missed once is seen on the next tick."""
    out, t0 = [], time.time()
    for p in range(LIST_PAGES):
        if time.time() - t0 > budget_s:
            break
        d = _page(f"https://frontend-api-v3.pump.fun/coins?offset={p * 50}&limit=50&sort=created_timestamp&order=DESC&includeNsfw=true")
        if d:
            out.extend(d)
        time.sleep(2.7)
    return out


def refresh_onchain(coins, mints, budget_s=85):
    """Free Solana on-chain features for tracked coins, stalest first, within a time budget (the public RPC is
    paced to ~4.5 calls/s; 3 dynamic calls per coin plus 2 static calls the first time we see it)."""
    todo = [m for m in mints if coins[m]["meta"].get("bonding_curve")]
    # 1) coins close to graduating (curve >= 75%, the 20 closest: their curve moves fastest and matters most),
    # 2) never-read coins, newest first (their early features are the training data), 3) the stalest of the rest.
    # First version put every coin >= 50% (65 of them) in tier 1, which starved 141 never-read coins.
    def cp_of(m):
        return (coins[m].get("oc_dyn") or {}).get("curve_progress") or 0
    close = set(sorted((m for m in todo if cp_of(m) >= 0.75), key=lambda m: -cp_of(m))[:20])

    def prio(m):
        if m in close:
            return (0, coins[m].get("oc_t", 0))
        if not coins[m].get("oc_t"):
            return (1, -coins[m]["created"])
        return (2, coins[m].get("oc_t", 0))
    todo.sort(key=prio)
    deadline = time.time() + budget_s

    def one(m):
        if time.time() > deadline:
            return
        c = coins[m]
        meta = {**c["meta"], "mint": m}
        try:
            if "oc_static" not in c:
                c["oc_static"] = onchain.solana_static(meta)
            dyn = onchain.solana_dynamic(meta, c["created"], c.get("oc_dyn"))
            if dyn:
                c["oc_dyn"] = dyn
                c["oc_t"] = int(time.time() * 1000)
        except Exception:
            pass

    with ThreadPoolExecutor(4) as ex:
        list(ex.map(one, todo))


def oc_features(c, now_ms):
    f = {**(c.get("oc_static") or {}), **(c.get("oc_dyn") or {})}
    out = {k: f[k] for k in ONCHAIN_KEYS if k in f}
    if c.get("oc_t"):
        out["oc_age_min"] = max(0.0, (now_ms - c["oc_t"]) / 60000)
    return out


TRACKED = os.path.join(HERE, "data", "tracked.json")     # written by the coin watcher (agent.py)
BASE_SNAPS = os.path.join(PRE, "base_snapshots.jsonl")
BASE_BOARD = os.path.join(PRE, "base_board.json")


def base_tick(now_ms, budget_s=25):
    """Base coins the watcher already tracks: holder distribution from free Base RPC logs. Data collection only
    (Base has no bonding-curve graduation event, so there's no label or model yet). Applies the watcher's own
    sanity filters: no clone tokens with fake caps (cap/liquidity > 100) or under $8k liquidity."""
    t = load(TRACKED, {})
    cands = []
    for x in t.get("tokens", []):
        if x.get("net") != "base" or not x.get("launch") or x.get("why"):
            continue
        age = now_ms / 60000 - x["launch"] / 60
        mc, liq = x.get("mc") or 0, x.get("liq") or 0
        if 5 <= age <= 180 and mc >= 5000 and liq >= 8000 and mc / max(liq, 1) <= 100:
            cands.append((x, age))
    cands.sort(key=lambda z: -(z[0].get("mc") or 0))
    deadline = time.time() + budget_s
    rows = []
    for x, age in cands[:40]:
        if time.time() > deadline:
            break
        f = onchain.base_features(x["token"], x["launch"] * 1000)
        if f:
            rows.append({"token": x["token"], "symbol": x.get("symbol"), "t": int(now_ms), "age_min": round(age, 1),
                         "mc": x.get("mc"), "liq": x.get("liq"), **f})
    if rows:
        with open(BASE_SNAPS, "a") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
    save(BASE_BOARD, {"updated": time.time(), "candidates": len(cands), "rows": rows})
    return len(rows)


def tick():
    import faulthandler
    faulthandler.dump_traceback_later(270, exit=True)
    t0 = time.time(); now_ms = t0 * 1000
    coins = load(COINS, {})
    listing = list_newest(budget_s=100)
    found = 0
    for c in listing:
        age = (now_ms - c["created_timestamp"]) / 60000
        if c["mint"] in coins or (c.get("usd_market_cap") or 0) < MIN_TRACTION_USD or age > 45:
            continue
        coins[c["mint"]] = {"created": c["created_timestamp"], "symbol": c.get("symbol"), "name": c.get("name"), "first_seen": now_ms,
                            "meta": {**{k: c.get(k) for k in ("creator", "description", "name", "symbol", "twitter", "website", "telegram", "bonding_curve")}, "mint": c["mint"]},
                            "first_mc": c.get("usd_market_cap"), "replies": c.get("reply_count")}
        found += 1
    # snapshot every tracked coin that is still in its window
    active = [m for m, c in coins.items() if MIN_AGE_MIN <= (now_ms - c["created"]) / 60000 <= MAX_AGE_TRACK_MIN]

    def one(m):
        c = coins[m]
        try:
            bars = pump.candles_5m(m, cache=False)
        except Exception:
            return None
        bars = [b for b in bars if b[0] + ml.BAR_MS <= now_ms - 20_000]
        if not bars:
            return None
        g = ml.grid(bars, c["created"]); k = len(g["close"]) - 1
        if g["t0"] + ml.BAR_MS < c["created"] - 3_600_000:
            return None
        return m, {**candle_features(g, k, c["created"]), **static_features(c["meta"], c["created"])}, float(g["close"][k])

    with ThreadPoolExecutor(12) as ex:
        res = [r for r in ex.map(one, active) if r]
    refresh_onchain(coins, [m for m, _, _ in res])
    rows = []
    for m, f, mc in res:
        rows.append({"mint": m, "t": int(now_ms), "mc": mc, "created": coins[m]["created"], **f, **oc_features(coins[m], now_ms)})
    if rows:
        write_snaps(rows)
    save(COINS, coins)
    update_curves(rows, coins)
    board = score_board(rows, coins)
    for r in board[:5]:
        if r["score"] is not None and "picked" not in coins[r["mint"]]:
            coins[r["mint"]]["picked"] = {"t": int(now_ms), "mc": r["mc"], "score": r["score"]}
    save(COINS, coins)
    resolved = resolve(coins, now_ms)
    n_base = base_tick(now_ms)
    prune()
    log(f"listing {len(listing)} · new tracked {found} · tracking {len(coins)} · snapshots {len(rows)} · "
        f"board {len(board)} · onchain {sum(1 for r in rows if 'curve_progress' in r)}/{len(rows)} · base {n_base} · resolved {resolved} · {time.time() - t0:.0f}s")


# ---------------------------------------------------------------- labels
def resolve(coins, now_ms):
    """4h+ after launch, label each tracked coin from its candles: grad = reached GRAD_MC within 4h;
    explode = the plus50 event (reaches EXPLODE_ENTRY_MC within 4h, ends +50% an hour after that entry)."""
    labels = load(LABELS, {})
    due = [m for m, c in coins.items() if m not in labels and now_ms - c["created"] > (HORIZON_H * 60 + 70) * 60000]
    due = due[:150]
    if not due:
        return 0

    def one(m):
        c = coins[m]
        try:
            bars = pump.candles_5m(m, cache=False)
        except Exception:
            return m, None
        if not bars:
            return m, {"grad": 0, "explode": 0, "resolved_at": int(now_ms), "empty": 1}
        g = ml.grid(bars, c["created"])
        ent = None
        for k in range(len(g["close"])):
            if (g["t0"] + (k + 1) * ml.BAR_MS - c["created"]) / 60000 > HORIZON_H * 60:
                break
            if g["close"][k] >= EXPLODE_ENTRY_MC:
                ent = k; break
        within = [g["close"][k] for k in range(len(g["close"])) if (g["t0"] + (k + 1) * ml.BAR_MS - c["created"]) / 60000 <= HORIZON_H * 60]
        grad = int(bool(within) and max(within) >= GRAD_MC)
        ex = None
        if ent is not None:
            end = g["close"][min(ent + 12, len(g["close"]) - 1)] / g["close"][ent]
            ex = int(end >= 1.5)
        return m, {"grad": grad, "explode": (ex or 0) if grad else 0, "resolved_at": int(now_ms)}

    with ThreadPoolExecutor(12) as ex_:
        for m, lab in ex_.map(one, due):
            if lab is not None:
                labels[m] = lab
    save(LABELS, labels)
    ledger_picks(coins, labels)
    return len([m for m in due if m in labels])


def ledger_picks(coins, labels):
    """Grade each board pick once its coin is resolved: hold from the pick's market cap for 2h (from candles),
    cost 1.5% per side. Win = 1.5x or better. Goes to trade-core's ledger as product 'prelaunch'."""
    sys.path.insert(0, os.path.expanduser("~/trade-core"))
    try:
        import trade_core
    except Exception:
        return
    for m, c in coins.items():
        pk = c.get("picked")
        if not pk or c.get("ledgered") or m not in labels:
            continue
        try:
            bars = pump.candles_5m(m, cache=False)
            g = ml.grid(bars, c["created"])
            k0 = int((pk["t"] - g["t0"]) // ml.BAR_MS) - 1
            k1 = min(k0 + 24, len(g["close"]) - 1)
            if k0 < 0 or k0 >= len(g["close"]):
                continue
            mult = float(g["close"][k1] / g["close"][k0])
        except Exception:
            continue
        cost = 0.03
        trade_core.add_event("prelaunch", "top5", f"SOL:{m}", "pumpfun", pk["t"], pk["mc"],
                             {"net": mult - 1 - cost, "gross": mult - 1, "win": mult >= 1.5, "cost": cost},
                             meta={"symbol": c.get("symbol"), "score": pk["score"], "grad": labels[m].get("grad")})
        c["ledgered"] = True
    save(COINS, coins)


# ---------------------------------------------------------------- models
_models = {}


def _predictor(path):
    if path not in _models:
        if not os.path.isdir(path):
            _models[path] = None
        else:
            from autogluon.tabular import TabularPredictor
            _models[path] = TabularPredictor.load(path, require_py_version_match=False)
    return _models[path]


CURVES = os.path.join(PRE, "curves.json")


def update_curves(rows, coins):
    """Latest on-chain curve reading for EVERY coin seen (the board only shows the top 40 by model score, which
    left the 'closest to graduating' list empty). Keeps coins read in the last 30 min; completed curves go to
    a 24 h graduated list (the on-chain `complete` flag is the truth: after migration the API market cap collapses)."""
    now = time.time() * 1000
    d = load(CURVES, {"coins": {}, "graduated": []})
    grads = {g["mint"]: g for g in d["graduated"] if now - g["t"] < 86400_000}
    live = {m: v for m, v in d["coins"].items() if now - v["t"] < 30 * 60_000}
    for r in rows:
        cp = r.get("curve_progress")
        if cp is None:
            continue
        m, c = r["mint"], coins.get(r["mint"], {})
        if cp >= 0.999:
            if m not in grads:
                # a real graduation is one we watched go from incomplete to complete; a coin first read already
                # complete graduated at an unknown time, so it is listed but not counted as a fresh event
                grads[m] = {"mint": m, "symbol": c.get("symbol"), "t": int(r["t"]), "watched": m in live,
                            "mins_after_launch": round((r["t"] - (r.get("created") or c.get("created") or r["t"])) / 60000, 1)}
            live.pop(m, None)
            continue
        live[m] = {"mint": m, "symbol": c.get("symbol"), "t": int(r["t"]), "curve": round(float(cp), 3), "mc": round(float(r["mc"])),
                   "age_min": round(float(r["age_min"]), 1), "read_min_ago": round(float(r.get("oc_age_min") or 0), 1), "std": r.get("curve_std"), "trades_5m": r.get("n_trades_5m"), "fail_share": r.get("fail_share"),
                   "ret_15m": round(float(r["ret_15m"]), 3), "socials": int(r["has_twitter"] + r["has_website"] + r["has_telegram"])}
    save(CURVES, {"updated": time.time(), "coins": live, "graduated": sorted(grads.values(), key=lambda g: -g["t"])})


def score_board(rows, coins):
    """Score this tick's snapshots and write the board (top by P(grad) x P(explode|grad))."""
    import pandas as pd
    if not rows:
        save(BOARD, {"updated": time.time(), "a_ready": False, "b_ready": False, "rows": []})
        return []
    df = pd.DataFrame(rows)
    meta = load(META, {})
    pa = _predictor(MODEL_A) if meta.get("a_active") else None
    pb = _predictor(MODEL_B) if meta.get("b_active") else None
    for k in meta.get("features_a", []):
        if k not in df.columns:
            df[k] = np.nan
    df["p_grad"] = pa.predict_proba(df[meta["features_a"]])[1].values if pa is not None else np.nan
    df["p_explode"] = pb.predict_proba(df[meta["features_b"]])[1].values if pb is not None else np.nan
    # unconditional score when both exist; else the stage that exists
    df["score"] = np.where(df.p_grad.notna() & df.p_explode.notna(), df.p_grad * df.p_explode,
                           df.p_explode.fillna(df.p_grad))
    live = df[(df.mc < GRAD_MC)]
    out = []
    for _, r in live.sort_values("score", ascending=False).head(40).iterrows():
        c = coins[r["mint"]]
        out.append({"mint": r["mint"], "symbol": c.get("symbol"), "name": c.get("name"), "age_min": round(float(r.age_min), 1),
                    "mc": round(float(r.mc)), "score": None if np.isnan(r.score) else round(float(r.score), 4),
                    "p_grad": None if np.isnan(r.p_grad) else round(float(r.p_grad), 4),
                    "p_explode": None if np.isnan(r.p_explode) else round(float(r.p_explode), 4),
                    "ret_15m": round(float(r.ret_15m), 3), "vol_5m": round(math.expm1(float(r.log_vol_5m))),
                    "curve": None if "curve_progress" not in r or np.isnan(r.get("curve_progress", np.nan)) else round(float(r["curve_progress"]), 3),
                    "trades_5m": None if "n_trades_5m" not in r or np.isnan(r.get("n_trades_5m", np.nan)) else int(r["n_trades_5m"]),
                    "fail_share": None if "fail_share" not in r or np.isnan(r.get("fail_share", np.nan)) else round(float(r["fail_share"]), 2),
                    "creator_prior_grads": int(r.creator_prior_grads), "socials": int(r.has_twitter + r.has_website + r.has_telegram)})
    save(BOARD, {"updated": time.time(), "a_ready": pa is not None, "b_ready": pb is not None, "rows": out,
                 "labeled": len(load(LABELS, {}))})
    return out


# ---------------------------------------------------------------- retention
KEEP_SNAP_DAYS, KEEP_COIN_DAYS = 14, 3


def prune(force=False):
    """Bound local disk use. Snapshots: delete day files older than KEEP_SNAP_DAYS (instant; they are per-day gzips).
    Base snapshots: 30 days. coins.json: drop coins 3 days after labeling unless a pick still awaits grading (each
    snapshot row carries its own launch time, so training never needs the dropped entries). Labels stay forever
    (tiny, and the candles that made them expire after ~3.5 days). Runs every 6 h from the scan and nightly."""
    marker = os.path.join(PRE, ".pruned")
    if not force and os.path.exists(marker) and time.time() - os.path.getmtime(marker) < 6 * 3600:
        return
    now = time.time() * 1000
    stats = {}
    cutoff = time.strftime("%Y-%m-%d", time.gmtime((now - KEEP_SNAP_DAYS * 86400_000) / 1000))
    migrate_legacy()
    pack_old()
    files = sorted(glob.glob(os.path.join(SNAP_DIR, "*.jsonl.gz")))
    old = [f for f in files if os.path.basename(f)[:10] < cutoff]
    freed = sum(os.path.getsize(f) for f in old)
    for f in old:
        os.remove(f)
    stats["snaps"] = f"{len(files) - len(old)}/{len(files)} day files (freed {freed / 1e6:.0f} MB)"
    if os.path.exists(BASE_SNAPS):
        cut, kept, total = now - 30 * 86400_000, 0, 0
        tmp = BASE_SNAPS + ".tmp"
        with open(BASE_SNAPS) as fin, open(tmp, "w") as fout:
            for line in fin:
                total += 1
                try:
                    if json.loads(line)["t"] >= cut:
                        fout.write(line); kept += 1
                except Exception:
                    pass
        os.replace(tmp, BASE_SNAPS)
        stats["base"] = f"{kept}/{total}"
    stats["candle archive"] = str(archive_candles())
    coins, labels = load(COINS, {}), load(LABELS, {})
    keep = {m: c for m, c in coins.items()
            if not (m in labels and now - c["created"] > KEEP_COIN_DAYS * 86400_000 and (not c.get("picked") or c.get("ledgered")))}
    save(COINS, keep)
    stats["coins.json"] = f"{len(keep)}/{len(coins)}"
    open(marker, "w").close()
    log("prune kept " + ", ".join(f"{k} {v}" for k, v in stats.items()))


# ---------------------------------------------------------------- training
def _fit(df, feats, path, seconds):
    """AutoGluon fit into path.new, swapped in only if it produced models. Folds are hashed from the coin id
    (5 buckets): groups=mint would make one bag fold per coin and time out every model (2026-09-28 bug)."""
    import shutil, zlib
    from autogluon.tabular import TabularPredictor
    tmp = path + ".new"
    shutil.rmtree(tmp, ignore_errors=True)
    data = df[feats + ["y"]].assign(mint=df["mint"].map(lambda m: zlib.crc32(m.encode()) % 5))
    p = TabularPredictor(label="y", problem_type="binary", eval_metric="log_loss", path=tmp, groups="mint", verbosity=1)
    p.fit(data, presets="medium_quality", time_limit=seconds, num_bag_folds=5, num_stack_levels=0, dynamic_stacking=False,
          excluded_model_types=["FASTAI"])
    if not p.model_names():
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError("no models trained")
    shutil.rmtree(path, ignore_errors=True)
    os.replace(tmp, path)
    return TabularPredictor.load(path, require_py_version_match=False)   # the moved model must be reloaded from its final path


def _auc(y, s):
    import pandas as pd
    y, s = np.asarray(y), np.asarray(s)
    pos, neg = s[y == 1], s[y == 0]
    if not len(pos) or not len(neg):
        return float("nan")
    r = pd.Series(np.concatenate([pos, neg])).rank().values
    return (r[: len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def build_b():
    """Stage B rows: every 5-minute bar of a cached graduate that is 6-120 min old, between $5k and $60k
    and before the coin first closed >= $60k. y = the plus50 event (reaches $150k within 4h, +50% an hour on)."""
    import pandas as pd
    pairs, _ = pump.backfill(False, log=log)
    rows = []
    for c, bars in pairs:
        if not bars:
            continue
        created = c["created_timestamp"]
        g = ml.grid(bars, created)
        if g["t0"] + ml.BAR_MS < created - 3_600_000:
            continue
        close = g["close"]
        ent = next((k for k in range(len(close)) if (g["t0"] + (k + 1) * ml.BAR_MS - created) / 60000 <= HORIZON_H * 60 and close[k] >= EXPLODE_ENTRY_MC), None)
        y = 0
        if ent is not None and (g["t0"] + (ent + 1) * ml.BAR_MS - created) / 60000 <= HORIZON_H * 60:
            y = int(close[min(ent + 12, len(close) - 1)] / close[ent] >= 1.5)
        kg = next((k for k in range(len(close)) if close[k] >= 60_000), len(close))
        st = static_features({k: c.get(k) for k in ("creator", "description", "name", "symbol", "twitter", "website", "telegram")}, created)
        for k in range(len(close)):
            age = (g["t0"] + (k + 1) * ml.BAR_MS - created) / 60000
            if k >= kg or age < MIN_AGE_MIN or age > MAX_AGE_TRACK_MIN or not (MIN_TRACTION_USD <= close[k] < 60_000):
                continue
            rows.append({"mint": c["mint"], "created": created, "y": y, **candle_features(g, k, created), **st})
    return pd.DataFrame(rows)


def build_a():
    """Stage A rows from our live snapshots joined to labels (grad within 4h)."""
    import pandas as pd
    labels, coins = load(LABELS, {}), load(COINS, {})
    rows = []
    for r in iter_snaps():
        lab = labels.get(r["mint"])
        if lab is None or lab.get("empty") or r["mc"] >= GRAD_MC:
            continue
        created = r.get("created") or (coins.get(r["mint"]) or {}).get("created")
        if not created:
            continue
        r["y"] = lab["grad"]; r["created"] = created
        rows.append(r)
    return pd.DataFrame(rows)


def _split(df):
    coins = df.groupby("mint")["created"].first().sort_values()
    n = len(coins)
    tag = {m: ("train" if i < 0.6 * n else "val" if i < 0.8 * n else "test") for i, m in enumerate(coins.index)}
    s = df["mint"].map(tag)
    return df[s == "train"], df[s == "val"], df[s == "test"]


def train():
    from datetime import timezone
    prune(force=True)
    meta = load(META, {"trained": {}})
    lines = [f"# Pre-graduation models: {datetime.now():%Y-%m-%d %H:%M} ET", ""]
    # ---- stage B: failed its test on 2026-09-29 and is off, so it is re-checked weekly, not nightly (each fit is ~4 CPU-minutes and 74 MB)
    b_at = (meta.get("trained", {}).get("b") or {}).get("at")
    b_due = (not b_at) or (time.time() - datetime.fromisoformat(b_at).timestamp() > 7 * 86400) or bool(meta.get("b_active"))
    if b_due:
        db = build_b()
        tr, va, te = _split(db)
        log(f"stage B: {len(db)} rows, {db.mint.nunique()} coins, explode base {db.y.mean():.1%} (train {tr.y.mean():.1%} / val {va.y.mean():.1%} / test {te.y.mean():.1%})")
        pb = _fit(tr, FEATURES, MODEL_B, 240)
        pv, pt = pb.predict_proba(va[FEATURES])[1].values, pb.predict_proba(te[FEATURES])[1].values
        lines += ["## Stage B: P(explode | graduates)", "",
                  f"Rows {len(db)} from {db.mint.nunique()} cached graduates (bars 6-120 min old, $5k-$60k, before first $60k close). "
                  f"Explode base rate: train {tr.y.mean():.1%}, val {va.y.mean():.1%}, test {te.y.mean():.1%}. AUC val {_auc(va.y, pv):.3f}, **test {_auc(te.y, pt):.3f}**.", "",
                  "| test predicted | rows | exploded |", "|---|---|---|"]
        for lo, hi in ((0, .05), (.05, .1), (.1, .2), (.2, .4), (.4, 1.01)):
            m = (pt >= lo) & (pt < hi)
            if m.sum():
                lines.append(f"| {lo:.0%}-{min(hi, 1):.0%} | {m.sum()} | {te.y.values[m].mean():.0%} |")
        b_ok = _auc(te.y, pt) >= MIN_AUC and int(te.y.sum()) >= 20
        lines += ["", f"**Stage B is {'ACTIVE' if b_ok else 'OFF'}**: it is used only if test AUC >= {MIN_AUC} with >= 20 positives "
                  f"(test AUC {_auc(te.y, pt):.3f}, {int(te.y.sum())} positive rows). Pre-graduation behaviour told us little about who explodes afterwards; "
                  "the plus50 model handles explosions once a coin reaches $150k."]
        meta["b_active"] = bool(b_ok)
        meta["features_b"] = FEATURES
        meta["trained"]["b"] = {"at": datetime.now(timezone.utc).isoformat(), "rows": len(db), "auc_val": _auc(va.y, pv), "auc_test": _auc(te.y, pt), "base_test": float(te.y.mean())}
    else:
        lines += ["## Stage B: skipped", "", f"Off (test AUC {(meta.get('trained', {}).get('b') or {}).get('auc_test', 0):.3f} on {b_at[:10]}); re-checked weekly to save CPU and disk."]

    # ---- stage A (when live labels suffice)
    da = build_a()
    if len(da) and "curve_progress" in da.columns:
        with_oc = da[da.curve_progress.notna()]
        if int(with_oc.y.sum()) >= MIN_A_POS and (len(with_oc) - int(with_oc.y.sum())) >= MIN_A_POS:
            da = with_oc                      # enough labeled snapshots that carry the on-chain features: use only those
    for k in FEATURES_A:
        if len(da) and k not in da.columns:
            da[k] = np.nan
    npos = int(da.y.sum()) if len(da) else 0
    lines += ["", "## Stage A: P(graduate)", ""]
    if npos >= MIN_A_POS and (len(da) - npos) >= MIN_A_POS:
        tr, va, te = _split(da)
        pa = _fit(tr, FEATURES_A, MODEL_A, 240)
        pv, pt = pa.predict_proba(va[FEATURES_A])[1].values, pa.predict_proba(te[FEATURES_A])[1].values
        lines += [f"Rows {len(da)} live snapshots from {da.mint.nunique()} coins; graduate base {da.y.mean():.1%}. AUC val {_auc(va.y, pv):.3f}, **test {_auc(te.y, pt):.3f}**."]
        a_ok = _auc(te.y, pt) >= MIN_AUC
        lines += ["", f"**Stage A is {'ACTIVE' if a_ok else 'OFF'}** (needs test AUC >= {MIN_AUC})."]
        meta["a_active"] = bool(a_ok)
        meta["features_a"] = FEATURES_A
        meta["trained"]["a"] = {"at": datetime.now(timezone.utc).isoformat(), "rows": len(da), "pos": npos, "auc_val": _auc(va.y, pv), "auc_test": _auc(te.y, pt), "base_test": float(te.y.mean())}
    else:
        lines += [f"Not trained yet: {npos} graduate positives among {len(da)} labeled live snapshots (needs {MIN_A_POS}+ of each class). "
                  f"Labels arrive 4h+ after each coin's launch. Coins labeled so far: {len(load(LABELS, {}))}."]
    lines += ["", "Not advice. Pump.fun outcomes are dominated by insiders and bundlers this model cannot see."]
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w") as f:
        f.write("\n".join(lines) + "\n")
    save(META, meta)
    print("\n".join(lines))


# ---------------------------------------------------------------- candle archive (git backup)
# data/ is not in git, and the candle cache is the only history beyond pump.fun's ~3.5-day window. Once a
# calendar day is >= ARCHIVE_AFTER_D old, its candle files are packed into archive/candles/<day>.jsonl.gz
# (immutable, one commit per day, ~1 MB/day compressed). Raw files are deleted RAW_KEEP_D days after their
# day was archived AND verified by reading it back. `restore_candles()` puts them back for retraining.
ARCHIVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "archive", "candles")
CANDLE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "pump", "candles")
ARCHIVE_AFTER_D, RAW_KEEP_D = 3, 14


def archive_candles(now=None):
    now = now or time.time()
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    if not os.path.isdir(CANDLE_DIR):
        return {"archived_days": 0, "deleted": 0}
    by_day = {}
    for fn in os.listdir(CANDLE_DIR):
        p = os.path.join(CANDLE_DIR, fn)
        if fn.endswith(".json") and os.path.isfile(p):
            m = os.path.getmtime(p)
            if now - m >= ARCHIVE_AFTER_D * 86400:
                by_day.setdefault(time.strftime("%Y-%m-%d", time.localtime(m)), []).append((fn, p, m))
    new_days = deleted = 0
    for day, files in sorted(by_day.items()):
        out = os.path.join(ARCHIVE_DIR, day + ".jsonl.gz")
        if not os.path.exists(out):
            tmp = out + ".tmp"
            with gzip.open(tmp, "wt", compresslevel=9) as g:
                for fn, p, _ in files:
                    try:
                        g.write(json.dumps({"mint": fn[:-5], "bars": json.load(open(p))}, separators=(",", ":")) + "\n")
                    except (OSError, ValueError):
                        continue
            os.replace(tmp, out)
            new_days += 1
        # delete raw files only when they are old enough AND present in the archive
        if now - max(m for _, _, m in files) >= RAW_KEEP_D * 86400:
            have = {r["mint"] for r in _read_archive(out)}
            for fn, p, _ in files:
                if fn[:-5] in have:
                    os.remove(p); deleted += 1
    return {"archived_days": new_days, "deleted": deleted}


def _read_archive(path):
    try:
        with gzip.open(path, "rt") as g:
            for line in g:
                try:
                    yield json.loads(line)
                except ValueError:
                    pass
    except (EOFError, OSError, zlib.error):
        return


def restore_candles(day=None):
    """Put archived candle files back into the cache (all days, or one YYYY-MM-DD). Never overwrites."""
    os.makedirs(CANDLE_DIR, exist_ok=True)
    n = 0
    for fn in sorted(os.listdir(ARCHIVE_DIR)):
        if day and not fn.startswith(day):
            continue
        for r in _read_archive(os.path.join(ARCHIVE_DIR, fn)):
            p = os.path.join(CANDLE_DIR, r["mint"] + ".json")
            if not os.path.exists(p):
                json.dump(r["bars"], open(p, "w")); n += 1
    return n


def backup():
    """Pack finished days of candles, then commit + push archive/ (only that folder) if it changed."""
    import subprocess
    r = archive_candles()
    here = os.path.dirname(os.path.abspath(__file__))
    if os.path.exists(LABELS):                                   # tiny, irreplaceable once the candles expire
        shutil.copyfile(LABELS, os.path.join(here, "archive", "labels.json"))
    git = ["/usr/local/bin/git", "-C", here]
    subprocess.run(git + ["add", "archive"], capture_output=True)
    if subprocess.run(git + ["diff", "--cached", "--quiet", "--", "archive"]).returncode:
        msg = "candle archive: %d new day(s)" % r["archived_days"]
        subprocess.run(git + ["commit", "-q", "-m", msg, "--", "archive"], capture_output=True)
        p = subprocess.run(git + ["push", "-q"], capture_output=True, text=True)
        r["pushed"] = p.returncode == 0
        if p.returncode:
            r["push_error"] = p.stderr.strip()[:200]
    return r


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "tick":
        tick()
    elif cmd == "train":
        train()
    elif cmd == "prune":
        prune()
    elif cmd == "backup":
        print(backup())
    elif cmd == "restore":
        print("restored", restore_candles(sys.argv[2] if len(sys.argv) > 2 else None), "candle files")
    elif cmd == "board":
        b = load(BOARD, None)
        print(json.dumps(b, indent=1)[:4000] if b else "no board yet")
    else:
        print(__doc__)



