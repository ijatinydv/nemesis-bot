"""Local paper evaluator for the YeNo x Builderr volume challenge.

Why this exists: the official evaluator and its recorded data are not public,
so this module re-creates the published contract (fees, 250 ms latency, later
L2 fills, 2 s book staleness, 15 s entry block, $5 BUY cap, one position,
min order size) on top of a parametric synthetic BTC five-minute market.
Scenario knobs control how much exploitable edge the synthetic book leaves.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field, replace
from typing import Any, Callable

TICK = 0.5          # world resolution in seconds
MARKET_SECONDS = 300
WINDOW_SECONDS = 86400
TARGET_VOLUME = 1000.0
MAX_BUY_USD = 5.0
START_CASH = 10.0
MIN_ORDER = 5.0
LATENCY = 0.25
BASE_TS = 1_789_800_000.0


def _phi(x: float) -> float:
    return 0.5 * math.erfc(-x / 1.4142135623730951)


# scenario parameters describing how the synthetic book relates to BTC
@dataclass(frozen=True)
class Scenario:
    name: str = "base"
    vol_ann: float = 0.50          # average annualised BTC volatility
    vol_regime_sd: float = 0.35    # log-sd of hourly volatility regime
    t_df: float = 4.0              # student-t degrees of freedom for returns
    jump_per_hour: float = 2.0
    ar1: float = 0.0               # tick autocorrelation (momentum if > 0)
    mm_lag: float = 1.5            # seconds the book lags the reference
    mm_vol_mult: float = 1.0       # book vol assumption vs truth
    tail_k: float = 1.0            # >1 overconfident book, <1 underconfident
    mm_noise: float = 0.02         # sd of idiosyncratic book mispricing
    mm_noise_tau: float = 20.0     # seconds of persistence of that noise
    update_prob: float = 0.7       # chance a book refreshes on a tick
    spread_extra: float = 0.0      # extra spread added to every book
    depth_median: float = 70.0
    ref_age_stale_prob: float = 0.03
    ref_noise_usd: float = 0.0     # iid noise on the public reference


SCENARIOS = {
    "efficient": Scenario("efficient", mm_lag=0.0, mm_noise=0.004),
    "lag1.5": Scenario("lag1.5", mm_lag=1.5),
    "lag3": Scenario("lag3", mm_lag=3.0, mm_noise=0.03),
    "underconf": Scenario("underconf", mm_lag=0.5, tail_k=0.85),
    "overconf": Scenario("overconf", mm_lag=0.5, tail_k=1.15),
    "momentum": Scenario("momentum", mm_lag=1.0, ar1=0.06),
    "wide": Scenario("wide", mm_lag=2.0, spread_extra=0.01, mm_noise=0.03),
    "refnoise": Scenario("refnoise", mm_lag=2.5, mm_noise=0.03, ref_noise_usd=12.0),
    "weak": Scenario("weak", mm_lag=1.0, mm_noise=0.012),
}


@dataclass
class Result:
    volume: float = 0.0
    cash: float = START_CASH
    flat: bool = True
    crossed: bool = False
    cash_at_cross: float | None = None
    time_to_cross: float | None = None
    max_drawdown: float = 0.0
    cycles: int = 0
    wins: int = 0
    losses_usd: float = 0.0
    rejected: int = 0
    unsold_settlements: int = 0
    queries: int = 0
    end_t: float = 0.0
    fees: float = 0.0


def fee_of(price: float, shares: float) -> float:
    gross = price * shares
    return round(0.07 * shares * price * (1 - price) + 1e-12, 5) + round(gross * 0.01 + 1e-12, 5)


def _book(mid: float, spread: float, tau: float, rng: random.Random, depth_median: float):
    mid = min(0.985, max(0.015, mid))
    bid = math.floor((mid - spread / 2) * 100 + 1e-9) / 100
    bid = min(0.98, max(0.01, bid))
    ask = round(min(0.99, max(bid + 0.01, bid + spread)), 2)
    scale = 0.45 if tau < 20 else (0.75 if tau < 45 else 1.0)
    bids, asks = [], []
    for i in range(4):
        pb = round(bid - 0.01 * i, 2)
        pa = round(ask + 0.01 * i, 2)
        if pb >= 0.01:
            bids.append([pb, round(rng.lognormvariate(math.log(depth_median * scale), 0.9) * (1 + 0.4 * i), 2)])
        if pa <= 0.99:
            asks.append([pa, round(rng.lognormvariate(math.log(depth_median * scale), 0.9) * (1 + 0.4 * i), 2)])
    if tau < 20 and rng.random() < 0.10:
        # near expiry the loser side frequently loses its bids
        if mid < 0.1:
            bids = []
        if mid > 0.9:
            asks = []
    return bids, asks


def run_path(bot_factory: Callable[[], Any], seed: int, sc: Scenario, *, cadence: float = 1.0,
             hours: float = 24.0, stop_at_target: bool = True, verbose: bool = False) -> Result:
    rng = random.Random(seed * 7919 + 13)       # world randomness
    steps = int(hours * 3600 / TICK)
    per_q = max(1, int(round(cadence / TICK)))
    res = Result()
    bot = bot_factory()

    price0 = 80000.0 * math.exp(rng.gauss(0, 0.05))
    logp = math.log(price0)
    hist = [price0]
    sigma_reg = sc.vol_ann
    reg_log = 0.0
    phase = rng.uniform(0, MARKET_SECONDS)
    prev_ret = 0.0
    nu = sc.t_df
    tscale = math.sqrt((nu - 2) / nu)

    # market state
    market_idx = -1
    target = price0
    market_open_t = 0.0
    target_obs_delay = 9.0
    noise = 0.0
    books = {"YES": ([], [], -10.0), "NO": ([], [], -10.0)}
    updated = {"YES": False, "NO": False}

    # account
    cash = START_CASH
    pos: dict | None = None   # outcome, shares, buy_gross, buy_fees, sell_gross, sell_fees
    pending: dict | None = None
    volume = 0.0
    cycles = 0
    peak = START_CASH
    cycle_cash_before = START_CASH
    cycle_buy_gross = 0.0
    cycle_sell_gross = 0.0
    last_flat_cash = START_CASH

    def settle_value():
        gap = hist[-1] - target
        return gap >= 0

    for step in range(steps):
        t = step * TICK
        ts = BASE_TS + t
        # hourly vol regime drifts
        if step % int(3600 / TICK) == 0:
            reg_log = 0.7 * reg_log + 0.714 * rng.gauss(0, sc.vol_regime_sd)
            sigma_reg = sc.vol_ann * math.exp(reg_log)
        sigma_sec = sigma_reg / math.sqrt(31_536_000)
        eps = rng.gauss(0, 1)
        if nu < 50:
            chi = rng.gammavariate(nu / 2, 2 / nu)
            eps = eps / math.sqrt(chi) * tscale
        ret = sigma_sec * math.sqrt(TICK) * eps + sc.ar1 * prev_ret
        if rng.random() < sc.jump_per_hour * TICK / 3600:
            ret += rng.choice((-1, 1)) * rng.uniform(2, 5) * sigma_sec * math.sqrt(10)
        prev_ret = ret
        logp += ret
        price = math.exp(logp)
        hist.append(price)

        # market roll
        idx = int((t + phase) // MARKET_SECONDS)
        if idx != market_idx:
            if pos is not None:
                # unsold position settles at the end of the market, no volume credited
                won = (hist[-1] - target >= 0) == (pos["outcome"] == "YES")
                cash += pos["shares"] * (1.0 if won else 0.0)
                res.unsold_settlements += 1
                pos = None
                cycles += 1
                res.losses_usd += max(0.0, cycle_cash_before - cash)
            pending = None
            market_idx = idx
            market_open_t = t
            target = price + rng.gauss(0, 1.5)
            target_obs_delay = rng.uniform(6, 14)
            noise = 0.0
            books = {"YES": ([], [], -10.0), "NO": ([], [], -10.0)}
        tau = MARKET_SECONDS - ((t + phase) - idx * MARKET_SECONDS)

        # book refresh: mm prices off lagged reference
        noise += (-noise * TICK / sc.mm_noise_tau) + sc.mm_noise * math.sqrt(2 * TICK / sc.mm_noise_tau) * rng.gauss(0, 1)
        lag_steps = int(round(sc.mm_lag / TICK))
        lag_px = hist[max(0, len(hist) - 1 - lag_steps)]
        sig_mm = sc.mm_vol_mult * sigma_reg / math.sqrt(31_536_000) * lag_px / 1.0
        # sigma in dollars per sqrt second
        sig_mm = sc.mm_vol_mult * sigma_reg / math.sqrt(31_536_000) * lag_px
        z = sc.tail_k * (lag_px - target) / (sig_mm * math.sqrt(max(tau, 0.5) + 0.5))
        p_yes = _phi(z)
        for oc in ("YES", "NO"):
            updated[oc] = False
            if rng.random() < sc.update_prob or books[oc][2] < 0:
                mid = p_yes + noise if oc == "YES" else (1 - p_yes) - noise + rng.gauss(0, 0.004)
                mid = min(0.985, max(0.015, mid))
                near_mid = 1 - abs(2 * mid - 1)
                u = rng.random()
                spread = 0.01 if u < 0.62 else (0.02 if u < 0.9 else 0.03)
                spread += int(round(sc.spread_extra * 100)) * 0.01 * (1 if near_mid > 0.3 else 0.5)
                if tau < 25 and rng.random() < 0.4:
                    spread += 0.01
                b, a = _book(mid, round(spread, 2), tau, rng, sc.depth_median)
                books[oc] = (b, a, t)
                updated[oc] = True

        # execute a pending order against a later update of its own outcome
        if pending is not None and t >= pending["t"] + LATENCY - 1e-9:
            oc = pending["outcome"]
            if updated[oc] or t - pending["t"] > 5:
                b, a, bt = books[oc]
                age_ok = (t - bt) <= 2.0
                if pending["type"] == "BUY":
                    if tau <= 0 or not age_ok:
                        res.rejected += 1
                    else:
                        cap = min(pending["cap"], MAX_BUY_USD, cash)
                        shares_got = 0.0
                        gross = 0.0
                        fees = 0.0
                        fills = []
                        for pxl, size in sorted(map(tuple, a)):
                            per = pxl + 0.07 * pxl * (1 - pxl) + 0.01 * pxl
                            room = cap - gross - fees
                            can = min(size, math.floor(room / per * 100 + 1e-9) / 100)
                            if can <= 0:
                                break
                            fills.append((pxl, can))
                            gross += pxl * can
                            fees = sum(fee_of(p, q) for p, q in fills)
                            shares_got += can
                            if can < size:
                                break
                        # recompute fee-inclusive affordability after rounding
                        while shares_got > 0 and gross + fees > cap + 1e-9:
                            px_last, q_last = fills[-1]
                            dq = min(q_last, 0.01)
                            fills[-1] = (px_last, q_last - dq)
                            if fills[-1][1] <= 1e-9:
                                fills.pop()
                            shares_got = sum(q for _, q in fills)
                            gross = sum(p * q for p, q in fills)
                            fees = sum(fee_of(p, q) for p, q in fills)
                        if shares_got + 1e-9 < MIN_ORDER:
                            res.rejected += 1
                        else:
                            cash -= gross + fees
                            res.fees += fees
                            pos = {"outcome": oc, "shares": round(shares_got, 2), "buy_gross_usd": gross,
                                   "buy_fees_usd": fees, "sell_gross_usd": 0.0, "sell_fees_usd": 0.0,
                                   "avgPrice": gross / shares_got}
                            cycle_cash_before = cash + gross + fees
                            cycle_buy_gross = gross
                            cycle_sell_gross = 0.0
                    pending = None
                else:  # SELL
                    if pos is None or not age_ok:
                        res.rejected += 1
                    else:
                        remaining = pos["shares"]
                        got = 0.0
                        gross = 0.0
                        fills = []
                        for pxl, size in sorted(map(tuple, b), reverse=True):
                            q = min(size, remaining - got)
                            if q <= 0:
                                break
                            fills.append((pxl, q))
                            got += q
                            gross += pxl * q
                        if got <= 1e-9:
                            res.rejected += 1
                        else:
                            fees = sum(fee_of(p, q) for p, q in fills)
                            cash += gross - fees
                            res.fees += fees
                            pos["sell_gross_usd"] += gross
                            pos["sell_fees_usd"] += fees
                            pos["shares"] = round(pos["shares"] - got, 2)
                            cycle_sell_gross += gross
                            if pos["shares"] < 1e-6:
                                volume += cycle_buy_gross + cycle_sell_gross
                                cycles += 1
                                pnl = cash - cycle_cash_before
                                if pnl >= 0:
                                    res.wins += 1
                                else:
                                    res.losses_usd += -pnl
                                pos = None
                    pending = None

        peak = max(peak, cash if pos is None else cash)
        # drawdown measured on cash while flat (conservative proxy for settled cash)
        if pos is None:
            res.max_drawdown = max(res.max_drawdown, peak - cash)
            if volume >= TARGET_VOLUME and pending is None and not res.crossed:
                res.crossed = True
                res.cash_at_cross = cash
                res.time_to_cross = t
                if stop_at_target:
                    res.end_t = t
                    break

        # query the bot
        if step % per_q == 0 and tau > 0:
            res.queries += 1
            ref_age = TICK * (1 if rng.random() < 0.6 else 2)
            if rng.random() < sc.ref_age_stale_prob:
                ref_age = rng.uniform(2.0, 6.0)
            ref_idx = max(0, len(hist) - 1 - int(round(ref_age / TICK)))
            elapsed_m = t - market_open_t
            provisional = elapsed_m < target_obs_delay + 2.0
            obs = {
                "schemaVersion": 1,
                "timestamp": ts,
                "market": {"id": f"m{market_idx}", "secondsToClose": round(tau, 3)},
                "account": {
                    "cashUsd": round(cash, 6),
                    "eligibleVolumeUsd": round(volume, 6),
                    "completedCycles": cycles,
                    "position": None if pos is None else dict(pos),
                },
                "rules": {"maximumBuyCashUsd": MAX_BUY_USD, "targetVolumeUsd": TARGET_VOLUME,
                          "evaluationWindowHours": 24, "onePositionAtATime": True,
                          "buyFeesIncludedInMaximum": True},
                "reference": {
                    "btcMidUsd": hist[ref_idx] + (rng.gauss(0, sc.ref_noise_usd) if sc.ref_noise_usd else 0.0),
                    "openingTargetUsd": target,
                    "observedAt": BASE_TS + (ref_idx * TICK),
                    "targetObservedAt": BASE_TS + market_open_t + target_obs_delay,
                    "targetProvisional": provisional,
                },
                "books": {
                    oc: {"bids": books[oc][0], "asks": books[oc][1], "minOrderSize": MIN_ORDER,
                         "updatedAt": BASE_TS + books[oc][2]}
                    for oc in ("YES", "NO")
                },
            }
            try:
                act = bot.decide(obs)
            except Exception:
                act = {"action": "HOLD"}
            kind = act.get("action") if isinstance(act, dict) else None
            if kind == "BUY":
                ok = (pos is None and pending is None and tau > 15 and act.get("outcome") in ("YES", "NO")
                      and 0 < float(act.get("maxCashUsd", 0)) <= MAX_BUY_USD + 1e-9
                      and float(act["maxCashUsd"]) <= cash + 1e-9
                      and (t - books[act["outcome"]][2]) <= 2.0)
                if ok:
                    pending = {"type": "BUY", "t": t, "outcome": act["outcome"], "cap": float(act["maxCashUsd"])}
                else:
                    res.rejected += 1
            elif kind == "SELL":
                if pos is not None and pending is None:
                    pending = {"type": "SELL", "t": t, "outcome": pos["outcome"]}
                else:
                    res.rejected += 1
    else:
        res.end_t = steps * TICK

    res.volume = volume
    res.cash = cash
    res.cycles = cycles
    res.flat = pos is None and pending is None
    return res
