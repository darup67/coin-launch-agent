# Explosion model: 2026-09-23 23:55 ET

Question: at each 5-minute tick, for a graduated pump.fun coin under 4h old and worth at least $75k, will it be worth at least 2× its current cap 60 minutes later?

Data: 421 coins, 5026 decision points, launches Sep 22 22:48 to Sep 23 18:00. Split by launch time: train 253 coins (2801 rows), val 84 (1045), test 84 (1180).

Base rate (still 2× an hour later): train 3.6%, val 3.7%, test 3.2%.

## Test set (scored once)

| model | AUC | top-1 per tick hit | top-3 per tick hit | tick base rate | top-1 end multiple, median / mean |
|---|---|---|---|---|---|
| AutoGluon + Chronos-2 | 0.850 | 14.5% (n=83) | 10.5% (n=237) | 3.2% | 0.63× / 1.27× |
| AutoGluon, no Chronos | 0.850 | 15.7% (n=83) | 10.5% (n=237) | 3.2% | 0.63× / 1.33× |
| Chronos-2 alone (q90 best) | 0.797 | 3.6% (n=83) | 5.5% (n=237) | 3.2% | 0.05× / 0.60× |
| 15-min momentum | 0.791 | 0.0% (n=83) | 3.0% (n=237) | 3.2% | 0.92× / 0.71× |

Every coin at every tick: median end multiple 1.01×, mean 0.87×. Positives in test: 38 rows from 9 coins; with this few, treat every test number as noisy.

## Calibration on test (AutoGluon + Chronos-2)

| predicted | n | held 2× | median 60-min end multiple |
|---|---|---|---|
| 0%–2% | 1022 | 1.9% | 1.02× |
| 2%–5% | 61 | 13.1% | 0.14× |
| 5%–10% | 24 | 8.3% | 0.23× |
| 10%–20% | 27 | 11.1% | 0.20× |
| 20%–40% | 23 | 13.0% | 0.40× |
| 40%–100% | 23 | 13.0% | 0.54× |

## Gate

Chosen on validation: **P ≥ 0.08**. On test that gave 81 picks, 11.1% of which were still 2× an hour later (base 3.2%). Buying at the tick close and selling 60 minutes later returned a median 0.40× and mean 1.07×, before fees and slippage.

## What the model leans on (test permutation importance, top 10)

| feature | importance |
|---|---|
| bars_to_75k | 0.0104 |
| log_first_close | 0.0091 |
| log_vol_60m | 0.0037 |
| log_vol_5m | 0.0024 |
| age_min | 0.0010 |
| hour_utc | 0.0005 |
| ret_60m | 0.0005 |
| bars_since_ath | 0.0004 |
| chr_q10_60m | 0.0003 |
| chr_q90_60m | 0.0002 |

## Leaderboard (validation log-loss inside AutoGluon)

```
              model  score_val   fit_time
WeightedEnsemble_L3  -0.112915 885.690655
  LightGBMXT_BAG_L2  -0.113682 885.686688
  LightGBMXT_BAG_L1  -0.114904 588.512854
WeightedEnsemble_L2  -0.114904 588.513900
```

Not advice. End multiples are from 5-minute closes, before fees, slippage and the price impact of buying into a thin pool.
