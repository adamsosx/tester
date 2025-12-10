# Telegram Bot Configuration using .env file
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# TELEGRAM BOT TOKEN - from .env file
BOT_TOKEN = os.getenv('BOT_TOKEN', 'your_bot_token_here')

# CHAT IDs - from .env file (can be comma-separated for multiple users)
CHAT_ID_STRING = os.getenv('CHAT_ID', 'your_chat_id_here')
CHAT_IDS = [id.strip() for id in CHAT_ID_STRING.split(',') if id.strip()]

# Update interval - from .env file or default to 5 seconds
UPDATE_INTERVAL = int(os.getenv('UPDATE_INTERVAL', '5'))

# Redis configuration - from .env file
REDIS_URL = os.getenv('REDIS_URL')  # e.g., redis://localhost:6379/0
REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.getenv('REDIS_PORT', '6379'))
REDIS_DB = int(os.getenv('REDIS_DB', '0'))
REDIS_USERNAME = os.getenv('REDIS_USERNAME')
REDIS_PASSWORD = os.getenv('REDIS_PASSWORD')

# Maximum message length for Telegram
MAX_MESSAGE_LENGTH = 4096

# Emoji configuration - can be customized in .env file
EMOJI = {
    'connected': os.getenv('EMOJI_CONNECTED', '🟢'),
    'disconnected': os.getenv('EMOJI_DISCONNECTED', '🔴'),
    'error': os.getenv('EMOJI_ERROR', '❌'),
    'warning': os.getenv('EMOJI_WARNING', '⚠️'),
    'success': os.getenv('EMOJI_SUCCESS', '✅'),
    'waiting': os.getenv('EMOJI_WAITING', '⏳'),
    'active': os.getenv('EMOJI_ACTIVE', '🔥'),
    'api': os.getenv('EMOJI_API', '🌐'),
    'websocket': os.getenv('EMOJI_WEBSOCKET', '📡'),
    'time': os.getenv('EMOJI_TIME', '⏰'),
    'speed': os.getenv('EMOJI_SPEED', '⚡'),
    'robot': os.getenv('EMOJI_ROBOT', '🤖')
}
