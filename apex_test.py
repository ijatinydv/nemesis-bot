"""Quick functional test for APEX bot — exercises BUY, SELL, HOLD, and cap safety."""
import json
import urllib.request

def call(obs):
    req = urllib.request.Request(
        "http://127.0.0.1:8080/decide",
        method="POST",
        data=json.dumps(obs).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.loads(r.read())

BASE = {
    "schemaVersion": 1,
    "rules": {
        "maximumBuyCashUsd": 5.0,
        "targetVolumeUsd": 1000,
        "evaluationWindowHours": 24,
        "onePositionAtATime": True,
        "buyFeesIncludedInMaximum": True,
    },
}

REF_BULL = {
    "btcMidUsd": 80700.0,
    "openingTargetUsd": 80424.0,
    "observedAt": 1789880823.0,
    "targetObservedAt": 1789880814.0,
    "targetProvisional": False,
}

BOOKS_BALANCED = {
    "YES": {"bids": [[0.49, 100]], "asks": [[0.50, 100]], "minOrderSize": 1},
    "NO":  {"bids": [[0.49, 100]], "asks": [[0.50, 100]], "minOrderSize": 1},
}

# ── Test 1: BUY ───────────────────────────────────────────────────────────────
obs_buy = {
    **BASE,
    "timestamp": 1789880823.619,
    "market": {"id": "mkt-buy", "secondsToClose": 100},
    "account": {"cashUsd": 10.0, "eligibleVolumeUsd": 0, "completedCycles": 0, "position": None},
    "reference": REF_BULL,
    "books": BOOKS_BALANCED,
}
r1 = call(obs_buy)
print("BUY test  :", json.dumps(r1))
assert r1.get("action") == "BUY", f"Expected BUY, got {r1}"
assert r1.get("outcome") in {"YES", "NO"}, "BUY must have outcome"
assert 0 < r1.get("maxCashUsd", 0) <= 5.0, f"maxCashUsd out of range: {r1}"
print(f"  amount=${r1['maxCashUsd']:.6f}, outcome={r1['outcome']} ✓")

# ── Test 2: SELL (profitable) ─────────────────────────────────────────────────
obs_sell = {
    **BASE,
    "timestamp": 1789880900.0,
    "market": {"id": "mkt-buy", "secondsToClose": 55},
    "account": {
        "cashUsd": 7.39,
        "eligibleVolumeUsd": 0,
        "completedCycles": 0,
        "position": {
            "outcome": "YES",
            "shares": 5.0,
            "buy_gross_usd": 2.50,
            "buy_fees_usd": 0.1125,
            "sell_gross_usd": 0.0,
            "sell_fees_usd": 0.0,
        },
    },
    "reference": None,
    "books": {
        "YES": {"bids": [[0.52, 100]], "asks": [[0.53, 100]]},
        "NO":  {"bids": [[0.47, 100]], "asks": [[0.48, 100]]},
    },
}
r2 = call(obs_sell)
print("SELL test :", json.dumps(r2))
assert r2.get("action") == "SELL", f"Expected SELL, got {r2}"
print("  SELL issued correctly ✓")

# ── Test 3: HOLD (tiny gap < 30 USD) ─────────────────────────────────────────
obs_hold = {
    **BASE,
    "timestamp": 1789880823.619,
    "market": {"id": "mkt-hold", "secondsToClose": 100},
    "account": {"cashUsd": 10.0, "eligibleVolumeUsd": 0, "completedCycles": 0, "position": None},
    "reference": {
        "btcMidUsd": 80430.0,
        "openingTargetUsd": 80424.0,  # gap only $6 → below $30 threshold
        "observedAt": 1789880823.0,
        "targetObservedAt": 1789880814.0,
        "targetProvisional": False,
    },
    "books": BOOKS_BALANCED,
}
r3 = call(obs_hold)
print("HOLD test :", json.dumps(r3))
assert r3.get("action") == "HOLD", f"Expected HOLD, got {r3}"
print("  HOLD (small gap) correctly ✓")

# ── Test 4: forced SELL near market close ──────────────────────────────────────
obs_forced = {
    **BASE,
    "timestamp": 1789880950.0,
    "market": {"id": "mkt-buy", "secondsToClose": 20},  # <60s → forced exit
    "account": {
        "cashUsd": 7.39,
        "eligibleVolumeUsd": 0,
        "completedCycles": 0,
        "position": {
            "outcome": "YES",
            "shares": 5.0,
            "buy_gross_usd": 2.50,
            "buy_fees_usd": 0.1125,
            "sell_gross_usd": 0.0,
            "sell_fees_usd": 0.0,
        },
    },
    "reference": None,
    "books": {
        "YES": {"bids": [[0.45, 100]], "asks": [[0.46, 100]]},  # losing position
        "NO":  {"bids": [[0.47, 100]], "asks": [[0.48, 100]]},
    },
}
r4 = call(obs_forced)
print("FORCE test:", json.dumps(r4))
assert r4.get("action") == "SELL", f"Expected forced SELL, got {r4}"
print("  Forced SELL at market close ✓")

print("\nAll tests passed ✓")
