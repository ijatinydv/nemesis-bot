# YeNo house-bot development benchmark

Date: September 22, 2026

## Contract tested

- $10 starting paper cash
- $5 maximum fee-inclusive BUY
- $1,000 eligible-volume target
- 24-hour evaluation window
- One open position or pending BUY
- Terminal-flat finish required

## Result

House Bot v1 was evaluated on 30 resampled 24-hour paths built from recorded development regimes.

- Median volume across all 30 paths: **$558.83**
- Terminal-flat paths: **18 of 30**
- Median volume among those 18 flat paths: **$679.94**
- Best terminal-flat path: **$942.59**, ending with **$7.13** after 137 completed cycles
- Paths reaching at least $750: **6 of 30**
- Paths reaching $1,000: **0 of 30**

Under a two-cent adverse execution stress, maximum volume fell to **$384.97** and median volume fell to **$70.42**.

## What this means

The house bot is a useful starting point, not a qualified solution. Its strongest observed development run came within $57.41 of the target, but it did not cross $1,000 in the 30-path validation. Candidate selection and path construction reused recorded development regimes, so these numbers are not fresh-forward evidence and are not an official score.

## What builders should improve

1. Detect when the BTC-reference signal is persistent rather than choppy.
2. Avoid adverse fills through better timing and depth-aware sizing.
3. Scale risk using remaining cash, recent fill quality, spread, and drawdown.
4. Stop entering early enough to guarantee a terminal-flat finish.
5. Test passive execution only if it is supported under the same public rules.

The exact source for House Bot v1 and two alternative tested configurations are included in the starter kit.
