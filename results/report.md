# Explosion model: 2026-09-26 05:43 ET

Question: at each 5-minute tick, for a graduated pump.fun coin under 4h old and worth at least $75k, will it be worth at least 2× its current cap 60 minutes later?

Data: 1565 coins, 18483 decision points, launches Sep 22 22:48 to Sep 26 04:09. Split by launch time: train 939 coins (10587 rows), val 313 (4011), test 313 (3885).

Base rate (still 2× an hour later): train 2.9%, val 4.2%, test 4.9%.

## Test set (scored once)

| model | AUC | top-1 per tick hit | top-3 per tick hit | tick base rate | top-1 end multiple, median / mean |
|---|---|---|---|---|---|
| AutoGluon + Chronos-2 | 0.720 | 10.5% (n=171) | 10.0% (n=512) | 4.9% | 0.46× / 0.96× |
| AutoGluon, no Chronos | 0.733 | 12.9% (n=171) | 11.7% (n=512) | 4.9% | 0.29× / 1.13× |
| Chronos-2 alone (q90 best) | 0.694 | 9.4% (n=171) | 9.6% (n=512) | 4.9% | 0.06× / 0.69× |
| 15-min momentum | 0.600 | 5.3% (n=171) | 6.6% (n=512) | 4.9% | 1.02× / 0.90× |

Every coin at every tick: median end multiple 1.01×, mean 0.87×. Positives in test: 189 rows from 34 coins; with this few, treat every test number as noisy.

## Calibration on test (AutoGluon + Chronos-2)

| predicted | n | held 2× | median 60-min end multiple |
|---|---|---|---|
| 0%–2% | 1833 | 0.6% | 1.03× |
| 2%–5% | 1080 | 8.7% | 0.04× |
| 5%–10% | 695 | 9.2% | 0.51× |
| 10%–20% | 215 | 7.4% | 0.34× |
| 20%–40% | 57 | 7.0% | 0.04× |
| 40%–100% | 5 | 0.0% | 0.02× |

## Gate

Chosen on validation: **P ≥ 0.05**. On test that gave 972 picks, 8.6% of which were still 2× an hour later (base 4.9%). Buying at the tick close and selling 60 minutes later returned a median 0.46× and mean 0.82×, before fees and slippage.

## What the model leans on (test permutation importance, top 10)

| feature | importance |
|---|---|
| bars_to_75k | 0.0030 |
| log_first_close | 0.0012 |
| ret_30m | 0.0009 |
| up_frac_30m | 0.0008 |
| chr_q10_60m | 0.0005 |
| hour_utc | 0.0005 |
| ret_15m | 0.0004 |
| chr_q90_60m | 0.0003 |
| drawdown | 0.0003 |
| bars_since_ath | 0.0002 |

## Leaderboard (validation log-loss inside AutoGluon)

```
                model  score_val   fit_time
  WeightedEnsemble_L3  -0.100347 884.766035
      LightGBM_BAG_L1  -0.101133 596.435503
  WeightedEnsemble_L2  -0.101133 596.436844
NeuralNetTorch_BAG_L2  -0.105255 884.758458
```

Not advice. End multiples are from 5-minute closes, before fees, slippage and the price impact of buying into a thin pool.
