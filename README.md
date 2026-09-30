# coin-launch-agent

Watches brand-new coins on **Solana** and **Base**, the two chains the Coinbase
app trades onchain. It fires a banner and a sound when a coin that **launched or
graduated** in the last **4 hours** reaches **$150k market cap**. It also flags new
Coinbase Exchange listings.

Read-only: it never trades and holds no keys. It keeps a dedupe list, the
current board, and pump.fun training history in `data/`.

```
New coins ≤4h old · threshold $150.0k · watcher, updated 4s ago

🚀 GS            $360.1k mc      9m old  graduated    liq  $47.9k   2204/1295  b/s 1h  solana  2jPe4j…
🚀 BITSHARK      $224.1k mc     11m old  new pool     liq  $20.6k    106/71    b/s 1h  base    0x0109…
✗  NASA          $168.0k mc      9m old  graduated    liq  $41.1k   3526/11    b/s 1h  solana  6TsQW9…   ← honeypot? 11 sells vs 3526 buys
✗  familiars     $10.05M mc     26m old  new pool     liq  $1.71M     62/6     b/s 1h  base    0xc596…   ← 62 buys/1h
   up            $133.1k mc     12m old  graduated    liq  $20.7k   1604/56    b/s 1h  solana  87MhoU…
```

Each hit opens a small alert window that closes itself after 30s, such as
`🚀 GS $360.1k in 9m · graduated · SOL`, and plays the Submarine sound. The
window replaced macOS banners because the banner applet never registered as a
notification sender on this Mac. The banner channel is still in `config.json`
in case notifications are allowed later. Each hit also lands in `agent.out.log`
with the full token address.

**Digest email:** every 20 hits (muted ones included) go out as one email to
darup67@gmail.com through `~/flip-notifier/send-email.js`, with the Keychain app
password flip-notifier uses. Each coin lists its cap at the alert, its cap now,
the contract address to paste into the Coinbase app, and a DexScreener link.
The queue is kept in `data/state.json`, so a restart doesn't lose it. Set
`email_digest.every` to change the batch size; `--test-digest` sends what's
queued right away.

## Retired 2026-09-28: the 2× model

`score.py`, `train.py` and their LaunchAgents were removed. The code is in git history and the plists are in `~/Library/LaunchAgents.retired-20260928/`. The pump.fun backfill that plus50 needs now lives in `pump.backfill()`. The model's picks lost money on test (median 0.08×), its emails had been off since 9/24, and it was the Mac's heaviest job (~800 MB bursts every 5 min, ~25 min of nightly training). The +50% model replaced it.

## Only the coins worth looking at (+50% filter, since 2026-09-28)

`agent.py` now shows **only coins with a good chance of being worth 50% more one hour after
they were added**. Everything else is hidden; `agent.py --all` shows the old raw board.

- **Added** = the first 5-minute close at or above `min_mc_usd` ($150k) within 4h of launch. That's the watcher's alert rule.
- **Good chance** = held-out coins with that score hit +50% at least 40% of the time. The gate is set on validation and goes live only if the test set agrees (`results/plus50_report.md`).
- **Filters:** honeypot, low-liquidity and fake-cap flags block a pick. The low-buy-count flag doesn't, because a just-graduated coin's pool is minutes old (user, 2026-09-28).
- **Kill switch:** every judged coin's real 1-hour result is recorded. If the last 20 qualifying coins hit +50% less than 40% of the time, picks pause until the live record or a retrain recovers.
- **Coverage:** only pump.fun coins on Solana. Base coins and other launchpads have no candle history, so they are never shown.

Why: of 2,263 coins that reached $150k, the median was at 0.01x an hour later; most rug within the hour.

| launchd label | schedule | does |
|---|---|---|
| `com.dhruv.coinlaunch.plus50` | every 5 min (:02, :07, …) | judges newly added coins once, records 1h outcomes, writes `data/plus50.json` |
| `com.dhruv.coinlaunch.plus50train` | 06:00 daily | retrains on the pump.fun cache, re-sets the gate, commits the report |

`plus50_email` in config.json emails each pick once (off while emails are paused).

## Pre-graduation watch (2026-09-29)

`prelaunch.py` scans brand-new pump.fun coins **before** they graduate and scores each for
`P(graduate) x P(explode | graduate)`. It appears as the "🔮 Pre-graduation watch" section of `agent.py`.

- **Discovery:** the newest ~600 coins from pump.fun's listing every 5 minutes, keeping those with traction (>= $5k market cap). pump.fun sustains roughly one request every 2.7 s (erratically), so the collector skips failed pages and overlaps ticks.
- **Tracking:** each coin is followed by mint through the candles endpoint (no rate limit), snapshotted every tick while 6-120 min old, and labeled 4h+ after launch from its candles (graduated = reaches $75k within 4h; explode = the plus50 event).
- **Stage A, P(graduate):** trained on our own live labeled snapshots (pump.fun's listing only reaches back ~55 min, so non-graduates can't be backfilled). Trains nightly at 06:30 once 60+ positives and 60+ negatives exist (about a day of collection). Used only if held-out test AUC >= 0.60.
- **Stage B, P(explode | graduate):** trained from the ~4,000 cached graduates and their candles from launch. **Failed its test on 2026-09-29 (test AUC 0.44, about 20 exploding coins), so it is off**: pre-graduation behaviour doesn't tell us who explodes afterwards. The plus50 model still judges explosions at $150k.
- **Until stage A is live** the section says "no ranking yet" and lists movers, clearly marked as not a prediction.
- **Ledger:** once a model ranks, each coin's first time in the top 5 is graded 2h later (net of 3% costs) as product `prelaunch`.
- **Coinbase CDP SQL API (optional, free tier):** `cdp_sql.py` can query Solana SPL Token / Token-2022 transfers (about 3 months of history) to add holder, early-buyer and dev-sold features and to backfill coins that never graduated. It needs a free CDP Client API key (`python3 cdp_sql.py --set-key`, stored in the Keychain). It is capped at 900 queries a month (the free tier is 1,000; overage is $0.0083 each) and never uses x402 or any wallet. `--probe` discovers the real table schema first; nothing else is built on it until the schema is known.
- Jobs: `com.dhruv.coinlaunch.pre` (every 5 min), `com.dhruv.coinlaunch.pretrain` (06:30). Report: `results/prelaunch_report.md`.

## Commands

```
~/.venvs/market-ml/bin/python agent.py               # the board right now
~/.venvs/market-ml/bin/python agent.py --test-alert  # one sample banner + sound
~/.venvs/market-ml/bin/python agent.py --run --dry   # watcher in the foreground, no alerts
tail -f agent.out.log                                # HIT / hold / heartbeat lines
```

| launchd label | schedule | does |
|---|---|---|
| `com.dhruv.coinlaunch` | always on (KeepAlive) | continuous watcher; banner + sound on each hit |

```
cp com.dhruv.coinlaunch.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.dhruv.coinlaunch.plist
launchctl bootout gui/$(id -u)/com.dhruv.coinlaunch      # stop
```

This is a long-running process, not a `StartInterval` job. `StartInterval` jobs
stopped firing on this Mac in Sep 2026. Rebuild the banner applet with
`./build-alert-app.sh` after moving the folder.

## Where "launched" and "graduated" come from

Coinbase's API has no feed for new tokens. Its Advanced Trade and Exchange APIs
only list centrally listed products, and those arrive far above these caps. The
Coinbase app trades new Solana and Base tokens through DEX aggregators, so this
feed has to be onchain. Two free APIs, no keys:

- **GeckoTerminal** (discovery): the new-pool lists and 5-minute trending lists.
- **DexScreener** (tracking): `/token-pairs` returns every pool a token has, both
  bonding curve and graduated AMM, with market cap, liquidity, 1h buys and
  sells, and creation time.

Stages:

- **bonding:** launched on a launchpad (pump.fun, Meteora DBC, LaunchLab, …) and
  still on its curve.
- **graduated:** the curve completed and the token has an AMM pool, such as
  PumpSwap or Meteora. Cap and liquidity are read from its deepest pool.
- **new pool:** any other new DEX pool, such as a Base Uniswap v4 launch.

Age is always measured from the token's **oldest** pool, so a new pool on an old
coin does not count.

Coinbase Exchange's public `/products` is checked every 5 minutes. A new base
currency fires `🆕 Coinbase lists XYZ`.

## The rule and the filters (`config.json`)

A token alerts **once**, when `market cap ≥ min_mc_usd` and `age ≤ max_age_hours`
and it passes the filters below. The filters remove the bundled launches and fake
caps that make up much of the raw list:

| key | default | why |
|---|---|---|
| `min_liquidity_usd` | 8000 | a $100k "cap" on $3k of liquidity is one wallet |
| `max_mc_to_liquidity` | 100 | catches the $2B caps on $0 of liquidity that are common on Base |
| `min_buys_h1` | 100 | buy txns across all pools in the last hour; bundled launches show 1–30 |
| `min_sell_ratio` | 0.10 | honeypot check: 1h sells must be ≥10% of buys. Organic graduations ran 0.13–0.73; a suspected honeypot showed 3,526 buys to 11 sells |
| `max_alerts_per_hour` | 20 | anything past the cap is logged as `muted` |

Set a filter to 0 to turn it off. A held-back token can still alert later if it
passes inside the window. `networks` takes any id valid on both APIs, for
example `solana` or `base`.

## Budget, coverage, volume

On this connection GeckoTerminal's free tier measured about **10 calls/min**
(2026-09-23), not the documented 30. The watcher uses 8/min, for discovery only:
4 pages of Solana new pools (about 50 pools/min open there) and 1 of Base every
minute, plus trending every other minute. DexScreener allows 300/min; the watcher
uses up to 200. Tokens near the threshold refresh every 30s, warm ones every
90s, the rest every 4 min. A token is dropped if it stays under $7.5k and under
2× its first cap for 15 minutes, or stays under $15k for an hour. The lists are
cached for 60s, so a hit can land 1–2 min after the cross.

**Observed in a 10-minute live trial (2026-09-23, ~22:00 ET):** 222 tokens
tracked, about 64 API calls/min, **19 hits**, and 87 held back. With SOL around
$115, a pump.fun coin graduates at about $50k, so a $50k threshold fires on
nearly every graduation that gets real buying: roughly 100 an hour. The hourly
cap keeps banners to 20. **On 2026-09-23 the threshold was raised to $150k** and
the honeypot filter was added. To make it stricter again, raise `min_mc_usd` or
`min_buys_h1`.

## Explosion model: Chronos-2 + AutoGluon (every 5 minutes)

The question, asked at every 5-minute tick about each **graduated pump.fun coin
under 4h old and worth at least $75k**: *will its market cap reach 2× within the
next 60 minutes?*

| piece | does |
|---|---|
| `pump.py` | pump.fun data: the graduated-coins list (it pages back about 1.5–2 days) and 5-minute candles from launch. Price × 1B supply = market cap. |
| `ml.py` | shared features, so training and live scoring compute the same thing: returns over 5/15/30/60 min, drawdown from the high, volume and its acceleration, activity, realized volatility, age, launch spike. Chronos-2 (`amazon/chronos-2`, run on the Mac's GPU) forecasts the next 12 bars of log market cap with volume as a covariate; its 10/50/90% quantiles become 7 more features. |
| `train.py` | backfills history, builds one row per coin per tick with the label, fits **AutoGluon Tabular** (`best_quality`, 5-fold bagging grouped by coin, 1 stacking level), and trains a no-Chronos copy to measure what Chronos adds. Writes `results/report.md` and `models/meta.json`. |
| `score.py` | live, at :01/:06/…: scores the watcher's candidates and writes `data/scores.json` (shown on the board). If the model has a gate, emails picks. |

**Rules fixed before any data was seen:**
- Split by launch time: oldest 60% of coins train, next 20% validate, newest 20% test. A coin's rows never appear in two splits.
- The gate is the lowest probability whose **validation** precision is at least 2× the validation base rate, with at least 20 picks. If none qualifies there is no gate, and the model only ranks on the board.
- Test is scored once and reported as it comes out, next to three baselines: 15-minute momentum, Chronos-2 alone, and AutoGluon without Chronos.

**Why the $75k floor:** pump.fun coins graduate below it. So every coin at
$75k+ is already on the graduated list, and drawing training coins from that
list adds no survivorship bias.

| launchd label | schedule | does |
|---|---|---|
| `com.dhruv.coinlaunch.score` | :01, :06, … every 5 min | score candidates, update the board, email picks |
| `com.dhruv.coinlaunch.train` | daily 05:15 | backfill, retrain, commit `results/report.md` + `models/meta.json`, push |

```
~/.venvs/market-ml/bin/python train.py            # backfill + train + report (~25 min)
~/.venvs/market-ml/bin/python score.py --print    # one scoring tick, printed
```

**First results (2026-09-23, 421 coins, test = the newest 84):** the model ranks
well. AUC is 0.85, and each tick's top pick held 2× 14.5% of the time against a
3.2% base rate. But you don't get paid for ranking: the top pick's 60-minute end
multiple was a **median 0.63×** and a mean 1.27×, and at the gate a median 0.40×
and a mean 1.07×, before fees. A few big winners carry the average; most picks
still fall. **Chronos-2 added nothing** over AutoGluon without it (AUC 0.850 vs
0.850), and on its own Chronos was no better than momentum. The first target,
"touches 2× within the hour", was dropped: its picks ended at a median 0.03×,
spike then rug. **Pick emails stay off** until a nightly retrain shows held-out
picks at the gate with a median end multiple of at least 1.0×. Until then the
model only ranks on the board. Full numbers are in [`results/report.md`](results/report.md). Coverage is
pump.fun coins only; Base coins and other Solana launchpads still get the
threshold alerts but no score. Training history is cached in `data/pump/`, which
is not committed. It is the one thing this repo keeps on disk, because a model
can't be trained without it, and it grows every night.

## Not advice

Most of these coins go to zero, and many graduations are coordinated. The
filters remove obvious fakes, not rugs. An alert means a coin crossed a line; it
is not a buy signal.

## Jev shadow (since 2026-09-24)

`jev_shadow.py` asks Jev (via `~/jev-client`) about every hit, muted ones
included, from text alone: name, ticker, pump.fun description and links. It
asks about brand or celebrity impersonation, how clear the concept is, and
pump/scam language. The call runs in a thread, so the watcher never waits on
it. Each coin's cap is re-read ~60 min after the alert (from the watcher's own
DexScreener budget). Nothing changes alerts, the digest or model picks.

```
~/.venvs/market-ml/bin/python jev_shadow.py   # held / halved / median by each Jev answer
```

Wait for ~100+ outcomes before acting. `data/jev.jsonl` and `data/jev_outcomes.jsonl`
hold the raw records. No API key means it does nothing.

## Disk footprint and backup (2026-09-29)

| What | Local rule | Backup |
|---|---|---|
| Pre-graduation snapshots | one atomic gzip per scan, packed into one `snaps/<day>.jsonl.gz` per finished day (level 9, ~4x smaller), kept 14 days | none needed (regenerable in about a day) |
| Candle cache (`data/pump/candles`) | finished days packed into `archive/candles/<day>.jsonl.gz` after 3 days; raw files deleted 14 days later, only after the archive is read back | **git** (`archive/`, ~1 MB/day, immutable, one commit a day). `python3 prelaunch.py restore [YYYY-MM-DD]` puts files back for retraining |
| Labels | kept forever | `archive/labels.json`, committed with the candle archive |
| Stage B model | re-fit weekly (or when stage B is live), not nightly | n/a |
| Logs / screenshots | `market-lab/ops/sweep.py` daily: logs over 1.5 MB cut to the last 300 KB; TradingView screenshots older than 3 days deleted | n/a |

`python3 prelaunch.py backup` = pack + commit + push `archive/` only. The watchdog starts `sweep.py` once a day after 03:00 and warns if the
last sweep is 2+ days old or a push failed. `http.postBuffer` is set to 100 MB (GitHub answered HTTP 400 to pushes over 1 MB without it).

## Closest-to-graduating list (2026-09-29)
`data/pre/curves.json` keeps the latest on-chain curve reading for every coin read in the last 30 min (the board only holds the top 40 by model
score, which left this list empty). `agent.py` ranks it by curve progress. A coin counts as a fresh graduation only if the scan watched its curve go
from incomplete to complete (`watched: true`); coins first read already complete are reported separately. Market caps after migration are meaningless
(the curve empties); the on-chain `complete` flag is the truth. `trades/5m` and `failed txs` show `?` when the public RPC skipped that call.

## Links per candidate (2026-09-29)
Every candidate row in `agent.py` (picks, closest-to-graduating, movers, ranked board, Base watch) and the +50% pick email now carries `feeds.links()`:
DexScreener chart, Pump.fun coin page (Solana), and a Coinbase Wallet deep link (`go.cb-w.com/dapp?cb_url=…`) that opens Jupiter (Solana) or Uniswap (Base) for that
address inside Coinbase Wallet's browser. Coinbase has no per-token page for onchain coins. Opening a link places no order.

## Custom curves (2026-09-29)
`onchain.py` computes `curve_k_ratio` = virtual token x virtual SOL / the default curve's invariant (3.22e25) and `curve_std` (1 if 0.85-1.6). Some
launches use custom curves with a few SOL of virtual reserves (Quine: 2.4 SOL virtual, ~1 SOL to finish), so their progress % is not comparable and
graduating means little. Both are model features (stage A), and `agent.py` ranks standard curves first and flags the rest with a warning.


## 2026-09-29 (evening): Coinbase / Robinhood only
The user trades only through Coinbase and Robinhood, so the detector's default view (`agent.py`) is now `listed.py`'s board: tokens listed on Coinbase
(online spot product) or Robinhood that are **A** rising fast and steadily and **B** holding the gain (thresholds under `listed` in config.json).
- Venue check is by contract address (Coinbase `/currencies` gives Solana/Base addresses), never by ticker. Robinhood-only assets are matched by ticker AND name via CoinGecko.
- Blockchain check: tokens must live on a chain Coinbase supports (63 networks from its API) or Robinhood's own chain; `listed.py chains` prints them, config `listed.allowed_chains` narrows the list. Assets whose chain cannot be verified are excluded and named on the board.
- Data: trade-core's 15-minute bar store (no price API calls); market cap from DexScreener. LaunchAgent `com.dhruv.coinlaunch.listed` scans at :08 :23 :38 :53.
- **Loose ends (pump.fun / DEX / other exchanges that may get listed later)**, three parts, none of which changes the original engine:
  1. **Original watcher stays on, parameters untouched** (`com.dhruv.coinlaunch`: >= $150k within 4 h, same liquidity / buys / honeypot filters, Solana + Base). Every token it tracks or has ever flagged (`data/watcher_hits.json`, tiny and durable) is checked against the Coinbase/Robinhood address lists each scan; a hit that later gets listed becomes an event with the watcher's record attached. Verified against the pruned backup: 0 of 1,636 pump.fun coins and 0 of 994 watcher tokens were listed.
  2. **Listing watch:** each scan diffs Coinbase (tradable products, plus currencies with deposits open but no product yet, a classic precursor) and Robinhood (list refreshed WEEKLY now by the `robinhood-crypto-refresh` task, was monthly) and records new assets with their origin (token age, pump.fun launch) via GeckoTerminal.
  3. **Radar (separate section, not the watcher):** unlisted Solana/Base tokens with real volume (top pools by 24 h volume, liquidity >= $1M, volume >= $2M, cap >= $20M, >= 3 days old, no stablecoins/wrapped natives), given the same A/B test from hourly candles and tagged with OTHER exchanges they trade on (Kraken, Bitstamp, OKX, Binance.US, Upbit, Gemini), matched by contract address through CoinGecko. Marked not buyable on Coinbase/Robinhood today. Hourly. `listed.radar.show: false` hides it.
- The later ML add-ons (pre-graduation collector, plus50, trainers) are unloaded (plists renamed `*.disabled` in ~/Library/LaunchAgents); 124 MB of their data was backed up to `data/_pruned_2026-09-29.tgz` (the daily sweep deletes it after 7 days) and removed; the 230 graduation labels are in git (`archive/labels.json`). Resume: rename the plists back and `launchctl bootstrap gui/$(id -u) <plist>`.
- Coinbase's `/currencies` returns some Solana mints lower-cased (RAY did), so all address matching is case-insensitive and `canon` (from CoinGecko) restores real case for links.
- `agent.py --pump` = old boards, `listed.py check <address>` = is this token supported.

## Social sentiment and FOMO (2026-09-30)
`sentiment.py` scores every listed token (and radar / watcher-listed tokens) from -100 (dump talk) to +100 (pump talk). LaunchAgent `com.dhruv.coinlaunch.sentiment` refreshes at :12 :27 :42 :57 (about 30 tokens a run, so the whole universe every hour or so); the listed board, the coin-detector email, the crypto-scanner alert email and the 08:55 / 16:30 briefs all show it.
- **Sources (all keyless and tested):** StockTwits (posts tagged Bullish / Bearish by their authors, 40%), Google News headlines (20%), Reddit via the Arctic Shift archive (15%), CoinGecko holder votes (15%), Telegram channel preview (10%), plus X when a key is stored (30%). Fear & Greed is shown as market mood, not scored. Wording is scored with a crypto lexicon, not a language model.
- **Calibration:** StockTwits is bullish for almost every coin (typical coin about +54), so labels are set against the typical coin (BULLISH = 20+ above it), and a genuinely negative absolute score always reads bearish. `PUMP TALK` / `DUMP TALK` = post rate at least 2x the token's own baseline with |score| 25+.
- **FOMO heat (0-100):** attention surge, StockTwits trending, price momentum and acceleration, headline surge, and crowd one-sidedness beyond the typical coin. 60+ = FOMO BUILDING. It measures how hot the crowd is, not what comes next.
- **X / Twitter:** no free tier (X moved to pay-per-use in 2026, about $0.005 per post read). `python3 sentiment.py --set-key x` stores a bearer token in the Keychain; X is then read only for tokens that pass both tests, capped at 3,000 post reads a month (about $15), counted before each call. Not connected today.
- **Not available:** Discord, Bluesky, LunarCrush / Santiment / CryptoPanic (keys), and the Fomo trading app (no public API; the third-party fomoapi.io needs its own paid key).
- **Evidence, not faith:** every reading is logged with the price (`data/sentiment/history.jsonl`, archived daily to `archive/sentiment/` in git). `sentiment.py evidence` tests score vs the next 6 h / 24 h return once there is enough data; `backfill-test` is a quick StockTwits look. First backfill (17 token-days, 10 big coins): correlation of the day's bullish share with the next day's return -0.08, too small a sample to say anything.
