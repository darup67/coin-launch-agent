#!/usr/bin/env python3
"""Train the explosion model: Chronos-2 forecast features + AutoGluon Tabular.

  train.py              backfill pump.fun history, build the dataset, train, evaluate, write the report
  train.py --no-fetch   reuse cached data (data/pump/)
  train.py --push       also commit results/ and models/meta.json and push (nightly job)

The evaluation rules are fixed before any data is seen:
  - Coins are split by launch time: the oldest 60% train, the next 20% validate,
    the newest 20% test. A coin's rows never appear in two splits.
  - The alert gate is picked on validation only: the lowest probability whose
    validation precision is at least 2x the validation base rate, with at least
    20 picks. If no probability qualifies, there is no gate and the model only
    ranks on the board.
  - Test is scored once and reported as it comes out, with the baselines
    (15-minute momentum, Chronos-2 alone, AutoGluon without Chronos) run alongside.
"""
import json, os, subprocess, sys, time, zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import ml, pump

HERE = os.path.dirname(os.path.abspath(__file__))
MODELS = os.path.join(HERE, "models")
RESULTS = os.path.join(HERE, "results")
LABEL = "hold2x"      # see ml.py for why not hit2x
LOOKBACK_H = 80       # the 5m endpoint returns 1000 bars ≈ 83h, so older launches lose their first hours


def log(msg):
    print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)


def backfill(fetch=True):
    coins = pump.graduated() if fetch else json.load(open(os.path.join(pump.CACHE, "coins.json")))
    now = time.time() * 1000
    todo = [c for c in coins.values() if (c.get("ath_market_cap") or 0) >= ml.MIN_MC
            and now - c["created_timestamp"] < LOOKBACK_H * 3.6e6 or
            os.path.exists(os.path.join(pump.CACHE, "candles", c["mint"] + ".json"))]
    log(f"{len(coins)} graduated coins cached, {len(todo)} reached ${ml.MIN_MC:,.0f}")

    def one(c):
        done = now - c["created_timestamp"] > (ml.MAX_AGE_MIN + 90) * 60000   # window + label hour closed
        bars = pump.candles_5m(c["mint"], cache=True) if fetch or done else []
        if fetch and done and bars and not os.path.exists(os.path.join(pump.CACHE, "candles", c["mint"] + ".json")):
            pump.save_candles(c["mint"], bars)
        return c, bars

    with ThreadPoolExecutor(12) as ex:
        return list(ex.map(one, todo)), now


def build(pairs, until_ms):
    rows, ctx = [], []
    for c, bars in pairs:
        if not bars:
            continue
        g = ml.grid(bars, c["created_timestamp"])
        if g["t0"] + ml.BAR_MS < c["created_timestamp"] - 3600_000:
            continue   # candle history doesn't reach launch
        for k in ml.decision_indices(g, c["created_timestamp"], until_ms, need_label=True):
            rows.append({"mint": c["mint"], "symbol": c.get("symbol"), "created": c["created_timestamp"],
                         "t_decision": g["t0"] + (k + 1) * ml.BAR_MS,
                         **ml.features(g, k, c["created_timestamp"]), **ml.label(g, k)})
            ctx.append(ml.chronos_context(g, k))
    log(f"{len(rows)} decision points from {len({r['mint'] for r in rows})} coins; Chronos-2 on each …")
    t = time.time()
    for r, f in zip(rows, ml.chronos_features(ctx)):
        r.update(f)
    log(f"Chronos-2 done in {time.time() - t:.0f}s")
    return pd.DataFrame(rows)


def auc(y, s):
    y, s = np.asarray(y), np.asarray(s)
    pos, neg = s[y == 1], s[y == 0]
    if not len(pos) or not len(neg):
        return float("nan")
    r = pd.Series(np.concatenate([pos, neg])).rank().values
    return (r[: len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def per_tick(df, score, k):
    """Of each 5-minute tick's top-k coins by score, how many exploded, vs. all coins that tick."""
    hits = n = 0
    base_hits = base_n = 0
    for _, g in df.assign(_s=score).groupby("t_decision"):
        if len(g) < 2:
            continue
        top = g.nlargest(k, "_s")
        hits += top[LABEL].sum(); n += len(top)
        base_hits += g[LABEL].sum(); base_n += len(g)
    return hits / max(n, 1), base_hits / max(base_n, 1), n


def top1_end(df, score):
    """Median and mean 60-minute end multiple of each tick's top-ranked coin, vs. every coin."""
    tops = [g.nlargest(1, "_s") for _, g in df.assign(_s=score).groupby("t_decision") if len(g) >= 2]
    e = pd.concat(tops)["fut_end_mult"] if tops else pd.Series(dtype=float)
    return (e.median(), e.mean()) if len(e) else (float("nan"), float("nan"))


def fit(train, features, name, time_limit):
    """Fit into models/<name>.new and swap it in only if it produced models.

    Until 2026-09-28 this fitted straight into models/<name> after deleting it, so
    the failed nights of Sep 27 and 28 (every model out of time or failing to
    import FastAI, on a Mac loaded by a Backblaze upload) left no working model
    and score.py had nothing to load. A failed fit now keeps yesterday's model.
    FastAI is excluded: it raised ImportError on every run and only burned budget."""
    from autogluon.tabular import TabularPredictor
    import shutil
    path = os.path.join(MODELS, name)
    tmp = path + ".new"
    shutil.rmtree(tmp, ignore_errors=True)
    p = TabularPredictor(label=LABEL, problem_type="binary", eval_metric="log_loss",
                         path=tmp, groups="mint", verbosity=1)
    # AutoGluon makes one bag fold per distinct group, ignoring num_bag_folds. Grouping by raw
    # mint meant ~1,650 folds of ~0.4s each, so every model timed out (Sep 27-28 2026). Hash
    # each coin into 5 stable buckets: still no coin split across folds, and 5 real folds.
    data = train[features + [LABEL]].assign(mint=train["mint"].map(lambda m: zlib.crc32(m.encode()) % 5))
    p.fit(data, presets="best_quality", time_limit=time_limit,
          dynamic_stacking=False, num_bag_folds=5, num_stack_levels=1,
          excluded_model_types=["FASTAI"])
    if not p.model_names():
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(f"{name}: no models trained; keeping the previous model")
    shutil.rmtree(path, ignore_errors=True)
    os.replace(tmp, path)
    return TabularPredictor.load(path, require_py_version_match=False)


def main():
    fetch = "--no-fetch" not in sys.argv
    os.makedirs(MODELS, exist_ok=True)
    os.makedirs(RESULTS, exist_ok=True)
    pairs, until = backfill(fetch)
    df = build(pairs, until)
    df.to_parquet(os.path.join(HERE, "data", "dataset.parquet"))

    coins = df.groupby("mint")["created"].first().sort_values()
    n = len(coins)
    split = {m: ("train" if i < 0.6 * n else "val" if i < 0.8 * n else "test") for i, m in enumerate(coins.index)}
    df["split"] = df["mint"].map(split)
    tr, va, te = (df[df.split == s] for s in ("train", "val", "test"))
    log(f"train {len(tr)} rows / val {len(va)} / test {len(te)}; base rate "
        f"{tr[LABEL].mean():.1%} / {va[LABEL].mean():.1%} / {te[LABEL].mean():.1%}")

    full_feats = ml.FEATURES + ml.CHR_FEATURES
    budget = int(os.environ.get("AG_TIME", 900))
    full = fit(tr, full_feats, "ag_full", budget)
    nochr = fit(tr, ml.FEATURES, "ag_nochronos", budget // 2)

    scores = {}
    for split_df, tag in ((va, "val"), (te, "test")):
        scores[tag] = {
            "AutoGluon + Chronos-2": full.predict_proba(split_df[full_feats])[1].values,
            "AutoGluon, no Chronos": nochr.predict_proba(split_df[ml.FEATURES])[1].values,
            "Chronos-2 alone (q90 best)": split_df["chr_q90_max"].values,
            "15-min momentum": split_df["ret_15m"].values,
        }

    # Gate: chosen on validation only (rule in the docstring).
    pv, base_v = scores["val"]["AutoGluon + Chronos-2"], va[LABEL].mean()
    gate = None
    for thr in np.round(np.arange(0.05, 0.96, 0.01), 2):
        m = pv >= thr
        if m.sum() >= 20 and va[LABEL].values[m].mean() >= 2 * base_v:
            gate = float(thr)
            break

    lines = [f"# Explosion model: {datetime.now():%Y-%m-%d %H:%M} ET", "",
             f"Question: at each 5-minute tick, for a graduated pump.fun coin under {ml.MAX_AGE_MIN // 60}h old and "
             f"worth at least ${ml.MIN_MC / 1000:.0f}k, will it be worth at least {ml.MULT:.0f}× its current cap 60 minutes later?", "",
             f"Data: {df.mint.nunique()} coins, {len(df)} decision points, launches "
             f"{datetime.fromtimestamp(coins.iloc[0] / 1000):%b %d %H:%M} to {datetime.fromtimestamp(coins.iloc[-1] / 1000):%b %d %H:%M}. "
             f"Split by launch time: train {tr.mint.nunique()} coins ({len(tr)} rows), val {va.mint.nunique()} ({len(va)}), "
             f"test {te.mint.nunique()} ({len(te)}).", "",
             f"Base rate (still 2× an hour later): train {tr[LABEL].mean():.1%}, val {base_v:.1%}, test {te[LABEL].mean():.1%}.", "",
             "## Test set (scored once)", "",
             "| model | AUC | top-1 per tick hit | top-3 per tick hit | tick base rate | top-1 end multiple, median / mean |",
             "|---|---|---|---|---|---|"]
    metrics = {}
    for name, s in scores["test"].items():
        a = auc(te[LABEL], s)
        h1, b, n1 = per_tick(te, s, 1)
        h3, _, n3 = per_tick(te, s, 3)
        em, ea = top1_end(te, s)
        metrics[name] = {"auc": round(a, 4), "top1": round(h1, 4), "top3": round(h3, 4), "tick_base": round(b, 4),
                         "top1_end_median": round(float(em), 4), "top1_end_mean": round(float(ea), 4)}
        lines.append(f"| {name} | {a:.3f} | {h1:.1%} (n={n1}) | {h3:.1%} (n={n3}) | {b:.1%} | {em:.2f}× / {ea:.2f}× |")
    lines += ["", f"Every coin at every tick: median end multiple {te.fut_end_mult.median():.2f}×, mean {te.fut_end_mult.mean():.2f}×. "
              f"Positives in test: {int(te[LABEL].sum())} rows from {te[te[LABEL] == 1].mint.nunique()} coins; "
              "with this few, treat every test number as noisy."]

    pt = scores["test"]["AutoGluon + Chronos-2"]
    lines += ["", "## Calibration on test (AutoGluon + Chronos-2)", "",
              "| predicted | n | held 2× | median 60-min end multiple |", "|---|---|---|---|"]
    for lo, hi in ((0, .02), (.02, .05), (.05, .1), (.1, .2), (.2, .4), (.4, 1.01)):
        m = (pt >= lo) & (pt < hi)
        if m.sum():
            lines.append(f"| {lo:.0%}–{min(hi, 1):.0%} | {m.sum()} | {te[LABEL].values[m].mean():.1%} | "
                         f"{np.median(te.fut_end_mult.values[m]):.2f}× |")

    lines += ["", "## Gate", ""]
    if gate is None:
        lines.append("No probability met the rule on validation (precision ≥ 2× base rate with ≥ 20 picks), "
                     "so there is **no gate**. The model ranks coins on the board and sends no pick emails.")
        gm = None
    else:
        m = pt >= gate
        gm = {"n": int(m.sum()), "hit": float(te[LABEL].values[m].mean()) if m.sum() else None,
              "median_end_mult": float(np.median(te.fut_end_mult.values[m])) if m.sum() else None,
              "mean_end_mult": float(np.mean(te.fut_end_mult.values[m])) if m.sum() else None}
        lines.append(f"Chosen on validation: **P ≥ {gate:.2f}**. On test that gave {gm['n']} picks, "
                     + (f"{gm['hit']:.1%} of which were still 2× an hour later (base {te[LABEL].mean():.1%}). "
                        f"Buying at the tick close and selling 60 minutes later returned a median {gm['median_end_mult']:.2f}× "
                        f"and mean {gm['mean_end_mult']:.2f}×, before fees and slippage."
                        if gm["n"] else "none of them on test."))
    fi = full.feature_importance(te[full_feats + [LABEL]], num_shuffle_sets=3, silent=True)
    lines += ["", "## What the model leans on (test permutation importance, top 10)", "",
              "| feature | importance |", "|---|---|"]
    lines += [f"| {f} | {v:.4f} |" for f, v in fi["importance"].head(10).items()]
    lines += ["", "## Leaderboard (validation log-loss inside AutoGluon)", "", "```",
              full.leaderboard(silent=True)[["model", "score_val", "fit_time"]].head(8).to_string(index=False), "```", "",
              "Not advice. End multiples are from 5-minute closes, before fees, slippage and the price impact "
              "of buying into a thin pool."]
    report = "\n".join(lines) + "\n"
    with open(os.path.join(RESULTS, "report.md"), "w") as f:
        f.write(report)
    meta = {"trained": datetime.now(timezone.utc).isoformat(), "label": LABEL, "features": full_feats, "gate": gate,
            "gate_test": gm, "metrics_test": metrics, "base_rate_test": float(te[LABEL].mean()),
            "coins": int(df.mint.nunique()), "rows": int(len(df)),
            "min_mc": ml.MIN_MC, "max_age_min": ml.MAX_AGE_MIN, "horizon_min": ml.HORIZON * 5, "mult": ml.MULT}
    with open(os.path.join(MODELS, "meta.json"), "w") as f:
        json.dump(meta, f, indent=2)
    print(report)

    if "--push" in sys.argv:
        subprocess.run(["git", "-C", HERE, "add", "results/report.md", "models/meta.json"])
        subprocess.run(["git", "-C", HERE, "commit", "-q", "-m",
                        f"Nightly retrain: {meta['coins']} coins, test AUC {metrics['AutoGluon + Chronos-2']['auc']}"])
        subprocess.run(["git", "-C", HERE, "pull", "-q", "--rebase"])
        subprocess.run(["git", "-C", HERE, "push", "-q"])


if __name__ == "__main__":
    main()
