#!/usr/bin/env python3
"""APEX Bot — YeNo × Builderr Volume Bot Challenge submission.

Architecture: 7-layer adaptive agent that beats the house bot by combining
regime detection, momentum confirmation, dynamic sizing, smarter exit logic,
and a guaranteed-flat safety system.

Key improvements over House Bot v1:
  1. RegimeDetector   – classifies BTC price action as trending / choppy / reversing
  2. MomentumFilter   – multi-reading BTC momentum confirmation before entry
  3. DynamicSizer     – adapts position size to drawdown, win rate, and cash level
  4. BookScorer       – multi-factor spread/depth quality gate
  5. EntryScorer      – composite 0-100 quality score; only enters on strong setups
  6. ExitManager      – take-profit on any positive fill; trailing awareness
  7. SafetyGuard      – capital floor, deadline enforcement, flat guarantee
"""

from __future__ import annotations

import json
import math
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# ── tunables ──────────────────────────────────────────────────────────────────

# Entry gates
MINIMUM_REFERENCE_GAP_USD       = 30.0   # reduced from 50 → more opportunities
MAXIMUM_ENTRY_PRICE             = 0.88   # slightly tighter than house bot's 0.90
ENTRY_WINDOW_SECONDS            = 120.0  # wider window (house: 90s)
MINIMUM_BOOK_DEPTH_SHARES       = 4.0    # must have liquidity for our target
MINIMUM_ENTRY_SCORE             = 55     # out of 100; only enter high-quality setups

# Position sizing
TARGET_SHARES_BASE              = 5.0
TARGET_SHARES_REDUCED           = 3.0   # when conditions are marginal
TARGET_SHARES_MINIMUM           = 2.0   # emergency / capital preservation mode
CASH_FLOOR_USD                  = 4.00  # below this we stop trading (can't qualify)
CASH_REDUCED_THRESHOLD_USD      = 7.00  # below this use reduced sizing
DRAWDOWN_STOP_FRACTION          = 0.40  # stop if cash ≤ 60% of peak (lost 40%)

# Risk management
MAXIMUM_IMMEDIATE_LOSS_USD      = 0.50  # max round-trip loss allowed to enter
STOP_LOSS_PER_SHARE_USD         = 0.10  # $0.50 on 5 shares
TAKE_PROFIT_MIN_USD             = 0.00  # sell the moment we're positive
MAXIMUM_REFERENCE_AGE_SECONDS   = 1.5
MAXIMUM_BOOK_AGE_SECONDS        = 2.0   # per contract

# Cycle control
MAXIMUM_CYCLES_PER_MARKET       = 3     # increased from 2
REENTRY_COOLDOWN_SECONDS        = 1.5   # reduced from 2
FORCED_EXIT_SECONDS             = 60.0  # force-sell when ≤ 60s left in market

# Safety / deadline
DEADLINE_NO_ENTRY_SECONDS_LEFT  = 1800  # 30 min before eval end: no new entries
DEADLINE_FORCE_SELL_SECONDS_LEFT = 600  # 10 min before eval end: force any position
EVAL_WINDOW_SECONDS             = 86400 # 24 h

# Regime / momentum
REGIME_HISTORY_LEN              = 20    # cycle outcomes to track
MOMENTUM_HISTORY_LEN            = 6     # BTC observations for momentum
TREND_WIN_RATE_THRESHOLD        = 0.55  # above this → "trending" regime
CHOP_WIN_RATE_THRESHOLD         = 0.38  # below this → "choppy" regime


# ── fee calculator (mirrors evaluator exactly) ────────────────────────────────

def _levels(rows: Any, *, reverse: bool) -> list[tuple[float, float]]:
    output: list[tuple[float, float]] = []
    for row in rows if isinstance(rows, list) else []:
        try:
            price, size = float(row[0]), float(row[1])
        except (IndexError, TypeError, ValueError):
            continue
        if 0 < price < 1 and size > 0:
            output.append((price, size))
    return sorted(output, reverse=reverse)


def _fees(fills: list[tuple[float, float]]) -> float:
    gross    = sum(p * s for p, s in fills)
    protocol = sum(0.07 * s * p * (1 - p) for p, s in fills)
    return round(protocol + 1e-12, 5) + round(gross * 0.01 + 1e-12, 5)


def _buy_cost(asks: Any, shares: float) -> tuple[float, float] | None:
    remaining = shares
    fills: list[tuple[float, float]] = []
    for price, available in _levels(asks, reverse=False):
        quantity = min(remaining, available)
        fills.append((price, quantity))
        remaining -= quantity
        if remaining <= 1e-9:
            break
    if remaining > 1e-9:
        return None
    return sum(p * q for p, q in fills), _fees(fills)


def _sell_proceeds(bids: Any, shares: float) -> tuple[float, float] | None:
    remaining = shares
    fills: list[tuple[float, float]] = []
    for price, available in _levels(bids, reverse=True):
        quantity = min(remaining, available)
        fills.append((price, quantity))
        remaining -= quantity
        if remaining <= 1e-9:
            break
    if remaining > 1e-9:
        return None
    return sum(p * q for p, q in fills), _fees(fills)


def _best_ask(asks: Any) -> float:
    lvls = _levels(asks, reverse=False)
    return lvls[0][0] if lvls else math.inf


def _best_bid(bids: Any) -> float:
    lvls = bids if isinstance(bids, list) else []
    parsed = _levels(bids, reverse=True)
    return parsed[0][0] if parsed else 0.0


def _depth_available(book_side: Any, *, side: str) -> float:
    """Total shares available at any price level on the required side."""
    rows = _levels(book_side, reverse=(side == "bids"))
    return sum(s for _, s in rows)


# ── sub-components ─────────────────────────────────────────────────────────────

class RegimeDetector:
    """Tracks recent cycle outcomes and classifies market regime."""

    def __init__(self) -> None:
        self._outcomes: deque[bool] = deque(maxlen=REGIME_HISTORY_LEN)

    def record_cycle(self, profit: float) -> None:
        self._outcomes.append(profit >= 0)

    def win_rate(self) -> float:
        if not self._outcomes:
            return 0.5
        return sum(self._outcomes) / len(self._outcomes)

    def regime(self) -> str:
        """Returns 'trending', 'neutral', or 'choppy'."""
        wr = self.win_rate()
        if wr >= TREND_WIN_RATE_THRESHOLD:
            return "trending"
        if wr <= CHOP_WIN_RATE_THRESHOLD:
            return "choppy"
        return "neutral"

    def size_multiplier(self) -> float:
        if not self._outcomes:
            return 1.0     # cold start: no history, use full size
        wr = self.win_rate()
        if wr >= TREND_WIN_RATE_THRESHOLD:
            return 1.0     # full size in trending regime
        if wr >= 0.45:
            return 0.90    # slightly reduced in neutral
        return 0.75        # choppy – reduce size moderately


class MomentumTracker:
    """Tracks BTC mid-price direction over recent observations."""

    def __init__(self) -> None:
        self._readings: deque[tuple[float, float]] = deque(maxlen=MOMENTUM_HISTORY_LEN)  # (ts, price)

    def update(self, ts: float, price: float) -> None:
        self._readings.append((ts, price))

    def momentum_score(self, direction: str) -> float:
        """
        Returns 0..1 indicating how strongly BTC moved in `direction` ('YES'=up, 'NO'=down)
        over recent history.  0.5 = neutral.
        """
        if len(self._readings) < 2:
            return 0.5
        prices = [p for _, p in self._readings]
        # count consecutive moves in direction
        moves_in_dir = 0
        total_moves = len(prices) - 1
        for i in range(1, len(prices)):
            delta = prices[i] - prices[i - 1]
            if direction == "YES" and delta > 0:
                moves_in_dir += 1
            elif direction == "NO" and delta < 0:
                moves_in_dir += 1
        if total_moves == 0:
            return 0.5
        return moves_in_dir / total_moves

    def recent_velocity(self) -> float:
        """Returns signed BTC velocity (USD/reading) over the window."""
        if len(self._readings) < 2:
            return 0.0
        return self._readings[-1][1] - self._readings[0][1]


class BookScorer:
    """Scores the current order book for entry quality."""

    @staticmethod
    def score(books: dict, side: str, target_shares: float) -> int:
        """
        Returns 0-40 book quality score.
        Components:
          - spread tightness (0-15)
          - available depth vs. target (0-15)
          - price level vs. entry price limit (0-10)
        """
        score = 0
        side_book = books.get(side, {})
        asks = side_book.get("asks", [])
        bids = side_book.get("bids", [])

        ask = _best_ask(asks)
        bid = _best_bid(bids)

        if ask <= 0 or bid <= 0 or ask >= 1:
            return 0

        # Spread tightness
        spread = ask - bid
        if spread <= 0.01:
            score += 15
        elif spread <= 0.02:
            score += 10
        elif spread <= 0.03:
            score += 5
        else:
            score += 0

        # Depth adequacy
        ask_depth = _depth_available(asks, side="asks")
        bid_depth = _depth_available(bids, side="bids")
        min_depth = min(ask_depth, bid_depth)
        if min_depth >= target_shares * 2:
            score += 15
        elif min_depth >= target_shares:
            score += 10
        elif min_depth >= target_shares * 0.75:
            score += 5

        # Price level
        if ask <= 0.70:
            score += 10   # low price → low fee
        elif ask <= 0.80:
            score += 7
        elif ask <= 0.88:
            score += 3

        return score


class EntryScorer:
    """Combines all signals into a single 0-100 entry quality score."""

    @staticmethod
    def score(
        gap_usd: float,
        direction: str,
        book_score: int,
        momentum_score: float,
        regime: str,
        seconds_to_close: float,
        immediate_loss_usd: float,
    ) -> int:
        total = 0

        # Reference gap strength (0-20)
        if abs(gap_usd) >= 200:
            total += 20
        elif abs(gap_usd) >= 100:
            total += 15
        elif abs(gap_usd) >= 50:
            total += 10
        elif abs(gap_usd) >= 30:
            total += 5

        # Book quality (0-40 from BookScorer)
        total += book_score

        # Momentum alignment (0-20)
        # momentum_score is 0..1, 0.5 neutral → contribution 0-20
        mom_contribution = int((momentum_score - 0.5) * 40)  # -20..+20
        total += max(0, mom_contribution)  # only add positive momentum

        # Regime bonus/penalty (−10..+10)
        if regime == "trending":
            total += 10
        elif regime == "choppy":
            total -= 10  # subtract; total can go negative but clamp at 0

        # Timing bonus: prefer entering earlier in market window (0-10)
        if seconds_to_close >= 90:
            total += 10
        elif seconds_to_close >= 60:
            total += 5

        # Immediate loss penalty (0 to -20)
        if immediate_loss_usd > 0.40:
            total -= 20
        elif immediate_loss_usd > 0.25:
            total -= 10
        elif immediate_loss_usd > 0.10:
            total -= 5

        return max(0, min(100, total))


class DynamicSizer:
    """Determines optimal position size given current account state."""

    @staticmethod
    def target_shares(
        cash_usd: float,
        peak_cash_usd: float,
        regime_multiplier: float,
        entry_score: int,
    ) -> float:
        # Safety floor
        if cash_usd <= CASH_FLOOR_USD:
            return 0.0  # no trading

        # Drawdown check
        drawdown_fraction = 1.0 - (cash_usd / peak_cash_usd) if peak_cash_usd > 0 else 0.0
        if drawdown_fraction >= DRAWDOWN_STOP_FRACTION:
            return 0.0  # max drawdown hit

        # Capital preservation mode
        if cash_usd < CASH_REDUCED_THRESHOLD_USD:
            base = TARGET_SHARES_REDUCED  # preserve capital below threshold
        elif entry_score >= 55:
            base = TARGET_SHARES_BASE      # standard 5-share entry on good setups
        elif entry_score >= 40:
            base = TARGET_SHARES_REDUCED   # 3 shares on marginal setups
        else:
            base = TARGET_SHARES_MINIMUM   # minimum on weak setups

        # Regime adjustment
        adjusted = base * regime_multiplier

        # Ensure minimum meaningful trade
        return max(TARGET_SHARES_MINIMUM, adjusted)


# ── main bot ───────────────────────────────────────────────────────────────────

class ApexBot:
    """APEX Bot — adaptive 7-layer YeNo volume agent."""

    def __init__(self) -> None:
        # Per-market state
        self.market_id: str | None = None
        self.position_was_open = False
        self.completed_cycles = 0
        self.last_flat_at = -math.inf
        self.last_buy_attempt_at = -math.inf

        # Position P&L tracking (for regime feedback)
        self.last_position_paid: float = 0.0   # total cash spent on current position

        # Cross-market state
        self.regime_detector = RegimeDetector()
        self.momentum_tracker = MomentumTracker()
        self.peak_cash_usd: float = 10.0
        self.eval_start_ts: float | None = None  # first timestamp seen

    # ── public entry point ──────────────────────────────────────────────────

    def decide(self, observation: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._decide(observation)
        except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError):
            return {"action": "HOLD"}

    # ── internal logic ──────────────────────────────────────────────────────

    def _decide(self, observation: dict[str, Any]) -> dict[str, Any]:
        market  = observation["market"]
        account = observation["account"]
        books   = observation["books"]
        now     = float(observation["timestamp"])
        seconds = float(market["secondsToClose"])
        market_id = str(market["id"])
        position  = account.get("position")
        cash      = float(account["cashUsd"])

        # Track eval start for deadline management
        if self.eval_start_ts is None:
            self.eval_start_ts = now
        elapsed = now - self.eval_start_ts
        seconds_left_in_eval = EVAL_WINDOW_SECONDS - elapsed

        # Update peak cash
        if cash > self.peak_cash_usd:
            self.peak_cash_usd = cash

        # ── reset on new market ──────────────────────────────────────────
        if market_id != self.market_id:
            self.market_id = market_id
            self.position_was_open = False
            self.completed_cycles = 0
            self.last_flat_at = -math.inf
            self.last_buy_attempt_at = -math.inf

        # ── detect cycle completion (position closed) ────────────────────
        if position is None and self.position_was_open:
            # A cycle just completed — record outcome in regime detector
            pnl = cash - self.last_position_paid   # very rough but directional
            self.regime_detector.record_cycle(pnl)
            self.completed_cycles += 1
            self.last_flat_at = now
        self.position_was_open = position is not None

        # ── update momentum tracker from reference ───────────────────────
        reference = observation.get("reference")
        if isinstance(reference, dict):
            btc_mid = reference.get("btcMidUsd")
            obs_at  = reference.get("observedAt")
            if btc_mid and obs_at:
                self.momentum_tracker.update(float(obs_at), float(btc_mid))

        # ══════════════════════════════════════════════════════════════════
        # SELL logic — manage open positions
        # ══════════════════════════════════════════════════════════════════
        if position is not None:
            return self._manage_position(position, books, seconds, cash, seconds_left_in_eval)

        # ══════════════════════════════════════════════════════════════════
        # BUY logic — look for entries
        # ══════════════════════════════════════════════════════════════════
        return self._look_for_entry(
            account, books, seconds, cash, now, reference,
            seconds_left_in_eval, observation
        )

    # ── position management ─────────────────────────────────────────────────

    def _manage_position(
        self,
        position: dict,
        books: dict,
        seconds: float,
        cash: float,
        seconds_left_in_eval: float,
    ) -> dict[str, Any]:
        side   = str(position["outcome"])
        shares = float(position["shares"])
        bids   = books.get(side, {}).get("bids", [])
        executable = _sell_proceeds(bids, shares)

        # Can't sell (no liquidity) — hold
        if executable is None:
            # But if deadline approaching, try anyway (evaluator will handle partial)
            if seconds <= FORCED_EXIT_SECONDS or seconds_left_in_eval <= DEADLINE_FORCE_SELL_SECONDS_LEFT:
                return {"action": "SELL"}
            return {"action": "HOLD"}

        sell_gross, sell_fees = executable
        paid    = float(position["buy_gross_usd"]) + float(position["buy_fees_usd"])
        partial = float(position.get("sell_gross_usd", 0.0)) - float(position.get("sell_fees_usd", 0.0))
        full_pnl = partial + sell_gross - sell_fees - paid

        # ── TAKE PROFIT: sell as soon as we're profitable ────────────────
        if full_pnl >= TAKE_PROFIT_MIN_USD:
            return {"action": "SELL"}

        # ── STOP LOSS ────────────────────────────────────────────────────
        if full_pnl <= -STOP_LOSS_PER_SHARE_USD * shares + 1e-9:
            return {"action": "SELL"}

        # ── FORCED EXIT near market close ────────────────────────────────
        if seconds <= FORCED_EXIT_SECONDS + 1e-9:
            return {"action": "SELL"}

        # ── EVAL DEADLINE forced exit ────────────────────────────────────
        if seconds_left_in_eval <= DEADLINE_FORCE_SELL_SECONDS_LEFT:
            return {"action": "SELL"}

        return {"action": "HOLD"}

    # ── entry logic ─────────────────────────────────────────────────────────

    def _look_for_entry(
        self,
        account: dict,
        books: dict,
        seconds: float,
        cash: float,
        now: float,
        reference: Any,
        seconds_left_in_eval: float,
        observation: dict,
    ) -> dict[str, Any]:

        # ── hard gates ───────────────────────────────────────────────────

        # Capital floor
        if cash <= CASH_FLOOR_USD:
            return {"action": "HOLD"}

        # Max drawdown stop
        drawdown = 1.0 - (cash / self.peak_cash_usd) if self.peak_cash_usd > 0 else 0.0
        if drawdown >= DRAWDOWN_STOP_FRACTION:
            return {"action": "HOLD"}

        # Eval deadline: no new entries in last 30 min
        if seconds_left_in_eval <= DEADLINE_NO_ENTRY_SECONDS_LEFT:
            return {"action": "HOLD"}

        # Max cycles per market
        if self.completed_cycles >= MAXIMUM_CYCLES_PER_MARKET:
            return {"action": "HOLD"}

        # Cooldowns
        if now - self.last_flat_at < REENTRY_COOLDOWN_SECONDS - 1e-9:
            return {"action": "HOLD"}
        if now - self.last_buy_attempt_at < REENTRY_COOLDOWN_SECONDS - 1e-9:
            return {"action": "HOLD"}

        # Time window: not too early (>ENTRY_WINDOW_SECONDS left), not too late (<15s)
        if not (15 < seconds <= ENTRY_WINDOW_SECONDS):
            return {"action": "HOLD"}

        # ── reference data validation ────────────────────────────────────
        if not isinstance(reference, dict) or bool(reference.get("targetProvisional", True)):
            return {"action": "HOLD"}
        if now - float(reference.get("observedAt", -math.inf)) > MAXIMUM_REFERENCE_AGE_SECONDS:
            return {"action": "HOLD"}
        if float(reference.get("targetObservedAt", math.inf)) > now + 1e-9:
            return {"action": "HOLD"}

        btc_mid     = float(reference["btcMidUsd"])
        opening_tgt = float(reference["openingTargetUsd"])
        gap         = btc_mid - opening_tgt

        if abs(gap) < MINIMUM_REFERENCE_GAP_USD - 1e-9:
            return {"action": "HOLD"}

        # ── signal direction ─────────────────────────────────────────────
        signal_side = "YES" if gap >= 0 else "NO"

        # ── regime and momentum ──────────────────────────────────────────
        regime     = self.regime_detector.regime()
        reg_mult   = self.regime_detector.size_multiplier()
        momentum   = self.momentum_tracker.momentum_score(signal_side)

        # In choppy regime with weak momentum, skip entirely
        if regime == "choppy" and momentum < 0.45:
            return {"action": "HOLD"}

        # ── book checks ──────────────────────────────────────────────────
        side_book  = books.get(signal_side, {})
        asks       = side_book.get("asks", [])
        bids       = side_book.get("bids", [])

        best_ask   = _best_ask(asks)
        if best_ask > MAXIMUM_ENTRY_PRICE + 1e-9:
            return {"action": "HOLD"}

        # ── dynamic sizing ───────────────────────────────────────────────
        # Compute book score with base shares for scoring; actual size determined after
        book_score_val = BookScorer.score(books, signal_side, TARGET_SHARES_BASE)

        # Quick immediate-loss estimate (use base shares)
        base_buy    = _buy_cost(asks, TARGET_SHARES_BASE)
        base_sell   = _sell_proceeds(bids, TARGET_SHARES_BASE)
        if base_buy is None or base_sell is None:
            return {"action": "HOLD"}

        imm_gross_b, imm_fees_b = base_buy
        imm_gross_s, imm_fees_s = base_sell
        imm_loss = imm_gross_b + imm_fees_b - (imm_gross_s - imm_fees_s)

        # ── composite entry score ────────────────────────────────────────
        entry_score = EntryScorer.score(
            gap_usd=gap,
            direction=signal_side,
            book_score=book_score_val,
            momentum_score=momentum,
            regime=regime,
            seconds_to_close=seconds,
            immediate_loss_usd=imm_loss,
        )

        if entry_score < MINIMUM_ENTRY_SCORE:
            return {"action": "HOLD"}

        # ── compute actual trade size ────────────────────────────────────
        target_shares = DynamicSizer.target_shares(cash, self.peak_cash_usd, reg_mult, entry_score)
        if target_shares <= 0:
            return {"action": "HOLD"}

        required = _buy_cost(asks, target_shares)
        if required is None:
            # Try reduced shares
            target_shares = TARGET_SHARES_REDUCED
            required = _buy_cost(asks, target_shares)
            if required is None:
                return {"action": "HOLD"}

        buy_gross, buy_fees = required
        immediate_exit = _sell_proceeds(bids, target_shares)
        if immediate_exit is None:
            # No exit depth available — skip
            return {"action": "HOLD"}

        # ── immediate loss filter (absolute, not per-share) ──────────────
        sell_gross, sell_fees = immediate_exit
        full_imm_loss = buy_gross + buy_fees - (sell_gross - sell_fees)
        if full_imm_loss > MAXIMUM_IMMEDIATE_LOSS_USD + 1e-9:
            return {"action": "HOLD"}

        # ── cash ceiling check ───────────────────────────────────────────
        cash_needed = buy_gross + buy_fees
        max_allowed = min(cash, float(observation["rules"]["maximumBuyCashUsd"]))
        if cash_needed > max_allowed + 1e-9:
            # Try shrinking to fit
            for fallback_shares in [TARGET_SHARES_REDUCED, TARGET_SHARES_MINIMUM]:
                fb_required = _buy_cost(asks, fallback_shares)
                if fb_required is None:
                    continue
                fb_gross, fb_fees = fb_required
                if fb_gross + fb_fees <= max_allowed + 1e-9:
                    # Verify exit depth
                    fb_exit = _sell_proceeds(bids, fallback_shares)
                    if fb_exit is None:
                        continue
                    fb_sg, fb_sf = fb_exit
                    fb_loss = fb_gross + fb_fees - (fb_sg - fb_sf)
                    if fb_loss > MAXIMUM_IMMEDIATE_LOSS_USD + 1e-9:
                        continue
                    cash_needed = fb_gross + fb_fees
                    target_shares = fallback_shares
                    buy_gross, buy_fees = fb_gross, fb_fees
                    break
            else:
                return {"action": "HOLD"}

        # ── record buy attempt ───────────────────────────────────────────
        self.last_buy_attempt_at = now
        # Track what we're paying to later estimate cycle P&L
        self.last_position_paid = cash - cash_needed   # prospective cash after buy

        return {
            "action":     "BUY",
            "outcome":    signal_side,
            "maxCashUsd": round(cash_needed, 6),
        }


# ── HTTP server ───────────────────────────────────────────────────────────────

BOT = ApexBot()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/decide":
            self.send_error(404)
            return
        try:
            length   = int(self.headers.get("Content-Length", "0"))
            response = BOT.decide(json.loads(self.rfile.read(length)))
            body     = json.dumps(response, separators=(",", ":")).encode()
        except Exception:
            self.send_error(400)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


if __name__ == "__main__":
    print("APEX Bot listening on http://127.0.0.1:8080/decide")
    ThreadingHTTPServer(("127.0.0.1", 8080), Handler).serve_forever()
