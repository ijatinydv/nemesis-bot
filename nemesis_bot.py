#!/usr/bin/env python3
"""NEMESIS: a self-calibrating volume bot for the YeNo x Builderr challenge.

The hidden evaluator data is unseen, so fixed thresholds are a guess. NEMESIS
instead runs a bank of candidate strategies ("arms") as free shadow traders on
every observation it receives, prices their fills with the published fee and
latency rules, and lets only the arm with the best fee-adjusted evidence place
live orders. A pace controller widens the arm set when volume lags schedule,
and a hard guard layer keeps the account flat, capped and deadline-safe.

Standard library only. Serves POST /decide on 127.0.0.1:8080.
"""

from __future__ import annotations

import json
import math
import os
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MAX_BUY_USD = 5.0
MIN_ORDER_SHARES = 5.0
LATENCY_SECONDS = 0.25
ENTRY_BLOCK_SECONDS = 15.0
EVAL_WINDOW_SECONDS = 86400.0
FINAL_NO_ENTRY_SECONDS = 240.0
FINAL_FORCE_EXIT_SECONDS = 75.0

MAX_REFERENCE_AGE = 1.5
MAX_BOOK_AGE = 1.8
VOL_PRIOR_REL = 8.9e-5
VOL_HALF_LIFE_SECONDS = 900.0
MODEL_SHARPNESS = 0.92

SHADOW_SLIP = 0.004
MAX_LOSS_PER_CYCLE_USD = 1.2
STRONG_ARM_SCORE = 0.01
WEAK_ARM_CAP_USD = 3.5
WARMUP_SECONDS = 2400.0
RERANK_SECONDS = 20.0
STATS_HALF_LIFE_SECONDS = 21600.0
PRIOR_CYCLES = 10.0
PRIOR_MEAN = -0.02
PRIOR_CYCLE_SD_USD = 0.35
KAPPA = 1.0
MIN_EFFECTIVE_CYCLES = 10.0
GATE_SCORE = -1.0
LIVE_WEIGHT = 2.0
MAX_CYCLES_PER_MARKET = 4
SHADOW_COOLDOWN = 3.0
LIVE_COOLDOWN = 2.0
BUY_PENDING_SECONDS = 1.6
SELL_RESEND_SECONDS = 0.8


# tolerant parser so one malformed book row never breaks the decision
def _levels(rows: Any, *, reverse: bool) -> list[tuple[float, float]]:
    output: list[tuple[float, float]] = []
    for row in rows if isinstance(rows, list) else []:
        try:
            price, size = float(row[0]), float(row[1])
        except (IndexError, TypeError, ValueError):
            continue
        if 0 < price < 1 and size > 0:
            output.append((price, size))
    output.sort(reverse=reverse)
    return output


# mirrors the published two-layer fee so local cost estimates match the evaluator
def _fee(price: float, shares: float) -> float:
    return round(0.07 * shares * price * (1 - price) + 1e-12, 5) + round(price * shares * 0.01 + 1e-12, 5)


# prices a fee-inclusive cash-capped buy by walking asks so sizing matches evaluator fills
def _buy_walk(asks: list[tuple[float, float]], cap: float, slip: float = 0.0) -> tuple[float, float, float]:
    spent = 0.0
    shares = 0.0
    gross = 0.0
    fees = 0.0
    for price, size in asks:
        price = min(0.995, price + slip)
        unit = price + 0.07 * price * (1 - price) + 0.01 * price
        take = min(size, math.floor((cap - spent) / unit * 100 + 1e-9) / 100)
        if take <= 0:
            break
        shares += take
        gross += take * price
        fees += _fee(price, take)
        spent = gross + fees
        if take < size:
            break
    return shares, gross, fees


# prices a sell of up to the given shares against bids so exits respect real depth
def _sell_walk(bids: list[tuple[float, float]], shares: float, slip: float = 0.0) -> tuple[float, float, float]:
    left = shares
    got = 0.0
    gross = 0.0
    fees = 0.0
    for price, size in bids:
        price = max(0.005, price - slip)
        take = min(size, left)
        if take <= 0:
            break
        got += take
        gross += take * price
        fees += _fee(price, take)
        left -= take
    return got, gross, fees


# standard normal cdf used to turn the reference gap into a win probability
def _phi(x: float) -> float:
    return 0.5 * math.erfc(-x / 1.4142135623730951)


# fixed grid of candidate strategies so learning has a finite, auditable search space
def _build_arms() -> list["Arm"]:
    entries: list[tuple[str, dict[str, float]]] = []
    for edge in (0.05, 0.09, 0.13, 0.18):
        for lo, hi in ((15, 90), (15, 200)):
            entries.append(("edge", {"e": edge, "lo": lo, "hi": hi, "pmax": 0.93}))
    for zmin in (0.7, 1.2, 2.0):
        entries.append(("trend", {"z": zmin, "lo": 15, "hi": 90, "pmax": 0.93}))
    for mom in (1.5, 2.5):
        entries.append(("burst", {"m": mom, "lo": 15, "hi": 120, "pmax": 0.90}))
    for zmin in (1.5, 2.5):
        entries.append(("lock", {"z": zmin, "lo": 15, "hi": 40, "pmax": 0.96}))
    exits = (
        {"tp": 0.0, "sl": 0.10, "hold": 60.0, "tx": 12.0, "mq": None},
        {"tp": 0.02, "sl": 0.06, "hold": 30.0, "tx": 12.0, "mq": None},
        {"tp": 9.0, "sl": 0.15, "hold": 9999.0, "tx": 8.0, "mq": None},
        {"tp": 9.0, "sl": 0.15, "hold": 9999.0, "tx": 8.0, "mq": 0.0},
        {"tp": 9.0, "sl": 0.12, "hold": 9999.0, "tx": 8.0, "mq": 0.03},
    )
    arms = []
    for kind, params in entries:
        for ex in exits:
            arms.append(Arm(len(arms), kind, params, ex))
    return arms


# one candidate strategy plus its shadow position and realised statistics
class Arm:
    __slots__ = ("id", "kind", "p", "x", "state", "side", "shares", "buy_gross", "buy_fees", "sell_gross",
                 "sell_fees", "t_dec", "t_entry", "cool_until", "market_cycles", "cycles", "score", "n_eff",
                 "rate", "name")

    # builds an arm in the flat state with an empty history
    def __init__(self, ident: int, kind: str, params: dict[str, float], exit_rule: dict[str, float]) -> None:
        self.id = ident
        self.kind = kind
        self.p = params
        self.x = exit_rule
        self.cycles: deque = deque(maxlen=400)
        self.score = PRIOR_MEAN
        self.n_eff = 0.0
        self.rate = 0.0
        self.name = f"{kind}:" + ",".join(f"{k}={v}" for k, v in params.items()) + "|" + ",".join(
            f"{k}={v}" for k, v in exit_rule.items())
        self.reset_position()
        self.cool_until = 0.0
        self.market_cycles = 0

    # clears the shadow position after a completed, failed or settled cycle
    def reset_position(self) -> None:
        self.state = 0
        self.side = ""
        self.shares = 0.0
        self.buy_gross = 0.0
        self.buy_fees = 0.0
        self.sell_gross = 0.0
        self.sell_fees = 0.0
        self.t_dec = 0.0
        self.t_entry = 0.0


# per-side snapshot of prices, depth and model quantities for one observation
class View:
    __slots__ = ("ask", "bid", "spread", "asks", "bids", "z", "q", "edge", "mom", "shares", "unit")

    # stores only what entry rules need so evaluating ~70 arms stays cheap
    def __init__(self) -> None:
        self.ask = 1.0
        self.bid = 0.0
        self.spread = 1.0
        self.asks: list[tuple[float, float]] = []
        self.bids: list[tuple[float, float]] = []
        self.z = 0.0
        self.q = 0.5
        self.edge = -1.0
        self.mom = 0.0
        self.shares = 0.0
        self.unit = 1.0


# the adaptive agent: shadow bank, ranker, pace controller and live executor
class NemesisBot:
    # initialises all learning state so a restart simply re-learns safely
    def __init__(self) -> None:
        self.arms = _build_arms()
        self.start_ts: float | None = None
        self.market_id: str | None = None
        self.sigma_rel = VOL_PRIOR_REL
        self.ref_hist: deque = deque(maxlen=64)
        self.last_ref_obs = 0.0
        self.last_ref_px = 0.0
        self.last_gap = 0.0
        self.last_rerank = -1e9
        self.active: list[Arm] = []
        self.delta = 0.005
        self.last_pace_check = 0.0
        self.live_arm: Arm | None = None
        self.live_cash_before = 0.0
        self.live_vol_before = 0.0
        self.live_entry_t = 0.0
        self.live_buy_at = -1e9
        self.live_sell_at = -1e9
        self.live_flat_at = -1e9
        self.live_market_cycles = 0
        self.had_position = False
        self.live_cycles = 0
        self.live_pnl = 0.0
        self.live_fail_streak = 0
        self.volume_seen = 0.0

    # never raises so a bug degrades to HOLD instead of an invalid decision
    def decide(self, obs: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._decide(obs)
        except Exception:
            return {"action": "HOLD"}

    # one observation: update models, advance shadows, then make the live call
    def _decide(self, obs: dict[str, Any]) -> dict[str, Any]:
        now = float(obs["timestamp"])
        market = obs["market"]
        account = obs["account"]
        tau = float(market["secondsToClose"])
        market_id = str(market["id"])
        position = account.get("position")
        cash = float(account["cashUsd"])
        volume = float(account.get("eligibleVolumeUsd", 0.0))
        self.volume_seen = volume
        target_volume = float(obs.get("rules", {}).get("targetVolumeUsd", 1000.0))
        books = obs["books"]
        if self.start_ts is None:
            self.start_ts = now
        elapsed = now - self.start_ts
        remaining = EVAL_WINDOW_SECONDS - elapsed

        if market_id != self.market_id:
            self._roll_market(market_id, now)

        ref_ok = self._update_reference(obs, now)
        ctx_views = self._build_views(books, obs, tau, now, ref_ok)
        self._step_shadows(ctx_views, tau, now, ref_ok)
        if now - self.last_rerank >= RERANK_SECONDS:
            self._rerank(now, elapsed, volume, target_volume)

        self._track_live(position, cash, volume, now)

        if position is not None:
            return self._live_exit(position, ctx_views, books, tau, now, remaining)
        if volume >= target_volume - 1e-9:
            return {"action": "HOLD"}
        return self._live_entry(ctx_views, cash, tau, now, elapsed, remaining, ref_ok, books)

    # resets per-market counters and settles any shadow inventory left at the close
    def _roll_market(self, market_id: str, now: float) -> None:
        for arm in self.arms:
            if arm.state in (2, 3) and arm.shares > 1e-9:
                won = (self.last_gap >= 0) == (arm.side == "YES")
                value = arm.shares * (1.0 if won else 0.0)
                pnl = arm.sell_gross - arm.sell_fees + value - arm.buy_gross - arm.buy_fees
                arm.cycles.append((now, pnl, arm.buy_gross + arm.sell_gross, 1.0))
            arm.reset_position()
            arm.market_cycles = 0
            arm.cool_until = 0.0
        self.market_id = market_id
        self.live_market_cycles = 0
        self.live_buy_at = -1e9
        self.last_gap = 0.0

    # tracks the reference and an ewma volatility, returning whether it is usable now
    def _update_reference(self, obs: dict[str, Any], now: float) -> bool:
        ref = obs.get("reference")
        if not isinstance(ref, dict):
            return False
        px = float(ref["btcMidUsd"])
        seen = float(ref["observedAt"])
        if px <= 0:
            return False
        if seen > self.last_ref_obs + 1e-9:
            if self.last_ref_px > 0 and 0.15 <= seen - self.last_ref_obs <= 6.0:
                dt = seen - self.last_ref_obs
                ret = math.log(px / self.last_ref_px)
                cap = 6.0 * self.sigma_rel * math.sqrt(dt)
                ret = max(-cap, min(cap, ret))
                weight = 1.0 - 0.5 ** (dt / VOL_HALF_LIFE_SECONDS)
                var = (1 - weight) * self.sigma_rel ** 2 + weight * (ret * ret / dt)
                self.sigma_rel = min(4e-4, max(2e-5, math.sqrt(var)))
            self.last_ref_obs = seen
            self.last_ref_px = px
            self.ref_hist.append((seen, px))
        if bool(ref.get("targetProvisional", True)):
            return False
        if float(ref.get("targetObservedAt", math.inf)) > now + 1e-9:
            return False
        if now - seen > MAX_REFERENCE_AGE:
            return False
        return True

    # builds per-side prices and model values for this observation
    def _build_views(self, books: dict, obs: dict, tau: float, now: float, ref_ok: bool) -> dict[str, View]:
        views: dict[str, View] = {}
        gap = 0.0
        sigma_usd = 0.0
        age = 0.0
        px = 0.0
        if ref_ok:
            ref = obs["reference"]
            px = float(ref["btcMidUsd"])
            gap = px - float(ref["openingTargetUsd"])
            self.last_gap = gap
            age = max(0.0, now - float(ref["observedAt"]))
            sigma_usd = self.sigma_rel * px
        tau_eff = max(1.0, tau + age)
        mom_usd = 0.0
        if ref_ok and self.ref_hist:
            newest = self.ref_hist[-1]
            for seen, old in reversed(self.ref_hist):
                if newest[0] - seen >= 3.0:
                    mom_usd = newest[1] - old
                    break
        for side, sign in (("YES", 1.0), ("NO", -1.0)):
            raw = books.get(side)
            if not isinstance(raw, dict):
                continue
            asks = _levels(raw.get("asks"), reverse=False)
            bids = _levels(raw.get("bids"), reverse=True)
            view = View()
            view.asks = asks
            view.bids = bids
            if asks:
                view.ask = asks[0][0]
            if bids:
                view.bid = bids[0][0]
            view.spread = view.ask - view.bid if asks and bids else 1.0
            shares, gross, fees = _buy_walk(asks, MAX_BUY_USD)
            view.shares = shares
            view.unit = (gross + fees) / shares if shares > 0 else 1.0
            if ref_ok and sigma_usd > 0:
                view.z = sign * gap / (sigma_usd * math.sqrt(tau_eff))
                view.q = _phi(MODEL_SHARPNESS * view.z)
                if shares > 0:
                    exit_fee = 0.01 * view.q + 0.07 * view.q * (1 - view.q)
                    view.edge = view.q - exit_fee - view.unit
                view.mom = sign * mom_usd / (sigma_usd * math.sqrt(3.0))
            views[side] = view
        return views

    # decides which side the gap favours, used by directional entry rules
    @staticmethod
    def _lead(views: dict[str, View]) -> str | None:
        yes, no = views.get("YES"), views.get("NO")
        if yes is None or no is None:
            return None
        return "YES" if yes.z >= no.z else "NO"

    # evaluates one arm's entry rule and returns the side to buy or None
    def _entry_side(self, arm: Arm, views: dict[str, View], tau: float) -> str | None:
        p = arm.p
        if not (p["lo"] < tau <= p["hi"]):
            return None
        if arm.kind == "edge":
            best = None
            for side, view in views.items():
                if view.shares < MIN_ORDER_SHARES or view.ask > p["pmax"] or view.ask < 0.05 or view.spread > 0.04:
                    continue
                if view.edge >= p["e"] and (best is None or view.edge > best[0]):
                    best = (view.edge, side)
            return best[1] if best else None
        side = self._lead(views)
        if side is None:
            return None
        view = views[side]
        if view.shares < MIN_ORDER_SHARES or view.ask > p["pmax"] or view.spread > 0.04:
            return None
        if arm.kind == "trend":
            return side if view.z >= p["z"] else None
        if arm.kind == "burst":
            return side if (view.mom >= p["m"] and view.z >= 0.2) else None
        if arm.kind == "lock":
            return side if view.z >= p["z"] else None
        return None

    # evaluates an exit rule for any position, real or shadow, and returns True to sell
    @staticmethod
    def _exit_due(x: dict[str, Any], shares: float, paid: float, received: float, view: View | None,
                  tau: float, held: float, slip: float) -> bool:
        if tau <= x["tx"]:
            return True
        if view is None:
            return False
        got, gross, fees = _sell_walk(view.bids, shares, slip)
        pnl = received + gross - fees - paid
        if got + 1e-9 < shares:
            return pnl <= -min(x["sl"] * shares, MAX_LOSS_PER_CYCLE_USD) + 1e-9
        if pnl <= -min(x["sl"] * shares, MAX_LOSS_PER_CYCLE_USD) + 1e-9:
            return True
        if x["tp"] < 5 and pnl >= x["tp"] * shares - 1e-9:
            return True
        if x["mq"] is not None and view.edge > -0.9:
            hold_value = view.q - (0.01 * view.q + 0.07 * view.q * (1 - view.q)) - x["mq"]
            if (gross - fees) / shares >= hold_value:
                return True
        return held >= x["hold"]

    # advances every shadow arm one observation with latency-aware fills
    def _step_shadows(self, views: dict[str, View], tau: float, now: float, ref_ok: bool) -> None:
        for arm in self.arms:
            state = arm.state
            if state == 1:
                if now < arm.t_dec + LATENCY_SECONDS - 1e-9:
                    continue
                view = views.get(arm.side)
                shares, gross, fees = _buy_walk(view.asks, MAX_BUY_USD, SHADOW_SLIP) if view else (0.0, 0.0, 0.0)
                if shares + 1e-9 < MIN_ORDER_SHARES or tau <= 0:
                    arm.reset_position()
                    arm.cool_until = now + SHADOW_COOLDOWN
                    continue
                arm.shares, arm.buy_gross, arm.buy_fees = shares, gross, fees
                arm.t_entry = now
                arm.state = state = 2
            elif state == 3:
                if now < arm.t_dec + LATENCY_SECONDS - 1e-9:
                    continue
                view = views.get(arm.side)
                got, gross, fees = _sell_walk(view.bids, arm.shares, SHADOW_SLIP) if view else (0.0, 0.0, 0.0)
                arm.sell_gross += gross
                arm.sell_fees += fees
                arm.shares -= got
                if arm.shares <= 1e-9:
                    pnl = arm.sell_gross - arm.sell_fees - arm.buy_gross - arm.buy_fees
                    arm.cycles.append((now, pnl, arm.buy_gross + arm.sell_gross, 1.0))
                    arm.reset_position()
                    arm.cool_until = now + SHADOW_COOLDOWN
                    arm.market_cycles += 1
                    continue
                arm.state = state = 2
            if state == 2:
                paid = arm.buy_gross + arm.buy_fees
                received = arm.sell_gross - arm.sell_fees
                forced = tau <= arm.x["tx"]
                if forced or self._exit_due(arm.x, arm.shares, paid, received, views.get(arm.side), tau,
                                            now - arm.t_entry, SHADOW_SLIP):
                    arm.state = 3
                    arm.t_dec = now
            elif state == 0 and ref_ok:
                if arm.market_cycles >= MAX_CYCLES_PER_MARKET or now < arm.cool_until:
                    continue
                side = self._entry_side(arm, views, tau)
                if side is not None:
                    arm.state = 1
                    arm.side = side
                    arm.t_dec = now

    # scores arms by shrunk fee-adjusted return per dollar of volume and picks the live set
    def _rerank(self, now: float, elapsed: float, volume: float, target_volume: float) -> None:
        self.last_rerank = now
        for arm in self.arms:
            num = 0.0
            den = 0.0
            sq = 0.0
            n_eff = 0.0
            first = None
            for stamp, pnl, vol, weight in arm.cycles:
                w = weight * 0.5 ** ((now - stamp) / STATS_HALF_LIFE_SECONDS)
                num += w * pnl
                den += w * vol
                n_eff += w
                if first is None:
                    first = stamp
            prior_vol = PRIOR_CYCLES * 9.0
            mu = (num + PRIOR_MEAN * prior_vol) / (den + prior_vol)
            for stamp, pnl, vol, weight in arm.cycles:
                w = weight * 0.5 ** ((now - stamp) / STATS_HALF_LIFE_SECONDS)
                sq += (w * (pnl - mu * vol)) ** 2
            se = math.sqrt(sq + PRIOR_CYCLES * PRIOR_CYCLE_SD_USD ** 2) / (den + prior_vol)
            arm.score = mu - KAPPA * se
            arm.n_eff = n_eff
            span = max(900.0, min(elapsed, 4 * 3600.0))
            arm.rate = len(arm.cycles) / span * 3600.0 if arm.cycles else 0.0
        eligible = [a for a in self.arms if a.n_eff >= MIN_EFFECTIVE_CYCLES]
        if not eligible:
            self.active = []
            return
        eligible.sort(key=lambda a: a.score, reverse=True)
        schedule = min(1.0, elapsed / (18.0 * 3600.0)) * target_volume
        if now - self.last_pace_check >= 600.0:
            self.last_pace_check = now
            if volume < 0.85 * schedule:
                self.delta = min(0.06, self.delta + 0.005)
            elif volume > 1.1 * schedule:
                self.delta = max(0.0, self.delta - 0.005)
        best = eligible[0].score
        floor = max(best - self.delta, GATE_SCORE)
        self.active = [a for a in eligible if a.score >= floor][:12]

    # follows the real account so live results feed the arm statistics
    def _track_live(self, position: Any, cash: float, volume: float, now: float) -> None:
        if position is not None:
            self.had_position = True
            return
        if self.had_position:
            self.had_position = False
            pnl = cash - self.live_cash_before
            vol = volume - self.live_vol_before
            self.live_cycles += 1
            self.live_pnl += pnl
            self.live_flat_at = now
            self.live_market_cycles += 1
            if self.live_arm is not None:
                gross = vol if vol > 0 else 9.0
                self.live_arm.cycles.append((now, pnl, gross, LIVE_WEIGHT))
            self.live_arm = None

    # manages the live position with the arm's exit rule or a safe default
    def _live_exit(self, position: dict, views: dict[str, View], books: dict, tau: float, now: float,
                   remaining: float) -> dict[str, Any]:
        side = str(position["outcome"])
        shares = float(position["shares"])
        paid = float(position["buy_gross_usd"]) + float(position["buy_fees_usd"])
        received = float(position.get("sell_gross_usd", 0.0)) - float(position.get("sell_fees_usd", 0.0))
        rule = self.live_arm.x if self.live_arm is not None else {"tp": 0.0, "sl": 0.10, "hold": 60.0, "tx": 12.0, "mq": None}
        if now - self.live_sell_at < SELL_RESEND_SECONDS:
            return {"action": "HOLD"}
        view = views.get(side)
        if view is None:
            raw = books.get(side, {})
            view = View()
            view.bids = _levels(raw.get("bids"), reverse=True)
        urgent = tau <= rule["tx"] or remaining <= FINAL_FORCE_EXIT_SECONDS
        held = now - self.live_entry_t if self.live_entry_t else 0.0
        if urgent or self._exit_due(rule, shares, paid, received, view, tau, held, 0.0):
            self.live_sell_at = now
            return {"action": "SELL"}
        return {"action": "HOLD"}

    # picks the first active arm that fires and sizes the order to the cash cap
    def _live_entry(self, views: dict[str, View], cash: float, tau: float, now: float, elapsed: float,
                    remaining: float, ref_ok: bool, books: dict) -> dict[str, Any]:
        if not ref_ok or not self.active or elapsed < WARMUP_SECONDS:
            return {"action": "HOLD"}
        if remaining <= FINAL_NO_ENTRY_SECONDS or tau <= ENTRY_BLOCK_SECONDS + 1.0:
            return {"action": "HOLD"}
        if now - self.live_buy_at < BUY_PENDING_SECONDS or now - self.live_flat_at < LIVE_COOLDOWN:
            return {"action": "HOLD"}
        if self.live_market_cycles >= MAX_CYCLES_PER_MARKET:
            return {"action": "HOLD"}
        cap = min(MAX_BUY_USD, cash - 0.005)
        if cap <= 0:
            return {"action": "HOLD"}
        for arm in self.active:
            side = self._entry_side(arm, views, tau)
            if side is None:
                continue
            order_cap = cap
            if arm.score < STRONG_ARM_SCORE:
                order_cap = min(cap, max(WEAK_ARM_CAP_USD, MIN_ORDER_SHARES * 1.04 * views[side].unit))
            shares, gross, fees = _buy_walk(views[side].asks, order_cap)
            if shares + 1e-9 < MIN_ORDER_SHARES:
                continue
            self.live_arm = arm
            self.live_cash_before = cash
            self.live_vol_before = self.volume_seen
            self.live_entry_t = now
            self.live_buy_at = now
            return {"action": "BUY", "outcome": side, "maxCashUsd": round(order_cap, 4)}
        return {"action": "HOLD"}

    # exposes a compact status snapshot for debugging and tests
    def status(self) -> dict[str, Any]:
        top = sorted(self.arms, key=lambda a: a.score, reverse=True)[:5]
        return {
            "live_cycles": self.live_cycles,
            "live_pnl": round(self.live_pnl, 4),
            "sigma_rel": self.sigma_rel,
            "delta": self.delta,
            "active": [a.name for a in self.active],
            "top": [(a.name, round(a.score, 4), round(a.n_eff, 1)) for a in top],
        }


BOT = NemesisBot()
LOCK = threading.Lock()


# http adapter exposing the evaluator contract and a health probe
class Handler(BaseHTTPRequestHandler):
    # serves decisions as compact json and answers 400 on malformed requests
    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/decide":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            with LOCK:
                response = BOT.decide(payload)
            body = json.dumps(response, separators=(",", ":")).encode()
        except Exception:
            self.send_error(400)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # lets operators confirm the process is alive and inspect learning state
    def do_GET(self) -> None:  # noqa: N802
        with LOCK:
            body = json.dumps(BOT.status() if self.path == "/status" else {"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # silences per-request logging that would slow a 24 hour run
    def log_message(self, _format: str, *_args: Any) -> None:
        return


if __name__ == "__main__":
    ThreadingHTTPServer((os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
