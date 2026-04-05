#!/usr/bin/env bash
# One-time EC2 setup script. Run this manually on the instance:
#   bash scripts/setup_ec2.sh
set -e

REPO_URL="https://github.com/utkarshp845/kalshi-trading-bot.git"
REPO_DIR="/home/ubuntu/money-money"
SERVICE_NAME="kalshi-bot"

echo "=== Installing system dependencies ==="
sudo apt-get update -q
sudo apt-get install -y -q python3 python3-pip python3-venv git

echo "=== Cloning repo ==="
if [ -d "$REPO_DIR/.git" ]; then
  echo "Repo already cloned, pulling latest..."
  git -C "$REPO_DIR" pull origin main
else
  git clone "$REPO_URL" "$REPO_DIR"
fi

echo "=== Creating virtual environment ==="
python3 -m venv "$REPO_DIR/venv"
"$REPO_DIR/venv/bin/pip" install -q --upgrade pip
"$REPO_DIR/venv/bin/pip" install -q -r "$REPO_DIR/requirements.txt"

echo "=== Creating data and log directories ==="
mkdir -p "$REPO_DIR/data" "$REPO_DIR/logs"

echo "=== Installing systemd service ==="
sudo cp "$REPO_DIR/kalshi-bot.service" "/etc/systemd/system/$SERVICE_NAME.service"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

echo ""
echo "=== Setup complete ==="
echo "Next steps:"
echo "  1. Copy your .env file:           scp .env ubuntu@<EC2_IP>:$REPO_DIR/.env"
echo "  2. Copy your Kalshi PEM key:      scp bitcoin-key.pem ubuntu@<EC2_IP>:$REPO_DIR/bitcoin-key.pem"
echo "  3. Start the bot:                 sudo systemctl start $SERVICE_NAME"
echo "  4. Check status:                  sudo systemctl status $SERVICE_NAME"
echo "  5. Tail logs:                     tail -f $REPO_DIR/logs/bot.log"
