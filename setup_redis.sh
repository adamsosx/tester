#!/bin/bash

echo "🔧 Setting up Redis for OutLight.fun WebSocket & API Monitor"
echo "============================================================"

# Check if Redis is already installed
if command -v redis-server &> /dev/null; then
    echo "✅ Redis is already installed"
    redis-server --version
else
    echo "📦 Installing Redis..."
    
    # Detect OS and install Redis
    if [[ "$OSTYPE" == "darwin"* ]]; then
        # macOS
        if command -v brew &> /dev/null; then
            echo "🍺 Installing Redis via Homebrew..."
            brew install redis
        else
            echo "❌ Homebrew not found. Please install Homebrew first:"
            echo "   /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
            exit 1
        fi
    elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
        # Linux
        if command -v apt-get &> /dev/null; then
            echo "🐧 Installing Redis via apt..."
            sudo apt-get update
            sudo apt-get install -y redis-server
        elif command -v yum &> /dev/null; then
            echo "🐧 Installing Redis via yum..."
            sudo yum install -y redis
        else
            echo "❌ Package manager not found. Please install Redis manually."
            exit 1
        fi
    else
        echo "❌ Unsupported OS: $OSTYPE"
        echo "Please install Redis manually: https://redis.io/download"
        exit 1
    fi
fi

echo ""
echo "🚀 Starting Redis server..."

# Start Redis server
if [[ "$OSTYPE" == "darwin"* ]]; then
    # macOS with Homebrew
    brew services start redis
    echo "✅ Redis started as a service (will auto-start on boot)"
elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
    # Linux
    if command -v systemctl &> /dev/null; then
        sudo systemctl start redis-server
        sudo systemctl enable redis-server
        echo "✅ Redis started as a service (will auto-start on boot)"
    else
        # Fallback: start Redis in background
        redis-server --daemonize yes
        echo "✅ Redis started in background"
    fi
fi

echo ""
echo "🧪 Testing Redis connection..."
if redis-cli ping | grep -q PONG; then
    echo "✅ Redis is running and responding to ping"
else
    echo "❌ Redis is not responding. Please check the installation."
    exit 1
fi

echo ""
echo "📋 Redis Configuration:"
echo "  Host: localhost"
echo "  Port: 6379"
echo "  Database: 0"
echo ""
echo "💡 Optional: Add Redis configuration to your .env file:"
echo "  REDIS_HOST=localhost"
echo "  REDIS_PORT=6379"
echo "  REDIS_DB=0"
echo "  # Or use Redis URL:"
echo "  # REDIS_URL=redis://localhost:6379/0"
echo ""
echo "✅ Redis setup complete!"
echo "🚀 You can now run the Telegram bot with Redis session persistence."

