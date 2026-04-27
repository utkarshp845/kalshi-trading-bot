# Kalshi Multi-Asset Safer-PnL Bot

A safer mispricing bot for [Kalshi](https://kalshi.com) crypto price-level markets. It now supports both BTC and ETH, runs a shared multi-asset strategy pipeline, stores every cycle's market snapshot and decision trace, and supports `observe`, `paper`, and `live` trading modes.

## What It Does

Each cycle the bot:

1. Fetches BTC/ETH spot, realized vol, and trailing drift from Kraken
2. Pulls Kalshi market quotes, public orderbook depth, and optional Deribit ATM IV
3. Builds per-asset snapshots and per-market features
4. Rejects weak or noisy markets using:
   - minimum time-to-expiry
   - spread and stale-price filters
   - probability-band gating
   - sigma-distance gating
   - strike-chain consistency checks
   - thin-book and depth-slippage filters
5. Scores each candidate with a dynamic hurdle:
   - recent edge leak
   - expected slippage
   - settled-trade uncertainty penalty
   - orderbook depth slippage
6. Sizes trades with portfolio-aware risk and liquidity coverage:
   - account-level caps
   - per-symbol caps
   - stronger same-asset correlation discount
   - weaker cross-asset discount
   - degraded-data budget reduction
   - visible-size gate at the intended entry price
7. Executes with maker-first post-only entry in `live`, simulates fills in `paper`, and logs only in `observe`
8. Persists asset runs, market snapshots, signal decisions, execution attempts, fills, labeled market outcomes, and reports for replay and diagnostics

## Trading Modes

| Mode | Behavior |
|---|---|
| `observe` | Build features and decisions, persist everything, place no trades |
| `paper` | Run the same strategy and sizing path, simulate fills for validation |
| `live` | Place real Kalshi orders |

Default mode is `observe`.

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
  config.py            # Environment-driven config

tests/
  test_feature_builder.py
  test_strategy_engine.py
  test_portfolio_risk.py
  test_replay.py
  ...

docs/
  strategy.md          # Strategy reference
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
cp .env.example .env
```

Recommended first run:

```bash
python -m bot.main --dry-run
```

That forces `observe` behavior even if `TRADING_MODE` is set differently.

## Configuration Highlights

Copy `.env.example` to `.env` and set your credentials plus the mode you want.

### Core

| Variable | Default | Description |
|---|---|---|
| `TRADING_MODE` | `observe` | `observe`, `paper`, or `live` |
| `ENABLE_BTC` | `true` | Enable BTC markets |
| `ENABLE_ETH` | `true` | Enable ETH markets |
| `MIN_EDGE` | `0.15` | Hard minimum net edge floor |
| `KALSHI_TAKER_FEE` | `0.07` | Taker fee coefficient used in `rate * contracts * price * (1-price)` |
| `KALSHI_MAKER_FEE` | `0.0175` | Maker fee coefficient where maker fees apply |

### Safer-PnL Strategy Gates

| Variable | Default | Description |
|---|---|---|
| `THEO_PROB_BAND_MIN` | `0.25` | Lower fair-value probability gate |
| `THEO_PROB_BAND_MAX` | `0.75` | Upper fair-value probability gate |
| `MAX_SIGMA_DISTANCE` | `1.5` | Reject strikes too far from spot in modeled sigma units |
| `MAX_CHAIN_BREAK_PCT` | `0.10` | Reject assets with too many strike-chain inconsistencies |
| `EDGE_LEAK_LOOKBACK_FILLS` | `50` | Lookback for dynamic edge hurdle and slippage |
| `EDGE_HURDLE_BUFFER` | `0.02` | Buffer added on top of recent edge leak |
| `SETTLED_MAE_LOOKBACK_TRADES` | `30` | Lookback for uncertainty penalty |
| `MAX_DEPTH_SLIPPAGE_PER_CONTRACT` | `0.02` | Reject when orderbook-implied slippage per contract exceeds this |
| `LIQUIDITY_ENTRY_MULTIPLIER` | `5.0` | Visible resting size required vs intended contract count |
| `ORDERBOOK_DEPTH` | `20` | Orderbook levels requested per market snapshot |
| `BUCKET_EDGE_MIN_TRADES` | `8` | Minimum similar fills before blocking a losing trade bucket |
| `BUCKET_EDGE_LOOKBACK_DAYS` | `21` | Lookback for bucket-level realized edge |
| `MAKER_FILL_MIN_ATTEMPTS` | `8` | Minimum live attempts before trusting maker fill rate |
| `DEFAULT_MAKER_FILL_PROB` | `0.50` | Conservative maker fill probability before enough history |

### Data Freshness

| Variable | Default | Description |
|---|---|---|
| `DATA_STALE_AFTER_SEC_KRAKEN` | `20` | Spot feed freshness threshold |
| `DATA_STALE_AFTER_SEC_KALSHI` | `20` | Kalshi quote freshness threshold |
| `DATA_STALE_AFTER_SEC_DERIBIT` | `120` | Deribit IV freshness threshold |

### Portfolio Risk

| Variable | Default | Description |
|---|---|---|
| `DAILY_SPEND_PCT` | `0.10` | Account-level daily capital cap |
| `MAX_SYMBOL_DAILY_SPEND_PCT` | `0.05` | Per-symbol daily capital cap |
| `MAX_POSITIONS` | `2` | Portfolio-wide open-position cap |
| `MAX_SYMBOL_POSITIONS` | `1` | Per-symbol open-position cap |
| `KELLY_FRACTION` | `0.10` | Fractional Kelly sizing |
| `MAX_DRAWDOWN_PCT` | `0.20` | Portfolio drawdown halt |

### Monitoring

| Variable | Default | Description |
|---|---|---|
| `ALERT_WEBHOOK_URL` | _(empty)_ | Slack/Discord webhook |
| `ALERT_WEBHOOK_MIN_LEVEL` | `WARNING` | Minimum webhook severity |
| `ALERT_DEDUP_SECONDS` | `900` | Duplicate alert suppression window |

## Replay And Research

The bot now stores enough cycle data to replay the shared decision path offline.

Replay BTC and ETH over a date range:

```bash
python -m bot.replay --from 2026-04-01 --to 2026-04-20 --symbols BTC,ETH
```

Replay uses persisted `asset_runs`, `market_snapshots`, `signal_decisions`, `execution_attempts`, and labeled `market_outcomes` to report walk-forward predicted edge, realized edge, win rate, maker fill rate, cancel rate, capital utilization, and max drawdown.

## Reporting And Persistence

The SQLite database now stores:

- `orders`
- `daily_snapshots`
- `runs`
- `asset_runs`
- `market_snapshots`
- `signal_decisions`
- `execution_attempts`
- `market_outcomes`

Daily reports now include:

- realized P&L
- fill quality
- market context
- decision-quality breakdown
- asset diagnostics by symbol

Generate a report manually:

```bash
python -m bot.report --date 2026-04-20
```

## Run Tests

```bash
python -m pytest -q
```

Current test status: `122 passed`.

## Deployment Notes

This repo still uses the existing GitHub Actions deployment flow to push `main` to EC2. For safer rollout:

1. Start in `observe`
2. Move to `paper`
3. Promote to `live` only after replay and paper validation

## Strategy Reference

See [docs/strategy.md](docs/strategy.md) for the underlying pricing and strategy background. The codebase has moved beyond the original BTC-only runtime described there, but the document remains the mathematical reference point for the option-pricing side.
