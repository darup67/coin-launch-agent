#!/usr/bin/env python3
"""Free on-chain features for the pre-graduation watch (2026-09-29). No keys, no accounts, no cost.

Solana (public endpoint https://api.mainnet-beta.solana.com; the only free one that works keyless, and it
429s the heavy getTokenLargestAccounts call, so that one is not used). Per pump.fun coin:
  curve_progress / curve_sol   bonding-curve account decoded from getAccountInfo (real token/sol reserves)
  n_trades / n_trades_5m/15m   signatures touching the curve account (buys + sells, up to 1000)
  fail_share                   failed transactions / all (bots fighting for the same block)
  first_slot_txs               transactions in the creation slot (snipers / bundles)
  creator_share, creator_sold  the dev's balance of the coin, and how much of it left since we last looked
  mint_authority, freeze_authority, n_extensions   Token program flags (freeze authority = honeypot risk)
  creator_wallet_age_h, creator_txs   fresh throwaway wallets vs. an established one

Base (free RPCs mainnet.base.org, base.drpc.org, 1rpc.io, rotated), from ERC-20 Transfer logs since launch:
  holders, top1/top5-excluding-top1 share, n_transfers, unique_receivers, transfers_5m, owner_renounced
"""
import base64, json, math, struct, threading, time, urllib.error, urllib.request

SOL_RPC = "https://api.mainnet-beta.solana.com"
BASE_RPCS = ["https://mainnet.base.org", "https://base.drpc.org", "https://1rpc.io/base"]
INITIAL_K = 1_073_000_000_000_000 * 30_000_000_000  # pump.fun default curve: virtual token x virtual SOL at launch
INITIAL_REAL_TOKENS = 793_100_000_000_000          # pump.fun: 793.1M tokens x 1e6 decimals sold along the curve
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO = "0x" + "0" * 40

_lock = threading.Lock()
_last = {}
MIN_GAP = {SOL_RPC: 0.22}                           # ~4.5 calls/s across threads keeps us under the public limit


def rpc(url, method, params, timeout=15, tries=2):
    """One JSON-RPC call, paced per endpoint. Returns result or None (never raises)."""
    gap = MIN_GAP.get(url, 0.15)
    for i in range(tries):
        with _lock:
            wait = _last.get(url, 0) + gap - time.time()
            _last[url] = time.time() + max(wait, 0)
        if wait > 0:
            time.sleep(wait)
        try:
            req = urllib.request.Request(url, data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
                                         headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.load(r)
            if "result" in d:
                return d["result"]
            if i == 0:
                time.sleep(1.0)
        except urllib.error.HTTPError as e:
            time.sleep(2.0 if e.code == 429 else 0.5)
        except Exception:
            time.sleep(0.5)
    return None


# ------------------------------------------------------------------ Solana
def decode_curve(b64):
    """pump.fun BondingCurve account: 8-byte discriminator, then five u64 (virtual token, virtual sol,
    real token, real sol, total supply) and a bool `complete`."""
    raw = base64.b64decode(b64)
    if len(raw) < 49:
        return None
    vt, vs, rt, rs, supply = struct.unpack_from("<5Q", raw, 8)
    return {"virtual_token": vt, "virtual_sol": vs, "real_token": rt, "real_sol": rs, "supply": supply, "complete": bool(raw[48])}


def solana_dynamic(meta, created_ms, prev=None):
    """Features that change while a coin trades. meta needs bonding_curve, creator, mint."""
    now = time.time()
    out = {}
    acct = rpc(SOL_RPC, "getAccountInfo", [meta["bonding_curve"], {"encoding": "base64"}])
    v = (acct or {}).get("value")
    if v:
        c = decode_curve(v["data"][0])
        if c:
            out["curve_progress"] = max(0.0, min(1.0, 1 - c["real_token"] / INITIAL_REAL_TOKENS))
            out["curve_sol"] = c["real_sol"] / 1e9
            out["curve_complete"] = int(c["complete"])
            # constant-product invariant vs the default curve: custom curves (tiny virtual SOL) can finish for ~1 SOL,
            # so their progress % is not comparable with a standard curve's (found 2026-09-29 on Quine)
            if c["virtual_token"] and c["virtual_sol"]:
                out["curve_k_ratio"] = c["virtual_token"] * c["virtual_sol"] / INITIAL_K
                out["curve_std"] = int(0.85 <= out["curve_k_ratio"] <= 1.6)
    sigs = rpc(SOL_RPC, "getSignaturesForAddress", [meta["bonding_curve"], {"limit": 1000}])
    if sigs is not None:
        bt = [s.get("blockTime") or 0 for s in sigs]
        out["n_trades"] = len(sigs)
        out["n_trades_5m"] = sum(1 for t in bt if t >= now - 300)
        out["n_trades_15m"] = sum(1 for t in bt if t >= now - 900)
        out["fail_share"] = (sum(1 for s in sigs if s.get("err")) / len(sigs)) if sigs else 0.0
        slots = [s["slot"] for s in sigs if s.get("slot")]
        if slots:
            first = min(slots)
            out["first_slot_txs"] = sum(1 for s in slots if s == first)
            out["unique_slots"] = len(set(slots))
    ta = rpc(SOL_RPC, "getTokenAccountsByOwner", [meta["creator"], {"mint": meta["mint"]}, {"encoding": "jsonParsed"}])
    if ta is not None:
        bal = sum(float(a["account"]["data"]["parsed"]["info"]["tokenAmount"].get("uiAmount") or 0) for a in ta.get("value", []))
        supply = 1e9
        out["creator_share"] = bal / supply
        if prev and prev.get("creator_share") is not None:
            out["creator_sold"] = max(0.0, prev["creator_share"] - out["creator_share"])
    return out


def solana_static(meta):
    """Features that do not change after launch (fetched once per coin)."""
    out = {}
    mi = rpc(SOL_RPC, "getAccountInfo", [meta["mint"], {"encoding": "jsonParsed"}])
    v = (mi or {}).get("value")
    if v and isinstance(v.get("data"), dict):
        info = (v["data"].get("parsed") or {}).get("info") or {}
        out["mint_authority"] = int(bool(info.get("mintAuthority")))
        out["freeze_authority"] = int(bool(info.get("freezeAuthority")))
        out["n_extensions"] = len(info.get("extensions") or [])
    cs = rpc(SOL_RPC, "getSignaturesForAddress", [meta["creator"], {"limit": 100}])
    if cs is not None:
        out["creator_txs"] = len(cs)
        old = [s.get("blockTime") for s in cs if s.get("blockTime")]
        out["creator_wallet_age_h"] = (time.time() - min(old)) / 3600 if old else 0.0
    return out


# ------------------------------------------------------------------ Base
_base_i = 0


def base_rpc(method, params):
    global _base_i
    for k in range(len(BASE_RPCS)):
        url = BASE_RPCS[(_base_i + k) % len(BASE_RPCS)]
        r = rpc(url, method, params, tries=1)
        if r is not None:
            _base_i = (_base_i + k) % len(BASE_RPCS)
            return r
    return None


def base_features(token, launch_ms):
    """Holder distribution from Transfer logs since launch. Returns {} if the RPCs decline."""
    head = base_rpc("eth_blockNumber", [])
    if head is None:
        return {}
    head = int(head, 16)
    age_s = max(60, time.time() - launch_ms / 1000)
    frm = max(0, head - int(age_s / 2) - 300)                    # Base ~2 s blocks, small margin
    logs, step, a = [], 9000, frm
    while a <= head and len(logs) < 20000:
        b = min(head, a + step - 1)
        r = base_rpc("eth_getLogs", [{"fromBlock": hex(a), "toBlock": hex(b), "address": token, "topics": [TRANSFER_TOPIC]}])
        if r is None:
            return {}
        logs.extend(r); a = b + 1
    bal, recv, times5 = {}, set(), 0
    now_block = head
    for lg in logs:
        if len(lg.get("topics", [])) < 3:
            continue
        fr, to = "0x" + lg["topics"][1][-40:], "0x" + lg["topics"][2][-40:]
        amt = int(lg["data"], 16) if lg.get("data", "0x") != "0x" else 0
        if fr != ZERO:
            bal[fr] = bal.get(fr, 0) - amt
        if to != ZERO:
            bal[to] = bal.get(to, 0) + amt
            recv.add(to)
        if now_block - int(lg["blockNumber"], 16) <= 150:        # last ~5 minutes
            times5 += 1
    pos = sorted((v for v in bal.values() if v > 0), reverse=True)
    total = sum(pos)
    out = {"n_transfers": len(logs), "transfers_5m": times5, "unique_receivers": len(recv), "holders": len(pos)}
    if total > 0 and pos:
        out["top1_share"] = pos[0] / total
        out["top5_ex1_share"] = sum(pos[1:6]) / total
    ow = base_rpc("eth_call", [{"to": token, "data": "0x8da5cb5b"}, "latest"])
    if isinstance(ow, str) and len(ow) >= 66:
        out["owner_renounced"] = int(int(ow, 16) == 0)
    return out
