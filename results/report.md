# Explosion model: 2026-09-28 14:27 ET

Question: at each 5-minute tick, for a graduated pump.fun coin under 4h old and worth at least $75k, will it be worth at least 2× its current cap 60 minutes later?

Data: 2889 coins, 39034 decision points, launches Sep 22 22:48 to Sep 28 12:57. Split by launch time: train 1734 coins (21175 rows), val 578 (9446), test 577 (8413).

Base rate (still 2× an hour later): train 3.9%, val 7.8%, test 14.0%.

## Test set (scored once)

| model | AUC | top-1 per tick hit | top-3 per tick hit | tick base rate | top-1 end multiple, median / mean |
|---|---|---|---|---|---|
| AutoGluon + Chronos-2 | 0.751 | 35.1% (n=282) | 32.0% (n=846) | 14.1% | 1.22× / 1.31× |
| AutoGluon, no Chronos | 0.752 | 29.1% (n=282) | 29.7% (n=846) | 14.1% | 1.08× / 1.10× |
| Chronos-2 alone (q90 best) | 0.619 | 13.8% (n=282) | 15.4% (n=846) | 14.1% | 0.04× / 0.81× |
| 15-min momentum | 0.658 | 18.8% (n=282) | 24.1% (n=846) | 14.1% | 1.05× / 1.12× |

Every coin at every tick: median end multiple 0.88×, mean 0.96×. Positives in test: 1182 rows from 164 coins; with this few, treat every test number as noisy.

## Calibration on test (AutoGluon + Chronos-2)

| predicted | n | held 2× | median 60-min end multiple |
|---|---|---|---|
| 0%–2% | 2576 | 1.9% | 1.03× |
| 2%–5% | 1344 | 8.9% | 0.00× |
| 5%–10% | 2273 | 17.3% | 0.05× |
| 10%–20% | 1602 | 24.9% | 0.09× |
| 20%–40% | 598 | 35.8% | 1.02× |
| 40%–100% | 20 | 30.0% | 0.01× |

## Gate

Chosen on validation: **P ≥ 0.05**. On test that gave 4493 picks, 22.5% of which were still 2× an hour later (base 14.0%). Buying at the tick close and selling 60 minutes later returned a median 0.08× and mean 1.08×, before fees and slippage.

## What the model leans on (test permutation importance, top 10)

| feature | importance |
|---|---|
| log_ath | 0.0097 |
| log_vol_60m | 0.0093 |
| age_min | 0.0084 |
| ret_since_launch | 0.0065 |
| bars_since_ath | 0.0058 |
| log_first_close | 0.0048 |
| hour_utc | 0.0037 |
| log_mc | 0.0032 |
| drawdown | 0.0027 |
| chr_q50_15m | 0.0015 |

## Leaderboard (validation log-loss inside AutoGluon)

```
                    model  score_val   fit_time
      WeightedEnsemble_L3  -0.130375 705.214365
      WeightedEnsemble_L2  -0.131057 335.789246
          CatBoost_BAG_L2  -0.132390 559.080977
     CatBoost_r177_BAG_L2  -0.132777 557.715833
       CatBoost_r9_BAG_L2  -0.133167 584.848403
      CatBoost_r13_BAG_L1  -0.133297  13.478302
NeuralNetTorch_r22_BAG_L2  -0.133387 601.593252
    NeuralNetTorch_BAG_L2  -0.133425 573.655275
```

Not advice. End multiples are from 5-minute closes, before fees, slippage and the price impact of buying into a thin pool.
