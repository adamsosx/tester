#!/bin/bash

# Setup script for Telegram Bot
cd "$(dirname "$0")"

echo "🤖 Setting up Telegram Bot for WebSocket & API Monitoring"
echo "=" * 60
echo ""

echo "📦 Installing required packages..."
source venv/bin/activate
pip install --trusted-host pypi.org --trusted-host pypi.python.org --trusted-host files.pythonhosted.org python-telegram-bot python-dotenv

echo ""
echo "⚙️ Configuration needed:"
echo ""
echo "1️⃣ Create Telegram Bot:"
echo "   • Go to @BotFather on Telegram"
echo "   • Send: /newbot"
echo "   • Choose bot name: WebSocket Monitor Bot"
echo "   • Choose username: wsmonitor_bot (or similar)"
echo "   • Copy the token"
echo ""
echo "2️⃣ Create .env file:"
echo "   • Copy: cp env.example .env"
echo "   • Edit .env and add your BOT_TOKEN"
echo "   • Add your CHAT_ID (get from bot)"
echo ""
echo "3️⃣ Get your Chat ID:"
echo "   • Send /start to your bot"
echo "   • Bot will show your chat ID"
echo ""
echo "4️⃣ Run the bot:"
echo "   • python src/telegram_bot.py"
echo ""
echo "✨ Features:"
echo "   • 🔄 Live updating message (edits same message)"
echo "   • 📊 Real-time WebSocket & API status"
echo "   • 📱 Beautiful emoji formatting"
echo "   • ⚡ Updates every second"
echo "   • 📈 Statistics and counters"
echo ""
