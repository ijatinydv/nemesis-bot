# YeNo × Builderr starter kit

Build a trading agent that returns `HOLD`, `BUY`, or `SELL`. YeNo is looking for bots that can generate trading volume while controlling fees, spreads, and losses. This is a free global bot-building competition sponsored by [YeNo](https://yeno.market). You submit code; you do not deposit money, receive a trading account, or place real trades. Builderr supplies the market data, simulated cash, execution, fees, and scoring.

## The challenge

Your bot starts with $10 in simulated cash. It has 24 hours to produce at least $1,000 in completed BUY plus SELL volume. Each BUY may spend at most $5 including fees. Hold at most one position or pending BUY and finish flat.

## Files

- `yeno_competition_starter_bot.py`: minimal working server that always returns `HOLD`.
- `yeno-house-bot-v1.py`: strongest validated development baseline.
- `yeno-house-bot-configs.json`: House Bot v1 plus two alternative tested configurations.
- `yeno-sample-observation.json`: one valid `/decide` request.
- `yeno-smoke-test.py`: checks your local endpoint's response shape and $5 BUY cap.
- `yeno-evaluator-contract.md`: exact input, output, execution, scoring, and reward rules.
- `yeno-house-bot-benchmark.md`: results, limits, and improvement opportunities.

## Start

Run either server:

```bash
python yeno_competition_starter_bot.py
# or
python yeno-house-bot-v1.py
```

Both expose `POST http://127.0.0.1:8080/decide`.

In another terminal, check the endpoint contract:

```bash
python yeno-smoke-test.py
```

The smoke test proves only that the endpoint can receive an observation and return a valid decision. It does not produce a competition score.

Change the strategy logic. Do not change evaluator-owned cash, fills, fees, volume, or position accounting.

Before submitting, we recommend running your bot through a full 24-hour local simulation. This is your development check, not the official score. Builderr runs every submitted bot again for 24 hours on unseen paper-market data.

The first-place qualifying bot wins $500. YeNo sponsors the prize and may offer additional rewards or pilot opportunities to selected bots under separate written terms.

## Submit

Email `submit@builderr.ai` with your bot name, contact, repository or HTTPS `/decide` endpoint, and immutable commit or container digest.
