# Kalshi Multi-Asset Mispricing Bot

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](requirements.txt)
[![Deploy](https://github.com/utkarshp845/kalshi-trading-bot/actions/workflows/deploy.yml/badge.svg)](https://github.com/utkarshp845/kalshi-trading-bot/actions/workflows/deploy.yml)

An automated trading bot for [Kalshi](https://kalshi.com) BTC and ETH price-level markets. It prices each contract against a log-normal model calibrated from realized volatility and Deribit implied vol, trades when the modeled edge clears a dynamic, self-adjusting hurdle, and sizes positions with a fractional-Kelly, drawdown-aware risk engine. Every cycle's inputs, decisions, and outcomes are persisted to SQLite so the whole strategy can be replayed and audited offline.

This is a real, running project — not a backtest demo. It trades a small live account, and the codebase and this README are updated as the strategy evolves.

> **⚠️ Disclaimer:** This project is for educational and research purposes. It is not financial advice, and nothing here is a recommendation to trade Kalshi markets, cryptocurrency, or anything else. Prediction markets and crypto are volatile; you can lose money you put at risk, including all of it. The strategy, thresholds, and risk limits in this repo reflect one person's ongoing experiment, not a validated or guaranteed-profitable system. Read the code, understand the risk model, and use your own judgment before running this against a funded account.

## What It Does

Each cycle (roughly once a minute) the bot:

1. Fetches BTC/ETH spot price, realized vol, and trailing drift from Kraken
2. Pulls Kalshi market quotes, public orderbook depth, and Deribit ATM implied vol
3. Blends realized and implied vol into an adaptive, self-calibrating volatility estimate
4. Builds a theoretical fair-value probability for every open strike via a log-normal (Black-Scholes-style) binary pricer
5. Filters out markets that are structurally unsafe to trade — wide spreads, near-certain outcomes, thin order books, inconsistent strike chains, imminent expiry
6. Scores surviving candidates against a dynamic edge hurdle that adapts to recent fill quality, slippage, and model error
7. Sizes the trade with a portfolio-aware risk engine — fractional Kelly, per-symbol and account-wide daily caps, same-asset correlation discounting, and a graduated drawdown throttle that scales sizing down (and eventually halts) as losses accumulate
8. Executes maker-first (post at the bid, fall back to crossing the spread only if the edge still clears after fees) in `live`, simulates the same path in `paper`, or just records the decision in `observe`
9. Persists every asset snapshot, market feature, decision, execution attempt, fill, and settled outcome — enough to replay any historical cycle through today's strategy code

See [docs/strategy.md](docs/strategy.md) for the full pricing derivation and the mathematical background.

## Lessons Learned

This project has gone through several rounds of "the bot is too safe to ever trade" followed by "here's why, and here's the fix." Some of the more interesting ones, in roughly chronological order:

- **Filters that are individually reasonable can jointly reject everything.** A spread filter, a probability band, a minimum time-to-expiry, and a sigma-distance cap each sound like sane risk controls in isolation. Stacked together, they can and did produce days with thousands of markets evaluated and zero trades placed. This has happened more than once (May, June, July) as thresholds crept conservative again after tuning — it's a recurring failure mode worth watching for, not a one-time bug.
- **A percentage-of-mid spread test structurally excludes cheap contracts.** A 2¢ absolute spread on a 4¢ contract is a "50% spread" — it fails almost any relative spread test, despite being perfectly tradeable. Since the whole point of a binary-outcome market is that cheap, far-out-of-the-money contracts carry the best risk/reward (small capped loss, large multiple on a win), a pct-spread test alone was quietly filtering out exactly the trades the strategy is meant to find. Fix: waive the relative test below a small absolute-spread threshold.
- **Config drift between code defaults and the deployed `.env` silently undoes tuning.** Every strategy parameter is environment-overridable, and the production `.env` had several values pinned from months earlier. Multiple sessions of deliberate threshold changes in `config.py` had zero effect in production because the `.env` file was still shadowing them. The fix wasn't just updating the file once — it was shrinking `.env` down to only genuinely machine-specific values (credentials, mode, monitoring, and explicitly-called-out deviations) so the code's defaults are the actual source of truth, and drift can't silently reoccur.
- **A silent per-cycle cost can look like "slow markets" instead of a bug.** The daily report and a settlement-outcome backfill both ran unthrottled on every cycle. As the database grew, full-table-scan report queries and a wall-clock-unbounded backfill loop stretched a 60-second poll interval into an effective 7.5-minute cycle — with no error, no crash, and no log line calling it out. The fix was adding per-phase timing instrumentation to every cycle; once the timing was visible, the two offenders (a 4-minute report regen, a 200-second backfill loop) were obvious. Lesson: instrument before you optimize — guessing at what's slow wastes far more time than logging it.
- **Real order execution has more edge cases than the pricing model.** Partial fills, maker-order fee accounting, `post_only` rejections needing a taker fallback, stale quotes during a slow fill, and 5xx/429 retries all needed dedicated fixes after the model itself was already "correct." Getting the theoretical edge right is necessary but nowhere near sufficient for a live trading system.
- **A feedback-based score input can deadlock itself with no way out.** The score gate discounted a signal by its recent measured maker-fill rate, with no time bound on the lookback window. Once the bot stopped placing orders for any reason, the fill-rate stat froze on stale attempts — measuring 0% forever — and the scoring formula multiplied `raw_score` by that 0%, rejecting every signal outright regardless of edge size. That meant no new orders were ever attempted, so the frozen stat could never update: a closed loop with no self-recovery, silently mimicking "the market has no edge right now" for over three months. Two fixes were needed together: bound the lookback window by time (a cold streak ages out and the stat resets to a neutral default) and stop the formula from being able to zero out `raw_score` entirely (since `ENABLE_TAKER_ESCALATION` already exists to convert a maker miss into a taker fill — a low fill rate isn't actually a lost trade). Lesson: any input that's fed by the bot's own past actions needs an explicit escape hatch, or a bad patch becomes permanent.
- **A third-party API can deprecate a working endpoint out from under you with no code change on your end.** After the maker-fill deadlock fix above shipped and the bot resumed placing real orders, every single one failed with `HTTP 410 Gone` — the legacy `POST /portfolio/orders` order-creation endpoint had been sunset in favor of a V2 endpoint (`POST /portfolio/events/orders`) with a materially different request/response shape (fixed-point decimal strings instead of cents integers, a single YES-side `bid`/`ask` book instead of separate `yes`/`no` sides, new required fields, no `status` field in the create response). Nothing in this repo changed; Kalshi's API did. The bare exception message (`"410 Client Error: Gone for url: ..."`) gave no hint why — logging the response *body*, not just the status line, was what actually revealed the cause (`{"error": {"code": "deprecated_v1_order_endpoint", ...}}`). Lesson: for any external API integration, log the failure response body by default, not just the status code — the reason is usually right there and saves a live-API archaeology session.

## Trading Modes

| Mode | Behavior |
|---|---|
| `observe` | Build features and decisions, persist everything, place no trades |
| `paper` | Run the identical strategy and sizing path, simulate fills for validation |
| `live` | Place real Kalshi orders |

Default mode is `observe`. The safe on-ramp is `observe` → `paper` → `live`, promoting only after replaying enough history to trust the decision path.

## Project Structure

```text
bot/
  main.py              # Cycle orchestration and mode-aware runtime
  models.py            # Shared typed snapshots/features/decisions
  providers.py         # Kraken / Kalshi / Deribit provider wrappers
  feature_builder.py   # Asset snapshots and market feature construction
  strategy_engine.py   # Pure multi-asset scoring and rejection logic
  portfolio_risk.py    # Portfolio-aware sizing and per-symbol caps
  execution_engine.py  # Live order execution helpers
  replay.py            # Replay persisted cycles through the strategy path
  pricing.py           # Log-normal binary option pricer
  implied_vol.py       # IV back-out and adaptive vol margin
  kalshi_client.py     # Kalshi REST API client
  price_feed.py        # Kraken spot / realized vol / drift
  store.py             # SQLite persistence and analytics queries
  report.py            # Daily markdown report generator
  monitor.py           # Slack/Discord webhook alerting
  config.py            # Environment-driven config (every default documented inline)

tests/         # 150+ tests covering strategy, risk, execution, replay, reporting
docs/
  strategy.md  # Pricing model and strategy reference
```

## Setup

### Prerequisites

- Python 3.9+
- A [Kalshi](https://kalshi.com) account with API access
- Kalshi API key + RSA private key

### Local

```bash
git clone https://github.com/utkarshp845/kalshi-trading-bot.git
cd kalshi-trading-bot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in your credentials
```

Recommended first run:

```bash
python -m bot.main --dry-run
```

That forces `observe` behavior even if `TRADING_MODE` is set differently — nothing gets traded, everything gets logged and persisted so you can inspect the decisions.

## Configuration

Copy `.env.example` to `.env` and fill in credentials. **Every value in `config.py` has a code default and is individually overridable via environment variable** — the intent is to keep `.env` minimal (credentials, mode, monitoring, and any deliberate deviation you want to document) and let the code own the actual defaults, so tuning in one place can't be silently shadowed by a stale `.env` (see [Lessons Learned](#lessons-learned)).

### Core

| Variable | Default | Description |
|---|---|---|
| `TRADING_MODE` | `observe` | `observe`, `paper`, or `live` |
| `ENABLE_BTC` / `ENABLE_ETH` | `true` | Enable each underlying independently |
| `MIN_EDGE` | `0.025` | Hard minimum net edge floor |
| `KALSHI_TAKER_FEE` / `KALSHI_MAKER_FEE` | `0.07` / `0.0175` | Fee coefficients: `fee = rate × contracts × price × (1 - price)` |
| `POLL_INTERVAL_SECONDS` | `60` | Target cycle cadence (the loop sleeps only the remainder after cycle work) |

### Strategy Gates

| Variable | Default | Description |
|---|---|---|
| `MIN_T_HOURS` | `0.25` | Reject markets closer to expiry than this |
| `THEO_PROB_BAND_MIN` / `MAX` | `0.05` / `0.90` | Fair-value probability band — allows cheap longshots, still caps against risking most of a dollar to win a few cents |
| `MAX_BID_ASK_SPREAD` / `MAX_BID_ASK_PCT_SPREAD` | `0.15` / `0.35` | Absolute and relative spread caps |
| `SPREAD_PCT_EXEMPT_ABS` | `0.05` | Below this absolute spread, the relative-spread test is waived (see Lessons Learned) |
| `MAX_SIGMA_DISTANCE` | `3.0` | Reject strikes too far from spot in modeled sigma units |
| `MAX_CHAIN_BREAK_PCT` | `0.10` | Reject assets with too many strike-chain inconsistencies |
| `MAX_DEPTH_SLIPPAGE_PER_CONTRACT` | `0.05` | Reject when orderbook-implied slippage per contract exceeds this |
| `EDGE_LEAK_LOOKBACK_FILLS` / `EDGE_HURDLE_BUFFER` | `50` / `0.010` | Dynamic edge hurdle from recent realized fill quality |
| `LIVE_MIN_REQUIRED_EDGE` / `COLD_START_MIN_EDGE` | `0.025` / `0.025` | Minimum required edge in live mode, before/after enough fill history exists |
| `ENABLE_TAKER_ESCALATION` | `true` | If a maker bid times out unfilled and the taker-priced edge still clears the hurdle, cross the spread for the remainder instead of cancelling |
| `MAKER_FILL_LOOKBACK_DAYS` | `7` | Recent-maker-fill-rate stat only looks this far back — bounds how long a cold streak with no new attempts can freeze the stat on stale data (see Lessons Learned) |
| `MIN_EFFECTIVE_MAKER_FILL_PROB` | `0.15` | Floor on the fill rate used for scoring while `ENABLE_TAKER_ESCALATION` is on, so a measured 0% rate can't multiply a large edge down to zero |
| `LIVE_HALT_MAX_AVG_REALIZED_EDGE` | `0.0` | Halt live trading if recent average realized edge falls to/below this |

### Data Freshness

| Variable | Default | Description |
|---|---|---|
| `DATA_STALE_AFTER_SEC_KRAKEN` / `_KALSHI` | `20` | Spot/quote freshness threshold |
| `DATA_STALE_AFTER_SEC_DERIBIT` | `120` | Deribit IV freshness threshold |

### Portfolio Risk

| Variable | Default | Description |
|---|---|---|
| `DAILY_SPEND_PCT` / `DAILY_SPEND_FLOOR` | `0.30` / `$10` | Account-level daily capital cap |
| `MAX_SYMBOL_DAILY_SPEND_PCT` | `0.18` | Per-symbol daily capital cap |
| `MAX_POSITIONS` / `MAX_SYMBOL_POSITIONS` | `8` / `5` | Open-position caps, portfolio-wide and per-symbol |
| `MAX_CONTRACTS_PER_MARKET` | `40` | Hard cap on contracts in a single market |
| `KELLY_FRACTION` / `BANKROLL_FRACTION` | `0.60` / `0.60` | Fractional Kelly sizing and max share of balance deployable |
| `CORRELATION_DISCOUNT_FACTOR` | `0.85` | Per-open-position sizing discount (compounds: `factor^n`) |
| `MAX_DRAWDOWN_PCT` | `0.20` | Hard halt: no new trades once session drawdown reaches this |
| `DRAWDOWN_TIER_1_PCT` / `_2_PCT` | `0.10` / `0.15` | Graduated sizing cuts before the hard halt |

### Cycle Timing & Housekeeping

| Variable | Default | Description |
|---|---|---|
| `OUTCOME_BACKFILL_MAX_PER_CYCLE` / `_TIME_BUDGET_SEC` | `25` / `15` | Caps settlement-outcome lookups per cycle so backfill can't eat the poll interval |
| `OUTCOME_BACKFILL_RETRY_SEC` | `900` | Minimum time between re-checking the same unsettled market |
| `REPORT_REFRESH_SEC` | `900` | Minimum time between daily-report regenerations |

### Monitoring

| Variable | Default | Description |
|---|---|---|
| `ALERT_WEBHOOK_URL` | _(empty)_ | Slack/Discord webhook; empty means log-only |
| `ALERT_WEBHOOK_MIN_LEVEL` | `WARNING` | Minimum webhook severity |
| `ALERT_DEDUP_SECONDS` | `900` | Duplicate alert suppression window |

## Replay And Research

The bot persists enough per-cycle state to replay the shared decision path offline against historical data:

```bash
python -m bot.replay --from 2026-04-01 --to 2026-04-20 --symbols BTC,ETH
```

Replay reports walk-forward predicted edge, realized edge, win rate, maker fill rate, cancel rate, capital utilization, and max drawdown — useful for validating a config change against history before it goes live.

## Reporting And Persistence

The SQLite database stores:

- `orders`, `daily_snapshots`, `runs`, `asset_runs`
- `market_snapshots`, `signal_decisions`, `execution_attempts`, `market_outcomes`

Daily reports include realized P&L, fill quality, market context, decision-quality (reject-reason) breakdown, and per-asset diagnostics. Generate one manually:

```bash
python -m bot.report --date 2026-04-20
```

## Run Tests

```bash
python -m pytest -q
```

Current test status: `159 passed`.

## Deployment

This repo deploys `main` to EC2 via GitHub Actions (`.github/workflows/deploy.yml`), running the test suite before every deploy. For any strategy change:

1. Start in `observe`
2. Move to `paper`
3. Promote to `live` only after replay and paper validation

## Strategy Reference

See [docs/strategy.md](docs/strategy.md) for the underlying pricing derivation. The codebase has grown well beyond the BTC-only single-asset runtime that document originally described, but it remains the mathematical reference for the option-pricing side.

## License

[MIT](LICENSE) — see the disclaimer at the top of this README before using this against a funded account.
