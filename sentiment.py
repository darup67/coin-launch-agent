#!/usr/bin/env python3
"""Social sentiment for the coin detector (2026-09-30): a bull / bear ("pump talk" / "dump talk") score per token from every free source that works.

Sources (all keyless, all tested from this Mac on 2026-09-30):
  StockTwits   the crypto crowd's own posts. Each post can carry a Bullish / Bearish tag chosen by its author: the closest thing to a
               labelled sentiment feed. Also post rate, watchlist size, trending list.
  Google News  headlines from the last 3 days (count = attention, wording = tone).
  Reddit       posts in r/CryptoMoonShots, r/solana, r/CryptoCurrency through the Arctic Shift archive (reddit.com itself blocks scripts).
  CoinGecko    holders' up/down votes, watchlist size, and the token's Twitter / Telegram / subreddit handles.
  Telegram     the token's public channel preview (subscribers, latest posts), when CoinGecko names a channel.
  Fear & Greed the whole crypto market's mood (context, not part of a token's score).
NOT available for free: X / Twitter (no keyless API; the syndication endpoint rate-limits at once), Discord, LunarCrush / Santiment / CryptoPanic (keys),
Bluesky (blocks scripts). Their absence is stated on every reading, not hidden.

Score = weighted mean of the components that answered, -100 (dump talk) to +100 (pump talk), with a confidence from how many sources and posts
back it. Wording is scored with a small crypto lexicon (moon / breakout / listing vs dump / rug / hack / lawsuit), not a language model.
IT IS A READING OF TALK, NOT A VALIDATED SIGNAL: every reading is logged with the price, and `sentiment.py evidence` tests it against what
the price did next once enough data exists.

  sentiment.py refresh [--budget 110]   score the universe (priority: passing / near-passing / radar / watcher-listed tokens, then the stalest)
  sentiment.py show SYMBOL              every component and headline for one token
  sentiment.py --set-key x              store an X API bearer token in the Keychain (hidden prompt); X is then read for tokens that pass both tests
  sentiment.py x SYMBOL                 one X lookup (50 posts, about $0.25), counted against the monthly cap
  sentiment.py evidence                 does the score predict the next 6 h / 24 h return? (needs history)
  sentiment.py backfill-test            quick look: past StockTwits days vs the price move that followed
"""
import json, math, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from xml.etree import ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(HERE, "data", "sentiment")
LATEST = os.path.join(DIR, "latest.json")
HISTORY = os.path.join(DIR, "history.jsonl")
CG_IDS = os.path.join(HERE, "data", "cg_ids.json")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
sys.path.insert(0, os.path.expanduser("~/trade-core"))
os.makedirs(DIR, exist_ok=True)

WEIGHTS = {"crowd": 0.40, "news": 0.20, "reddit": 0.15, "votes": 0.15, "telegram": 0.10, "x": 0.30}   # x only counts once a key is stored
GAP = {"api.x.com": 1.0, "api.stocktwits.com": 1.3, "news.google.com": 1.0, "arctic-shift.photon-reddit.com": 2.5, "api.coingecko.com": 4.5, "t.me": 1.5, "api.alternative.me": 1.0}
_last = {}

BULL = {"moon": 2, "mooning": 2, "pump": 1.5, "pumping": 2, "breakout": 2, "break out": 2, "rally": 1.5, "rallies": 1.5, "surge": 2, "surges": 2, "soar": 2, "soars": 2, "skyrocket": 2,
        "ath": 2, "all-time high": 2, "bullish": 2, "bull run": 2, "accumulate": 1.5, "accumulating": 1.5, "buy": 1, "buying": 1, "long": 0.5, "undervalued": 1.5, "gem": 1.5,
        "100x": 2, "10x": 1.5, "listing": 1.5, "listed": 1, "partnership": 1.5, "adoption": 1.5, "upgrade": 1, "launch": 0.5, "burn": 1, "etf": 1, "approval": 1.5, "approved": 1.5,
        "gains": 1, "jumps": 1.5, "climbs": 1, "rebound": 1, "recovery": 1, "record": 1, "strong": 0.5, "higher": 0.5, "upside": 1, "green": 0.5, "rocket": 1.5, "lfg": 1.5, "hodl": 1}
BEAR = {"dump": 2, "dumping": 2, "crash": 2.5, "crashes": 2.5, "plunge": 2, "plunges": 2, "tumble": 2, "tumbles": 2, "rug": 3, "rugpull": 3, "rug pull": 3, "scam": 3, "sell": 1, "selling": 1,
        "short": 0.5, "bearish": 2, "overvalued": 1.5, "fud": 1.5, "hack": 3, "hacked": 3, "exploit": 2.5, "lawsuit": 2, "sec ": 1, "delist": 2.5, "delisting": 2.5, "ban": 2, "banned": 2,
        "liquidation": 2, "liquidated": 2, "rekt": 2, "collapse": 2.5, "drop": 1.5, "drops": 1.5, "slump": 2, "warning": 1.5, "investigation": 2, "fraud": 3, "lower": 0.5, "downside": 1,
        "red": 0.5, "bleeding": 2, "bagholder": 2, "dead": 1.5, "worthless": 2.5, "correction": 1, "corrects": 1, "outflow": 1.5, "unlock": 1, "whale sold": 2}
NEG = ("not ", "no ", "never ", "isn't", "won't", "doesn't", "without ")


def load(p, d):
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def save(p, o):
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(o, f)
    os.replace(tmp, p)


def http(url, timeout=15, raw=False):
    """Paced GET. Returns parsed JSON (or text when raw) or None; never raises."""
    host = urllib.parse.urlparse(url).netloc
    wait = _last.get(host, 0) + GAP.get(host, 1.0) - time.time()
    if wait > 0:
        time.sleep(wait)
    for attempt in (0, 1):
        _last[host] = time.time()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"}), timeout=timeout) as r:
                body = r.read().decode("utf-8", "replace")
            return body if raw else json.loads(body)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                time.sleep(6)
                continue
            return None
        except Exception:
            return None
    return None


def lex(text):
    """(-1..1, hits). Crypto word lists with a one-word negation flip ("not bullish")."""
    t = " " + re.sub(r"\s+", " ", text.lower()) + " "
    b = be = 0.0
    for words, sign in ((BULL, 1), (BEAR, -1)):
        for w, wt in words.items():
            for m in re.finditer(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])" if not w.endswith(" ") else re.escape(w), t):
                before = t[max(0, m.start() - 12):m.start()]
                s = -sign if any(n in before for n in NEG) else sign
                if s > 0:
                    b += wt
                else:
                    be += wt
    tot = b + be
    return ((b - be) / (tot + 1.0), tot)


# ------------------------------------------------------------------ sources
def stocktwits(sym):
    d = http(f"https://api.stocktwits.com/api/2/streams/symbol/{sym}.X.json?limit=30")
    if not d or "messages" not in d:
        return None
    ms = d["messages"]
    if not ms:
        return {"n": 0}
    tags = [((m.get("entities") or {}).get("sentiment") or {}).get("basic") for m in ms]
    bull, bear = tags.count("Bullish"), tags.count("Bearish")
    ts = [time.mktime(time.strptime(m["created_at"], "%Y-%m-%dT%H:%M:%SZ")) - time.timezone for m in ms]
    span_h = max((max(ts) - min(ts)) / 3600, 0.25)
    untagged = [m["body"] for m, t in zip(ms, tags) if not t]
    lx = [lex(b)[0] for b in untagged]
    return {"sym": sym, "n": len(ms), "bull": bull, "bear": bear, "rate_per_h": round(len(ms) / span_h, 2), "watchers": (d.get("symbol") or {}).get("watchlist_count"),
            "lex_untagged": round(sum(lx) / len(lx), 3) if lx else None, "newest_h": round((time.time() - max(ts)) / 3600, 1)}


def news(name, sym):
    q = f'"{name}" crypto' if len(sym) <= 3 else f"{name} {sym} crypto"
    x = http("https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": q + " when:3d", "hl": "en-US", "gl": "US", "ceid": "US:en"}), raw=True)
    if not x:
        return None
    try:
        items = ET.fromstring(x).findall(".//item")
    except ET.ParseError:
        return None
    out, srcs = [], set()
    for it in items[:25]:
        title = (it.findtext("title") or "")
        src = (it.findtext("source") or "")
        clean = re.sub(r"\s+-\s+[^-]+$", "", title)
        low = clean.lower()
        if name.lower() not in low and sym.lower() not in low.split():   # the headline must actually name the token
            continue
        out.append((lex(clean)[0], clean))
        srcs.add(src)
    if not out:
        return {"n": 0}
    return {"n": len(out), "tone": round(sum(s for s, _ in out) / len(out), 3), "sources": len(srcs), "top": [t for _, t in sorted(out, key=lambda z: -abs(z[0]))[:3]]}


def reddit(name, sym, subs=("CryptoMoonShots", "solana", "CryptoCurrency")):
    posts = []
    for sub in subs:
        d = http("https://arctic-shift.photon-reddit.com/api/posts/search?" + urllib.parse.urlencode({"subreddit": sub, "query": sym if len(sym) > 3 else name, "limit": 12, "sort": "desc"}), timeout=12)
        for p in (d or {}).get("data") or []:
            if time.time() - p.get("created_utc", 0) < 5 * 86400:
                posts.append(p)
    if not posts:
        return {"n": 0}
    sc = [(lex(p["title"] + " " + (p.get("selftext") or "")[:300])[0], math.log1p(max(p.get("score", 0), 0)) + 1) for p in posts]
    w = sum(x for _, x in sc)
    return {"n": len(posts), "tone": round(sum(s * x for s, x in sc) / w, 3), "avg_score": round(sum(p.get("score", 0) for p in posts) / len(posts), 1)}


def cg_ids():
    c = load(CG_IDS, {})
    if c and time.time() - c.get("at", 0) < 7 * 86400:
        return c["coins"]
    d = http("https://api.coingecko.com/api/v3/coins/list", timeout=60)
    if not d:
        return c.get("coins", [])
    coins = [{"id": x["id"], "sym": x["symbol"].upper(), "name": x["name"].lower()} for x in d]
    save(CG_IDS, {"at": time.time(), "coins": coins})
    return coins


def cg_id_for(sym, name, cache):
    cands = [x for x in cache if x["sym"] == sym]
    nm = (name or "").lower()
    same = [x for x in cands if x["name"] == nm]
    if same:
        return same[0]["id"]
    near = [x for x in cands if nm and (nm in x["name"] or x["name"] in nm) and len(x["name"]) > 3]
    if near:
        return near[0]["id"]
    return cands[0]["id"] if len(cands) == 1 else None


def coingecko(cid):
    d = http(f"https://api.coingecko.com/api/v3/coins/{cid}?localization=false&tickers=false&market_data=false&community_data=true&developer_data=false&sparkline=false")
    if not d or "id" not in d:
        return None
    cd = d.get("community_data") or {}
    ln = d.get("links") or {}
    sub = (ln.get("subreddit_url") or "").rstrip("/").split("/")[-1] or None
    return {"up": d.get("sentiment_votes_up_percentage"), "down": d.get("sentiment_votes_down_percentage"), "watchers": d.get("watchlist_portfolio_users"),
            "twitter": ln.get("twitter_screen_name") or None, "telegram": ln.get("telegram_channel_identifier") or None, "subreddit": sub,
            "twitter_followers": cd.get("twitter_followers"), "tg_users": cd.get("telegram_channel_user_count")}


def telegram(handle):
    x = http(f"https://t.me/s/{handle}", raw=True)
    if not x:
        return None
    m = re.search(r'counter_value">([\d.,KM]+)</span>\s*<span class="counter_type">subscribers', x)
    posts = [re.sub(r"<[^>]+>", " ", p) for p in re.findall(r'tgme_widget_message_text[^>]*>(.*?)</div>', x, re.S)][-8:]
    tone = [lex(p)[0] for p in posts]
    return {"subs": m.group(1) if m else None, "posts": len(posts), "tone": round(sum(tone) / len(tone), 3) if tone else None}


def fear_greed():
    d = http("https://api.alternative.me/fng/?limit=2")
    try:
        v = d["data"]
        return {"value": int(v[0]["value"]), "label": v[0]["value_classification"], "prev": int(v[1]["value"])}
    except Exception:
        return None


# ------------------------------------------------------------------ X / Twitter (paid, optional)
X_SERVICE = "x-bearer-token"
X_BUDGET = os.path.join(DIR, "x_budget.json")
X_MONTHLY_READS = 3000          # post reads per calendar month. X charges about $0.005 per post read (pay-per-use, 2026), so 3,000 reads is about $15.


def x_key():
    import subprocess
    r = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", X_SERVICE, "-w"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def x_used():
    b = load(X_BUDGET, {})
    return b.get("reads", 0) if b.get("month") == time.strftime("%Y-%m") else 0


def x_search(sym, max_results=50):
    """Recent posts (last 7 days) with the token's cashtag, no retweets, English. Counted against the monthly read cap BEFORE the call. None if no key or cap reached."""
    key = x_key()
    if not key or x_used() + max_results > X_MONTHLY_READS:
        return None
    save(X_BUDGET, {"month": time.strftime("%Y-%m"), "reads": x_used() + max_results, "cap": X_MONTHLY_READS})
    q = urllib.parse.quote(f"${sym} -is:retweet lang:en")
    url = f"https://api.x.com/2/tweets/search/recent?query={q}&max_results={max(10, min(max_results, 100))}&tweet.fields=created_at,public_metrics"
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
    except Exception as e:
        return {"error": str(e)[:120]}
    posts = d.get("data") or []
    if not posts:
        return {"n": 0}
    sc = []
    for p in posts:
        m = p.get("public_metrics") or {}
        w = math.log1p(m.get("like_count", 0) + 2 * m.get("retweet_count", 0) + m.get("reply_count", 0)) + 1
        sc.append((lex(p["text"])[0], w))
    tot = sum(w for _, w in sc)
    ts = [time.mktime(time.strptime(p["created_at"][:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone for p in posts]
    return {"n": len(posts), "tone": round(sum(s * w for s, w in sc) / tot, 3), "span_h": round((max(ts) - min(ts)) / 3600, 1),
            "engagement": sum((p.get("public_metrics") or {}).get("like_count", 0) for p in posts)}


def set_key(name):
    import getpass, subprocess
    if name != "x":
        return print("only `x` is supported: sentiment.py --set-key x")
    k = getpass.getpass("X API bearer token (input hidden): ").strip()
    if not k:
        return print("nothing entered; unchanged")
    r = subprocess.run(["/usr/bin/security", "add-generic-password", "-U", "-a", os.environ.get("USER", "x"), "-s", X_SERVICE, "-w", k], capture_output=True, text=True)
    print("stored in the Keychain (service x-bearer-token)." if r.returncode == 0 else f"Keychain write failed: {r.stderr.strip()}")


def trending_crypto():
    """Crypto tickers on StockTwits' trending list right now."""
    d = http("https://api.stocktwits.com/api/2/trending/symbols.json")
    try:
        return {x["symbol"][:-2] for x in d["symbols"] if x["symbol"].endswith(".X")}
    except Exception:
        return set()


def relabel(score, rel, n_covered):
    """StockTwits skews bullish (the typical coin scores about +50), so with enough coins covered the label is the score against the typical coin,
    and a genuinely negative absolute score always shows as bearish."""
    if score <= -35:
        return "BEARISH"
    if score <= -12:
        return "LEANING BEAR"
    if n_covered < 20:
        return "BULLISH" if score >= 35 else "LEANING BULL" if score >= 12 else "NEUTRAL"
    return "BULLISH" if rel >= 20 else "LEANING BULL" if rel >= 8 else "BEARISH" if rel <= -20 else "LEANING BEAR" if rel <= -8 else "NEUTRAL"


def fomo(c, res, trending, ret, crowd_med=0.6):
    """FOMO heat 0-100: how loudly and how one-sidedly the crowd is piling in RIGHT NOW. Measured from attention (post-rate and headline surge), StockTwits
    trending, price momentum and acceleration, and euphoria (nearly all tagged posts Bullish). It describes crowd heat, not what happens next."""
    st, nw = c.get("stocktwits") or {}, c.get("news") or {}
    parts = {}
    if res.get("hype") is not None:
        parts["attention"] = min(1.0, max(0.0, (res["hype"] - 1) / 3))
    parts["news surge"] = min(1.0, (nw.get("n") or 0) / 15)
    parts["trending"] = 1.0 if (st.get("sym") in trending) else 0.0
    if ret.get("r6") is not None:
        parts["momentum"] = min(1.0, max(0.0, ret["r6"]) / 0.10)
        if ret.get("r1") is not None and ret["r6"] > 0:
            parts["acceleration"] = 1.0 if ret["r1"] > 2 * ret["r6"] / 6 else 0.0
    tagged = (st.get("bull") or 0) + (st.get("bear") or 0)
    if tagged >= 10:
        crowd = (st["bull"] - st["bear"]) / (tagged + 4.0)
        parts["euphoria"] = min(1.0, max(0.0, (crowd - crowd_med) / 0.3))     # more one-sided than the typical coin's crowd, not just bullish
    w = {"attention": 0.30, "momentum": 0.25, "trending": 0.15, "news surge": 0.10, "euphoria": 0.10, "acceleration": 0.10}
    tot = sum(w[k] for k in parts)
    idx = round(100 * sum(w[k] * v for k, v in parts.items()) / tot) if tot else None
    if idx is None:
        return None
    return {"index": idx, "label": "FOMO BUILDING" if idx >= 60 else "WARMING" if idx >= 35 else "QUIET", "parts": {k: round(v, 2) for k, v in parts.items()}}


# ------------------------------------------------------------------ scoring
def compose(c, base=None):
    """Component scores (-1..1) -> {score, label, confidence, sources, missing}. `base` = the token's earlier post rate, for the attention flag."""
    comp, used = {}, []
    st = c.get("stocktwits") or {}
    tagged = (st.get("bull") or 0) + (st.get("bear") or 0)
    if st.get("n"):
        crowd = ((st.get("bull", 0) - st.get("bear", 0)) / (tagged + 4.0)) if tagged >= 3 else None
        if crowd is None and st.get("lex_untagged") is not None:
            crowd = 0.6 * st["lex_untagged"]                  # too few tags: fall back to the wording, discounted
        if crowd is not None:
            comp["crowd"] = crowd
    nw = c.get("news") or {}
    if nw.get("n", 0) >= 2 and nw.get("tone") is not None:
        comp["news"] = max(-1, min(1, nw["tone"] * 2.0))
    rd = c.get("reddit") or {}
    if rd.get("n", 0) >= 3 and rd.get("tone") is not None:
        comp["reddit"] = max(-1, min(1, rd["tone"] * 2.0))
    cg = c.get("coingecko") or {}
    if cg.get("up") is not None:
        comp["votes"] = max(-1, min(1, (cg["up"] - 65) / 35))        # most coins sit above 65% up, so 65 is neutral
    xx = c.get("x") or {}
    if xx.get("n", 0) >= 10 and xx.get("tone") is not None:
        comp["x"] = max(-1, min(1, xx["tone"] * 2.0))
    tg = c.get("telegram") or {}
    if tg.get("tone") is not None and tg.get("posts", 0) >= 3:
        comp["telegram"] = max(-1, min(1, tg["tone"] * 2.0))
    if not comp:
        return {"score": None, "label": "NO DATA", "confidence": "none", "sources": [], "components": {}}
    w = sum(WEIGHTS[k] for k in comp)
    score = round(100 * sum(WEIGHTS[k] * v for k, v in comp.items()) / w)
    n_posts = st.get("n", 0) + nw.get("n", 0) + rd.get("n", 0)
    conf = "high" if len(comp) >= 3 and tagged >= 10 else "medium" if len(comp) >= 2 or tagged >= 8 else "low"
    hype = None
    if st.get("rate_per_h") and base and base > 0:
        hype = round(st["rate_per_h"] / base, 2)
    label = ("BULLISH" if score >= 35 else "LEANING BULL" if score >= 12 else "BEARISH" if score <= -35 else "LEANING BEAR" if score <= -12 else "NEUTRAL")
    talk = None
    if hype and hype >= 2 and abs(score) >= 25:
        talk = "PUMP TALK" if score > 0 else "DUMP TALK"
    return {"score": score, "label": label, "talk": talk, "confidence": conf, "hype": hype, "sources": sorted(comp), "n_posts": n_posts,
            "components": {k: round(100 * v) for k, v in comp.items()}}


# ------------------------------------------------------------------ refresh
def universe():
    """[(priority, symbol, name, address/chain or None)]. Passing and near-passing listed tokens first, then radar, watcher-listed, then the rest."""
    b = load(os.path.join(HERE, "data", "listed_board.json"), {})
    v = load(os.path.join(HERE, "data", "venues.json"), {})
    out = {}
    for r in b.get("rows", []):
        pr = 0 if r.get("clean") else 1 if (r.get("A") or r.get("B")) else 3
        out[r["symbol"]] = (pr, r["symbol"], (v.get("coinbase", {}).get(r["symbol"]) or (v.get("robinhood", {}).get(r["symbol"]) or {}).get("name") or r["symbol"]), None, "listed")
    for r in (b.get("radar") or {}).get("rows", []):
        out.setdefault(r["symbol"], (2, r["symbol"], r["symbol"], (r["chain"], r["address"]), "radar"))
    for r in ((b.get("watcher") or {}).get("listed_hits") or []):
        if r.get("symbol"):
            out[r["symbol"]] = (0, r["symbol"], r["symbol"], (r["chain"], r["address"]), "watcher")
    return list(out.values())


def refresh(budget_s=110):
    t0 = time.time()
    lat = load(LATEST, {"tokens": {}})
    toks = lat["tokens"]
    ids = None
    order = sorted(universe(), key=lambda u: (u[0], toks.get(u[1], {}).get("t", 0)))
    fg = fear_greed()
    trending = trending_crypto()
    have_x = bool(x_key())
    cg_budget, rd_budget = 6, 4
    done = 0
    hist = []
    for pr, sym, name, addr, kind in order:
        if time.time() - t0 > budget_s:
            break
        old = toks.get(sym, {})
        if time.time() - old.get("t", 0) < (600 if pr <= 1 else 3000):       # fresh enough for its priority tier
            continue
        c = {"stocktwits": stocktwits(sym), "news": news(name, sym)}
        if pr <= 2 and rd_budget > 0 and time.time() - t0 < budget_s - 25:
            c["reddit"] = reddit(name, sym); rd_budget -= 1
        else:
            c["reddit"] = old.get("raw", {}).get("reddit")
        cg = old.get("raw", {}).get("coingecko")
        if (not cg or time.time() - old.get("cg_t", 0) > 12 * 3600) and cg_budget > 0 and time.time() - t0 < budget_s - 15:
            ids = ids or cg_ids()
            cid = cg_id_for(sym, name, ids)
            cg = coingecko(cid) if cid else cg
            cg_budget -= 1
            old["cg_t"] = time.time()
        c["coingecko"] = cg
        handle = (cg or {}).get("telegram")
        if handle and (pr <= 1 or not old.get("raw", {}).get("telegram")):
            c["telegram"] = telegram(handle)
        else:
            c["telegram"] = old.get("raw", {}).get("telegram")
        if have_x and pr == 0 and time.time() - old.get("x_t", 0) > 2 * 3600:     # X costs money: only tokens that pass both tests or were flagged by the watcher
            c["x"] = x_search(sym)
            old["x_t"] = time.time()
        else:
            c["x"] = old.get("raw", {}).get("x")
        base = old.get("base_rate")
        res = compose(c, base)
        ret = {}
        try:
            import trade_core
            bars = trade_core.bars(f"CRYPTO:{sym}", days=2)
            if len(bars) > 30:
                last = bars[-1]["c"]
                ret = {"r1": last / bars[-5]["c"] - 1, "r6": last / bars[-25]["c"] - 1}
        except Exception:
            pass
        res["fomo"] = fomo(c, res, trending, ret, lat.get("crowd_median", 0.6))
        rate = (c["stocktwits"] or {}).get("rate_per_h")
        toks[sym] = {"t": time.time(), "cg_t": old.get("cg_t", 0), "x_t": old.get("x_t", 0), "kind": kind, "name": name, "raw": c, "base_rate": round(0.8 * base + 0.2 * rate, 2) if base and rate else rate or base, **res}
        done += 1
        try:
            import trade_core
            bars = trade_core.bars(f"CRYPTO:{sym}", days=1)
            px = bars[-1]["c"] if bars else None
        except Exception:
            px = None
        hist.append({"t": int(time.time()), "sym": sym, "score": res["score"], "conf": res["confidence"], "n": res.get("n_posts"), "px": px, "comp": res["components"]})
    # StockTwits skews bullish (most tagged posts are Bullish for almost every coin), so also report each token against the typical coin.
    sc = sorted(t["score"] for t in toks.values() if t.get("score") is not None)
    med = sc[len(sc) // 2] if sc else 0
    cr = sorted(t["components"]["crowd"] for t in toks.values() if t.get("components", {}).get("crowd") is not None)
    cmed = cr[len(cr) // 2] / 100 if cr else 0.6
    for t in toks.values():
        if t.get("score") is not None:
            t["rel"] = t["score"] - med
            t["label"] = relabel(t["score"], t["rel"], len(sc))
    lat.update({"updated": time.time(), "fear_greed": fg, "market_median": med, "crowd_median": cmed, "tokens": toks, "x": {"connected": have_x, "reads_used": x_used(), "cap": X_MONTHLY_READS}})
    save(LATEST, lat)
    if hist:
        with open(HISTORY, "a") as f:
            f.write("\n".join(json.dumps(h) for h in hist) + "\n")
    print(f"sentiment: refreshed {done} of {len(order)} tokens in {time.time() - t0:.0f}s · fear&greed {fg and fg['value']}")
    return done


# ------------------------------------------------------------------ views
def summary(sym):
    """One-line reading for tables and emails."""
    t = load(LATEST, {}).get("tokens", {}).get(sym)
    if not t or t.get("score") is None:
        return None
    return t


def line(t):
    talk = f" · {t['talk']}" if t.get("talk") else ""
    rel = f", {t['rel']:+d} vs the typical coin" if t.get("rel") is not None else ""
    fo = f" · FOMO {t['fomo']['index']} {t['fomo']['label'].lower()}" if t.get("fomo") else ""
    return f"{t['label']} {t['score']:+d}{talk}{fo} ({t['confidence']} confidence: {', '.join(t['sources'])}{rel})"


def backup():
    """Yesterday-and-older history lines -> archive/sentiment/<day>.jsonl.gz (git). The logged readings are what `evidence` needs, and they cannot be re-created."""
    import gzip
    arch = os.path.join(HERE, "archive", "sentiment")
    os.makedirs(arch, exist_ok=True)
    try:
        rows = [json.loads(l) for l in open(HISTORY)]
    except OSError:
        return 0
    by = {}
    for r in rows:
        by.setdefault(time.strftime("%Y-%m-%d", time.gmtime(r["t"])), []).append(r)
    today = time.strftime("%Y-%m-%d", time.gmtime())
    n = 0
    for day, rs in by.items():
        p = os.path.join(arch, day + ".jsonl.gz")
        if day < today and not os.path.exists(p):
            with gzip.open(p + ".tmp", "wt") as g:
                g.write("\n".join(json.dumps(r) for r in rs) + "\n")
            os.replace(p + ".tmp", p); n += 1
    return n


def show(sym):
    t = load(LATEST, {}).get("tokens", {}).get(sym.upper())
    if not t:
        return print(f"no reading for {sym.upper()} yet (refreshes every 15 min for the listed universe)")
    print(f"{sym.upper()}: {line(t)}   read {(time.time() - t['t']) / 60:.0f} min ago")
    print("  components (-100..+100):", t["components"])
    if t.get("fomo"):
        print("  FOMO heat:", t["fomo"]["index"], t["fomo"]["label"], t["fomo"]["parts"])
    for k, v in t["raw"].items():
        print(f"  {k:<10}", json.dumps(v)[:300])
    xs = load(LATEST, {}).get("x", {})
    print("  X/Twitter: " + (f"connected, {xs.get('reads_used', 0)} of {xs.get('cap')} monthly post reads used" if xs.get("connected") else "not connected (pay-per-use; `sentiment.py --set-key x`)") + ". Not available: Discord, Bluesky, LunarCrush, Santiment, CryptoPanic, the Fomo app (no public API).")


def evidence():
    """Does the score predict the next 6 h / 24 h return? Uses the logged readings and trade-core's 15-minute bars."""
    import trade_core
    rows = []
    try:
        rows = [json.loads(l) for l in open(HISTORY)]
    except OSError:
        pass
    rows = [r for r in rows if r.get("score") is not None and r.get("px")]
    if len(rows) < 50:
        return print(f"collecting: {len(rows)} logged readings; needs at least 200 over 5+ days before anything can be said")
    out = {6: [], 24: []}
    cache = {}
    for r in rows:
        bars = cache.setdefault(r["sym"], trade_core.bars(f"CRYPTO:{r['sym']}", days=30))
        for h in (6, 24):
            tt = (r["t"] + h * 3600) * 1000
            fut = next((b["c"] for b in bars if b["t"] >= tt), None)
            if fut and time.time() > r["t"] + h * 3600:
                out[h].append((r["score"], fut / r["px"] - 1))
    for h, pts in out.items():
        if len(pts) < 30:
            print(f"{h}h: only {len(pts)} matured readings so far"); continue
        pts.sort(key=lambda z: z[0])
        q = len(pts) // 4
        lo, hi = pts[:q], pts[-q:]
        m = lambda a: sum(x for _, x in a) / len(a)
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys)); den = math.sqrt(sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys))
        print(f"{h}h ahead, n={len(pts)}: correlation {cov / den if den else 0:+.3f} · most bullish quarter avg {100 * m(hi):+.2f}% vs most bearish quarter {100 * m(lo):+.2f}%")


def backfill_test(n_tokens=10, pages=6):
    """Quick look with StockTwits history: for each token and day, share of Bullish among tagged posts vs the NEXT day's return."""
    import trade_core
    b = load(os.path.join(HERE, "data", "listed_board.json"), {})
    syms = [r["symbol"] for r in sorted(b.get("rows", []), key=lambda r: -r["usd_vol_24h"])[:n_tokens]]
    pts = []
    for sym in syms:
        maxid, msgs = None, []
        for _ in range(pages):
            d = http(f"https://api.stocktwits.com/api/2/streams/symbol/{sym}.X.json?limit=30" + (f"&max={maxid}" if maxid else ""))
            ms = (d or {}).get("messages") or []
            if not ms:
                break
            msgs += ms
            maxid = ms[-1]["id"] - 1
        by_day = {}
        for m in msgs:
            tag = ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
            if tag:
                day = m["created_at"][:10]
                by_day.setdefault(day, [0, 0])[0 if tag == "Bullish" else 1] += 1
        bars = trade_core.bars(f"CRYPTO:{sym}", days=12)
        closes = {}
        for bar in bars:
            closes[time.strftime("%Y-%m-%d", time.gmtime(bar["t"] / 1000))] = bar["c"]
        days = sorted(closes)
        for day, (bu, be) in by_day.items():
            if bu + be >= 8 and day in closes and days.index(day) + 1 < len(days):
                nxt = days[days.index(day) + 1]
                pts.append(((bu - be) / (bu + be), closes[nxt] / closes[day] - 1, sym, day))
        print(f"{sym}: {len(msgs)} posts back to {msgs[-1]['created_at'][:10] if msgs else '-'}", flush=True)
    if len(pts) < 8:
        return print(f"only {len(pts)} token-days: too few to say anything")
    pts.sort(key=lambda z: z[0])
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys)); den = math.sqrt(sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys))
    hi = [y for x, y, *_ in pts if x >= 0.5]; lo = [y for x, y, *_ in pts if x < 0.5]
    print(f"{len(pts)} token-days · correlation of the day's bullish share with the NEXT day's return: {cov / den if den else 0:+.3f} · "
          f"days with bullish share >= 50%: avg next-day {100 * sum(hi) / max(len(hi), 1):+.2f}% (n={len(hi)}) vs below: {100 * sum(lo) / max(len(lo), 1):+.2f}% (n={len(lo)})")


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        print(__doc__)
    elif a[0] == "refresh":
        refresh(int(a[a.index("--budget") + 1]) if "--budget" in a else 110)
    elif a[0] == "--set-key":
        set_key(a[1] if len(a) > 1 else "")
    elif a[0] == "x" and len(a) > 1:
        print(json.dumps(x_search(a[1].upper(), 50), indent=1) if x_key() else "no X key stored: python3 sentiment.py --set-key x")
    elif a[0] == "show" and len(a) > 1:
        show(a[1])
    elif a[0] == "backup":
        print("archived days:", backup())
    elif a[0] == "evidence":
        evidence()
    elif a[0] == "backfill-test":
        backfill_test()
    else:
        print(__doc__)
