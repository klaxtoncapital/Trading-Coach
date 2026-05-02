#!/bin/bash
# ─────────────────────────────────────────────
# TRADING COACH BOT — START SCRIPT
# Run this file to start your bot
# ─────────────────────────────────────────────

echo "Installing requirements..."
pip install -r requirements.txt

echo "Starting Trading Coach Bot..."
python bot.py
