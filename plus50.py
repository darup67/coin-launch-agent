#!/usr/bin/env python3
"""+50%-in-1-hour filter: judge each coin once, at the moment it is added, and show only
the ones with a good chance.

The user's rule (2026-09-28): show a coin only if it has a good chance of being worth
50% more one hour after it was added. "Added" is the watcher's alert rule: the first
5-minute bar that closes at or above min_mc_usd ($150k) within 4h of launch. "Good
chance" means coins with a similar score hit +50% at least 40% of the time on held-out
coins.

Why one judgment per coin: of 2,252 coins that reached $150k within 4h (Sep 2026 cache),
14.7% were up 50%+ an hour later and the median coin was at 0.01x. The old model scored
every coin every 5 minutes; this one asks once, at the price a buyer would actually get.

Evaluation rules, fixed before seeing results:
  - One row per coin. Coins split by launch time: oldest 60% train, next 20% validate,
    newest 20% test.
  - Gate = the lowest probability whose VALIDATION hit rate is >= MIN_HIT with >= MIN_PICKS
    picks. Picks go live only if the TEST hit rate at that gate is also >= MIN_HIT.
    Otherwise nothing is shown: no gate beats showing junk.

  plus50.py train [--no-fetch] [--push]   nightly: dataset from the pump.fun cache, fit, gate, report
  plus50.py score [--print]               every 5 min: judge coins added since the last run
"""
import json, os, shutil, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import ml, pump

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
MODEL = os.path.join(HERE, "models", "plus50")
META = os.path.join(HERE, "models", "plus50_meta.json")
REPORT = os.path.join(HERE, "results", "plus50_report.md")
STATE = os.path.join(DATA, "plus50_state.json")
OUT = os.path.join(DATA, "plus50.json")

MULT = 1.5          # +50%
HORIZON = 12        # 5-minute bars = 60 minutes
MIN_HIT = 0.40      # "good chance": the user's pick 2026-09-28
MIN_PICKS = 10
MAX_AGE_MIN = 240
ROLL_N, ROLL_MIN = 20, 10   # kill switch: last 20 resolved candidates, judged once 10 have resolved
FIT_SECONDS = int(os.environ.get("PLUS50_TIME", 300))


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
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def entry_index(g, created_ms, entry_mc):
    """First bar whose close is >= entry_mc within MAX_AGE_MIN of launch, or None."""
    for k in range(len(g["close"])):
        t_close = g["t0"] + (k + 1) * ml.BAR_MS
        if (t_close - created_ms) / 60000 > MAX_AGE_MIN:
            return None
        if g["close"][k] >= entry_mc:
            return k
    return None


def end_mult(g, k):
    """Close HORIZON bars after the entry close. No bar = no trades, so the last close stands."""
    return float(g["close"][min(k + HORIZON, len(g["close"]) - 1)] / g["close"][k])


# ---------------------------------------------------------------- training
def build(pairs, until_ms, entry_mc):
    rows = []
    for c, bars in pairs:
        if not bars:
            continue
        created = c["created_timestamp"]
        g = ml.grid(bars, created)
        if g["t0"] + ml.BAR_MS < created - 3600_000:
            continue                                   # history doesn't reach launch
        k = entry_index(g, created, entry_mc)
        if k is None:
            continue
        t_entry = g["t0"] + (k + 1) * ml.BAR_MS
        if t_entry + HORIZON * ml.BAR_MS > until_ms:
            continue                                   # the hour hasn't happened yet
        em = end_mult(g, k)
        rows.append({"mint": c["mint"], "symbol": c.get("symbol"), "created": created, "t_entry": t_entry,
                     **ml.features(g, k, created), "end_mult": em, "y": int(em >= MULT)})
    return pd.DataFrame(rows)


def auc(y, s):
    y, s = np.asarray(y), np.asarray(s)
    pos, neg = s[y == 1], s[y == 0]
    if not len(pos) or not len(neg):
        return float("nan")
    r = pd.Series(np.concatenate([pos, neg])).rank().values
    return (r[: len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def train():
    import train as nightly                             # reuse its pump.fun backfill + cache
    from autogluon.tabular import TabularPredictor
    cfg = load(os.path.join(HERE, "config.json"), {})
    entry_mc = float(cfg.get("min_mc_usd", 150_000))
    pairs, until = nightly.backfill("--no-fetch" not in sys.argv)
    df = build(pairs, until, entry_mc).sort_values("created").reset_index(drop=True)
    n = len(df)
    if n < 300:
        log(f"only {n} coins reached {entry_mc:,.0f}; not training")
        return 1
    df["split"] = np.where(df.index < 0.6 * n, "train", np.where(df.index < 0.8 * n, "val", "test"))
    tr, va, te = (df[df.split == s] for s in ("train", "val", "test"))
    feats = ml.FEATURES
    log(f"{n} coins added at >= ${entry_mc / 1000:.0f}k; +50% in 1h: train {tr.y.mean():.1%} / "
        f"val {va.y.mean():.1%} / test {te.y.mean():.1%}")

    tmp = MODEL + ".new"
    shutil.rmtree(tmp, ignore_errors=True)
    p = TabularPredictor(label="y", problem_type="binary", eval_metric="log_loss", path=tmp, verbosity=1)
    p.fit(tr[feats + ["y"]], presets="best_quality", time_limit=FIT_SECONDS, num_bag_folds=5,
          num_stack_levels=1, dynamic_stacking=False, excluded_model_types=["FASTAI"])
    if not p.model_names():
        shutil.rmtree(tmp, ignore_errors=True)
        log("no models trained; keeping the previous model")
        return 1

    pv = p.predict_proba(va[feats])[1].values
    pt = p.predict_proba(te[feats])[1].values
    gate = None
    for thr in np.round(np.arange(0.05, 0.96, 0.01), 2):
        m = pv >= thr
        if m.sum() >= MIN_PICKS and va.y.values[m].mean() >= MIN_HIT:
            gate = float(thr)
            break
    gv = gt = None
    if gate is not None:
        mv, mt = pv >= gate, pt >= gate
        gv = {"n": int(mv.sum()), "hit": float(va.y.values[mv].mean())}
        gt = {"n": int(mt.sum()), "hit": float(te.y.values[mt].mean()) if mt.sum() else None,
              "median_end_mult": float(np.median(te.end_mult.values[mt])) if mt.sum() else None,
              "mean_end_mult": float(np.mean(te.end_mult.values[mt])) if mt.sum() else None}
    live = bool(gate is not None and gt["n"] >= 5 and (gt["hit"] or 0) >= MIN_HIT)

    L = [f"# +50% in 1 hour: {datetime.now():%Y-%m-%d %H:%M} ET", "",
         f"Question: when a coin is added (first 5-minute close at or above ${entry_mc / 1000:.0f}k within "
         f"{MAX_AGE_MIN // 60}h of launch), is it worth at least {MULT}x that an hour later? One row per coin.", "",
         f"Coins: {n} (train {len(tr)}, val {len(va)}, test {len(te)}, split by launch time).", "",
         f"Base rate (+50% after 1h): train {tr.y.mean():.1%}, val {va.y.mean():.1%}, test {te.y.mean():.1%}. "
         f"Median coin 1h later: {df.end_mult.median():.2f}x; mean {df.end_mult.mean():.2f}x.", "",
         f"AUC: validation {auc(va.y, pv):.3f}, test {auc(te.y, pt):.3f}.", "",
         "## Calibration on test", "", "| predicted | coins | hit +50% | median 1h multiple |", "|---|---|---|---|"]
    for lo, hi in ((0, .1), (.1, .2), (.2, .3), (.3, .4), (.4, .6), (.6, 1.01)):
        m = (pt >= lo) & (pt < hi)
        if m.sum():
            L.append(f"| {lo:.0%}–{min(hi, 1):.0%} | {m.sum()} | {te.y.values[m].mean():.0%} | "
                     f"{np.median(te.end_mult.values[m]):.2f}x |")
    L += ["", "## Gate", ""]
    if gate is None:
        L.append(f"No probability reached a {MIN_HIT:.0%} hit rate with at least {MIN_PICKS} picks on validation. "
                 "**Nothing is shown.**")
    else:
        L.append(f"Validation: P ≥ {gate:.2f} gave {gv['n']} picks, {gv['hit']:.0%} hit +50%.")
        L.append(f"Test (scored once): {gt['n']} picks" + (
            f", {gt['hit']:.0%} hit +50%; median 1h multiple {gt['median_end_mult']:.2f}x, "
            f"mean {gt['mean_end_mult']:.2f}x." if gt["n"] else ", none."))
        L.append("")
        L.append("**Picks are LIVE.**" if live else
                 f"**Picks are OFF:** the test hit rate didn't also reach {MIN_HIT:.0%} (with at least 5 picks).")
    L += ["", "Not advice. Multiples are 5-minute closes, before fees, slippage and the price impact of buying "
          "into a thin pool."]
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w") as f:
        f.write("\n".join(L) + "\n")

    shutil.rmtree(MODEL, ignore_errors=True)
    os.replace(tmp, MODEL)
    save(META, {"trained": datetime.now(timezone.utc).isoformat(), "entry_mc": entry_mc, "mult": MULT,
                "horizon_min": HORIZON * 5, "min_hit": MIN_HIT, "features": feats, "gate": gate, "live": live,
                "gate_val": gv, "gate_test": gt, "base_rate_test": float(te.y.mean()), "coins": n,
                "auc_val": auc(va.y, pv), "auc_test": auc(te.y, pt)})
    print("\n".join(L))
    if "--push" in sys.argv:
        import subprocess
        subprocess.run(["git", "-C", HERE, "add", REPORT, META])
        subprocess.run(["git", "-C", HERE, "commit", "-q", "-m", f"plus50 retrain: {n} coins, live={live}"])
        subprocess.run(["git", "-C", HERE, "pull", "-q", "--rebase"])
        subprocess.run(["git", "-C", HERE, "push", "-q"])
    return 0


# ---------------------------------------------------------------- live scoring
def score():
    import faulthandler
    faulthandler.dump_traceback_later(240, exit=True)   # a hung run would block every later one
    now_ms = time.time() * 1000
    meta = load(META, None)
    tracked = load(os.path.join(DATA, "tracked.json"), {})
    st = load(STATE, {"judged": {}})
    judged = st["judged"]
    if not meta or not os.path.isdir(MODEL):
        log("no plus50 model yet (run plus50.py train)")
        return 0
    if now_ms / 1000 - tracked.get("updated", 0) > 300:
        log("watcher data is stale; is com.dhruv.coinlaunch running?")
        return 0
    entry_mc = meta["entry_mc"]
    by_token = {t["token"]: t for t in tracked["tokens"]}
    cands = [t for t in tracked["tokens"] if t["net"] == "solana" and t.get("launch")
             and t["token"] not in judged and (t.get("mc") or 0) >= 0.6 * entry_mc
             and now_ms / 1000 - t["launch"] <= MAX_AGE_MIN * 60]

    def bars_for(token, created):
        try:
            bars = pump.candles_5m(token, cache=False)
        except Exception:
            return None
        bars = [b for b in bars if b[0] + ml.BAR_MS <= now_ms - 20_000]      # closed bars only
        return ml.grid(bars, created) if bars else None                     # None: not a pump.fun coin

    def one(t):
        created = int(t["launch"] * 1000)
        g = bars_for(t["token"], created)
        if g is None:
            return None
        k = entry_index(g, created, entry_mc)
        if k is None:
            return None
        late_min = (now_ms - (g["t0"] + (k + 1) * ml.BAR_MS)) / 60000
        return t, g, k, created, late_min

    with ThreadPoolExecutor(8) as ex:
        new = [x for x in ex.map(one, cands) if x]
    if new:
        from autogluon.tabular import TabularPredictor
        pred = TabularPredictor.load(MODEL, require_py_version_match=False)
        X = pd.DataFrame([ml.features(g, k, created) for _, g, k, created, _ in new])
        ps = pred.predict_proba(X[meta["features"]])[1].values
        paused = rolling(judged)["paused"]
        for (t, g, k, created, late), p in zip(new, ps):
            # candidate = passes the model gate and the watcher's filters; pick = candidate while the live
            # record holds. Candidates keep being scored and resolved while paused, so picks can resume.
            cand = bool(meta["gate"] is not None and p >= meta["gate"] and not t.get("why") and late <= 15)
            pick = bool(cand and meta["live"] and not paused)
            judged[t["token"]] = {"symbol": t["symbol"], "p": round(float(p), 4), "pick": pick, "cand": cand,
                                  "entry_mc": float(g["close"][k]), "t_entry": g["t0"] + (k + 1) * ml.BAR_MS,
                                  "late_min": round(late, 1), "why": t.get("why") or [], "judged_at": now_ms}
            log(f"{'PICK' if pick else 'skip'} {t['symbol']} P={p:.0%} entry ${g['close'][k] / 1000:,.0f}k"
                + (f" ({late:.0f}m late)" if late > 10 else "") + (f" ← {', '.join(t['why'])}" if t.get('why') else ""))

    # One hour after entry, record what actually happened (live hit rate, the honest scoreboard).
    due = [(tok, j) for tok, j in judged.items() if "end_mult" not in j and now_ms >= j["t_entry"] + (HORIZON * 5 + 5) * 60000]
    for tok, j in due:
        t = by_token.get(tok)
        created = int(t["launch"] * 1000) if t and t.get("launch") else None
        g = bars_for(tok, created) if created else None
        if g is None:
            if now_ms - j["t_entry"] > 3 * 3600_000:
                j["end_mult"] = None                  # dropped by the watcher (dead coin); count as unknown
            continue
        k = int(round((j["t_entry"] - g["t0"]) / ml.BAR_MS)) - 1
        if 0 <= k < len(g["close"]):
            j["end_mult"] = end_mult(g, k)
    st["judged"] = {k: v for k, v in judged.items() if now_ms - v["judged_at"] < 7 * 86400_000}
    save(STATE, st)

    rows = sorted(st["judged"].items(), key=lambda kv: -kv[1]["judged_at"])
    live_picks = [j for _, j in rows if j["pick"] and j.get("end_mult") is not None]
    roll = rolling(st["judged"])
    save(OUT, {"updated": time.time(), "live": meta["live"], "gate": meta["gate"], "entry_mc": entry_mc,
               "paused": roll["paused"], "rolling": roll,
               "test_hit": (meta.get("gate_test") or {}).get("hit"), "base_rate": meta.get("base_rate_test"),
               "live_record": {"picks": len(live_picks), "hits": sum(j["end_mult"] >= MULT for j in live_picks)},
               "rows": [{"token": tok, **j, "mc_now": (by_token.get(tok) or {}).get("mc")} for tok, j in rows[:200]]})
    if new:
        log(f"judged {len(new)} new coin(s), {sum(1 for x in new if judged[x[0]['token']]['pick'])} pick(s)")
    email_picks([(tok, j) for tok, j in rows if j["pick"] and not j.get("emailed")], st, meta)
    if "--print" in sys.argv:
        for tok, j in rows[:30]:
            res = f"{j['end_mult']:.2f}x" if j.get("end_mult") is not None else "pending"
            print(f"  {'PICK' if j['pick'] else '    '} {j['p']:>4.0%}  {j['symbol'][:14]:<14} entry ${j['entry_mc'] / 1000:>6,.0f}k  1h: {res}")
    return 0


def rolling(judged):
    """The user's 40% rule, enforced on live results: of the last ROLL_N candidates whose hour has
    resolved, how many hit +50%? Paused while that is under MIN_HIT (once ROLL_MIN have resolved)."""
    res = sorted((j for j in judged.values() if j.get("cand") and j.get("end_mult") is not None),
                 key=lambda j: j["t_entry"])[-ROLL_N:]
    hits = sum(j["end_mult"] >= MULT for j in res)
    return {"n": len(res), "hits": hits, "paused": len(res) >= ROLL_MIN and hits / len(res) < MIN_HIT}


def email_picks(picks, st, meta):
    cfg = load(os.path.join(HERE, "config.json"), {})
    if not picks or not cfg.get("plus50_email", False):
        return
    import agent
    gt = meta.get("gate_test") or {}
    lines = [f"{j['symbol']}  P(+50% in 1h) {j['p']:.0%}  added at ${j['entry_mc'] / 1000:,.0f}k  "
             f"{datetime.fromtimestamp(j['t_entry'] / 1000):%I:%M %p}\n    contract {tok}\n"
             f"    https://dexscreener.com/solana/{tok}\n" for tok, j in picks]
    body = (f"Coins with a good chance of +50% one hour after being added. On held-out coins, picks at this "
            f"score hit +50% {gt.get('hit') or 0:.0%} of the time (base rate {meta.get('base_rate_test') or 0:.0%}); "
            f"median 1h multiple {gt.get('median_end_mult') or 0:.2f}x, before fees and slippage.\n\n" + "\n".join(lines) +
            "\nNot advice. results/plus50_report.md has the full numbers.")
    if agent.send_email(f"🎯 +50% candidates: {', '.join(j['symbol'] for _, j in picks)}", body):
        for tok, _ in picks:
            st["judged"][tok]["emailed"] = True
        save(STATE, st)
        log(f"emailed picks: {[j['symbol'] for _, j in picks]}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    sys.exit(train() if cmd == "train" else score() if cmd == "score" else (print(__doc__) or 2))
