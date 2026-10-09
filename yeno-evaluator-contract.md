# YeNo × Builderr Volume Bot Challenge

## Fixed target

- Starting paper cash: **$10**
- Maximum cash per BUY, including fees: **$5**
- Official window: **24 hours**
- Target: **$1,000** in eligible volume
- Exposure: one open position or pending BUY
- Market: YeNo BTC five-minute Up/Down
- Finish: terminal flat, with no pending action

Eligible volume is actual gross executed BUY notional plus actual gross executed SELL notional from a fully closed cycle. Partial fills and split exits remain one cycle. Rejections, cancellations, zero fills, settlements, redemptions, and unfinished inventory add zero.

## API

Expose `POST /decide`. The evaluator sends current receive-time L2 books, market time remaining, paper cash, current owned position, completed volume, immutable limits, and the public BTC reference inputs available at that receive time.

The optional `reference` object has this shape:

```json
{
  "btcMidUsd": 80482.155,
  "openingTargetUsd": 80444.946,
  "observedAt": 1789880823.619,
  "targetObservedAt": 1789880814.203,
  "targetProvisional": false
}
```

The same timestamped fields are supplied in development replay and hidden paper evaluation. The evaluator excludes any row whose target was observed after the row's receive time. A bot should reject stale or provisional reference data rather than infer information that was not yet observable.

Return exactly one of:

```json
{"action":"HOLD"}
{"action":"BUY","outcome":"YES","maxCashUsd":5}
{"action":"SELL"}
```

`SELL` means “close the owned position.” The evaluator owns shares, cash, execution, and scoring.

## Execution model

- 250 ms execution latency; orders cross a later L2 update for their own outcome. An update to the opposite outcome cannot reuse a pre-decision quote.
- L2 depth, minimum order size, partial fills, split exits, and executable one-sided books near expiry. A BUY still requires asks; a SELL can execute against bids when asks have disappeared.
- Adjacent-market prefetch rows are grouped into chronological sessions; pre-open warmups and recorder-incomplete markets are excluded.
- Timestamped public BTC midpoint and YeNo opening-target inputs; future-stamped target rows are excluded.
- Books older than two seconds rejected.
- New entries blocked inside the final 15 seconds.
- Dynamic crypto taker fee: `0.07 × shares × price × (1 − price)` on each side, rounded to five decimal places as documented by Polymarket: <https://docs.polymarket.com/trading/fees>.
- The 1% YeNo overlay observed in the test account, applied to gross notional on each side.
- All BUY fees count inside the $5 ceiling.
- Hidden forward-collected paper data for the official 24-hour run.

## Ranking and reward

The official paper run lasts 24 hours. A bot qualifies by producing at least $1,000 of eligible paper volume and finishing with no position or pending action.

Qualifying paper bots rank by simulated settled cash at the first clean target crossing. Exact ties break on lower maximum drawdown, then earlier time to $1,000. The first-place qualifying bot receives the **$500 prize sponsored by YeNo**.

YeNo may offer additional rewards or pilot opportunities to selected bots. These are optional, do not affect competition ranking, and will use separate written terms.

There is no entry fee. This is an online software-building competition. Builders submit only their algorithm. They do not deposit money, receive a trading account, place real orders, or bear trading losses. Builderr owns the paper evaluation, simulated cash, fills, fees, and scoring.

Submit a repository or HTTPS `/decide` endpoint, bot name, contact, and immutable commit or container digest to `submit@builderr.ai`.

Learn about YeNo at <https://yeno.market>.
