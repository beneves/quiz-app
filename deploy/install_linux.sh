#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/home/beneves/quiz-app
PYTHON_BIN=${PYTHON_BIN:-python3}

mkdir -p "$APP_DIR"
cd "$APP_DIR"

if [ ! -d .git ]; then
  git clone https://github.com/beneves/quiz-app.git "$APP_DIR"
else
  git pull --ff-only
fi

$PYTHON_BIN -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

sudo cp deploy/systemd/quiz-app-telegram.service /etc/systemd/system/
sudo cp deploy/systemd/quiz-app-discord.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable quiz-app-telegram.service
sudo systemctl enable quiz-app-discord.service
sudo systemctl restart quiz-app-telegram.service
sudo systemctl restart quiz-app-discord.service
sudo systemctl status --no-pager quiz-app-telegram.service quiz-app-discord.service
