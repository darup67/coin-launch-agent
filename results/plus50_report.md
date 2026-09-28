# +50% in 1 hour: 2026-09-28 14:37 ET

Question: when a coin is added (first 5-minute close at or above $150k within 4h of launch), is it worth at least 1.5x that an hour later? One row per coin.

Coins: 2263 (train 1358, val 453, test 452, split by launch time).

Base rate (+50% after 1h): train 7.5%, val 18.5%, test 33.4%. Median coin 1h later: 0.01x; mean 0.77x.

AUC: validation 0.791, test 0.630.

## Calibration on test

| predicted | coins | hit +50% | median 1h multiple |
|---|---|---|---|
| 0%–10% | 259 | 27% | 1.04x |
| 10%–20% | 106 | 35% | 0.14x |
| 20%–30% | 42 | 40% | 0.36x |
| 30%–40% | 16 | 62% | 1.98x |
| 40%–60% | 29 | 55% | 1.72x |

## Gate

Validation: P ≥ 0.16 gave 117 picks, 42% hit +50%.
Test (scored once): 121 picks, 45% hit +50%; median 1h multiple 0.77x, mean 1.20x.

**Picks are LIVE.**

Not advice. Multiples are 5-minute closes, before fees, slippage and the price impact of buying into a thin pool.
