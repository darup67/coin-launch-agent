# coin-launch-agent

Watches brand-new coins on **Solana** and **Base**, the two chains the Coinbase
app trades onchain. It fires a banner and a sound when a coin that **launched or
graduated** in the last **4 hours** reaches **$150k market cap**. It also flags new
Coinbase Exchange listings.

Read-only: it never trades, holds no keys, and records nothing except a dedupe
list and the current board.

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

## Not advice

Most of these coins go to zero, and many graduations are coordinated. The
filters remove obvious fakes, not rugs. An alert means a coin crossed a line; it
is not a buy signal.
