#!/usr/bin/env python3
"""Every 5 minutes: score each live graduated pump.fun coin under 4h old and worth at least
$75k for P(still worth 2x its current cap 60 minutes later), using the model train.py fitted.

Candidates come from the watcher (data/tracked.json). Features come from the same
pump.fun 5-minute candles and the same ml.py code used in training. Output:
data/scores.json (shown on the board) and score.log. If the trained model has a
gate, coins at or above it that also pass the watcher's filters are emailed as
picks, each coin at most once every 2 hours.

  score.py           one tick (launchd runs it at :01, :06, … so the 5m bar has closed)
  score.py --print   one tick, printing the ranking
"""
import json, os, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import numpy as np
import pandas as pd

import ml, pump

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
MODEL = os.path.join(HERE, "models", "ag_full")


def log(msg):
    print(f"{datetime.now():%m-%d %H:%M:%S} {msg}", flush=True)


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def main():
    now_ms = time.time() * 1000
    meta = load(os.path.join(HERE, "models", "meta.json"), None)
    tracked = load(os.path.join(DATA, "tracked.json"), {})
    if not meta or not os.path.isdir(MODEL):
        log("no trained model yet (run train.py)")
        return
    if now_ms / 1000 - tracked.get("updated", 0) > 300:
        log("watcher data is stale; is com.dhruv.coinlaunch running?")
        return
    cands = [t for t in tracked["tokens"] if t["net"] == "solana" and t.get("launch")
             and (t.get("mc") or 0) >= 0.6 * ml.MIN_MC and now_ms / 1000 - t["launch"] <= ml.MAX_AGE_MIN * 60]

    def one(t):
        created = int(t["launch"] * 1000)
        try:
            bars = pump.candles_5m(t["token"], cache=False)
        except Exception:
            return None
        bars = [b for b in bars if b[0] + ml.BAR_MS <= now_ms - 20_000]   # closed bars only
        if not bars:
            return None                                                    # not a pump.fun coin
        g = ml.grid(bars, created)
        ks = ml.decision_indices(g, created, now_ms, need_label=False)
        k = len(g["close"]) - 1
        if not ks or ks[-1] != k:
            return None
        return t, g, k, created

    with ThreadPoolExecutor(12) as ex:
        live = [x for x in ex.map(one, cands) if x]
    rows = []
    if live:
        chr_ = ml.chronos_features([ml.chronos_context(g, k) for _, g, k, _ in live])
        for (t, g, k, created), c in zip(live, chr_):
            rows.append({"token": t["token"], "symbol": t["symbol"], "why": t.get("why") or [],
                         "liq": t.get("liq"), **ml.features(g, k, created), **c})
        from autogluon.tabular import TabularPredictor
        pred = TabularPredictor.load(MODEL, require_py_version_match=False)
        df = pd.DataFrame(rows)
        df["p"] = pred.predict_proba(df[meta["features"]])[1].values
        rows = df.sort_values("p", ascending=False).to_dict("records")

    gate = meta.get("gate")
    out = [{"token": r["token"], "symbol": r["symbol"], "p": round(float(r["p"]), 4),
            "mc": float(np.exp(r["log_mc"])), "age_min": round(r["age_min"], 1),
            "ret_15m": round(float(r["ret_15m"]), 3), "chr_q90_max": round(float(r["chr_q90_max"]), 3),
            "why": r["why"], "pick": bool(gate is not None and r["p"] >= gate and not r["why"])} for r in rows]
    with open(os.path.join(DATA, "scores.json"), "w") as f:
        json.dump({"updated": time.time(), "gate": gate, "base_rate": meta.get("base_rate_test"),
                   "candidates": len(cands), "rows": out}, f)
    log(f"scored {len(out)} of {len(cands)} candidates; top: " +
        ", ".join(f"{r['symbol']} {r['p']:.0%}" for r in out[:5]))
    if "--print" in sys.argv:
        for r in out:
            print(f"  {r['p']:>5.0%}  {r['symbol'][:14]:<14} ${r['mc'] / 1000:>7,.0f}k  {r['age_min']:>5.0f}m old  "
                  f"15m {r['ret_15m']:+.2f}  chronos q90 {r['chr_q90_max']:+.2f}" + ("  PICK" if r["pick"] else "")
                  + (f"  ← {', '.join(r['why'])}" if r["why"] else ""))
    picks(out, meta)


def picks(out, meta):
    cfg = load(os.path.join(HERE, "config.json"), {})
    if not cfg.get("ml_picks_email", True):
        return
    st_path = os.path.join(DATA, "score_state.json")
    st = load(st_path, {})
    now = time.time()
    g = meta.get("gate_test") or {}
    if (g.get("median_end_mult") or 0) < 1.0:
        return   # emails only once held-out picks at the gate kept their value at the median (report.md)
    new = [r for r in out if r["pick"] and now - st.get(r["token"], 0) > 7200]
    if not new:
        return
    import agent
    g = meta["gate_test"] or {}
    lines = [f"{r['symbol']}  P(still 2x in 1h) {r['p']:.0%}  cap ${r['mc'] / 1000:,.0f}k  {r['age_min']:.0f}m old\n"
             f"    contract {r['token']}\n    https://dexscreener.com/solana/{r['token']}\n" for r in new]
    body = (f"Model picks at {datetime.now():%I:%M %p}. Gate P ≥ {meta['gate']:.2f}, set on validation. "
            f"On held-out coins, picks at this gate were still 2x an hour later {g.get('hit', 0):.0%} of the time, "
            f"against a {meta['base_rate_test']:.0%} base rate. The median 60-minute end multiple was "
            f"{g.get('median_end_mult') or 0:.2f}x, before fees and slippage.\n\n" + "\n".join(lines) +
            "\nNot advice. Full numbers are in results/report.md in the repo.")
    if agent.send_email(f"Coin model picks: {', '.join(r['symbol'] for r in new)}", body):
        for r in new:
            st[r["token"]] = now
        with open(st_path, "w") as f:
            json.dump({k: v for k, v in st.items() if now - v < 86400}, f)
        log(f"emailed picks: {[r['symbol'] for r in new]}")


if __name__ == "__main__":
    main()
