# +50% in 1 hour: 2026-09-29 06:08 ET

Question: when a coin is added (first 5-minute close at or above $150k within 4h of launch), is it worth at least 1.5x that an hour later? One row per coin.

Coins: 2714 (train 1629, val 543, test 542, split by launch time).

Base rate (+50% after 1h): train 9.5%, val 25.8%, test 19.0%. Median coin 1h later: 0.01x; mean 0.75x.

AUC: validation 0.742, test 0.575.

## Calibration on test

| predicted | coins | hit +50% | median 1h multiple |
|---|---|---|---|
| 0%–10% | 356 | 18% | 0.01x |
| 10%–20% | 117 | 14% | 0.01x |
| 20%–30% | 32 | 22% | 0.09x |
| 30%–40% | 18 | 28% | 0.01x |
| 40%–60% | 2 | 100% | 2.42x |
| 60%–100% | 17 | 59% | 1.69x |

## Gate

Validation: P ≥ 0.13 gave 183 picks, 42% hit +50%.
Test (scored once): 133 picks, 26% hit +50%; median 1h multiple 0.15x, mean 0.86x.

**Picks are OFF:** the test hit rate didn't also reach 40% (with at least 5 picks).

Not advice. Multiples are 5-minute closes, before fees, slippage and the price impact of buying into a thin pool.
