"""Shared features for the explosion model, used by train.py and score.py so the live
scorer computes exactly what was trained.

The question, asked every 5 minutes about each graduated pump.fun coin that is
under 4h old and worth at least $75k:

    will it be worth at least 2x its current cap 60 minutes from now?

The first version asked whether the cap would *touch* 2x within the hour
(hit2x). It ranked well, with test AUC 0.845, but its picks ended the hour at a
median 0.03x: it had learned to find coins that spike and then rug. The
target is now the price an hour later (hold2x), which is what a buyer keeps.

Every coin worth $75k or more has already graduated, so taking training coins
from the graduated list adds no survivorship bias for these decision points.

Features use only bars closed at decision time. Chronos-2 forecasts the next 12
bars of log market cap, with volume as a covariate, and its quantiles become
features. AutoGluon learns which combinations actually came before a 2x.
"""
import math
import numpy as np

BAR_MS = 300_000
MIN_MC = 75_000          # decision points: coins at least this big
MAX_AGE_MIN = 240        # ...and at most this old
HORIZON = 12             # bars ahead = 60 minutes
MULT = 2.0               # hold2x: close 60 min later >= MULT x the current cap
QUANTILES = [0.1, 0.5, 0.9]


def grid(bars, created_ms):
    """Wall-clock 5m grid from the launch bar to the last bar; empty bars carry the close, zero volume."""
    if not bars:
        return None
    t0 = (min(created_ms, bars[0][0]) // BAR_MS) * BAR_MS
    n = (bars[-1][0] - t0) // BAR_MS + 1
    close = np.full(n, np.nan)
    high = np.full(n, np.nan)
    vol = np.zeros(n)
    first_open = bars[0][1]
    for t, o, h, l, c, v in bars:
        i = (t - t0) // BAR_MS
        close[i], high[i], vol[i] = c, h, vol[i] + v
    last = first_open
    for i in range(n):
        if np.isnan(close[i]):
            close[i] = high[i] = last
        last = close[i]
    return {"t0": t0, "close": close, "high": high, "vol": vol, "first_open": first_open}


def decision_indices(g, created_ms, until_ms, need_label):
    """Bars k whose close is a decision point: cap ≥ MIN_MC, age ≤ MAX_AGE_MIN, and (for
    training) the whole next hour already happened by until_ms."""
    out = []
    for k in range(len(g["close"])):
        t_close = g["t0"] + (k + 1) * BAR_MS
        if t_close > until_ms:
            break
        if (t_close - created_ms) / 60000 > MAX_AGE_MIN:
            break
        if need_label and t_close + HORIZON * BAR_MS > until_ms:
            break
        if g["close"][k] >= MIN_MC:
            out.append(k)
    return out


def features(g, k, created_ms):
    c, h, v = g["close"], g["high"], g["vol"]
    lc = np.log(c[: k + 1])
    lmc = lc[-1]
    base = math.log(max(g["first_open"], 1.0))

    def ret(n):
        return lmc - (lc[-1 - n] if k - n >= 0 else base)

    ath_i = int(np.argmax(h[: k + 1]))
    d = np.diff(lc) if k >= 1 else np.zeros(1)
    last6 = d[-6:] if len(d) else np.zeros(1)
    first75 = int(np.argmax(c[: k + 1] >= MIN_MC))
    t_close = g["t0"] + (k + 1) * BAR_MS
    return {
        "age_min": (t_close - created_ms) / 60000,
        "log_mc": lmc,
        "ret_5m": ret(1), "ret_15m": ret(3), "ret_30m": ret(6), "ret_60m": ret(12),
        "ret_since_launch": lmc - base,
        "drawdown": lmc - math.log(h[ath_i]),
        "bars_since_ath": k - ath_i,
        "log_ath": math.log(h[ath_i]),
        "log_vol_5m": math.log1p(v[k]),
        "log_vol_15m": math.log1p(v[max(0, k - 2): k + 1].sum()),
        "log_vol_60m": math.log1p(v[max(0, k - 11): k + 1].sum()),
        "vol_accel": v[max(0, k - 2): k + 1].sum() / (v[max(0, k - 11): k + 1].sum() / 4 + 1),
        "active_30m": float((v[max(0, k - 5): k + 1] > 0).mean()),
        "rv_30m": float(np.std(last6)),
        "up_frac_30m": float((last6 > 0).mean()),
        "max_bar_jump": float(d.max()) if len(d) else 0.0,
        "log_first_close": math.log(c[0]),
        "bars_to_75k": first75,
        "hour_utc": (t_close // 3_600_000) % 24,
    }


def label(g, k):
    fut = g["high"][k + 1: k + 1 + HORIZON]
    end = g["close"][min(k + HORIZON, len(g["close"]) - 1)]
    return {"hold2x": int(end >= MULT * g["close"][k]),      # the target: still 2x an hour later
            "hit2x": int(fut.max() >= MULT * g["close"][k]),   # a 2x print at any point (often a wick)
            "fut_max_mult": float(fut.max() / g["close"][k]),
            "fut_end_mult": float(end / g["close"][k])}


_pipe = None


def chronos_features(contexts):
    """contexts: list of (log_close[0..k], log1p_vol[0..k]). Returns one dict per context:
    Chronos-2 quantile forecasts of log cap relative to now, at +15m and +60m, plus the
    best q50/q90 over the hour."""
    global _pipe
    import torch
    from chronos import BaseChronosPipeline
    if _pipe is None:
        dev = "mps" if torch.backends.mps.is_available() else "cpu"
        _pipe = BaseChronosPipeline.from_pretrained("amazon/chronos-2", device_map=dev)
    out = []
    for s in range(0, len(contexts), 256):
        batch = [{"target": np.asarray(lc, dtype=np.float32),
                  "past_covariates": {"vol": np.asarray(lv, dtype=np.float32)}}
                 for lc, lv in contexts[s:s + 256]]
        q, _ = _pipe.predict_quantiles(batch, prediction_length=HORIZON, quantile_levels=QUANTILES)
        for (lc, _), qq in zip(contexts[s:s + 256], q):
            a = qq[0].float().cpu().numpy() - lc[-1]          # (HORIZON, 3), relative to now
            out.append({"chr_q50_15m": a[2, 1], "chr_q10_60m": a[-1, 0], "chr_q50_60m": a[-1, 1],
                        "chr_q90_60m": a[-1, 2], "chr_q50_max": a[:, 1].max(), "chr_q90_max": a[:, 2].max(),
                        "chr_spread_60m": a[-1, 2] - a[-1, 0]})
    return out


def chronos_context(g, k):
    return np.log(g["close"][: k + 1]), np.log1p(g["vol"][: k + 1])


FEATURES = list(features({"close": np.array([1e5, 1e5]), "high": np.array([1e5, 1e5]),
                          "vol": np.array([1.0, 1.0]), "t0": 0, "first_open": 3e3}, 1, 0).keys())
CHR_FEATURES = ["chr_q50_15m", "chr_q10_60m", "chr_q50_60m", "chr_q90_60m",
                "chr_q50_max", "chr_q90_max", "chr_spread_60m"]
