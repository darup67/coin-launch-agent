#!/usr/bin/env python3
"""Coinbase CDP SQL API client, free tier only (2026-09-29).

  python3 cdp_sql.py --set-key   paste your free CDP *Client* API key (hidden); stored in the Keychain
  python3 cdp_sql.py --status    key present? queries used this month vs the cap
  python3 cdp_sql.py --probe     discover the Solana schema with a few free queries -> data/cdp_schema.json
  python3 cdp_sql.py "SELECT ..."  run one query (counts against the cap)

Why this exists: the pre-graduation model lacks holder / early-buyer / dev-sold features, and its
'graduate' model waits on live labels. The SQL API indexes Solana SPL Token + Token-2022 transfers
(about 3 months of history, fresh within ~250 ms of the chain tip). Docs (fetched 2026-09-29):
1,000 free queries per month, then $0.0083 each; 2 queries/second by default; POST
https://api.cdp.coinbase.com/platform/v2/data/query/run with `Authorization: Bearer <client key>`
and body {"sql": "..."}.

COST GUARD: this client refuses to send a query once MONTHLY_CAP (900, well under the 1,000 free) has been used
in the calendar month, so it can never run up a bill. It never uses the x402 pay-per-query route or any wallet.
The key lives only in the Keychain (service cdp-client-key); never in a file, log or shell history.
"""
import getpass, json, os, subprocess, sys, time, urllib.error, urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
BUDGET = os.path.join(DATA, "cdp_budget.json")
SCHEMA = os.path.join(DATA, "cdp_schema.json")
URL = "https://api.cdp.coinbase.com/platform/v2/data/query/run"
SERVICE = "cdp-client-key"
MONTHLY_CAP = 900          # hard stop; the free tier is 1,000 per month
MIN_GAP_S = 0.6            # stays under the default 2 queries/second
os.makedirs(DATA, exist_ok=True)
_last = 0.0


def _key():
    k = os.environ.get("CDP_CLIENT_KEY")
    if k:
        return k.strip()
    r = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", SERVICE, "-w"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def _month():
    return datetime.now().strftime("%Y-%m")


def used():
    b = {}
    try:
        with open(BUDGET) as f:
            b = json.load(f)
    except (OSError, ValueError):
        pass
    return b.get("used", 0) if b.get("month") == _month() else 0


def _count(n=1):
    # Read the old total BEFORE opening for write: open(..., "w") truncates the file, so reading inside
    # the with-block always saw 0 and the counter never passed 1 (bug found 2026-09-29).
    total = used() + n
    tmp = BUDGET + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"month": _month(), "used": total, "cap": MONTHLY_CAP, "updated": datetime.now().isoformat(timespec="seconds")}, f)
    os.replace(tmp, BUDGET)


class BudgetExceeded(RuntimeError):
    pass


def query(sql, timeout=30):
    """Run one SQL query. Returns the parsed JSON ({"metadata":..., "result":[...]}). Raises on any problem.
    Counted BEFORE sending, so a failure or retry can never sneak past the cap."""
    global _last
    key = _key()
    if not key:
        raise RuntimeError("no CDP client key: run python3 cdp_sql.py --set-key")
    if used() >= MONTHLY_CAP:
        raise BudgetExceeded(f"{used()} queries used this month (cap {MONTHLY_CAP}); the free tier is 1,000")
    wait = _last + MIN_GAP_S - time.time()
    if wait > 0:
        time.sleep(wait)
    _count()
    _last = time.time()
    req = urllib.request.Request(URL, data=json.dumps({"sql": sql}).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:400]
        raise RuntimeError(f"HTTP {e.code}: {body}")


def set_key():
    k = getpass.getpass("CDP Client API key (input hidden): ").strip()
    if not k:
        print("nothing entered; unchanged")
        return 1
    r = subprocess.run(["/usr/bin/security", "add-generic-password", "-U", "-a", os.environ.get("USER", "cdp"), "-s", SERVICE, "-w", k],
                       capture_output=True, text=True)
    if r.returncode:
        print(f"Keychain write failed: {r.stderr.strip()}")
        return 1
    print("stored in Keychain (service cdp-client-key). Testing with one free query...")
    try:
        out = query("SELECT 1 AS ok")
        print("works:", json.dumps(out)[:200])
        return 0
    except Exception as e:
        print("stored, but the test query failed:", e)
        return 1


def status():
    print(f"key in Keychain: {'yes' if _key() else 'NO'} · queries used this month: {used()} of {MONTHLY_CAP} (free tier 1,000)")


def probe():
    """A few free queries to learn the Solana table layout. Writes data/cdp_schema.json."""
    out = {"probed": datetime.now().isoformat(timespec="seconds"), "results": {}}
    tries = [("transfers_sample", "SELECT * FROM solana.transfers LIMIT 3"),
             ("instructions_sample", "SELECT * FROM solana.instructions LIMIT 3"),
             ("transfers_describe", "DESCRIBE TABLE solana.transfers")]
    for name, sql in tries:
        try:
            out["results"][name] = query(sql)
            print(f"[{name}] ok: {json.dumps(out['results'][name])[:600]}")
        except Exception as e:
            out["results"][name] = {"error": str(e)}
            print(f"[{name}] {e}")
    with open(SCHEMA, "w") as f:
        json.dump(out, f, indent=1)
    print(f"saved {SCHEMA}; queries used this month: {used()}")


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        print(__doc__)
    elif a[0] == "--set-key":
        sys.exit(set_key())
    elif a[0] == "--status":
        status()
    elif a[0] == "--probe":
        probe()
    else:
        print(json.dumps(query(a[0]), indent=1)[:4000])
