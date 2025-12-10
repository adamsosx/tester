#!/usr/bin/env python3
"""
Interactive Telegram Bot for WebSocket & API Monitoring
Users can start their own monitoring sessions with commands
"""

import asyncio
import sys
import os
from datetime import datetime, timedelta
import threading
import time
import json
import pickle
import psutil
import logging
import logging.handlers
from collections import defaultdict
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.error import TelegramError
import websockets
import requests

# Add parent directory to path for config import
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import WEBSOCKET_ENDPOINTS, API_CONFIG
from telegram_config import BOT_TOKEN, UPDATE_INTERVAL, EMOJI, REDIS_URL, REDIS_HOST, REDIS_PORT, REDIS_DB, REDIS_USERNAME, REDIS_PASSWORD, CHAT_IDS
from redis_session_manager import RedisSessionManager

def parse_chat_id(chat_id_str):
    """
    Parse chat ID string that may include forum topic ID
    Format: -1001234567890_423 -> (chat_id=-1001234567890, thread_id=423)
    Format: -1001234567890 -> (chat_id=-1001234567890, thread_id=None)
    """
    if '_' in str(chat_id_str):
        chat_id, thread_id = str(chat_id_str).split('_', 1)
        return int(chat_id), int(thread_id)
    else:
        return int(chat_id_str), None

class FileLogger:
    """
    Rotating file logger for bot errors and events
    - Rotates daily (24h)
    - Keeps max 10 days of logs
    - Separate files for different log types
    """
    
    def __init__(self, logs_dir="logs"):
        self.logs_dir = logs_dir
        self.loggers = {}
        
        # Create logs directory if it doesn't exist
        os.makedirs(logs_dir, exist_ok=True)
        
        # Setup different log types
        self.setup_logger('errors', 'errors.log', logging.ERROR)
        self.setup_logger('health', 'health.log', logging.INFO)
        self.setup_logger('bot', 'bot.log', logging.INFO)
        self.setup_logger('monitoring', 'monitoring.log', logging.INFO)
        
        print(f"📁 FileLogger initialized - logs directory: {os.path.abspath(logs_dir)}")
    
    def setup_logger(self, name, filename, level):
        """Setup a rotating file logger"""
        logger = logging.getLogger(f"outlight_{name}")
        logger.setLevel(level)
        
        # Remove existing handlers to avoid duplicates
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
        
        # Create rotating file handler
        # Rotate daily (when='midnight'), keep 10 days (backupCount=10)
        log_file = os.path.join(self.logs_dir, filename)
        handler = logging.handlers.TimedRotatingFileHandler(
            log_file,
            when='midnight',
            interval=1,
            backupCount=10,
            encoding='utf-8'
        )
        
        # Set format
        formatter = logging.Formatter(
            '%(asctime)s | %(levelname)-8s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        handler.setFormatter(formatter)
        
        logger.addHandler(handler)
        self.loggers[name] = logger
        
        print(f"📝 Setup {name} logger: {log_file} (rotate daily, keep 10 days)")
    
    def log_error(self, source, message, error_type="ERROR"):
        """Log error to errors.log"""
        log_message = f"{error_type} | {source} | {message}"
        self.loggers['errors'].error(log_message)
    
    def log_health(self, message):
        """Log health check info to health.log"""
        self.loggers['health'].info(message)
    
    def log_bot_event(self, event_type, message):
        """Log bot events to bot.log"""
        log_message = f"{event_type} | {message}"
        self.loggers['bot'].info(log_message)
    
    def log_monitoring(self, source, status, message=""):
        """Log monitoring events to monitoring.log"""
        log_message = f"{source} | {status}"
        if message:
            log_message += f" | {message}"
        self.loggers['monitoring'].info(log_message)
    
    def log_restart(self, reason, restart_type="ERROR"):
        """Log restart events"""
        log_message = f"RESTART | {restart_type} | {reason}"
        self.loggers['bot'].warning(log_message)
        # Also log to errors if it's an error-based restart
        if restart_type == "ERROR":
            self.loggers['errors'].error(f"RESTART_TRIGGERED | {reason}")

class HealthMonitor:
    """
    Health monitoring system for the bot
    Tracks errors, memory usage, and triggers restarts when needed
    """
    
    def __init__(self, bot):
        self.bot = bot
        self.error_counts = defaultdict(list)  # error_type -> list of timestamps
        self.restart_count_today = 0
        self.last_restart_date = None
        self.last_health_check = datetime.now()
        
        # Restart thresholds
        self.restart_thresholds = {
            'network_errors': {'count': 10, 'window': 300},      # 10 errors in 5 minutes
            'telegram_errors': {'count': 5, 'window': 180},      # 5 errors in 3 minutes
            'websocket_failures': {'count': 20, 'window': 600},  # 20 errors in 10 minutes
            'redis_errors': {'count': 3, 'window': 60},          # 3 errors in 1 minute
            'memory_usage': {'threshold': 500},                   # 500MB RAM usage
        }
        
        # Restart delays (exponential backoff)
        self.restart_delays = [60, 120, 300, 600, 1800]  # 1m, 2m, 5m, 10m, 30m
        
        print("🏥 HealthMonitor initialized")
    
    def add_error(self, error_type, message):
        """Add an error to the tracking system"""
        now = datetime.now()
        self.error_counts[error_type].append({
            'time': now,
            'message': message
        })
        
        # Clean old errors outside time window
        self.cleanup_old_errors(error_type)
        
        # Log the error
        print(f"🏥 Health error [{error_type}]: {message}")
        
        # Check if restart is needed
        if self.should_restart(error_type):
            asyncio.create_task(self.trigger_restart(f"Too many {error_type} errors"))
    
    def cleanup_old_errors(self, error_type):
        """Remove errors outside the time window"""
        if error_type not in self.restart_thresholds:
            return
            
        window = self.restart_thresholds[error_type].get('window', 300)
        cutoff_time = datetime.now() - timedelta(seconds=window)
        
        self.error_counts[error_type] = [
            error for error in self.error_counts[error_type]
            if error['time'] > cutoff_time
        ]
    
    def should_restart(self, error_type):
        """Check if restart is needed based on error count"""
        if error_type not in self.restart_thresholds:
            return False
            
        threshold = self.restart_thresholds[error_type]
        
        # Special case for memory usage
        if error_type == 'memory_usage':
            return self.check_memory_usage()[0]
        
        # Count-based restart
        if 'count' in threshold:
            current_count = len(self.error_counts[error_type])
            return current_count >= threshold['count']
        
        return False
    
    def check_memory_usage(self):
        """Check current memory usage"""
        try:
            process = psutil.Process()
            memory_mb = process.memory_info().rss / 1024 / 1024
            threshold = self.restart_thresholds['memory_usage']['threshold']
            
            if memory_mb > threshold:
                return True, f"High memory usage: {memory_mb:.1f}MB (threshold: {threshold}MB)"
            return False, f"Memory usage OK: {memory_mb:.1f}MB"
        except Exception as e:
            return False, f"Error checking memory: {e}"
    
    async def health_check_loop(self):
        """Main health check loop - runs every minute"""
        print("🏥 Starting health check loop")
        
        while self.bot.global_monitoring_active:
            try:
                await self.perform_health_check()
                await asyncio.sleep(60)  # Check every minute
            except Exception as e:
                print(f"❌ Error in health check loop: {e}")
                await asyncio.sleep(60)
    
    async def daily_restart_scheduler(self):
        """Daily restart scheduler - restarts bot at 3:00 AM every day"""
        print("⏰ Starting daily restart scheduler (3:00 AM)")
        
        while self.bot.global_monitoring_active:
            try:
                # Calculate time until next 3:00 AM
                now = datetime.now()
                next_restart = now.replace(hour=3, minute=0, second=0, microsecond=0)
                
                # If it's already past 3:00 AM today, schedule for tomorrow
                if now >= next_restart:
                    next_restart += timedelta(days=1)
                
                # Calculate sleep time
                sleep_seconds = (next_restart - now).total_seconds()
                hours_until = sleep_seconds / 3600
                
                print(f"⏰ Next daily restart scheduled for: {next_restart.strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"⏰ Time until restart: {hours_until:.1f} hours ({sleep_seconds:.0f} seconds)")
                
                # Sleep until restart time
                await asyncio.sleep(sleep_seconds)
                
                # Trigger daily restart
                if self.bot.global_monitoring_active:  # Check if bot is still active
                    print("⏰ Daily restart time reached - triggering scheduled restart")
                    await self.trigger_daily_restart()
                
            except Exception as e:
                print(f"❌ Error in daily restart scheduler: {e}")
                # Sleep for 1 hour before retrying
                await asyncio.sleep(3600)
    
    async def trigger_daily_restart(self):
        """Trigger daily maintenance restart"""
        print("🔄 Triggering daily maintenance restart")
        
        # Don't count daily restarts in the error-based restart count
        daily_restart_count = getattr(self, 'daily_restart_count', 0) + 1
        self.daily_restart_count = daily_restart_count
        
        # Log daily restart
        if hasattr(self.bot, 'file_logger'):
            self.bot.file_logger.log_restart(f"Daily maintenance restart #{daily_restart_count}", "MAINTENANCE")
        
        # Notify users about scheduled maintenance
        await self.notify_daily_restart()
        
        # Wait 30 seconds for users to see the notification
        print("⏳ Waiting 30 seconds before restart...")
        await asyncio.sleep(30)
        
        # Perform graceful restart
        await self.graceful_restart(f"Daily maintenance restart #{daily_restart_count}")
    
    async def notify_daily_restart(self):
        """Notify all active users about daily maintenance restart - PARALLEL"""
        message = f"🔄 **Daily Maintenance Restart**\n\n"
        message += f"**Time:** {datetime.now().strftime('%H:%M:%S')}\n"
        message += f"**Reason:** Scheduled daily maintenance\n"
        message += f"**Restart in:** 30 seconds\n\n"
        message += "✅ This is a routine restart to keep the bot healthy.\n"
        message += "✅ Your session will be automatically restored.\n"
        message += "✅ Monitoring will resume in ~10 seconds after restart."
        
        # Send to configured chats only (from .env CHAT_ID)
        await self.send_notification_to_configured_chats(message)
    
    async def perform_health_check(self):
        """Perform a single health check"""
        self.last_health_check = datetime.now()
        
        # Check memory usage
        memory_exceeded, memory_msg = self.check_memory_usage()
        if memory_exceeded:
            await self.trigger_restart(memory_msg)
            return
        
        # Check Redis connection
        try:
            if hasattr(self.bot, 'session_manager'):
                # Test Redis connection
                self.bot.session_manager.redis_client.ping()
        except Exception as e:
            self.add_error('redis_errors', f"Redis connection failed: {e}")
        
        # Reset daily restart count if new day
        today = datetime.now().date()
        if self.last_restart_date != today:
            self.restart_count_today = 0
            self.last_restart_date = today
        
        # Log health status (every 10 minutes)
        if self.last_health_check.minute % 10 == 0:
            await self.log_health_status()
    
    async def log_health_status(self):
        """Log current health status"""
        memory_ok, memory_msg = self.check_memory_usage()
        active_sessions = len(self.bot.active_sessions)
        
        status_msg = f"🏥 Health Check - {memory_msg}, Sessions: {active_sessions}, Restarts today: {self.restart_count_today}"
        print(status_msg)
        
        # Add to bot logs
        if hasattr(self.bot, 'add_log'):
            self.bot.add_log("HEALTH", "Monitor", status_msg)
    
    async def trigger_restart(self, reason):
        """Trigger a restart with the given reason"""
        self.restart_count_today += 1
        
        # Calculate restart delay based on restart count
        delay_index = min(self.restart_count_today - 1, len(self.restart_delays) - 1)
        delay = self.restart_delays[delay_index]
        
        print(f"🔄 Restart triggered: {reason}")
        print(f"⏳ Restart #{self.restart_count_today} today, delay: {delay}s")
        
        # Log restart trigger
        if hasattr(self.bot, 'file_logger'):
            self.bot.file_logger.log_restart(reason, "ERROR")
        
        # Notify users
        await self.notify_restart(reason, delay)
        
        # Wait for the delay
        await asyncio.sleep(delay)
        
        # Perform graceful restart
        await self.graceful_restart(reason)
    
    async def notify_restart(self, reason, delay):
        """Notify all active users about upcoming restart - PARALLEL"""
        message = f"🔄 **Bot Restart Scheduled**\n\n"
        message += f"**Reason:** {reason}\n"
        message += f"**Restart in:** {delay} seconds\n"
        message += f"**Time:** {datetime.now().strftime('%H:%M:%S')}\n"
        message += f"**Restart #:** {self.restart_count_today} today\n\n"
        message += "✅ Your session will be automatically restored after restart."
        
        # Send to configured chats only (from .env CHAT_ID)
        await self.send_notification_to_configured_chats(message)
    
    async def send_notification_to_chat(self, chat_id, message):
        """Send notification message to specific chat"""
        bot = Bot(token=self.bot.bot_token)
        session = self.bot.active_sessions.get(chat_id, {})
        
        send_kwargs = {
            'chat_id': chat_id,
            'text': message,
            'parse_mode': 'Markdown'
        }
        
        # Add thread_id if this is a forum topic
        if 'thread_id' in session and session['thread_id']:
            send_kwargs['message_thread_id'] = session['thread_id']
        
        await bot.send_message(**send_kwargs)
    
    async def send_notification_to_configured_chats(self, message):
        """Send notification only to chats configured in .env CHAT_ID"""
        if not CHAT_IDS or CHAT_IDS == ['your_chat_id_here']:
            print("⚠️ No configured CHAT_IDS in .env, skipping notifications")
            return
        
        print(f"📢 Sending notification to {len(CHAT_IDS)} configured chats")
        tasks = []
        
        for chat_id_str in CHAT_IDS:
            try:
                chat_id, thread_id = parse_chat_id(chat_id_str.strip())
                
                # Skip if there's an active user session for this chat
                # to avoid duplicate messages
                if chat_id in self.active_sessions:
                    print(f"⏭️ Skipping notification to {chat_id_str} - active user session exists")
                    continue
                
                send_kwargs = {
                    'chat_id': chat_id,
                    'text': message,
                    'parse_mode': 'Markdown'
                }
                
                # Add thread_id if this is a forum topic
                if thread_id:
                    send_kwargs['message_thread_id'] = thread_id
                    print(f"📍 Will send to forum topic {thread_id} in chat {chat_id}")
                
                # Create task for this chat
                async def send_to_chat(kwargs):
                    bot = Bot(token=self.bot.bot_token)
                    await bot.send_message(**kwargs)
                
                task = asyncio.create_task(send_to_chat(send_kwargs))
                tasks.append(task)
                
            except Exception as e:
                print(f"❌ Error parsing chat ID '{chat_id_str}': {e}")
        
        if tasks:
            # Send all notifications in parallel
            try:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                success_count = sum(1 for r in results if not isinstance(r, Exception))
                print(f"✅ Notifications sent to configured chats: {success_count}/{len(tasks)} successful")
            except Exception as e:
                print(f"❌ Error in parallel notifications to configured chats: {e}")
        else:
            print("⚠️ No valid chat IDs to send notifications to")
    
    async def graceful_restart(self, reason):
        """Perform graceful restart"""
        print(f"🔄 Starting graceful restart: {reason}")
        
        # Save all sessions to Redis
        if hasattr(self.bot, 'save_active_sessions'):
            self.bot.save_active_sessions()
            print("💾 Sessions saved to Redis")
        
        # Stop global monitoring
        self.bot.global_monitoring_active = False
        print("🛑 Global monitoring stopped")
        
        # Notify users about restart completion - PARALLEL
        message = f"✅ **Bot Restarted**\n\n"
        message += f"**Reason:** {reason}\n"
        message += f"**Time:** {datetime.now().strftime('%H:%M:%S')}\n\n"
        message += "🔄 Monitoring will resume automatically..."
        
        # Send to configured chats only (from .env CHAT_ID)
        await self.send_notification_to_configured_chats(message)
        
        # Wait a moment for messages to send
        await asyncio.sleep(2)
        
        print("🔄 Exiting process for restart...")
        # Exit process - systemd will restart it
        sys.exit(0)

class InteractiveTelegramBot:
    def __init__(self):
        self.bot_token = BOT_TOKEN
        self.active_sessions = {}  # chat_id -> session data (includes thread_id for forum topics)
        self.global_monitoring_active = False
        self.last_message_content = {}  # chat_id -> last message text (to avoid unnecessary edits)
        self.user_current_view = {}  # chat_id -> current view ('dashboard', 'logs', 'errors', etc.')
        self.last_edit_time = {}  # chat_id -> timestamp of last edit (for throttling)
        
        # Initialize file logging
        self.file_logger = FileLogger()
        
        # Initialize health monitoring
        self.health_monitor = HealthMonitor(self)
        
        # Global monitoring data (shared across all users)
        self.global_status_data = {
            'websockets': {},
            'api_endpoints': {},
            'system_info': {
                'start_time': datetime.now(),
                'total_messages': 0,
                'total_api_calls': 0,
                'active_users': 0
            },
            'recent_errors': [],  # Last 5 errors with timestamps
            'logs': [],  # All monitoring logs with timestamp and type
            'pending_history': []  # History of all pending errors (resolved and unresolved)
        }
        
        # Temporary error tracking for smart filtering
        self.pending_errors = {}  # source -> {'error': message, 'timestamp': datetime}
        
        # Initialize data
        self.initialize_data()
        
        # Session persistence with Redis
        self.session_manager = RedisSessionManager(
            redis_url=REDIS_URL,
            redis_host=REDIS_HOST,
            redis_port=REDIS_PORT,
            redis_db=REDIS_DB,
            redis_username=REDIS_USERNAME,
            redis_password=REDIS_PASSWORD
        )
        
        # Clean up old sessions before loading
        try:
            cleaned = self.session_manager.cleanup_old_sessions(max_age_hours=48)
            if cleaned > 0:
                print(f"🧹 Cleaned {cleaned} old sessions on startup")
        except Exception as e:
            print(f"⚠️ Could not clean old sessions: {e}")
        
        self.load_active_sessions()
    
    def add_pending_error(self, source, error_message):
        """Add error to pending list - will be promoted to real error if not resolved in 20s"""
        timestamp = datetime.now()
        self.pending_errors[source] = {
            'error': str(error_message)[:100],
            'timestamp': timestamp
        }
        
        # Also add to pending history for "All Errors" view
        pending_entry = {
            'timestamp': timestamp,
            'source': source,
            'message': str(error_message)[:100],
            'status': 'pending'  # pending, resolved, confirmed
        }
        self.global_status_data['pending_history'].append(pending_entry)
        
        # Keep only last 50 pending history entries
        if len(self.global_status_data['pending_history']) > 50:
            self.global_status_data['pending_history'] = self.global_status_data['pending_history'][-50:]
        
        print(f"⏳ PENDING ERROR [{timestamp.strftime('%H:%M:%S')}] {source}: {error_message}")
    
    def resolve_pending_error(self, source):
        """Remove pending error (connection recovered)"""
        if source in self.pending_errors:
            error_data = self.pending_errors[source]
            recovery_time = (datetime.now() - error_data['timestamp']).total_seconds()
            print(f"✅ RECOVERED [{datetime.now().strftime('%H:%M:%S')}] {source}: reconnected after {recovery_time:.1f}s")
            
            # Add recovery info to logs for history (not to errors)
            recovery_message = f"Connection recovered after {recovery_time:.1f}s (was: {error_data['error']})"
            self.add_log("SUCCESS", source, recovery_message)
            
            # Mark as resolved in pending history
            for entry in self.global_status_data['pending_history']:
                if (entry['source'] == source and 
                    entry['status'] == 'pending' and 
                    entry['message'] == error_data['error']):
                    entry['status'] = 'resolved'
                    entry['resolution_time'] = recovery_time
                    break
            
            # Remove from pending errors (disappears from main dashboard)
            del self.pending_errors[source]
            return True
        return False
    
    def add_error(self, source, error_message, error_type="ERROR"):
        """Add error to recent errors list (immediate errors, not connection issues)"""
        error_entry = {
            'timestamp': datetime.now(),
            'source': source,
            'message': str(error_message)[:100],  # Limit message length
            'type': error_type  # ERROR, RECOVERY, WARNING
        }
        
        # Only add actual errors to recent_errors (not recovery messages)
        if error_type == "ERROR":
            # Add to beginning of list
            self.global_status_data['recent_errors'].insert(0, error_entry)
            
            # Keep only last 5 errors
            if len(self.global_status_data['recent_errors']) > 5:
                self.global_status_data['recent_errors'] = self.global_status_data['recent_errors'][:5]
            
            print(f"🔴 ERROR [{error_entry['timestamp'].strftime('%H:%M:%S')}] {source}: {error_message}")
            
            # Log to file
            if hasattr(self, 'file_logger'):
                self.file_logger.log_error(source, error_message, error_type)
            
            # Add to health monitoring system
            if hasattr(self, 'health_monitor'):
                # Map sources to health monitor error types
                health_error_type = self.map_source_to_health_type(source, error_message)
                if health_error_type:
                    self.health_monitor.add_error(health_error_type, f"{source}: {error_message}")
        else:
            # Recovery messages go to logs, not errors
            print(f"✅ RECOVERY [{error_entry['timestamp'].strftime('%H:%M:%S')}] {source}: {error_message}")
            
            # Log recovery to file
            if hasattr(self, 'file_logger'):
                self.file_logger.log_monitoring(source, "RECOVERY", error_message)
    
    def map_source_to_health_type(self, source, error_message):
        """Map error source to health monitor error type"""
        error_msg_lower = error_message.lower()
        source_lower = source.lower()
        
        # Skip errors that shouldn't trigger restarts
        if any(keyword in error_msg_lower for keyword in ['chat not found', 'bot was blocked', 'user deactivated']):
            return None  # Don't track these errors for health monitoring
        
        # Redis errors (check first - more specific)
        if 'redis' in source_lower or 'redis' in error_msg_lower:
            return 'redis_errors'
        
        # Telegram API errors
        if 'telegram' in source_lower or any(keyword in error_msg_lower for keyword in ['flood control', 'rate limit', 'telegram']):
            return 'telegram_errors'
        
        # WebSocket errors
        if 'websocket' in source_lower or 'ws' in source_lower:
            return 'websocket_failures'
        
        # Network-related errors (check last - most general)
        if any(keyword in error_msg_lower for keyword in ['network', 'connection', 'timeout', 'dns', 'connect']):
            return 'network_errors'
        
        # Default to network errors for unknown types
        return 'network_errors'
    
    def check_pending_errors(self):
        """Check for pending errors that should be promoted to real errors (>20s old)"""
        current_time = datetime.now()
        expired_errors = []
        
        for source, error_data in self.pending_errors.items():
            age = (current_time - error_data['timestamp']).total_seconds()
            if age > 20:  # 20 seconds timeout
                expired_errors.append((source, error_data))
        
        # Promote expired errors to real errors
        for source, error_data in expired_errors:
            self.add_error(source, f"{error_data['error']} (connection failed for >20s)")
            del self.pending_errors[source]
    
    def save_active_sessions(self):
        """Save active sessions to Redis for restart recovery"""
        try:
            saved_count = 0
            skipped_count = 0
            
            for chat_id, session in self.active_sessions.items():
                # Skip configured sessions (they start with "configured_")
                if isinstance(chat_id, str) and chat_id.startswith('configured_'):
                    skipped_count += 1
                    continue
                
                # Save all user sessions - conflicts are now handled at message level
                
                # Only save non-conflicting user sessions (integer chat_id)
                if not isinstance(chat_id, int):
                    skipped_count += 1
                    continue
                
                session_data = {
                    'user_name': session['user_name'],
                    'start_time': session['start_time'],
                    # Don't save message_id as it becomes invalid after restart
                }
                
                # Add thread_id if it exists (for forum topics)
                if 'thread_id' in session and session['thread_id']:
                    session_data['thread_id'] = session['thread_id']
                
                if self.session_manager.save_session(chat_id, session_data, ttl_hours=24):
                    saved_count += 1
            
            print(f"💾 Saved {saved_count}/{len(self.active_sessions)} active sessions to Redis (skipped {skipped_count} configured/conflicting sessions)")
        except Exception as e:
            print(f"❌ Error saving sessions: {e}")
    
    def load_active_sessions(self):
        """Load active sessions from Redis after restart"""
        try:
            sessions_data = self.session_manager.load_all_sessions()
            
            if not sessions_data:
                print("📂 No previous sessions to restore")
                return
            
            # Convert back to active sessions
            for chat_id, session_data in sessions_data.items():
                restored_session = {
                    'message_id': None,  # Will be set when first message is sent
                    'user_name': session_data['user_name'],
                    'start_time': session_data['start_time'],
                    'restored': True  # Flag to indicate this was restored
                }
                
                # Restore thread_id if it exists
                if 'thread_id' in session_data and session_data['thread_id']:
                    restored_session['thread_id'] = session_data['thread_id']
                    print(f"🔄 Restored forum topic session {session_data['thread_id']} for user {session_data['user_name']} ({chat_id})")
                else:
                    print(f"🔄 Restored session for user {session_data['user_name']} ({chat_id})")
                
                self.active_sessions[chat_id] = restored_session
                # Set default view to dashboard for restored sessions
                self.user_current_view[chat_id] = 'dashboard'
            
            print(f"🔄 Restored {len(sessions_data)} active sessions from Redis")
            
        except Exception as e:
            print(f"❌ Error loading sessions: {e}")
    
    def cleanup_sessions(self):
        """Clean up sessions on normal shutdown"""
        try:
            # Redis sessions have TTL, so they'll expire automatically
            # But we can clean them up immediately if needed
            cleaned = 0
            for chat_id in list(self.active_sessions.keys()):
                if self.session_manager.delete_session(chat_id):
                    cleaned += 1
            
            if cleaned > 0:
                print(f"🧹 Cleaned up {cleaned} sessions from Redis")
            else:
                print("🧹 No sessions to clean up")
        except Exception as e:
            print(f"❌ Error cleaning up sessions: {e}")
    
    def initialize_data(self):
        """Initialize monitoring data"""
        # Initialize WebSocket endpoints
        for key, config in WEBSOCKET_ENDPOINTS.items():
            if config["enabled"]:
                self.global_status_data['websockets'][config['name']] = {
                    'status': 'WAITING',
                    'last_update': datetime.now(),
                    'message_count': 0,
                    'last_message': ''
                }
        
        # Initialize API endpoints
        for endpoint in API_CONFIG['endpoints']:
            self.global_status_data['api_endpoints'][endpoint] = {
                'status': 'WAITING',
                'last_update': datetime.now(),
                'response_times': [],
                'success_count': 0,
                'error_count': 0,
                'last_response_time': None
            }
    
    def update_websocket_status(self, endpoint, status, message=""):
        """Update WebSocket status"""
        if endpoint in self.global_status_data['websockets']:
            self.global_status_data['websockets'][endpoint].update({
                'status': status,
                'last_update': datetime.now(),
                'last_message': message
            })
            
            if status == 'ACTIVE' or (message and len(message) > 0):
                if 'message_count' not in self.global_status_data['websockets'][endpoint]:
                    self.global_status_data['websockets'][endpoint]['message_count'] = 0
                self.global_status_data['websockets'][endpoint]['message_count'] += 1
                self.global_status_data['system_info']['total_messages'] += 1
    
    def update_api_status(self, endpoint, status, response_time=None):
        """Update API status"""
        if endpoint in self.global_status_data['api_endpoints']:
            self.global_status_data['api_endpoints'][endpoint].update({
                'status': status,
                'last_update': datetime.now(),
                'last_response_time': response_time
            })
            
            if status == 'SUCCESS':
                self.global_status_data['api_endpoints'][endpoint]['success_count'] += 1
                if response_time:
                    self.global_status_data['api_endpoints'][endpoint]['response_times'].append(response_time)
                    if len(self.global_status_data['api_endpoints'][endpoint]['response_times']) > 10:
                        self.global_status_data['api_endpoints'][endpoint]['response_times'].pop(0)
            else:
                self.global_status_data['api_endpoints'][endpoint]['error_count'] += 1
            
            self.global_status_data['system_info']['total_api_calls'] += 1
    
    def format_status_message(self, chat_id):
        """Format monitoring data for Telegram message"""
        now = datetime.now()
        uptime = now - self.global_status_data['system_info']['start_time']
        
        # Header with user info
        message = f"🤖 **OutLight.fun WebSocket & API Monitor (291025)**\n"
        message += f"{EMOJI['time']} {now.strftime('%H:%M:%S')} | Uptime: {str(uptime).split('.')[0]}\n"
        message += f"👤 Your Chat ID: `{chat_id}`\n"
        
        # Show if session was restored
        if chat_id in self.active_sessions and self.active_sessions[chat_id].get('restored', False):
            message += f"🔄 Session restored after bot restart\n"
        
        message += "\n"
        
        # WebSocket Status
        message += f"{EMOJI['websocket']} **WebSocket Connections:**\n"
        for name, data in self.global_status_data['websockets'].items():
            status_emoji = self.get_status_emoji(data['status'])
            last_update = data['last_update'].strftime('%H:%M:%S')
            message_count = data['message_count']
            
            message += f"{status_emoji} `{name}`\n"
            message += f"   Status: **{data['status']}** | Messages: {message_count}\n"
            message += f"   Last: {last_update}\n\n"
        
        # API Status
        message += f"{EMOJI['api']} **API Endpoints:**\n"
        for endpoint, data in self.global_status_data['api_endpoints'].items():
            status_emoji = self.get_status_emoji(data['status'])
            last_update = data['last_update'].strftime('%H:%M:%S')
            success_count = data['success_count']
            error_count = data['error_count']
            
            # Average response time
            avg_time = ""
            if data['response_times']:
                avg = sum(data['response_times']) / len(data['response_times'])
                avg_time = f" | {EMOJI['speed']} {avg:.0f}ms"
            
            message += f"{status_emoji} `{endpoint}`\n"
            message += f"   {EMOJI['success']} {success_count} | {EMOJI['error']} {error_count}{avg_time}\n"
            message += f"   Last: {last_update}\n\n"
        
        # System Info
        total_messages = self.global_status_data['system_info']['total_messages']
        total_api_calls = self.global_status_data['system_info']['total_api_calls']
        active_users = len(self.active_sessions)
        
        message += f"📊 **Statistics:**\n"
        message += f"📨 Total Messages: {total_messages}\n"
        message += f"🌐 Total API Calls: {total_api_calls}\n"
        message += f"👥 Active Users: {active_users}\n"
        message += f"🔄 Auto-refresh: {UPDATE_INTERVAL}s\n\n"
        
        # Recent Errors section
        if self.global_status_data['recent_errors']:
            message += f"🔴 **Recent Errors (Last 5):**\n"
            for error in self.global_status_data['recent_errors']:
                error_time = error['timestamp'].strftime('%H:%M:%S')
                source = error['source']
                error_msg = error['message']
                message += f"`{error_time}` {source}: {error_msg}\n"
            message += "\n"
        
        # Pending Errors section (temporary issues)
        if self.pending_errors:
            message += f"⏳ **Pending Issues (recovering...):**\n"
            for source, error_data in self.pending_errors.items():
                error_time = error_data['timestamp'].strftime('%H:%M:%S')
                age = (datetime.now() - error_data['timestamp']).total_seconds()
                message += f"`{error_time}` {source}: {error_data['error']} ({age:.0f}s ago)\n"
            message += "\n"
        
        message += f"💡 **Use buttons below for quick actions**"
        
        return message
    
    def get_status_emoji(self, status):
        """Get emoji for status"""
        status_map = {
            'CONNECTED': EMOJI['connected'],
            'ACTIVE': EMOJI['active'],
            'SUCCESS': EMOJI['success'],
            'ERROR': EMOJI['error'],
            'DISCONNECTED': EMOJI['disconnected'],
            'WAITING': EMOJI['waiting'],
            'CONNECTING': EMOJI['warning']
        }
        return status_map.get(status, EMOJI['warning'])
    
    def get_keyboard(self, chat_id):
        """Create inline keyboard with action buttons"""
        if chat_id in self.active_sessions:
            # User has active session - show monitoring controls
            keyboard = [
                [
                    InlineKeyboardButton("🔄 Refresh Status", callback_data="refresh"),
                    InlineKeyboardButton("🚨 All Errors", callback_data="all_errors")
                ],
                [
                    InlineKeyboardButton("📋 Logs", callback_data="logs"),
                    InlineKeyboardButton("🧹 Clear Errors", callback_data="clear")
                ],
                [
                    InlineKeyboardButton("📥 Download Logs", callback_data="download_logs"),
                    InlineKeyboardButton("📥 Download Errors", callback_data="download_errors")
                ],
                [
                    InlineKeyboardButton("❓ Help", callback_data="help")
                ]
            ]
        else:
            # User has no session - show start button
            keyboard = [
                [
                    InlineKeyboardButton("▶️ Start Monitoring", callback_data="start"),
                    InlineKeyboardButton("❓ Help", callback_data="help")
                ]
            ]
        
        return InlineKeyboardMarkup(keyboard)
    
    def get_errors_keyboard(self):
        """Create keyboard for errors view with filter buttons"""
        keyboard = [
            [
                InlineKeyboardButton("🌐 API Errors", callback_data="api_errors"),
                InlineKeyboardButton("📡 WS Errors", callback_data="ws_errors")
            ],
            [
                InlineKeyboardButton("📥 Download All Errors", callback_data="download_errors"),
                InlineKeyboardButton("🔙 Back", callback_data="back")
            ]
        ]
        return InlineKeyboardMarkup(keyboard)
    
    def add_log(self, log_type, source, message):
        """Add log entry with timestamp"""
        log_entry = {
            'timestamp': datetime.now().strftime('%H:%M:%S'),
            'type': log_type,  # 'INFO', 'SUCCESS', 'WARNING', 'ERROR', 'CONNECTING'
            'source': source,
            'message': str(message)  # Just convert to string, HTML escaping happens in display
        }
        
        # Add to logs list
        self.global_status_data['logs'].append(log_entry)
        
        # Keep only last 100 logs to prevent memory issues
        if len(self.global_status_data['logs']) > 100:
            self.global_status_data['logs'] = self.global_status_data['logs'][-100:]
        
        # Log to file based on type
        if hasattr(self, 'file_logger'):
            if log_type in ['ERROR', 'CRITICAL']:
                self.file_logger.log_error(source, str(message), log_type)
            elif log_type == 'HEALTH':
                self.file_logger.log_health(f"{source} | {message}")
            elif log_type == 'SYSTEM':
                self.file_logger.log_bot_event(log_type, f"{source} | {message}")
            else:
                self.file_logger.log_monitoring(source, log_type, str(message))
    
    def sanitize_for_markdown(self, text):
        """Sanitize text to prevent Markdown parsing issues"""
        if not text:
            return text
        
        # Replace problematic characters that can break Markdown
        sanitized = str(text)
        
        # Escape Markdown special characters
        markdown_chars = ['*', '_', '`', '[', ']', '(', ')', '~', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
        for char in markdown_chars:
            sanitized = sanitized.replace(char, f'\\{char}')
        
        return sanitized
    
    def escape_html(self, text):
        """Escape HTML special characters"""
        if not text:
            return text
        
        text = str(text)
        text = text.replace('&', '&amp;')
        text = text.replace('<', '&lt;')
        text = text.replace('>', '&gt;')
        text = text.replace('"', '&quot;')
        text = text.replace("'", '&#x27;')
        
        return text
    
    def format_logs_message(self):
        """Format logs message"""
        logs = self.global_status_data.get('logs', [])
        
        message = f"📋 <b>OutLight.fun Monitoring Logs (291025)</b>\n\n"
        
        if not logs:
            message += "📝 <b>No logs yet!</b>\n"
            message += "Logs will appear here as monitoring activity occurs.\n\n"
            return message
        
        # Show log statistics
        log_types = {}
        for log in logs:
            log_type = log.get('type', 'UNKNOWN')
            log_types[log_type] = log_types.get(log_type, 0) + 1
        
        message += f"📊 <b>Log Summary (Last {len(logs)} entries):</b>\n"
        for log_type, count in sorted(log_types.items()):
            emoji = self.get_log_type_emoji(log_type)
            message += f"{emoji} {log_type}: {count}\n"
        message += "\n"
        
        # Show recent logs (last 20)
        message += f"🕒 <b>Recent Activity (Last {min(len(logs), 20)}):</b>\n"
        for log in logs[-20:]:
            timestamp = log.get('timestamp', 'Unknown')
            log_type = log.get('type', 'INFO')
            source = log.get('source', 'Unknown')
            msg = log.get('message', 'No message')[:60]
            
            emoji = self.get_log_type_emoji(log_type)
            # Use HTML formatting which is more reliable
            message += f"<code>{timestamp}</code> {emoji} <b>{self.escape_html(source)}</b>\n{self.escape_html(msg)}\n\n"
        
        return message
    
    def get_log_type_emoji(self, log_type):
        """Get emoji for log type"""
        emoji_map = {
            'INFO': 'ℹ️',
            'SUCCESS': '✅',
            'WARNING': '⚠️',
            'ERROR': '❌',
            'CONNECTING': '🔌',
            'MESSAGE': '📨',
            'SYSTEM': '🤖'
        }
        return emoji_map.get(log_type, 'ℹ️')
    
    def generate_logs_file(self) -> str:
        """Generate logs file content for download"""
        logs = self.global_status_data.get('logs', [])
        
        content = f"# OutLight.fun WebSocket & API Monitor - Logs Export\n"
        content += f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        content += f"# Total logs: {len(logs)}\n\n"
        
        # Group logs by type
        log_types = {}
        for log in logs:
            log_type = log.get('type', 'UNKNOWN')
            if log_type not in log_types:
                log_types[log_type] = []
            log_types[log_type].append(log)
        
        content += f"## Log Summary\n"
        for log_type, type_logs in sorted(log_types.items()):
            content += f"- {log_type}: {len(type_logs)} entries\n"
        content += "\n"
        
        # All logs chronologically
        content += f"## All Logs (Chronological)\n\n"
        for log in logs:
            timestamp = log.get('timestamp', 'Unknown')
            log_type = log.get('type', 'INFO')
            source = log.get('source', 'Unknown')
            message = log.get('message', 'No message')
            
            content += f"[{timestamp}] {log_type} - {source}: {message}\n"
        
        return content
    
    def generate_errors_file(self) -> str:
        """Generate errors file content for download"""
        recent_errors = self.global_status_data.get('recent_errors', [])
        pending_history = self.global_status_data.get('pending_history', [])
        current_pending = []
        
        # Convert current pending errors to list format
        for source, error_data in self.pending_errors.items():
            current_pending.append({
                'timestamp': error_data['timestamp'],
                'source': source,
                'message': error_data['error'],
                'status': 'pending'
            })
        
        content = f"# OutLight.fun WebSocket & API Monitor - Errors Export\n"
        content += f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        content += f"# Recent errors: {len(recent_errors)}\n"
        content += f"# Pending history: {len(pending_history)}\n"
        content += f"# Current pending: {len(current_pending)}\n\n"
        
        # Recent confirmed errors
        if recent_errors:
            content += f"## Recent Confirmed Errors ({len(recent_errors)})\n\n"
            for error in recent_errors:
                timestamp = error['timestamp'].strftime('%Y-%m-%d %H:%M:%S')
                source = error.get('source', 'Unknown')
                message = error.get('message', 'No message')
                content += f"[{timestamp}] ERROR - {source}: {message}\n"
            content += "\n"
        
        # Connection issues history
        if pending_history:
            content += f"## Connection Issues History ({len(pending_history)})\n\n"
            for entry in pending_history:
                timestamp = entry['timestamp'].strftime('%Y-%m-%d %H:%M:%S')
                source = entry['source']
                message = entry['message']
                status = entry['status'].upper()
                
                if status == 'RESOLVED':
                    resolution_time = entry.get('resolution_time', 0)
                    content += f"[{timestamp}] {status} - {source}: {message} (recovered in {resolution_time:.1f}s)\n"
                else:
                    content += f"[{timestamp}] {status} - {source}: {message}\n"
            content += "\n"
        
        # Current pending errors
        if current_pending:
            content += f"## Current Pending Errors ({len(current_pending)})\n\n"
            for error in current_pending:
                timestamp = error['timestamp'].strftime('%Y-%m-%d %H:%M:%S')
                source = error['source']
                message = error['message']
                content += f"[{timestamp}] PENDING - {source}: {message}\n"
            content += "\n"
        
        # Error statistics
        content += f"## Error Statistics\n\n"
        
        # Count by source
        sources = {}
        for error in recent_errors:
            source = error.get('source', 'Unknown')
            sources[source] = sources.get(source, 0) + 1
        
        for entry in pending_history:
            source = entry['source']
            sources[source] = sources.get(source, 0) + 1
        
        content += f"### Errors by Source\n"
        for source, count in sorted(sources.items(), key=lambda x: x[1], reverse=True):
            content += f"- {source}: {count} errors\n"
        
        return content
    
    def format_all_errors_message(self):
        """Format all errors message"""
        recent_errors = self.global_status_data.get('recent_errors', [])
        # Convert pending_errors dict to list format
        pending_errors = []
        for source, error_data in self.pending_errors.items():
            pending_errors.append({
                'timestamp': error_data['timestamp'].strftime('%H:%M:%S'),
                'source': source,
                'message': error_data['error']
            })
        
        message = f"🚨 **OutLight.fun Errors Overview (291025)**\n\n"
        
        # Count errors by type
        api_errors = [e for e in recent_errors if 'API' in e.get('source', '')]
        ws_errors = [e for e in recent_errors if 'WebSocket' in e.get('source', '')]
        pending_ws_errors = [e for e in pending_errors if 'WebSocket' in e.get('source', '')]
        
        message += f"📊 **Error Summary:**\n"
        message += f"🌐 API Errors: {len(api_errors)}\n"
        message += f"📡 WebSocket Errors: {len(ws_errors)}\n"
        message += f"⏳ Pending WS Errors: {len(pending_ws_errors)}\n\n"
        
        # Show recent errors (last 10)
        if recent_errors:
            message += f"🕒 **Recent Errors (Last {min(len(recent_errors), 10)}):**\n"
            for error in recent_errors[-10:]:
                timestamp = error.get('timestamp', 'Unknown')
                source = error.get('source', 'Unknown')
                msg = error.get('message', 'No message')[:50]
                message += f"• `{timestamp}` - {source}: {msg}...\n"
        else:
            message += "✅ **No recent errors!**\n"
        
        # Show current pending errors
        if pending_errors:
            message += f"\n⏳ **Current Pending Errors ({len(pending_errors)}):**\n"
            for error in pending_errors[-5:]:
                timestamp = error.get('timestamp', 'Unknown')
                source = error.get('source', 'Unknown')
                msg = error.get('message', 'No message')[:50]
                message += f"• `{timestamp}` - {source}: {msg}...\n"
        
        # Show pending history (all pending errors - resolved and unresolved)
        pending_history = self.global_status_data.get('pending_history', [])
        if pending_history:
            message += f"\n📋 **Connection Issues History ({len(pending_history)}):**\n"
            for entry in pending_history[-10:]:  # Show last 10
                timestamp = entry['timestamp'].strftime('%H:%M:%S')
                source = entry['source']
                msg = entry['message'][:50]
                status = entry['status']
                
                if status == 'resolved':
                    resolution_time = entry.get('resolution_time', 0)
                    message += f"• `{timestamp}` ✅ {source}: {msg}... (recovered in {resolution_time:.1f}s)\n"
                elif status == 'confirmed':
                    message += f"• `{timestamp}` ❌ {source}: {msg}... (confirmed)\n"
                else:
                    message += f"• `{timestamp}` ⏳ {source}: {msg}... (pending)\n"
        
        message += f"\n💡 Use filter buttons below to view specific error types."
        
        return message
    
    def format_filtered_errors_message(self, error_type):
        """Format filtered errors message"""
        recent_errors = self.global_status_data.get('recent_errors', [])
        # Convert pending_errors dict to list format
        pending_errors = []
        for source, error_data in self.pending_errors.items():
            pending_errors.append({
                'timestamp': error_data['timestamp'].strftime('%H:%M:%S'),
                'source': source,
                'message': error_data['error']
            })
        
        if error_type == "api":
            title = "🌐 **OutLight.fun API Errors (291025)**"
            filtered_errors = [e for e in recent_errors if 'API' in e.get('source', '')]
            filtered_pending = []  # API errors don't have pending state
            filtered_history = []  # API errors don't have pending history
        else:  # ws_errors
            title = "📡 **OutLight.fun WebSocket Errors (291025)**"
            filtered_errors = [e for e in recent_errors if 'WebSocket' in e.get('source', '')]
            filtered_pending = [e for e in pending_errors if 'WebSocket' in e.get('source', '')]
            # Add pending history for WebSocket errors
            pending_history = self.global_status_data.get('pending_history', [])
            filtered_history = [e for e in pending_history if 'WebSocket' in e.get('source', '')]
        
        message = f"{title}\n\n"
        
        # Show filtered errors
        if filtered_errors:
            message += f"🔴 **Confirmed Errors ({len(filtered_errors)}):**\n"
            for error in filtered_errors[-15:]:  # Show more for filtered view
                timestamp = error.get('timestamp', 'Unknown')
                source = error.get('source', 'Unknown')
                msg = error.get('message', 'No message')[:80]
                message += f"• `{timestamp}`\n  {source}: {msg}\n\n"
        
        # Show connection issues history for WebSocket
        if error_type == "ws" and filtered_history:
            message += f"📋 **Connection Issues History ({len(filtered_history)}):**\n"
            for entry in filtered_history[-15:]:
                timestamp = entry['timestamp'].strftime('%H:%M:%S')
                source = entry['source']
                msg = entry['message'][:80]
                status = entry['status']
                
                if status == 'resolved':
                    resolution_time = entry.get('resolution_time', 0)
                    message += f"• `{timestamp}` ✅ {source}\n  {msg} (recovered in {resolution_time:.1f}s)\n\n"
                elif status == 'confirmed':
                    message += f"• `{timestamp}` ❌ {source}\n  {msg} (confirmed)\n\n"
                else:
                    message += f"• `{timestamp}` ⏳ {source}\n  {msg} (pending)\n\n"
        
        # Check if we have any errors to show
        if not filtered_errors and not (error_type == "ws" and filtered_history):
            message += "✅ **No errors of this type!**\n\n"
        
        # Show pending errors for WebSocket
        if error_type == "ws" and filtered_pending:
            message += f"⏳ **Pending Errors ({len(filtered_pending)}):**\n"
            message += f"*(Will become confirmed if not resolved in 20s)*\n\n"
            for error in filtered_pending[-10:]:
                timestamp = error.get('timestamp', 'Unknown')
                source = error.get('source', 'Unknown')
                msg = error.get('message', 'No message')[:80]
                message += f"• `{timestamp}`\n  {source}: {msg}\n\n"
        
        return message
    
    async def send_or_update_user_message(self, chat_id, query=None):
        """Send or update message for specific user"""
        if chat_id not in self.active_sessions:
            print(f"⚠️ No active session for user {chat_id}")
            return
        
        max_retries = 3
        retry_delay = 2
        
        # Check if message content changed
        message_text = self.format_status_message(chat_id)
        if chat_id in self.last_message_content and self.last_message_content[chat_id] == message_text:
            # Message hasn't changed, skip update
            print(f"⏭️ Skipping update for {chat_id} - no changes")
            return
        
        # Throttling: don't edit more than once every 3 seconds (except for new messages)
        current_time = time.time()
        if (chat_id in self.last_edit_time and 
            self.active_sessions[chat_id].get('message_id') is not None and  # Only throttle edits, not new messages
            current_time - self.last_edit_time[chat_id] < 3):
            print(f"⏳ Throttling update for {chat_id} - too soon (last edit {current_time - self.last_edit_time[chat_id]:.1f}s ago)")
            return
        
        print(f"🔄 Message changed for {chat_id}, updating...")
        self.last_edit_time[chat_id] = current_time
        
        for attempt in range(max_retries):
            try:
                bot = Bot(token=self.bot_token)
                session = self.active_sessions[chat_id]
                
                # Use actual_chat_id if available (for configured chats), otherwise use chat_id
                # If chat_id is a string (session_key), we must use actual_chat_id
                if isinstance(chat_id, str) and 'actual_chat_id' in session:
                    actual_chat_id = session['actual_chat_id']
                else:
                    actual_chat_id = session.get('actual_chat_id', chat_id if isinstance(chat_id, int) else None)
                
                # Safety check
                if actual_chat_id is None:
                    print(f"❌ ERROR: Cannot determine actual_chat_id for session {chat_id}")
                    return
                
                keyboard = self.get_keyboard(chat_id)
                
                if query and session['message_id'] is not None:
                    # Edit via callback query only if we have a valid message_id
                    await query.edit_message_text(
                        text=message_text,
                        parse_mode='Markdown',
                        reply_markup=keyboard
                    )
                    session['message_id'] = query.message.message_id
                    print(f"🔄 Successfully updated message via callback for user {chat_id}")
                elif session['message_id'] is None:
                    # Send new message (with thread support for forum groups)
                    send_kwargs = {
                        'chat_id': actual_chat_id,
                        'text': message_text,
                        'parse_mode': 'Markdown',
                        'reply_markup': keyboard
                    }
                    
                    # Add thread_id if this is a forum topic
                    if 'thread_id' in session and session['thread_id']:
                        send_kwargs['message_thread_id'] = session['thread_id']
                        print(f"📍 Sending NEW message to forum topic {session['thread_id']} in chat {actual_chat_id} (session_key: {chat_id})")
                    else:
                        print(f"📍 Sending NEW message to main channel {actual_chat_id} (session_key: {chat_id}, thread_id: {session.get('thread_id', 'None')})")
                    
                    message = await bot.send_message(**send_kwargs)
                    session['message_id'] = message.message_id
                    print(f"✅ Sent initial message to user {chat_id} (ID: {message.message_id})")
                else:
                    # Edit existing message
                    print(f"🔄 Attempting to edit message {session['message_id']} for user {chat_id}")
                    edit_kwargs = {
                        'chat_id': actual_chat_id,
                        'message_id': session['message_id'],
                        'text': message_text,
                        'parse_mode': 'Markdown',
                        'reply_markup': keyboard
                    }
                    
                    # Add thread_id if this is a forum topic
                    if 'thread_id' in session and session['thread_id']:
                        edit_kwargs['message_thread_id'] = session['thread_id']
                        print(f"📍 Editing message in forum topic {session['thread_id']} in chat {actual_chat_id} (session_key: {chat_id})")
                    else:
                        print(f"📍 Editing message in main channel {actual_chat_id} (session_key: {chat_id}, thread_id: {session.get('thread_id', 'None')})")
                    
                    await bot.edit_message_text(**edit_kwargs)
                    print(f"🔄 Successfully updated message {session['message_id']} for user {chat_id}")
                
                # Success - save message content and break retry loop
                self.last_message_content[chat_id] = message_text
                break
                
            except TelegramError as e:
                error_msg = str(e).lower()
                if ("message to edit not found" in error_msg or 
                    "message is not modified" in error_msg or 
                    "bad request" in error_msg or
                    "there is no text in the message to edit" in error_msg or
                    "message can't be edited" in error_msg or
                    "too old" in error_msg):
                    print(f"📝 Message {session.get('message_id', 'unknown')} cannot be edited for {chat_id}, sending new one")
                    self.active_sessions[chat_id]['message_id'] = None
                    
                    # Send new message immediately instead of retrying
                    try:
                        send_kwargs = {
                            'chat_id': actual_chat_id,
                            'text': message_text,
                            'parse_mode': 'Markdown',
                            'reply_markup': keyboard
                        }
                        
                        # Add thread_id if this is a forum topic
                        if 'thread_id' in session and session['thread_id']:
                            send_kwargs['message_thread_id'] = session['thread_id']
                            print(f"📍 Sending to forum topic {session['thread_id']} in chat {chat_id}")
                        
                        message = await bot.send_message(**send_kwargs)
                        self.active_sessions[chat_id]['message_id'] = message.message_id
                        self.last_message_content[chat_id] = message_text
                        print(f"✅ Sent new message to user {chat_id} (ID: {message.message_id})")
                        break
                    except Exception as send_error:
                        print(f"❌ Error sending new message: {send_error}")
                        break
                elif "flood control exceeded" in str(e).lower():
                    print(f"⏳ Rate limited for {chat_id}, skipping this update")
                    break  # Don't retry rate limits
                elif "chat not found" in str(e).lower() or "forbidden: bot was blocked by the user" in str(e).lower():
                    print(f"🗑️ Chat {chat_id} not found or bot blocked - removing session")
                    # Remove the session as the chat is no longer accessible
                    if chat_id in self.active_sessions:
                        user_name = self.active_sessions[chat_id].get('user_name', 'Unknown')
                        del self.active_sessions[chat_id]
                        # Clean up related data
                        if chat_id in self.last_message_content:
                            del self.last_message_content[chat_id]
                        if chat_id in self.user_current_view:
                            del self.user_current_view[chat_id]
                        if chat_id in self.last_edit_time:
                            del self.last_edit_time[chat_id]
                        # Remove from Redis
                        try:
                            self.session_manager.delete_session(chat_id)
                            print(f"🗑️ Removed invalid session for {user_name} ({chat_id}) from Redis")
                        except Exception as redis_error:
                            print(f"⚠️ Could not remove session from Redis: {redis_error}")
                        
                        # Save updated sessions
                        self.save_active_sessions()
                    break  # Don't retry for invalid chats
                elif "networkerror" in str(e).lower() or "connecterror" in str(e).lower():
                    print(f"🌐 Network error for {chat_id} (attempt {attempt + 1}/{max_retries}): {e}")
                    if attempt == max_retries - 1:  # Only log on final attempt
                        self.add_error("Telegram API", f"Network error for user {chat_id}: {str(e)[:50]}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(retry_delay * (attempt + 1))  # Exponential backoff
                        continue
                    else:
                        print(f"❌ Failed to update {chat_id} after {max_retries} attempts")
                else:
                    self.add_error("Telegram API", f"Error for user {chat_id}: {str(e)[:50]}")
                    print(f"❌ Telegram error for {chat_id}: {e}")
                    break
            except Exception as e:
                print(f"❌ Unexpected error for {chat_id} (attempt {attempt + 1}/{max_retries}): {e}")
                if attempt == max_retries - 1:  # Only log on final attempt
                    self.add_error("Bot Error", f"Unexpected error for user {chat_id}: {str(e)[:50]}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay)
                    continue
                else:
                    print(f"❌ Failed to update {chat_id} after {max_retries} attempts")
    
    async def update_all_users(self):
        """Send interactive dashboard updates ONLY to configured chats from .env CHAT_ID"""
        if not CHAT_IDS or CHAT_IDS == ['your_chat_id_here']:
            print("⚠️ No configured CHAT_IDS in .env, skipping dashboard updates")
            return
        
        # Send to configured chats only with interactive buttons
        print(f"🔄 Sending interactive dashboard updates to {len(CHAT_IDS)} configured chats")
        tasks = []
        
        for chat_id_str in CHAT_IDS:
            try:
                chat_id, thread_id = parse_chat_id(chat_id_str.strip())
                
                # Check if there's already a user session for this chat_id
                # If so, skip configured updates to avoid conflicts
                user_session_exists = any(
                    isinstance(key, int) and key == chat_id 
                    for key in self.active_sessions.keys()
                )
                
                # Also check if there's a user session with the same thread_id
                if thread_id and not user_session_exists:
                    user_session_exists = any(
                        isinstance(key, int) and key == chat_id and 
                        self.active_sessions[key].get('thread_id') == thread_id
                        for key in self.active_sessions.keys()
                    )
                
                if user_session_exists:
                    print(f"⏭️ Skipping configured updates for {chat_id_str} - user session active")
                    continue
                
                # Create a fake session for configured chat to use existing interactive logic
                # Use full chat_id_str as key to avoid conflicts between main group and forum topics
                session_key = f"configured_{chat_id_str.replace('-', 'neg').replace('_', 'thread')}"
                
                if session_key not in self.active_sessions:
                    self.active_sessions[session_key] = {
                        'user_name': f'Configured Chat {chat_id_str}',
                        'start_time': datetime.now().isoformat(),
                        'message_id': None,
                        'thread_id': thread_id,
                        'actual_chat_id': chat_id  # Store the actual numeric chat ID
                    }
                    print(f"📝 Created configured session: key={session_key}, chat_id={chat_id}, thread_id={thread_id}")
                else:
                    print(f"📝 Using existing configured session: key={session_key}, chat_id={chat_id}, thread_id={thread_id}")
                
                # Set current view to dashboard for this session
                self.user_current_view[session_key] = 'dashboard'
                
                # Use existing interactive message sending logic with session key
                task = asyncio.create_task(self.send_or_update_user_message(session_key))
                tasks.append(task)
                
            except Exception as e:
                print(f"❌ Error preparing dashboard update for '{chat_id_str}': {e}")
        
        if tasks:
            # Send all updates in parallel
            try:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                success_count = sum(1 for r in results if not isinstance(r, Exception))
                print(f"✅ Interactive dashboard updates sent to configured chats: {success_count}/{len(tasks)} successful")
            except Exception as e:
                print(f"❌ Error in parallel dashboard updates: {e}")
        else:
            print("⚠️ No valid chat IDs for dashboard updates")
    
    # Command handlers
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        chat_id = update.effective_chat.id
        user_name = update.effective_user.first_name or "User"
        chat_type = update.effective_chat.type
        
        # Handle different chat types
        if chat_type in ['group', 'supergroup']:
            # In groups, use group chat_id but show user who started
            group_name = update.effective_chat.title or "Group"
            thread_id = getattr(update.message, 'message_thread_id', None)
            
            if thread_id:
                print(f"📱 Bot started in forum topic {thread_id} of group '{group_name}' ({chat_id}) by {user_name}")
                await update.message.reply_text(
                    f"🎉 Welcome to forum topic monitoring!\n\n"
                    f"Group: **{group_name}**\n"
                    f"Topic ID: **{thread_id}**\n"
                    f"Started by: **{user_name}**\n"
                    f"Chat ID: `{chat_id}_{thread_id}`\n\n"
                    f"This topic will receive live updates every {UPDATE_INTERVAL} seconds.\n\n"
                    f"Use the buttons below for easy control!",
                    parse_mode='Markdown'
                )
                await self.handle_start_action(chat_id, f"{group_name} Topic {thread_id} (started by {user_name})", thread_id)
            else:
                print(f"📱 Bot started in group '{group_name}' ({chat_id}) by {user_name}")
                await update.message.reply_text(
                    f"🎉 Welcome to group monitoring!\n\n"
                    f"Group: **{group_name}**\n"
                    f"Started by: **{user_name}**\n"
                    f"Chat ID: `{chat_id}`\n\n"
                    f"The group will receive live updates every {UPDATE_INTERVAL} seconds.\n\n"
                    f"Use the buttons below for easy control!",
                    parse_mode='Markdown'
                )
                await self.handle_start_action(chat_id, f"{group_name} (started by {user_name})")
        else:
            # Private chat
            await update.message.reply_text(
                f"🎉 Welcome {user_name}!\n\n"
                f"Your monitoring session has started!\n"
                f"Chat ID: `{chat_id}`\n\n"
                f"You will receive live updates every {UPDATE_INTERVAL} seconds.\n\n"
                f"Use the buttons below for easy control!",
                parse_mode='Markdown'
            )
            
            await self.handle_start_action(chat_id, user_name)
    
    
    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status command"""
        chat_id = update.effective_chat.id
        
        try:
            if chat_id in self.active_sessions:
                message_text = self.format_status_message(chat_id)
                await update.message.reply_text(message_text, parse_mode='Markdown')
            else:
                await update.message.reply_text(
                    "❌ You don't have an active monitoring session.\n"
                    "Use /start to begin monitoring."
                )
        except Exception as e:
            print(f"❌ Error in status command for {chat_id}: {e}")
            self.add_error("Command /status", str(e))
            try:
                await update.message.reply_text(
                    "❌ Network error. Please try again in a moment."
                )
            except:
                pass  # If even this fails, just log it
    
    async def clear_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /clear command - clear error history"""
        chat_id = update.effective_chat.id
        
        try:
            if chat_id in self.active_sessions:
                self.global_status_data['recent_errors'] = []
                await update.message.reply_text(
                    "✅ Error history cleared!\n"
                    "Recent errors list has been reset."
                )
                print(f"🧹 Error history cleared by user {chat_id}")
            else:
                await update.message.reply_text(
                    "❌ You don't have an active monitoring session.\n"
                    "Use /start to begin monitoring."
                )
        except Exception as e:
            print(f"❌ Error in clear command for {chat_id}: {e}")
            self.add_error("Command /clear", str(e))
    
    async def send_test_message_to_configured(self):
        """Send test message directly to configured CHAT_ID from .env"""
        try:
            if not CHAT_IDS or CHAT_IDS == ['your_chat_id_here']:
                print("⚠️ No CHAT_ID configured in .env file, skipping test message")
                return
            
            bot = Bot(token=self.bot_token)
            print(f"🧪 Sending test message to {len(CHAT_IDS)} configured chat(s)...")
            
            for chat_id_str in CHAT_IDS:
                try:
                    chat_id, thread_id = parse_chat_id(chat_id_str.strip())
                    
                    test_message = f"🧪 **Test Message from Bot**\n\n"
                    test_message += f"**Chat ID:** `{chat_id}`\n"
                    test_message += f"**Thread ID:** `{thread_id}`\n"
                    test_message += f"**Full CHAT_ID:** `{chat_id_str}`\n"
                    test_message += f"**Time:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                    
                    if thread_id:
                        test_message += f"📍 This message should appear in forum topic **{thread_id}**\n"
                        test_message += f"✅ If you see this in the topic, forum support is working!"
                    else:
                        test_message += f"📍 This message should appear on main channel\n"
                        test_message += f"✅ If you see this on main channel, it's working!"
                    
                    send_kwargs = {
                        'chat_id': chat_id,
                        'text': test_message,
                        'parse_mode': 'Markdown'
                    }
                    
                    if thread_id:
                        send_kwargs['message_thread_id'] = thread_id
                        print(f"🧪 Sending to forum topic {thread_id} in chat {chat_id}")
                    else:
                        print(f"🧪 Sending to main channel {chat_id}")
                    
                    await bot.send_message(**send_kwargs)
                    print(f"✅ Test message sent successfully to {chat_id_str}")
                    
                except Exception as e:
                    print(f"❌ Error sending test message to {chat_id_str}: {e}")
            
        except Exception as e:
            print(f"❌ Error in send_test_message_to_configured: {e}")
    
    async def test_forum_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Test command to send message directly to configured forum topic"""
        try:
            if not CHAT_IDS or CHAT_IDS == ['your_chat_id_here']:
                await update.message.reply_text(
                    "❌ No CHAT_ID configured in .env file"
                )
                return
            
            bot = Bot(token=self.bot_token)
            test_results = []
            
            for chat_id_str in CHAT_IDS:
                try:
                    chat_id, thread_id = parse_chat_id(chat_id_str.strip())
                    
                    test_message = f"🧪 **Test Message**\n\n"
                    test_message += f"**Chat ID:** `{chat_id}`\n"
                    test_message += f"**Thread ID:** `{thread_id}`\n"
                    test_message += f"**Full CHAT_ID:** `{chat_id_str}`\n"
                    test_message += f"**Time:** {datetime.now().strftime('%H:%M:%S')}\n\n"
                    
                    if thread_id:
                        test_message += f"📍 This message should appear in forum topic **{thread_id}**"
                    else:
                        test_message += f"📍 This message should appear on main channel"
                    
                    send_kwargs = {
                        'chat_id': chat_id,
                        'text': test_message,
                        'parse_mode': 'Markdown'
                    }
                    
                    if thread_id:
                        send_kwargs['message_thread_id'] = thread_id
                        print(f"🧪 TEST: Sending to forum topic {thread_id} in chat {chat_id}")
                    else:
                        print(f"🧪 TEST: Sending to main channel {chat_id}")
                    
                    await bot.send_message(**send_kwargs)
                    test_results.append(f"✅ Sent to {chat_id_str}")
                    
                except Exception as e:
                    error_msg = str(e)
                    test_results.append(f"❌ Error for {chat_id_str}: {error_msg[:100]}")
                    print(f"❌ TEST ERROR for {chat_id_str}: {e}")
            
            result_text = "🧪 **Test Results:**\n\n" + "\n".join(test_results)
            await update.message.reply_text(result_text, parse_mode='Markdown')
            
        except Exception as e:
            print(f"❌ Error in test_forum_command: {e}")
            await update.message.reply_text(f"❌ Test error: {str(e)[:200]}")
    
    async def safe_query_answer(self, query, text="", show_alert=False):
        """Safely answer callback query, ignoring timeout errors"""
        try:
            await query.answer(text=text, show_alert=show_alert)
        except TelegramError as e:
            if "query is too old" in str(e).lower() or "timeout expired" in str(e).lower():
                print(f"⚠️ Callback query too old, ignoring: {e}")
            else:
                print(f"❌ Callback query error: {e}")
    
    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle button callbacks"""
        query = update.callback_query
        
        # Safely acknowledge the callback - ignore if too old
        await self.safe_query_answer(query)
        
        chat_id = query.message.chat.id
        action = query.data
        
        try:
            if action == "start":
                # Simulate /start command
                await self.handle_start_action(chat_id, query.from_user.first_name or "User")
                self.user_current_view[chat_id] = 'dashboard'
                
                
            elif action == "refresh":
                # Force refresh status (with throttling)
                if chat_id in self.active_sessions:
                    # Check throttling - max 1 refresh per 5 seconds
                    current_time = time.time()
                    if (chat_id in self.last_edit_time and 
                        current_time - self.last_edit_time[chat_id] < 5):
                        remaining_time = 5 - (current_time - self.last_edit_time[chat_id])
                        await self.safe_query_answer(query, f"⏳ Please wait {remaining_time:.1f}s before refreshing again", show_alert=True)
                        return
                    
                    # Clear cache to force update
                    if chat_id in self.last_message_content:
                        del self.last_message_content[chat_id]
                    # Also clear throttling to allow immediate update
                    if chat_id in self.last_edit_time:
                        del self.last_edit_time[chat_id]
                    
                    await self.send_or_update_user_message(chat_id)
                    self.user_current_view[chat_id] = 'dashboard'
                    await self.safe_query_answer(query, "🔄 Status refreshed!")
                else:
                    await self.safe_query_answer(query, "❌ No active session to refresh.", show_alert=True)
                    
            elif action == "clear":
                # Clear error history (with throttling)
                if chat_id in self.active_sessions:
                    # Check throttling - max 1 clear per 3 seconds
                    current_time = time.time()
                    if (chat_id in self.last_edit_time and 
                        current_time - self.last_edit_time[chat_id] < 3):
                        remaining_time = 3 - (current_time - self.last_edit_time[chat_id])
                        await self.safe_query_answer(query, f"⏳ Please wait {remaining_time:.1f}s before clearing again", show_alert=True)
                        return
                    
                    self.global_status_data['recent_errors'] = []
                    await self.safe_query_answer(query, "✅ Error history cleared!")
                    print(f"🧹 Error history cleared by user {chat_id} via button")
                    
                    # Force refresh to show cleared errors
                    if chat_id in self.last_message_content:
                        del self.last_message_content[chat_id]
                    # Clear throttling to allow immediate update
                    if chat_id in self.last_edit_time:
                        del self.last_edit_time[chat_id]
                    
                    await self.send_or_update_user_message(chat_id)
                else:
                    await self.safe_query_answer(query, "❌ No active session.", show_alert=True)
                    
            elif action == "help":
                # Show help
                self.user_current_view[chat_id] = 'help'
                await self.show_help(chat_id, via_button=True, query=query)
                
            elif action == "logs":
                # Show logs view
                if chat_id in self.active_sessions:
                    message_text = self.format_logs_message()
                    keyboard = InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton("📥 Download Logs", callback_data="download_logs"),
                            InlineKeyboardButton("🔙 Back", callback_data="back")
                        ]
                    ])
                    await query.edit_message_text(
                        text=message_text,
                        parse_mode='HTML',
                        reply_markup=keyboard
                    )
                    self.user_current_view[chat_id] = 'logs'
                else:
                    await self.safe_query_answer(query, "❌ No active session.", show_alert=True)
                    
            elif action == "all_errors":
                # Show all errors view
                if chat_id in self.active_sessions:
                    message_text = self.format_all_errors_message()
                    keyboard = self.get_errors_keyboard()
                    await query.edit_message_text(
                        text=message_text,
                        parse_mode='Markdown',
                        reply_markup=keyboard
                    )
                    self.user_current_view[chat_id] = 'all_errors'
                else:
                    await self.safe_query_answer(query, "❌ No active session.", show_alert=True)
                    
            elif action == "api_errors":
                # Show filtered API errors
                if chat_id in self.active_sessions:
                    message_text = self.format_filtered_errors_message("api")
                    keyboard = InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton("📡 WS Errors", callback_data="ws_errors"),
                            InlineKeyboardButton("🚨 All Errors", callback_data="all_errors")
                        ],
                        [
                            InlineKeyboardButton("🔙 Back", callback_data="back")
                        ]
                    ])
                    await query.edit_message_text(
                        text=message_text,
                        parse_mode='Markdown',
                        reply_markup=keyboard
                    )
                    self.user_current_view[chat_id] = 'api_errors'
                else:
                    await self.safe_query_answer(query, "❌ No active session.", show_alert=True)
                    
            elif action == "ws_errors":
                # Show filtered WebSocket errors
                if chat_id in self.active_sessions:
                    message_text = self.format_filtered_errors_message("ws")
                    keyboard = InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton("🌐 API Errors", callback_data="api_errors"),
                            InlineKeyboardButton("🚨 All Errors", callback_data="all_errors")
                        ],
                        [
                            InlineKeyboardButton("🔙 Back", callback_data="back")
                        ]
                    ])
                    await query.edit_message_text(
                        text=message_text,
                        parse_mode='Markdown',
                        reply_markup=keyboard
                    )
                    self.user_current_view[chat_id] = 'ws_errors'
                else:
                    await self.safe_query_answer(query, "❌ No active session.", show_alert=True)
                    
            elif action == "back":
                # Go back to main status
                if chat_id in self.active_sessions:
                    # Clear cache to force update
                    if chat_id in self.last_message_content:
                        del self.last_message_content[chat_id]
                    self.user_current_view[chat_id] = 'dashboard'
                    await self.send_or_update_user_message(chat_id, query=query)
                else:
                    # Show welcome message for inactive users
                    welcome_text = f"""
👋 **Welcome to OutLight.fun WebSocket & API Monitor (291025)!**

This bot monitors WebSocket connections and API endpoints in real-time.

Press **Start Monitoring** to begin tracking:
📡 WebSocket connections (Price, Socket.IO)
🌐 API endpoints (/api/channels, /api/tokens/recent, /api/tokens/most-called)

Your monitoring session will persist across bot restarts!
                    """
                    keyboard = self.get_keyboard(chat_id)
                    await query.edit_message_text(
                        text=welcome_text,
                        parse_mode='Markdown',
                        reply_markup=keyboard
                    )
                    
            elif action == "download_logs":
                # Download logs file
                if chat_id in self.active_sessions:
                    try:
                        # Generate logs content
                        logs_content = self.generate_logs_file()
                        
                        # Create filename with timestamp
                        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                        filename = f"outlight_logs_{timestamp}.txt"
                        
                        # Save to temporary file
                        import tempfile
                        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
                            f.write(logs_content)
                            temp_path = f.name
                        
                        # Send file to user
                        from telegram import Bot
                        bot = Bot(token=self.bot_token)
                        
                        # Create keyboard with Back button
                        download_keyboard = InlineKeyboardMarkup([
                            [InlineKeyboardButton("🔙 Back to Dashboard", callback_data="back")]
                        ])
                        
                        # Prepare send_document arguments
                        session = self.active_sessions[chat_id]
                        send_kwargs = {
                            'chat_id': chat_id,
                            'document': None,  # Will be set below
                            'filename': filename,
                            'caption': f"📋 **Monitoring Logs Export**\n\nGenerated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\nTotal logs: {len(self.global_status_data.get('logs', []))}",
                            'reply_markup': download_keyboard
                        }
                        
                        # Add thread_id if this is a forum topic
                        if 'thread_id' in session and session['thread_id']:
                            send_kwargs['message_thread_id'] = session['thread_id']
                            print(f"📍 Sending logs file to forum topic {session['thread_id']} in chat {chat_id}")
                        
                        with open(temp_path, 'rb') as f:
                            send_kwargs['document'] = f
                            file_message = await bot.send_document(**send_kwargs)
                        
                        # Reset message_id so next status update sends new message instead of trying to edit file message
                        self.active_sessions[chat_id]['message_id'] = None
                        
                        # Clean up temp file
                        import os
                        os.unlink(temp_path)
                        
                        await self.safe_query_answer(query, "📥 Logs file sent!")
                        
                    except Exception as e:
                        print(f"❌ Error generating logs file: {e}")
                        await self.safe_query_answer(query, "❌ Error generating logs file", show_alert=True)
                else:
                    await self.safe_query_answer(query, "❌ No active session.", show_alert=True)
                    
            elif action == "download_errors":
                # Download errors file
                if chat_id in self.active_sessions:
                    try:
                        # Generate errors content
                        errors_content = self.generate_errors_file()
                        
                        # Create filename with timestamp
                        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                        filename = f"outlight_errors_{timestamp}.txt"
                        
                        # Save to temporary file
                        import tempfile
                        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
                            f.write(errors_content)
                            temp_path = f.name
                        
                        # Send file to user
                        from telegram import Bot
                        bot = Bot(token=self.bot_token)
                        
                        # Create keyboard with Back button
                        download_keyboard = InlineKeyboardMarkup([
                            [InlineKeyboardButton("🔙 Back to Dashboard", callback_data="back")]
                        ])
                        
                        # Prepare send_document arguments
                        session = self.active_sessions[chat_id]
                        send_kwargs = {
                            'chat_id': chat_id,
                            'document': None,  # Will be set below
                            'filename': filename,
                            'caption': f"🚨 **Errors Export**\n\nGenerated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\nRecent errors: {len(self.global_status_data.get('recent_errors', []))}\nPending history: {len(self.global_status_data.get('pending_history', []))}",
                            'reply_markup': download_keyboard
                        }
                        
                        # Add thread_id if this is a forum topic
                        if 'thread_id' in session and session['thread_id']:
                            send_kwargs['message_thread_id'] = session['thread_id']
                            print(f"📍 Sending errors file to forum topic {session['thread_id']} in chat {chat_id}")
                        
                        with open(temp_path, 'rb') as f:
                            send_kwargs['document'] = f
                            file_message = await bot.send_document(**send_kwargs)
                        
                        # Reset message_id so next status update sends new message instead of trying to edit file message
                        self.active_sessions[chat_id]['message_id'] = None
                        
                        # Clean up temp file
                        import os
                        os.unlink(temp_path)
                        
                        await self.safe_query_answer(query, "📥 Errors file sent!")
                        
                    except Exception as e:
                        print(f"❌ Error generating errors file: {e}")
                        await self.safe_query_answer(query, "❌ Error generating errors file", show_alert=True)
                else:
                    await self.safe_query_answer(query, "❌ No active session.", show_alert=True)
                
        except Exception as e:
            print(f"❌ Error in button callback {action} for {chat_id}: {e}")
            self.add_error(f"Button {action}", str(e))
            await self.safe_query_answer(query, "❌ Error processing request. Please try again.", show_alert=True)
    
    async def handle_start_action(self, chat_id, user_name, thread_id=None):
        """Handle start action (from command or button)"""
        # Add user to active sessions
        session_data = {
            'message_id': None,
            'start_time': datetime.now(),
            'user_name': user_name
        }
        
        # Add thread_id if this is a forum topic
        if thread_id:
            session_data['thread_id'] = thread_id
            print(f"📍 Starting session for forum topic {thread_id} in chat {chat_id}")
        
        self.active_sessions[chat_id] = session_data
        
        # Set default view to dashboard
        self.user_current_view[chat_id] = 'dashboard'
        
        # Start global monitoring if not already running
        if not self.global_monitoring_active:
            await self.start_global_monitoring()
            self.add_log("SYSTEM", "Bot", f"User {user_name} started monitoring")
        
        # Don't send message here - let update_user_sessions() handle it
        # This prevents duplicate messages on startup
        print(f"📝 Session created - update_user_sessions() will send first message")
        
        # Save sessions for restart recovery
        self.save_active_sessions()
        
        print(f"👤 User {user_name} ({chat_id}) started monitoring")
        
        # Log session start
        if hasattr(self, 'file_logger'):
            self.file_logger.log_bot_event("SESSION_START", f"User {user_name} ({chat_id}) started monitoring")
    
    
    async def show_help(self, chat_id, via_button=False, query=None):
        """Show help message"""
        help_text = f"""
🤖 **OutLight.fun WebSocket & API Monitor (291025)**

This bot monitors WebSocket connections and API endpoints in real-time.

**Features:**
• Real-time WebSocket monitoring
• API endpoint health checks every {UPDATE_INTERVAL}s
• Live message updates (no spam!)
• Personal monitoring sessions
• Multiple users supported
• Interactive buttons for easy control

**Monitored Services:**
📡 WebSocket: Price updates, Socket.IO
🌐 API: Channels, Tokens, Recent calls

**Button Controls:**
▶️ Start Monitoring - Begin your session
🔄 Refresh Status - Force update display
🧹 Clear Errors - Reset error history
❓ Help - Show this message

Just press the buttons to control your monitoring!
        """
        
        # Create help keyboard with Back button
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back", callback_data="back")]
        ])
        
        if via_button and query:
            # Edit message when called from button
            try:
                await query.edit_message_text(
                    text=help_text,
                    parse_mode='Markdown',
                    reply_markup=keyboard
                )
            except Exception as e:
                print(f"❌ Error editing help message for {chat_id}: {e}")
        elif via_button:
            # Send as new message when called from button (fallback)
            bot = Bot(token=self.bot_token)
            
            # Prepare send_message arguments
            send_kwargs = {
                'chat_id': chat_id,
                'text': help_text,
                'parse_mode': 'Markdown',
                'reply_markup': keyboard
            }
            
            # Add thread_id if this is a forum topic and user has active session
            if chat_id in self.active_sessions:
                session = self.active_sessions[chat_id]
                if 'thread_id' in session and session['thread_id']:
                    send_kwargs['message_thread_id'] = session['thread_id']
                    print(f"📍 Sending help message to forum topic {session['thread_id']} in chat {chat_id}")
            
            await bot.send_message(**send_kwargs)
        else:
            # Return text for command handler
            return help_text
    
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        help_text = await self.show_help(update.effective_chat.id, via_button=False)
        await update.message.reply_text(help_text, parse_mode='Markdown')
    
    # Monitoring functions (same as before but using global data)
    async def monitor_websocket(self, uri, name):
        """Monitor single WebSocket connection"""
        while self.global_monitoring_active:
            try:
                self.update_websocket_status(name, "CONNECTING")
                print(f"🔌 {name} - Connecting to {uri}")
                self.add_log("CONNECTING", name, f"Connecting to {uri}")
                
                async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as websocket:
                    # Connection successful - resolve any pending errors
                    self.resolve_pending_error(f"WebSocket {name}")
                    self.update_websocket_status(name, "CONNECTED")
                    print(f"✅ {name} - Connected successfully")
                    self.add_log("SUCCESS", name, "Connected successfully")
                    
                    # Force message update after connection
                    print(f"🔄 Forcing message update after {name} connection")
                    
                    async for message in websocket:
                        if not self.global_monitoring_active:
                            break
                        print(f"📨 {name} - Received: {message[:100]}...")
                        self.update_websocket_status(name, "ACTIVE", message[:50])
                        self.add_log("MESSAGE", name, f"Received: {message[:50]}")
                        
            except Exception as e:
                error_msg = str(e)
                print(f"❌ {name} - Error: {error_msg}")
                print(f"🔍 Error type: {type(e).__name__}")
                self.add_log("ERROR", name, f"Error: {error_msg}")
                
                # Check if this is a connection-related error that might recover
                error_str = str(e).lower()
                is_connection_error = any(keyword in error_str for keyword in [
                    'no close frame', 'connection closed', 'connection lost', 
                    'timeout', 'network', 'refused'
                ])
                
                if is_connection_error:
                    # Add as pending error - will become real error if not resolved in 20s
                    self.add_pending_error(f"WebSocket {name}", str(e))
                else:
                    # Immediate error (authentication, protocol, etc.)
                    self.add_error(f"WebSocket {name}", str(e))
                
                self.update_websocket_status(name, "ERROR", str(e)[:50])
            
            # Wait before reconnecting
            for _ in range(5):
                if not self.global_monitoring_active:
                    break
                await asyncio.sleep(1)
    
    def monitor_api_endpoints(self):
        """Monitor API endpoints in thread"""
        while self.global_monitoring_active:
            for endpoint in API_CONFIG['endpoints']:
                if not self.global_monitoring_active:
                    break
                    
                url = f"{API_CONFIG['base_url']}{endpoint}"
                start_time = datetime.now()
                
                try:
                    self.add_log("INFO", f"API {endpoint}", f"Checking {url}")
                    response = requests.get(url, timeout=10)
                    end_time = datetime.now()
                    response_time = (end_time - start_time).total_seconds() * 1000
                    
                    if response.status_code == 200:
                        self.update_api_status(endpoint, "SUCCESS", response_time)
                        self.add_log("SUCCESS", f"API {endpoint}", f"OK ({response_time:.1f}ms)")
                        print(f"✅ API {endpoint} - OK ({response_time:.1f}ms)")
                    else:
                        self.update_api_status(endpoint, "ERROR", response_time)
                        self.add_log("ERROR", f"API {endpoint}", f"HTTP {response.status_code} ({response_time:.1f}ms)")
                        print(f"❌ API {endpoint} - HTTP {response.status_code} ({response_time:.1f}ms)")
                        
                except Exception as e:
                    end_time = datetime.now()
                    response_time = (end_time - start_time).total_seconds() * 1000
                    self.add_error(f"API {endpoint}", str(e))
                    self.add_log("ERROR", f"API {endpoint}", f"Exception: {str(e)}")
                    self.update_api_status(endpoint, "ERROR", response_time)
            
            # Wait 30 seconds
            for _ in range(30):
                if not self.global_monitoring_active:
                    break
                time.sleep(1)
    
    async def update_user_sessions(self):
        """Update messages for active user sessions (not configured chats)"""
        user_sessions = {
            chat_id: session for chat_id, session in self.active_sessions.items()
            if isinstance(chat_id, int)  # Only real user sessions (integer chat_id)
        }
        
        if not user_sessions:
            return
        
        # Collect users who need updates (only those on dashboard view)
        users_to_update = []
        for chat_id in user_sessions.keys():
            current_view = self.user_current_view.get(chat_id, 'dashboard')
            if current_view == 'dashboard':
                users_to_update.append(chat_id)
            else:
                print(f"⏭️ Skipping auto-update for user {chat_id} - currently viewing {current_view}")
        
        if not users_to_update:
            return
        
        # Send all updates in parallel for synchronized delivery
        print(f"🔄 Sending parallel user dashboard updates to {len(users_to_update)} users")
        tasks = []
        for chat_id in users_to_update:
            task = asyncio.create_task(self.send_or_update_user_message(chat_id))
            tasks.append(task)
        
        # Wait for all updates to complete
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
            print(f"✅ Parallel user dashboard updates completed for {len(users_to_update)} users")
        except Exception as e:
            print(f"❌ Error in parallel user updates: {e}")

    async def update_message_loop(self):
        """Loop to update all user messages"""
        last_update = 0
        last_error_check = 0
        while self.global_monitoring_active:
            current_time = time.time()
            
            # Update user sessions and configured chats
            if current_time - last_update >= UPDATE_INTERVAL:
                # Count total sessions for logging
                user_sessions = sum(1 for k in self.active_sessions.keys() if isinstance(k, int))
                configured_sessions = sum(1 for k in self.active_sessions.keys() if isinstance(k, str))
                
                if user_sessions > 0 or configured_sessions > 0:
                    print(f"🔄 Running update cycle - {user_sessions} users, {configured_sessions} configured")
                    
                    # Update user sessions (real users)
                    await self.update_user_sessions()
                    
                    # Skip configured chats updates - only use interactive sessions
                    # await self.update_all_users()
                    
                    last_update = current_time
            
            # Check pending errors every 5 seconds
            if current_time - last_error_check >= 5:
                self.check_pending_errors()
                last_error_check = current_time
            
            await asyncio.sleep(1)
    
    async def notify_restored_users(self):
        """Clean up restored sessions without sending individual notifications"""
        # Just clear restored flags - notifications go through configured chats only
        for chat_id, session in list(self.active_sessions.items()):
            if session.get('restored', False):
                try:
                    # Remove restored flag - no individual notification needed
                    session['restored'] = False
                    print(f"📢 Cleared restored flag for session {chat_id}")
                    
                except Exception as e:
                    print(f"❌ Failed to process restored session {chat_id}: {e}")
                    # Remove failed session
                    del self.active_sessions[chat_id]
    
    async def start_global_monitoring(self):
        """Start global monitoring tasks"""
        if self.global_monitoring_active:
            return
            
        print("🚀 Starting global monitoring...")
        self.global_monitoring_active = True
        
        # Notify restored users if any
        if any(session.get('restored', False) for session in self.active_sessions.values()):
            await self.notify_restored_users()
        
        # Start API monitoring in thread
        api_thread = threading.Thread(target=self.monitor_api_endpoints, daemon=True)
        api_thread.start()
        
        # Start WebSocket monitoring tasks
        ws_tasks = []
        for key, config in WEBSOCKET_ENDPOINTS.items():
            if config["enabled"]:
                print(f"🚀 Starting WebSocket monitoring for {config['name']}")
                task = asyncio.create_task(
                    self.monitor_websocket(config["url"], config["name"])
                )
                ws_tasks.append(task)
        
        # Start health monitoring
        health_task = asyncio.create_task(self.health_monitor.health_check_loop())
        print("🏥 Started health monitoring")
        
        # Start daily restart scheduler
        daily_restart_task = asyncio.create_task(self.health_monitor.daily_restart_scheduler())
        print("⏰ Started daily restart scheduler")
        
        print(f"🚀 Started {len(ws_tasks)} WebSocket monitoring tasks")
        
        # Start message update task
        update_task = asyncio.create_task(self.update_message_loop())
        
        # Store tasks for cleanup
        self.monitoring_tasks = ws_tasks + [update_task]

def main():
    """Main entry point"""
    if BOT_TOKEN == "your_bot_token_here" or not BOT_TOKEN:
        print("❌ Please configure BOT_TOKEN in .env file")
        print("📝 Get token from @BotFather on Telegram")
        return
    
    print("🤖 Starting Interactive Telegram Bot...")
    print("📱 Users can start their own monitoring with /start")
    print("🔄 No need to configure chat IDs!")
    print("📍 Supports forum topics: use format CHAT_ID_THREAD_ID in .env (e.g., -1001234567890_423)")
    print()
    
    # Create bot instance
    bot = InteractiveTelegramBot()
    
    # Check for restored sessions - monitoring will start when first user connects
    if bot.active_sessions:
        print(f"🔄 Found {len(bot.active_sessions)} sessions to restore")
        print("📢 Monitoring will auto-start when bot event loop begins")
        print(f"📢 Will notify {len(bot.active_sessions)} restored users when bot starts")
    
    # Create application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Add command handlers
    application.add_handler(CommandHandler("start", bot.start_command))
    application.add_handler(CommandHandler("monitor", bot.start_command))  # Alternative command
    application.add_handler(CommandHandler("outlight", bot.start_command))  # Brand-specific command
    application.add_handler(CommandHandler("status", bot.status_command))
    application.add_handler(CommandHandler("clear", bot.clear_command))
    application.add_handler(CommandHandler("help", bot.help_command))
    application.add_handler(CommandHandler("test", bot.test_forum_command))  # Test forum topic
    
    # Add callback handler for buttons
    application.add_handler(CallbackQueryHandler(bot.button_callback))
    
    # Auto-start monitoring for restored sessions
    async def start_monitoring_for_restored():
        """Start monitoring for restored sessions without individual notifications"""
        await asyncio.sleep(2)  # Wait for bot to fully start
        
        # Send test message to configured CHAT_ID from .env
        await bot.send_test_message_to_configured()
        
        if bot.active_sessions:
            # Start global monitoring if not already running
            if not bot.global_monitoring_active:
                print("🚀 Auto-starting monitoring for restored sessions...")
                await bot.start_global_monitoring()
            
            # Just clear restored flags - no individual notifications
            restored_count = 0
            for chat_id, session in list(bot.active_sessions.items()):
                if session.get('restored', False):
                    user_name = session.get('user_name', 'User')
                    session['restored'] = False
                    restored_count += 1
                    print(f"📢 Cleared restored flag for {user_name} ({chat_id})")
            
            if restored_count > 0:
                print(f"✅ Processed {restored_count} restored sessions - monitoring active")
    
    # Schedule startup for restored sessions
    async def startup_callback(context):
        await start_monitoring_for_restored()
    
    if bot.active_sessions:
        try:
            if application.job_queue:
                application.job_queue.run_once(startup_callback, when=2)
            else:
                # Fallback: use threading timer if JobQueue not available
                import threading
                def delayed_startup():
                    import time
                    time.sleep(2)
                    import asyncio
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    loop.run_until_complete(start_monitoring_for_restored())
                
                startup_thread = threading.Thread(target=delayed_startup, daemon=True)
                startup_thread.start()
                print("📢 Using fallback startup method (no JobQueue)")
        except Exception as e:
            print(f"❌ Error scheduling notifications: {e}")
            print("📢 Users will need to check their status manually")
    
    # Setup signal handlers for graceful shutdown
    import signal
    
    def signal_handler(signum, frame):
        print(f"\n🛑 Bot stopped by signal {signum}")
        bot.global_monitoring_active = False
        # Save sessions before shutdown for restart recovery
        if bot.active_sessions:
            print("💾 Saving active sessions for restart recovery...")
            bot.save_active_sessions()
        else:
            # Clean up sessions if no active sessions
            bot.cleanup_sessions()
        exit(0)
    
    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)   # Ctrl+C
    signal.signal(signal.SIGTERM, signal_handler)  # Termination
    
    # Sessions are now saved automatically on every change
    # No need for periodic saving
    
    # Start the bot
    try:
        print("✅ Bot is running! Users can now send /start")
        if bot.active_sessions:
            print(f"🔄 Monitoring auto-started for {len(bot.active_sessions)} restored sessions")
        application.run_polling()
    except KeyboardInterrupt:
        print("\n🛑 Bot stopped by user (KeyboardInterrupt)")
        signal_handler(signal.SIGINT, None)

if __name__ == '__main__':
    main()
