"""Jev shadow judgments on alerted coins. Shadow only: nothing here changes an
alert, the digest, or the model picks.

  jev_shadow.judge(hit)        from the watcher, per hit: runs in a thread, never blocks or raises
  jev_shadow.outcomes(w)       from the watcher's loop: marks each judged coin's cap 60 min later
  python jev_shadow.py         report: do Jev's answers separate coins that held from ones that dumped?

Jev reads text, not numbers (its own docs say to keep arithmetic in code), so
it only sees the coin's name, ticker, description and links. The watcher's
filters and ml.py already cover the numbers. The question is whether the words
add anything on top.

data/jev.jsonl           one line per judged coin: what Jev was shown and its answers
data/jev_outcomes.jsonl  one line per coin: its cap ~60 min after the alert
"""
import json, os, sys, threading, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
JUDGED = os.path.join(HERE, "data", "jev.jsonl")
OUTCOMES = os.path.join(HERE, "data", "jev_outcomes.jsonl")
HORIZON = 3600
sys.path.insert(0, os.path.expanduser("~/jev-client"))
try:
    import jev
except ImportError:
    jev = None

QUESTIONS = {
    "impersonation": {
        "type": "noul",
        "instructions": "Does this token use the name, ticker, or official social accounts of a real company, brand, "
                        "celebrity, or established crypto project that is unlikely to have launched it?",
    },
    "concept": {
        "type": "score",
        "instructions": "How clear is the token's concept or meme, judged from `name`, `symbol` and `description`?",
        "criteria": [
            "None: random letters, placeholder text, or nothing to go on",
            "Generic: a common word or a template meme with no particular angle",
            "Clear: a specific meme, topic, trend or community the token is about",
        ],
    },
    "scam_language": {
        "type": "noul",
        "instructions": "Does `description` use pump or scam language, such as promised returns, '100x' or '1000x', "
                        "'next big thing', 'dev will pump', or urgent calls to buy now?",
    },
}

_lock = threading.Lock()
_pending = None   # key -> judged record, for coins still waiting on their outcome


def _append(path, rec):
    with _lock:
        with open(path, "a") as f:
            f.write(json.dumps(rec) + "\n")


def _read(path):
    try:
        with open(path) as f:
            return [json.loads(l) for l in f if l.strip()]
    except FileNotFoundError:
        return []


def _pump_meta(mint):
    try:
        req = urllib.request.Request(f"https://frontend-api-v3.pump.fun/coins/{mint}",
                                     headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.load(r)
        return {k: d.get(k) for k in ("name", "description", "twitter", "telegram", "website")}
    except Exception:
        return {}


def _state(h):
    """Text only: what the coin says about itself."""
    meta = h.get("meta") or {}
    info = meta.get("info") or {}
    links = [w.get("url") for w in info.get("websites") or []] + \
            [s.get("url") for s in info.get("socials") or []]
    s = {"name": meta.get("name") or h["symbol"], "symbol": h["symbol"], "description": None}
    if h["net"] == "solana" and h["token"].endswith("pump"):
        p = _pump_meta(h["token"])
        s["name"] = p.get("name") or s["name"]
        s["description"] = p.get("description")
        links += [p.get(k) for k in ("website", "twitter", "telegram")]
    s["links"] = sorted({l for l in links if l})
    return s


def _judge(h):
    try:
        state = _state(h)
        res = jev.ask(state, QUESTIONS, caller="coin-launch")
        if not res:
            return
        rec = {"key": f"{h['net']}:{h['token']}", "symbol": h["symbol"], "net": h["net"], "at": time.time(),
               "mc": h["mc"], "stage": h["stage"], "age": h["age"], "model": res["model"],
               "state": state, "answers": res["answers"]}
        _append(JUDGED, rec)
        if _pending is not None:
            with _lock:
                _pending[rec["key"]] = rec
    except Exception as e:
        print(f"jev_shadow: judge failed for {h.get('symbol')}: {e!r}", flush=True)


def judge(h):
    if jev is None or not jev.api_key():
        return
    threading.Thread(target=_judge, args=(h,), daemon=True).start()


def _load_pending():
    global _pending
    done = {o["key"] for o in _read(OUTCOMES)}
    _pending = {r["key"]: r for r in _read(JUDGED) if r["key"] not in done}


def outcomes(w, per_cycle=5, reserve=20):
    """Record the cap of coins judged >= HORIZON ago. Uses the watcher's DexScreener budget."""
    import feeds
    if _pending is None:
        _load_pending()
    now = time.time()
    with _lock:
        due = [r for r in _pending.values() if now - r["at"] >= HORIZON][:per_cycle]
    for r in due:
        if feeds.ds_limit.left() <= reserve:
            return
        net, token = r["key"].split(":", 1)
        try:
            pairs = feeds.ds_token_pairs(net, token)
            w.ds_calls += 1
        except Exception:
            continue
        amm = [p for p in pairs if p["liq"] > 0]
        mc = max(amm, key=lambda p: p["liq"])["mc"] if amm else 0.0
        _append(OUTCOMES, {"key": r["key"], "at": now, "after_s": round(now - r["at"]), "mc": mc,
                           "ratio": (mc / r["mc"]) if r["mc"] else None})
        with _lock:
            _pending.pop(r["key"], None)


def digest_line(key):
    """Jev's read of one coin as a short line for the digest email, or "" if it hasn't answered."""
    rec = None
    for r in _read(JUDGED):
        if r["key"] == key:
            rec = r
    if not rec:
        return ""
    a = rec["answers"]
    concept = ("none", "generic", "clear")[min(2, max(0, round(a["concept"]["score"])))]
    return (f"Jev: impersonation {a['impersonation']['noul']:.0%} · concept {concept} · "
            f"scam language {a['scam_language']['noul']:.0%}")


def report():
    out = {o["key"]: o for o in _read(OUTCOMES)}
    rows = [(r, out[r["key"]]) for r in _read(JUDGED) if r["key"] in out and out[r["key"]]["ratio"] is not None]
    judged = len(_read(JUDGED))
    print(f"{judged} coins judged, {len(rows)} with a 60-min outcome\n")
    if not rows:
        return

    def line(label, grp):
        if not grp:
            print(f"  {label:<34} {'—':>4}")
            return
        ratios = sorted(o["ratio"] for _, o in grp)
        held = sum(x >= 1 for x in ratios) / len(ratios)
        dumped = sum(x <= 0.5 for x in ratios) / len(ratios)
        print(f"  {label:<34} {len(grp):>4}   held {held:>4.0%}   halved {dumped:>4.0%}   median {ratios[len(ratios) // 2]:.2f}x")

    print("  outcome = cap 60 min after alert / cap at alert")
    line("all", rows)
    for q, cut in (("impersonation", 0.5), ("scam_language", 0.5)):
        print(f"\n{q}")
        line(f"yes (noul >= {cut})", [x for x in rows if x[0]["answers"][q]["noul"] >= cut])
        line(f"no  (noul <  {cut})", [x for x in rows if x[0]["answers"][q]["noul"] < cut])
    print("\nconcept")
    for lvl, name in enumerate(("none", "generic", "clear")):
        line(name, [x for x in rows if round(x[0]["answers"]["concept"]["score"]) == lvl])
    if len(rows) < 100:
        print(f"\n{len(rows)} outcomes is too few to act on; wait for ~100+ before changing anything.")


if __name__ == "__main__":
    report()
