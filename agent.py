#!/usr/bin/env python3
"""New-coin launch watcher. It alerts when a token that launched or graduated in
the last few hours reaches a market-cap threshold (default: $50k within 4h).

  agent.py               print the board: tokens under 4h old near or over the threshold
  agent.py --run         continuous watcher (launchd keeps it alive); banner + sound per hit
  agent.py --run --dry   same, with every alert channel off (log only)
  agent.py --test-alert  fire one sample alert through every enabled channel
  agent.py --test-digest email the coins queued for the next digest now (doesn't clear the queue)

Read-only: it never trades and holds no keys. It records nothing except a
dedupe list of what already alerted (data/state.json, pruned to 24h) and the
current board (data/live.json, overwritten every cycle).
"""
import json, os, sys, subprocess, time
from collections import deque
from datetime import datetime

import feeds
import jev_shadow
from feeds import RateLimited

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
STATE = os.path.join(DATA, "state.json")
LIVE = os.path.join(DATA, "live.json")
# Base tokens that are quote assets or majors, never new launches.
QUOTES = {"SOL", "WSOL", "WETH", "ETH", "USDC", "USDT", "CBBTC", "WBTC", "VIRTUAL",
          "ZORA", "DAI", "USDS", "EURC", "JUP", "BONK", "USD1", "CBETH", "AERO", "DEGEN"}
NEW_PAGES = {"solana": 4, "base": 1}   # Solana opens ~50 pools/min; a page is 20
INTAKE_EVERY = 60                      # GeckoTerminal caches its lists for 60s
# DexScreener dex ids that are bonding curves (pre-graduation).
LAUNCH_DEX = {"pumpfun", "meteoradbc", "launchlab", "moonshot", "boop", "believe", "heaven"}


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def clean(sym):
    """Drop control and bidi-override characters that scam tokens put in their names."""
    return "".join(ch for ch in sym if ch.isprintable() and not 0x202A <= ord(ch) <= 0x202E
                   and not 0x2066 <= ord(ch) <= 0x2069)[:16] or "?"


def log(msg):
    print(f"{datetime.now():%m-%d %H:%M:%S} {msg}", flush=True)


def money(x):
    if x is None:
        return "—"
    if x >= 1e6:
        return f"${x / 1e6:.2f}M"
    return f"${x / 1e3:.1f}k" if x >= 1e3 else f"${x:.0f}"


def dur(sec):
    m = int(sec // 60)
    return f"{m}m" if m < 60 else f"{m // 60}h{m % 60:02d}m"


class Watcher:
    def __init__(self, cfg, verbose=True):
        self.cfg, self.verbose = cfg, verbose
        self.tokens = {}                     # "net:token" -> record
        st = load(STATE, None)
        self.seeding = st is None            # first ever run: don't banner the backlog
        st = st or {}
        self.alerted = st.get("alerted", {})              # "net:token" -> alert time
        self.cb_bases = set(st.get("cb_bases", []))
        self.digest = st.get("digest", [])                # hits waiting for the next digest email
        self.alert_times = deque(sorted(t for t in self.alerted.values() if time.time() - t < 3600))
        self.last_intake = self.last_cb = self.last_beat = 0
        self.intakes = self.gt_calls = self.ds_calls = 0

    # ---- discovery (GeckoTerminal) -------------------------------------------
    def discover(self, p):
        p["symbol"] = clean(p["symbol"])
        if p["symbol"].upper() in QUOTES:
            return
        key = f"{p['net']}:{p['token']}"
        if key in self.alerted:
            return
        t = self.tokens.get(key)
        if t is None:
            if p["created"] and time.time() - p["created"] > self.cfg["max_age_hours"] * 3600:
                return
            t = self.tokens[key] = {
                "key": key, "net": p["net"], "token": p["token"], "symbol": p["symbol"],
                "launch": p["created"], "first_mc": p["mc"], "first_seen": time.time(),
                "launchpad": False, "grad_pct": None, "graduated": False,
                "snap": None, "due": 0, "why": []}
        if p["created"]:
            t["launch"] = min(t["launch"] or p["created"], p["created"])
        if p["launchpad"]:
            t["launchpad"] = True
            t["grad_pct"] = p["grad_pct"]
            t["graduated"] = t["graduated"] or p["graduated"]
        t["gt_mc"] = p["mc"]
        if (p["mc"] or 0) >= 0.5 * self.cfg["min_mc_usd"]:
            t["due"] = min(t["due"], time.time())   # moving: look now

    def intake(self):
        self.intakes += 1
        try:
            for net in self.cfg["networks"]:
                for page in range(1, NEW_PAGES.get(net, 1) + 1):
                    self.gt_calls += 1
                    for p in feeds.gt_new_pools(net, page):
                        self.discover(p)
            if self.intakes % 2:   # trending every other minute: the safety net for late runners
                for net in self.cfg["networks"]:
                    self.gt_calls += 1
                    for p in feeds.gt_trending(net, "5m"):
                        self.discover(p)
        except RateLimited:
            pass
        except Exception as e:
            log(f"discovery error: {e!r}")

    # ---- tracking (DexScreener) ----------------------------------------------
    def refresh(self, t):
        pairs = feeds.ds_token_pairs(t["net"], t["token"])
        self.ds_calls += 1
        now = time.time()
        if not pairs:
            t["due"] = now + 60     # not indexed yet
            return
        amm = [p for p in pairs if p["liq"] > 0]
        best = max(amm, key=lambda p: p["liq"]) if amm else max(pairs, key=lambda p: p["mc"] or 0)
        dexes = {p["dex"] for p in pairs}
        launched_on_pad = t["launchpad"] or bool(dexes & LAUNCH_DEX)
        if launched_on_pad and (t["graduated"] or dexes - LAUNCH_DEX):
            stage = "graduated"
        elif launched_on_pad:
            stage = f"bonding {t['grad_pct']:.0f}%" if t["grad_pct"] is not None else "bonding"
        else:
            stage = "new pool"
        created = [p["created"] for p in pairs if p["created"]]
        if created:   # the oldest pool is the token's real age; a new pool can belong to an old coin
            t["launch"] = min([t["launch"] or now] + created)
        t["symbol"] = clean(best["symbol"]) if best["symbol"] else t["symbol"]
        t["meta"] = {"name": best["name"], "info": next((p["info"] for p in pairs if p["info"]), None)}
        t["snap"] = {"mc": best["mc"], "liq": best["liq"], "dex": best["dex"], "url": best["url"],
                     "buys": sum(p["buys"] for p in pairs), "sells": sum(p["sells"] for p in pairs),
                     "vol": sum(p["vol"] for p in pairs), "stage": stage}
        r = (best["mc"] or 0) / self.cfg["min_mc_usd"]
        t["due"] = now + (30 if r >= 0.5 else 90 if r >= 0.2 else 240)
        self.check(t)

    def refresh_due(self, reserve=10):
        now = time.time()
        due = sorted((t for t in self.tokens.values() if t["due"] <= now), key=lambda t: t["due"])
        for t in due:
            if feeds.ds_limit.left() <= reserve:
                break
            try:
                self.refresh(t)
            except RateLimited:
                break
            except Exception as e:
                t["due"] = now + 60
                log(f"refresh error {t['symbol']}: {e!r}")

    def prune(self):
        now, c = time.time(), self.cfg
        for key, t in list(self.tokens.items()):
            age = now - (t["launch"] or t["first_seen"])
            mc = (t["snap"] or {}).get("mc") or t.get("gt_mc") or 0
            tracked = now - t["first_seen"]
            # Fixed dollars, not a share of the threshold: a higher threshold shouldn't drop slow starters.
            dead = (tracked > 900 and mc < 7500 and mc < 2 * (t["first_mc"] or 0)) \
                or (tracked > 3600 and mc < 15000)
            if age > c["max_age_hours"] * 3600 or dead or key in self.alerted:
                del self.tokens[key]
        self.alerted = {k: v for k, v in self.alerted.items() if now - v < 86400}

    # ---- the rule --------------------------------------------------------------
    def check(self, t):
        c, s, now = self.cfg, t["snap"], time.time()
        if not s["mc"] or s["mc"] < c["min_mc_usd"] or t["key"] in self.alerted:
            t["why"] = []
            return
        age = now - (t["launch"] or t["first_seen"])
        if age > c["max_age_hours"] * 3600:
            t["why"] = [f"{dur(age)} old"]
            return
        why = []
        if s["liq"] < c["min_liquidity_usd"]:
            why.append(f"liquidity {money(s['liq'])}" if s["liq"] else "no liquidity yet")
        elif s["mc"] / s["liq"] > c["max_mc_to_liquidity"]:
            why.append(f"cap/liquidity {s['mc'] / s['liq']:.0f}x")
        if s["buys"] < c["min_buys_h1"]:
            why.append(f"{s['buys']} buys/1h")
        elif s["sells"] < c.get("min_sell_ratio", 0) * s["buys"]:
            # Thousands of buys and almost no sells: holders likely can't sell (honeypot).
            why.append(f"honeypot? {s['sells']} sells vs {s['buys']} buys")
        if why:
            if why != t["why"] and self.verbose:
                log(f"hold {t['symbol']} {money(s['mc'])} {dur(age)} old ({', '.join(why)})")
            t["why"] = why
            return
        t["why"] = []
        self.alerted[t["key"]] = now
        self.alert({**t, **s, "age": age})

    def alert(self, h):
        line = card_line(h)
        if self.seeding:
            log(f"already over at first start (no banner) {line}")
            return
        jev_shadow.judge(h)   # first, so its answer is usually back before a digest goes out
        self.add_to_digest(h)
        while self.alert_times and time.time() - self.alert_times[0] > 3600:
            self.alert_times.popleft()
        if len(self.alert_times) >= self.cfg["max_alerts_per_hour"]:
            log(f"HIT (muted, hourly cap) {line}")
            return
        self.alert_times.append(time.time())
        log(f"HIT {line}")
        chain = "SOL" if h["net"] == "solana" else h["net"].upper()
        title = f"🚀 {h['symbol']} {money(h['mc'])} in {dur(h['age'])} · {h['stage']} · {chain}"
        body = f"liq {money(h['liq'])} · {h['buys']}/{h['sells']} buys/sells 1h · {h['dex']} · {h['token']}"
        notify(title, body, self.cfg["alerts"],
               speak=f"{h['symbol']} hit {h['mc'] / 1000:.0f} thousand in {dur(h['age'])}",
               detail=f"{title}\n{body}\n{h['url']}")

    # ---- digest email ------------------------------------------------------------
    def add_to_digest(self, h):
        """Every hit (muted ones too) joins the digest; each full batch goes out as one email."""
        d = self.cfg.get("email_digest", {})
        if not d.get("enabled"):
            return
        self.digest.append({k: h[k] for k in ("symbol", "net", "token", "mc", "liq", "buys", "sells",
                                              "stage", "dex", "url", "age")} | {"at": time.time()})
        n = d.get("every", 20)
        if len(self.digest) >= n and send_digest(self.digest[:n]):
            self.digest = self.digest[n:]

    # ---- Coinbase listings -----------------------------------------------------
    def coinbase(self):
        prods = feeds.coinbase_products()
        bases = {p["base_currency"] for p in prods.values()}
        if self.cb_bases and not self.seeding:
            for b in sorted(bases - self.cb_bases):
                pairs = sorted(k for k, p in prods.items() if p["base_currency"] == b)
                status = prods[pairs[0]].get("status", "")
                log(f"COINBASE LISTING {b} {pairs} {status}")
                notify(f"🆕 Coinbase lists {b} · {', '.join(pairs)}", f"status {status}",
                       self.cfg["alerts"], speak=f"Coinbase just listed {b}",
                       detail=f"New Coinbase Exchange product(s): {pairs} (status {status})")
                if self.cfg.get("coinbase_listing_email") and not self.cfg["alerts"].get("email"):
                    send_email(f"Coinbase lists {b}: {', '.join(pairs)}",
                               f"New Coinbase Exchange product(s): {pairs} (status {status})")
        self.cb_bases = bases

    # ---- output ----------------------------------------------------------------
    def board(self):
        now, rows = time.time(), []
        for t in self.tokens.values():
            s = t["snap"]
            if s and (s["mc"] or 0) >= 0.5 * self.cfg["min_mc_usd"]:
                rows.append({k: t[k] for k in ("symbol", "net", "token", "why")}
                            | {k: s[k] for k in ("mc", "liq", "buys", "sells", "stage", "dex", "url")}
                            | {"age": now - (t["launch"] or t["first_seen"])})
        rows.sort(key=lambda r: -(r["mc"] or 0))
        return rows

    def run(self):
        out = os.path.join(HERE, "agent.out.log")
        if os.path.exists(out) and os.path.getsize(out) > 5e6:   # launchd appends; keep it small
            open(out, "w").close()
        log(f"watching {self.cfg['networks']} for >= {money(self.cfg['min_mc_usd'])} within "
            f"{self.cfg['max_age_hours']}h" + (" (first start: seeding, no banners)" if self.seeding else ""))
        while True:
            now = time.time()
            try:
                if now - self.last_intake >= INTAKE_EVERY:
                    self.last_intake = now
                    self.intake()
                    self.prune()
                    if self.cfg.get("coinbase_listings", True) and now - self.last_cb >= 300:
                        self.last_cb = now
                        self.coinbase()
                    if self.seeding and self.intakes >= 3:
                        self.seeding = False
                        log("seeding done; alerts are live")
                self.refresh_due()
                jev_shadow.outcomes(self)
                save(STATE, {"alerted": self.alerted, "cb_bases": sorted(self.cb_bases),
                             "digest": self.digest})
                save(LIVE, {"updated": now, "rows": self.board()})
                save(os.path.join(DATA, "tracked.json"), {"updated": now, "tokens": [   # for score.py
                    {"token": t["token"], "net": t["net"], "symbol": t["symbol"], "launch": t["launch"],
                     "mc": t["snap"]["mc"], "liq": t["snap"]["liq"], "why": t["why"]}
                    for t in self.tokens.values() if t["snap"]]})
                if now - self.last_beat >= 600:
                    self.last_beat = now
                    log(f"tracking {len(self.tokens)} tokens · API calls GT {self.gt_calls} DS {self.ds_calls} · "
                        f"{len(self.alert_times)} alerts last hour")
            except Exception as e:
                log(f"cycle error: {e!r}")
            time.sleep(5)


def card_line(h):
    return (f"{h['symbol'][:12]:<12} {money(h['mc']):>8} mc  {dur(h['age']):>6} old  {h['stage']:<12} "
            f"liq {money(h['liq']):>7}  {h['buys']:>5}/{h['sells']:<5} b/s 1h  {h['net']:<6}  {h['token']}")


def notify(title, body, cfg, speak="", detail=""):
    """Each channel runs to completion, because launchd kills leftover children.
    The payload rides in the title, because this Mac hides notification bodies."""
    def run(cmd):
        try:
            subprocess.run(cmd, capture_output=True, timeout=20)
        except Exception as e:
            log(f"alert channel {cmd[0]} failed: {e}")
    if cfg.get("popup", False):
        # A small alert window that closes itself. Unlike banners it needs no notification
        # permission; the applet banner never registered on this Mac (2026-09-23).
        # Not awaited: the watcher is long-running, so launchd doesn't reap it.
        subprocess.Popen(["/usr/bin/osascript", "-e", "on run argv", "-e",
                          "display alert (item 1 of argv) message (item 2 of argv) giving up after "
                          + str(int(cfg.get("popup_seconds", 30))), "-e", "end run", title, body],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if cfg.get("banner", True):
        with open(os.path.join(DATA, "alert.txt"), "w") as f:
            f.write(title.replace("\n", " ") + "\n" + body.replace("\n", " ") + "\n")
        run(["/usr/bin/open", "-g", os.path.join(HERE, "CoinLaunchAlert.app")])
        time.sleep(2)   # let the applet read alert.txt before another alert overwrites it
    if cfg.get("sound", True):
        run(["/usr/bin/afplay", cfg.get("sound_file", "/System/Library/Sounds/Submarine.aiff")])
    if cfg.get("speak", False) and speak:
        run(["/usr/bin/say", speak])
    if cfg.get("email", False):
        send_email(title, detail or body)


def send_email(subject, body):
    """Gmail via flip-notifier's sender; the app password comes from Keychain, as there."""
    try:
        pw = subprocess.run(["/usr/bin/security", "find-generic-password", "-a", "darup67@gmail.com",
                             "-s", "flip-notifier-gmail", "-w"],
                            capture_output=True, text=True, timeout=10).stdout.strip()
        r = subprocess.run(["node", os.path.expanduser("~/flip-notifier/send-email.js"), subject, body],
                           capture_output=True, text=True, timeout=40,
                           env={**os.environ, "FLIP_GMAIL_APP_PASSWORD": pw, "SEND_EMAIL_TIMEOUT_MS": "35000"})
        if r.returncode:
            log(f"email failed: {r.stderr.strip()[-200:]}")
        return r.returncode == 0
    except Exception as e:
        log(f"email failed: {e!r}")
        return False


def cfg_jev_in_digest():
    return load(os.path.join(HERE, "config.json"), {}).get("jev_in_digest", False)


def send_digest(hits):
    """One email for a batch of hits, each with its market cap now, re-read from DexScreener."""
    lines = []
    for i, h in enumerate(hits, 1):
        now_mc = None
        try:
            pairs = feeds.ds_token_pairs(h["net"], h["token"])
            amm = [p for p in pairs if p["liq"] > 0]
            if amm:
                now_mc = max(amm, key=lambda p: p["liq"])["mc"]
        except Exception:
            pass
        chg = f" ({(now_mc / h['mc'] - 1) * 100:+.0f}%)" if now_mc and h["mc"] else ""
        chain = "Solana" if h["net"] == "solana" else h["net"].capitalize()
        jl = jev_shadow.digest_line(f"{h['net']}:{h['token']}") if cfg_jev_in_digest() else ""
        lines.append(
            f"{i:>2}. {h['symbol']} ({chain}, {h['stage']})\n"
            f"    alerted {datetime.fromtimestamp(h['at']):%b %d %I:%M %p} at {money(h['mc'])}, {dur(h['age'])} after launch\n"
            f"    now {money(now_mc)}{chg} · liq {money(h['liq'])} · {h['buys']}/{h['sells']} buys/sells 1h at alert\n"
            f"    contract {h['token']}\n"
            + (f"    {jl}\n" if jl else "")
            + f"    {h['url']}\n")
    first, last = hits[0]["at"], hits[-1]["at"]
    subject = (f"Coin launch digest: {len(hits)} new coins over threshold "
               f"({datetime.fromtimestamp(first):%b %d %I:%M %p} to {datetime.fromtimestamp(last):%I:%M %p})")
    body = ("New Solana and Base coins that crossed the market-cap threshold within 4h of launch and "
            "passed the liquidity, buyer and honeypot filters.\n"
            "To buy in the Coinbase app, search by contract address (onchain trading covers Solana and Base; "
            "a given token can still be missing there).\n\n" + "\n".join(lines)
            + ("\nJev lines are an unvalidated read of the coin's name and description; "
               "`python jev_shadow.py` shows whether they have predicted anything yet.\n"
               if cfg_jev_in_digest() else "")
            + "\nMost of these go to zero. An alert is a coin crossing a line, not a buy signal.\n"
            "- coin-launch-agent (~/coin-launch-agent)")
    ok = send_email(subject, body)
    log(f"digest email {'sent' if ok else 'FAILED, will retry on the next hit'}: {len(hits)} coins")
    return ok


def show_board(cfg):
    live = load(LIVE, None)
    if live and time.time() - live["updated"] < 180:
        rows, src = live["rows"], f"watcher, updated {dur(time.time() - live['updated'])} ago"
    else:
        w = Watcher(cfg, verbose=False)
        w.seeding, w.alerted, w.alert = False, {}, lambda h: None
        w.intake()
        for t in w.tokens.values():   # one-shot: only look closer at what GeckoTerminal shows moving
            if (t.get("gt_mc") or 0) < 0.3 * cfg["min_mc_usd"]:
                t["due"] = float("inf")
        w.refresh_due(reserve=0)
        rows, src = w.board(), "one-shot scan (watcher not running)"
    sc = load(os.path.join(DATA, "scores.json"), None)
    if sc and time.time() - sc["updated"] > 600:
        sc = None
    pmap = {r["token"]: r["p"] for r in sc["rows"]} if sc else {}
    thr = cfg["min_mc_usd"]
    print(f"New coins ≤{cfg['max_age_hours']}h old · threshold {money(thr)} · {src}\n")
    if not rows:
        print(f"  nothing at or above {money(thr / 2)} right now")
    for r in rows[:25]:
        mark = "✗ " if r["why"] else "🚀" if r["mc"] >= thr else "  "
        p = f"  P(2× held) {pmap[r['token']]:.0%}" if r["token"] in pmap else ""
        print(f"{mark} {card_line(r)}{p}" + (f"   ← {', '.join(r['why'])}" if r["why"] else ""))
    if sc:
        gate = f"gate P ≥ {sc['gate']:.0%}" if sc["gate"] is not None else "no gate: ranking only"
        print(f"\nModel, P(still 2× in 60 min), {dur(time.time() - sc['updated'])} ago · {gate} · "
              f"base rate {sc['base_rate'] or 0:.0%} · {len(sc['rows'])} scored")
        for r in sc["rows"][:8]:
            print(f"  {r['p']:>4.0%}  {r['symbol'][:12]:<12} ${r['mc'] / 1000:>7,.0f}k  {r['age_min']:>4.0f}m old"
                  + ("  PICK" if r["pick"] else "") + (f"  ← {', '.join(r['why'])}" if r["why"] else ""))
    print("\n🚀 over threshold, passes filters   ✗ held back by a filter   blank = approaching"
          "\nAlready-alerted coins leave the board; they are in agent.out.log.")


def main():
    cfg = load(os.path.join(HERE, "config.json"), {})
    os.makedirs(DATA, exist_ok=True)
    if "--dry" in sys.argv:
        cfg["alerts"] = {k: False for k in cfg["alerts"]}
        cfg["email_digest"] = {"enabled": False}
    if "--run" in sys.argv:
        Watcher(cfg).run()
    elif "--test-digest" in sys.argv:
        hits = load(STATE, {}).get("digest", [])
        print("sent" if hits and send_digest(hits) else f"nothing sent ({len(hits)} coins queued)")
    elif "--test-alert" in sys.argv:
        notify("🚀 TEST $72.4k in 38m · graduated · SOL", "liq $24k · 1180/640 buys/sells 1h · test alert",
               cfg["alerts"], speak="Test coin hit 72 thousand in 38 minutes")
        print("sent")
    else:
        show_board(cfg)


if __name__ == "__main__":
    main()
