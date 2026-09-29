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

import ml, pump

HERE = os.path.dirname(os.path.abspath(__file__))
PRE = os.path.join(HERE, "data", "pre")
COINS = os.path.join(PRE, "coins.json")            # tracked coins: static meta + snapshot bookkeeping
SNAPS = os.path.join(PRE, "snapshots.jsonl")       # one row per (coin, tick): features + ids
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


def tick():
    import faulthandler
    faulthandler.dump_traceback_later(270, exit=True)
    t0 = time.time(); now_ms = t0 * 1000
    coins = load(COINS, {})
    listing = list_newest()
    found = 0
    for c in listing:
        age = (now_ms - c["created_timestamp"]) / 60000
        if c["mint"] in coins or (c.get("usd_market_cap") or 0) < MIN_TRACTION_USD or age > 45:
            continue
        coins[c["mint"]] = {"created": c["created_timestamp"], "symbol": c.get("symbol"), "name": c.get("name"), "first_seen": now_ms,
                            "meta": {k: c.get(k) for k in ("creator", "description", "name", "symbol", "twitter", "website", "telegram")},
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
    rows = []
    for m, f, mc in res:
        rows.append({"mint": m, "t": int(now_ms), "mc": mc, **f})
    if rows:
        with open(SNAPS, "a") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
    save(COINS, coins)
    board = score_board(rows, coins)
    for r in board[:5]:
        if r["score"] is not None and "picked" not in coins[r["mint"]]:
            coins[r["mint"]]["picked"] = {"t": int(now_ms), "mc": r["mc"], "score": r["score"]}
    save(COINS, coins)
    resolved = resolve(coins, now_ms)
    log(f"listing {len(listing)} · new tracked {found} · tracking {len(coins)} · snapshots {len(rows)} · "
        f"board {len(board)} · resolved {resolved} · {time.time() - t0:.0f}s")


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
                    "creator_prior_grads": int(r.creator_prior_grads), "socials": int(r.has_twitter + r.has_website + r.has_telegram)})
    save(BOARD, {"updated": time.time(), "a_ready": pa is not None, "b_ready": pb is not None, "rows": out,
                 "labeled": len(load(LABELS, {}))})
    return out


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
    if not os.path.exists(SNAPS):
        return pd.DataFrame()
    rows = []
    with open(SNAPS) as f:
        for line in f:
            r = json.loads(line)
            lab = labels.get(r["mint"])
            if lab is None or lab.get("empty") or r["mint"] not in coins or r["mc"] >= GRAD_MC:
                continue
            r["y"] = lab["grad"]; r["created"] = coins[r["mint"]]["created"]
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
    meta = load(META, {"trained": {}})
    lines = [f"# Pre-graduation models: {datetime.now():%Y-%m-%d %H:%M} ET", ""]
    # ---- stage B (now)
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
    # ---- stage A (when live labels suffice)
    da = build_a()
    npos = int(da.y.sum()) if len(da) else 0
    lines += ["", "## Stage A: P(graduate)", ""]
    if npos >= MIN_A_POS and (len(da) - npos) >= MIN_A_POS:
        tr, va, te = _split(da)
        pa = _fit(tr, FEATURES, MODEL_A, 240)
        pv, pt = pa.predict_proba(va[FEATURES])[1].values, pa.predict_proba(te[FEATURES])[1].values
        lines += [f"Rows {len(da)} live snapshots from {da.mint.nunique()} coins; graduate base {da.y.mean():.1%}. AUC val {_auc(va.y, pv):.3f}, **test {_auc(te.y, pt):.3f}**."]
        a_ok = _auc(te.y, pt) >= MIN_AUC
        lines += ["", f"**Stage A is {'ACTIVE' if a_ok else 'OFF'}** (needs test AUC >= {MIN_AUC})."]
        meta["a_active"] = bool(a_ok)
        meta["features_a"] = FEATURES
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


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "tick":
        tick()
    elif cmd == "train":
        train()
    elif cmd == "board":
        b = load(BOARD, None)
        print(json.dumps(b, indent=1)[:4000] if b else "no board yet")
    else:
        print(__doc__)
