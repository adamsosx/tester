# WebSocket & API Monitoring Bot

Bot for monitoring WebSocket connections and API endpoints in real-time.

## Funkcje

- **Monitorowanie WebSocket**: Ciągłe monitorowanie połączeń WebSocket
  - Price WebSocket (`wss://price.outlight.fun/ws`)
  - Sauron Socket.IO (`wss://prod.api.sauron.outlight.fun/socket.io/`)

- **API Monitoring**: Checking endpoints every 60 seconds
  - `GET /api/channels`
  - `GET /api/tokens/recent`
  - `GET /api/tokens/most-called`

- **Kolorowe logi**: Przejrzyste statusy z kolorowym oznaczeniem
- **Automatyczne ponowne łączenie**: W przypadku utraty połączenia
- **Pomiar czasu odpowiedzi**: Dla endpointów API

## Instalacja

1. Zainstaluj wymagane pakiety:
```bash
pip install -r requirements.txt
```

2. Uruchom bota:
```bash
python src/main.py
```

## Struktura projektu

```
wsapi/
├── src/
│   ├── main.py              # Główny plik uruchamiający
│   ├── websocket_monitor.py # Monitor WebSocket
│   └── api_monitor.py       # Monitor API
├── requirements.txt         # Zależności Python
└── README.md               # Dokumentacja
```

## Statusy

- ✓ **SUCCESS/CONNECTED** (zielony): Połączenie działa prawidłowo
- ✗ **ERROR/DISCONNECTED** (czerwony): Błąd lub brak połączenia  
- ⚠ **WARNING** (żółty): Ostrzeżenie
- ℹ **INFO** (niebieski): Informacje systemowe

## 🌐 Web Dashboard

Nowy nowoczesny dashboard z interfejsem web!

### Uruchomienie dashboard:
```bash
./run_dashboard.sh
```

Lub ręcznie:
```bash
source venv/bin/activate
python src/dashboard_main.py
```

### Funkcje dashboard:
- 📊 **Real-time monitoring** - live updates przez WebSocket
- 📈 **Wykresy czasów odpowiedzi** - Chart.js integration
- 🎛️ **Kontrola z przeglądarki** - start/stop monitoring
- 📱 **Responsive design** - działa na mobile
- 🎨 **Nowoczesny UI** - gradient design z animacjami

### Dostęp:
- **URL**: http://localhost:5000
- **Alternatywnie**: http://127.0.0.1:5000

## Zatrzymywanie

### Console bot:
Naciśnij `Ctrl+C` aby bezpiecznie zatrzymać bota.

### Web dashboard:
Zamknij przeglądarkę lub naciśnij `Ctrl+C` w terminalu.

## 🤖 Telegram Bot

Nowy live dashboard w Telegram z edycją wiadomości!

### Konfiguracja:
```bash
# 1. Setup
./setup_telegram_bot.sh

# 2. Skopiuj przykład
cp env.example .env

# 3. Edytuj .env
nano .env
```

### Plik .env:
```env
BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrsTUVwxyz
CHAT_ID=123456789
UPDATE_INTERVAL=1
```

### Uruchomienie:
```bash
./run_telegram_bot.sh
```

### Funkcje Telegram Bot:
- 📱 **Live updates** - edytuje jedną wiadomość co sekundę
- 🎨 **Emoji statusy** - kolorowe wskaźniki 🟢🔴⚠️
- 📊 **Statystyki** - liczniki wiadomości i API calls
- ⏰ **Uptime** - czas działania bota
- 🚀 **No spam** - nie tworzy nowych wiadomości

### Jak uzyskać dane:
1. **Bot Token**: @BotFather → /newbot
2. **Chat ID**: wyślij /start do bota
