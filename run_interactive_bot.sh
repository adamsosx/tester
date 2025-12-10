#!/bin/bash

# Run Interactive Telegram Bot (no chat ID configuration needed!)
cd "$(dirname "$0")"

echo "🤖 Starting Interactive Telegram Bot..."
echo "📱 Users can start monitoring with /start command"
echo "🔄 No chat ID configuration needed!"
echo ""

# Check if .env file exists
if [ ! -f ".env" ]; then
    echo "❌ .env file not found!"
    echo "📝 Please run: ./setup_telegram_bot.sh"
    echo "📝 Then copy: cp env.example .env"
    echo "📝 Only BOT_TOKEN is needed (no CHAT_ID required!)"
    echo ""
    exit 1
fi

source venv/bin/activate
python src/interactive_telegram_bot.py

