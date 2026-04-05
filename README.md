# Kalshi BTC Arbitrage Bot

A mispricing arbitrage bot for [Kalshi](https://kalshi.com) BTC daily price-level markets. It prices binary contracts with a log-normal model and trades when the market ask is significantly below the theoretical value.

## How it works

Kalshi's `KXBTC` series are binary contracts that pay $1 if BTC closes above a given strike price. The bot:

1. **Fetches** all open `KXBTC` markets and the current BTC spot price + realized volatility
2. **Prices** each contract using a simplified Black-Scholes formula:
   ```
   P(BTC > K) = Φ( ln(S/K) / (σ × √T) )
   ```
3. **Compares** the theoretical probability to the market ask price — the difference is the edge
4. **Trades** the best opportunity if edge exceeds `MIN_EDGE`, sized by a fractional Kelly criterion
5. **Repeats** every `POLL_INTERVAL_SECONDS` during market hours (9 AM – 3:30 PM ET)

## Project structure

```
bot/
  main.py          # Entry point and main loop
  strategy.py      # Signal generation (parse strike, compute edge, rank)
  pricing.py       # Log-normal binary option pricer
  kalshi_client.py # Kalshi REST API v2 client (RSA-PSS auth)
  price_feed.py    # BTC spot price + realized volatility feed
  risk.py          # Position sizing (Kelly) and daily spend limits
  store.py         # SQLite persistence + trades CSV
  config.py        # All config loaded from environment variables
```

## Setup

### Prerequisites
- Python 3.10+
- A [Kalshi](https://kalshi.com) account with API access (API key + RSA private key)

### Local

```bash
git clone https://github.com/utkarshp845/kalshi-trading-bot.git
cd kalshi-trading-bot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your credentials
python -m bot.main --dry-run
```

### EC2 (production)

One-time bootstrap on the instance:

```bash
bash <(curl -s https://raw.githubusercontent.com/utkarshp845/kalshi-trading-bot/main/scripts/setup_ec2.sh)
```

Then copy your secrets to the instance:

```bash
scp -i your-ec2-key.pem .env ubuntu@<EC2_IP>:/home/ubuntu/money-money/.env
scp -i your-ec2-key.pem bitcoin-key.pem ubuntu@<EC2_IP>:/home/ubuntu/money-money/bitcoin-key.pem
```

Start and enable the bot:

```bash
sudo systemctl start kalshi-bot
sudo systemctl status kalshi-bot
tail -f ~/money-money/logs/bot.log
```

## Configuration

Copy `.env.example` to `.env` and set the values. Key options:

| Variable | Default | Description |
|---|---|---|
| `KALSHI_API_KEY_ID` | — | Your Kalshi API key ID |
| `KALSHI_PRIVATE_KEY_PATH` | — | Path to your Kalshi RSA private key |
| `MIN_EDGE` | `0.08` | Minimum edge (0–1) required to place a trade |
| `MAX_DAILY_SPEND` | `100` | Hard cap on spend per calendar day (USD) |
| `MAX_POSITIONS` | `5` | Max concurrent open positions |
| `KELLY_FRACTION` | `0.25` | Fraction of full Kelly to bet (0.25 = conservative) |
| `DRY_RUN` | `false` | Log signals without placing real orders |

## CI/CD pipeline

Every push to `main` automatically deploys to EC2 via GitHub Actions:

1. SSH into the EC2 instance
2. `git pull origin main`
3. Reinstall Python dependencies
4. `sudo systemctl restart kalshi-bot`

Required GitHub secrets:

| Secret | Value |
|---|---|
| `EC2_HOST` | EC2 public IP |
| `EC2_USER` | `ubuntu` |
| `EC2_SSH_KEY` | Full contents of your EC2 `.pem` key |

## Monitoring

```bash
# Live logs
tail -f ~/money-money/logs/bot.log

# Service status
sudo systemctl status kalshi-bot

# Today's trades
cat ~/money-money/logs/trades.csv
```
