# Configuration file for WebSocket & API Monitor

# WebSocket endpoints to monitor
WEBSOCKET_ENDPOINTS = {
    "price_ws": {
        "url": "wss://price.outlight.fun/ws?token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjbGllbnRJZCI6InRlc3QtY2xpZW50IiwiaWF0IjoxNzUzNDY5OTk5fQ.ru9jyhUxo7-HLpBk6-_gZTWSrKSXoKeHTd6gfkZznMU",
        "name": "Price WebSocket",
        "enabled": True,
        "type": "websocket"
    },
    "sauron_socketio": {
        "url": "wss://prod.api.sauron.outlight.fun/socket.io/?EIO=4&transport=websocket",
        "name": "Sauron Socket.IO",
        "enabled": True,  # Set to False to disable if problematic
        "type": "socketio"
    }
}

# API configuration
API_CONFIG = {
    "base_url": "https://prod.api.sauron.outlight.fun",
    "endpoints": [
        "/api/channels",
        "/api/tokens/recent", 
        "/api/tokens/most-called"
    ],
    "check_interval": 60,  # seconds - increased for better performance
    "timeout": 10  # seconds
}

# Logging configuration
LOG_CONFIG = {
    "show_timestamps": True,
    "use_colors": True,
    "log_level": "DEBUG"  # Changed to DEBUG for better troubleshooting
}

