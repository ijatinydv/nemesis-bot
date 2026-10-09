"""Contract and safety tests for the NEMESIS bot.

Run with: python3 -m unittest nemesis_test -v
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

import nemesis_bot as nb

SAMPLE = json.loads(Path(__file__).with_name("yeno-sample-observation.json").read_text())


# builds a valid observation with a strong YES signal for a given clock and market
def make_obs(ts: float, tau: float, *, market: str = "m1", cash: float = 10.0, volume: float = 0.0,
             position: dict | None = None, gap: float = 120.0, yes_ask: float = 0.70) -> dict:
    obs = copy.deepcopy(SAMPLE)
    obs["timestamp"] = ts
    obs["market"] = {"id": market, "secondsToClose": tau}
    obs["account"].update({"cashUsd": cash, "eligibleVolumeUsd": volume, "position": position})
    obs["reference"].update({"btcMidUsd": 80000.0 + gap, "openingTargetUsd": 80000.0,
                             "observedAt": ts - 0.4, "targetObservedAt": ts - 60.0, "targetProvisional": False})
    obs["books"]["YES"].update({"bids": [[yes_ask - 0.01, 200]], "asks": [[yes_ask, 200]]})
    obs["books"]["NO"].update({"bids": [[0.28, 200]], "asks": [[0.29, 200]]})
    return obs


# returns a bot whose first arm is promoted so live entry paths can be exercised
def armed_bot() -> nb.NemesisBot:
    bot = nb.NemesisBot()
    arm = bot.arms[0]
    arm.p = {"e": -1.0, "lo": 15, "hi": 280, "pmax": 0.95}
    bot.active = [arm]
    bot.start_ts = 1_000_000.0 - nb.WARMUP_SECONDS - 10
    bot.last_rerank = 1e12
    return bot


class NemesisContractTests(unittest.TestCase):
    # the published sample must produce a schema-valid decision
    def test_sample_observation_is_valid(self) -> None:
        decision = nb.NemesisBot().decide(SAMPLE)
        self.assertIn(decision["action"], {"HOLD", "BUY", "SELL"})

    # garbage input must degrade to HOLD rather than an error or invalid order
    def test_malformed_input_holds(self) -> None:
        bot = nb.NemesisBot()
        self.assertEqual(bot.decide({}), {"action": "HOLD"})
        self.assertEqual(bot.decide({"timestamp": "x", "market": 5}), {"action": "HOLD"})

    # every BUY must respect the five dollar fee-inclusive cap and available cash
    def test_buy_respects_cap_and_cash(self) -> None:
        for cash in (10.0, 4.2, 3.0):
            bot = armed_bot()
            decision = bot.decide(make_obs(1_000_000.0, 80.0, cash=cash))
            if decision["action"] == "BUY":
                self.assertIn(decision["outcome"], {"YES", "NO"})
                self.assertLessEqual(decision["maxCashUsd"], min(5.0, cash))
                self.assertGreater(decision["maxCashUsd"], 0)

    # no new entries when too little cash can fund the minimum order
    def test_no_buy_when_cash_cannot_fund_min_order(self) -> None:
        bot = armed_bot()
        self.assertEqual(bot.decide(make_obs(1_000_000.0, 80.0, cash=1.0))["action"], "HOLD")

    # no entries inside the final fifteen seconds of a market
    def test_no_entry_in_final_seconds(self) -> None:
        bot = armed_bot()
        self.assertEqual(bot.decide(make_obs(1_000_000.0, 14.0))["action"], "HOLD")

    # warmup keeps the account idle while shadows learn
    def test_warmup_holds(self) -> None:
        bot = armed_bot()
        bot.start_ts = 1_000_000.0 - 5
        self.assertEqual(bot.decide(make_obs(1_000_000.0, 80.0))["action"], "HOLD")

    # open positions must be sold before the market closes
    def test_forced_exit_near_close(self) -> None:
        bot = armed_bot()
        position = {"outcome": "YES", "shares": 5.0, "buy_gross_usd": 3.5, "buy_fees_usd": 0.08,
                    "sell_gross_usd": 0.0, "sell_fees_usd": 0.0}
        decision = bot.decide(make_obs(1_000_000.0, 6.0, position=position, cash=6.0))
        self.assertEqual(decision["action"], "SELL")

    # a stop loss must fire even when displayed depth cannot absorb the full position
    def test_stop_loss_with_thin_depth(self) -> None:
        bot = armed_bot()
        position = {"outcome": "YES", "shares": 9.0, "buy_gross_usd": 6.0, "buy_fees_usd": 0.1,
                    "sell_gross_usd": 0.0, "sell_fees_usd": 0.0}
        obs = make_obs(1_000_000.0, 70.0, position=position, cash=2.0, yes_ask=0.40)
        obs["books"]["YES"]["bids"] = [[0.39, 3]]
        self.assertEqual(bot.decide(obs)["action"], "SELL")

    # once the volume target is reached and flat the bot must stop trading
    def test_holds_after_target(self) -> None:
        bot = armed_bot()
        self.assertEqual(bot.decide(make_obs(1_000_000.0, 80.0, volume=1000.0))["action"], "HOLD")

    # near the end of the evaluation window no new entry may be opened
    def test_no_entry_near_window_end(self) -> None:
        bot = armed_bot()
        bot.start_ts = 1_000_000.0 - nb.EVAL_WINDOW_SECONDS + 100
        self.assertEqual(bot.decide(make_obs(1_000_000.0, 80.0))["action"], "HOLD")

    # positions are liquidated when the evaluation window is about to close
    def test_exit_near_window_end(self) -> None:
        bot = armed_bot()
        bot.start_ts = 1_000_000.0 - nb.EVAL_WINDOW_SECONDS + 30
        position = {"outcome": "YES", "shares": 5.0, "buy_gross_usd": 3.5, "buy_fees_usd": 0.08,
                    "sell_gross_usd": 0.0, "sell_fees_usd": 0.0}
        self.assertEqual(bot.decide(make_obs(1_000_000.0, 200.0, position=position, cash=6.0))["action"], "SELL")

    # a position with unparseable fields must still be closed instead of being held forever
    def test_unreadable_position_sells(self) -> None:
        bot = armed_bot()
        obs = make_obs(1_000_000.0, 100.0, position={"outcome": "YES"}, cash=6.0)
        self.assertEqual(bot.decide(obs)["action"], "SELL")

    # a provisional or stale reference must never produce an entry
    def test_bad_reference_blocks_entry(self) -> None:
        bot = armed_bot()
        obs = make_obs(1_000_000.0, 80.0)
        obs["reference"]["targetProvisional"] = True
        self.assertEqual(bot.decide(obs)["action"], "HOLD")
        bot = armed_bot()
        obs = make_obs(1_000_000.0, 80.0)
        obs["reference"]["observedAt"] = 1_000_000.0 - 10.0
        self.assertEqual(bot.decide(obs)["action"], "HOLD")

    # a BUY needs asks, so empty ask books must hold
    def test_no_buy_without_asks(self) -> None:
        bot = armed_bot()
        obs = make_obs(1_000_000.0, 80.0)
        obs["books"]["YES"]["asks"] = []
        obs["books"]["NO"]["asks"] = []
        self.assertEqual(bot.decide(obs)["action"], "HOLD")

    # a forced exit must work against a bids-only book near expiry
    def test_sell_with_bids_only(self) -> None:
        bot = armed_bot()
        position = {"outcome": "YES", "shares": 5.0, "buy_gross_usd": 3.5, "buy_fees_usd": 0.08,
                    "sell_gross_usd": 0.0, "sell_fees_usd": 0.0}
        obs = make_obs(1_000_000.0, 4.0, position=position, cash=6.0)
        obs["books"]["YES"]["asks"] = []
        self.assertEqual(bot.decide(obs)["action"], "SELL")

    # a market rollover with a repeated clock must not crash or return an invalid action
    def test_rollover_and_repeated_timestamps(self) -> None:
        bot = armed_bot()
        for market, tau in (("m1", 20.0), ("m1", 20.0), ("m2", 290.0), ("m2", 289.0)):
            self.assertIn(bot.decide(make_obs(1_000_000.0, tau, market=market))["action"], {"HOLD", "BUY", "SELL"})


if __name__ == "__main__":
    unittest.main()
