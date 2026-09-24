# coin-launch-agent

Watches brand-new coins on **Solana** and **Base**, the two chains the Coinbase
app trades onchain. It fires a banner and a sound when a coin that **launched or
graduated** in the last **4 hours** reaches **$50k market cap**. It also flags new
Coinbase Exchange listings.

Read-only: it never trades, holds no keys, and records nothing except a dedupe
list and the current board.

```
New coins ≤4h old · threshold $50.0k · watcher, updated 4s ago

🚀 SSM6900       $174.4k mc      2m old  graduated    liq  $37.2k    891 buys/1h  solana  9mKDte…
🚀 DogWifTipped  $234.3k mc     17m old  new pool     liq  $21.1k    132 buys/1h  base    0x47ad…
✗  Groyper       $121.0k mc      2m old  new pool     liq  $15.2k     14 buys/1h  base    0xfe78…   ← 14 buys/1h
✗  CZBUILDER      $79.9k mc      4m old  graduated    liq   $3.0k    655 buys/1h  solana  2E7oLA…   ← liquidity $3.0k
   Steam          $47.8k mc      2m old  graduated    liq  $18.7k    725 buys/1h  solana  EDjqYQ…
```

A banner looks like `🚀 SSM6900 $174.4k in 2m · graduated · SOL`. This Mac hides
notification bodies, so everything that matters is in the title. Each hit also
lands in `agent.out.log` with the full token address.

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
only list centrally listed products, and those arrive far above $50k. The
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
cap keeps banners to 20. To hear only the exceptional ones, raise `min_mc_usd`
(for example to $150k–250k) or `min_buys_h1`.

## Not advice

Most of these coins go to zero, and many graduations are coordinated. The
filters remove obvious fakes, not rugs. An alert means a coin crossed a line; it
is not a buy signal.
