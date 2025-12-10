"""
Redis Session Manager for Telegram Bot
Handles persistent storage of user sessions
"""

import redis
import json
import os
from datetime import datetime, timedelta
from typing import Dict, Optional, Any

class RedisSessionManager:
    def __init__(self, redis_url: str = None, redis_host: str = 'localhost', redis_port: int = 6379, redis_db: int = 0, redis_username: str = None, redis_password: str = None):
        """Initialize Redis connection"""
        try:
            if redis_url:
                self.redis_client = redis.from_url(redis_url)
            else:
                self.redis_client = redis.Redis(
                    host=redis_host,
                    port=redis_port,
                    db=redis_db,
                    username=redis_username,
                    password=redis_password,
                    decode_responses=True
                )
            
            # Test connection
            self.redis_client.ping()
            print("✅ Connected to Redis successfully")
            
        except redis.ConnectionError:
            print("❌ Failed to connect to Redis, falling back to file storage")
            self.redis_client = None
        except Exception as e:
            print(f"❌ Redis error: {e}, falling back to file storage")
            self.redis_client = None
    
    def save_session(self, chat_id: int, session_data: Dict[str, Any], ttl_hours: int = 24) -> bool:
        """Save user session with TTL"""
        if not self.redis_client:
            return self._save_to_file(chat_id, session_data)
        
        try:
            key = f"telegram_session:{chat_id}"
            
            # Convert datetime objects to ISO strings
            serializable_data = {}
            for k, v in session_data.items():
                if isinstance(v, datetime):
                    serializable_data[k] = v.isoformat()
                else:
                    serializable_data[k] = v
            
            # Save to Redis with TTL
            self.redis_client.setex(
                key,
                timedelta(hours=ttl_hours),
                json.dumps(serializable_data)
            )
            
            print(f"💾 Saved session for user {chat_id} to Redis (TTL: {ttl_hours}h)")
            return True
            
        except Exception as e:
            print(f"❌ Error saving session to Redis: {e}")
            return self._save_to_file(chat_id, session_data)
    
    def load_session(self, chat_id: int) -> Optional[Dict[str, Any]]:
        """Load user session"""
        if not self.redis_client:
            return self._load_from_file(chat_id)
        
        try:
            key = f"telegram_session:{chat_id}"
            data = self.redis_client.get(key)
            
            if not data:
                return None
            
            session_data = json.loads(data)
            
            # Convert ISO strings back to datetime objects
            if 'start_time' in session_data:
                session_data['start_time'] = datetime.fromisoformat(session_data['start_time'])
            
            print(f"🔄 Loaded session for user {chat_id} from Redis")
            return session_data
            
        except Exception as e:
            print(f"❌ Error loading session from Redis: {e}")
            return self._load_from_file(chat_id)
    
    def load_all_sessions(self) -> Dict[int, Dict[str, Any]]:
        """Load all active sessions"""
        if not self.redis_client:
            return self._load_all_from_file()
        
        try:
            sessions = {}
            pattern = "telegram_session:*"
            
            for key in self.redis_client.scan_iter(match=pattern):
                try:
                    # Try to parse as integer chat_id (normal sessions)
                    chat_id_str = key.split(':', 1)[1]
                    chat_id = int(chat_id_str)
                    session_data = self.load_session(chat_id)
                    if session_data:
                        sessions[chat_id] = session_data
                except ValueError:
                    # Skip non-integer keys (like configured_negCHATIDthreadTHREADID)
                    print(f"⏭️ Skipping non-integer session key: {key}")
                    continue
            
            print(f"🔄 Loaded {len(sessions)} sessions from Redis")
            return sessions
            
        except Exception as e:
            print(f"❌ Error loading all sessions from Redis: {e}")
            return self._load_all_from_file()
    
    def delete_session(self, chat_id: int) -> bool:
        """Delete user session"""
        if not self.redis_client:
            return self._delete_from_file(chat_id)
        
        try:
            key = f"telegram_session:{chat_id}"
            result = self.redis_client.delete(key)
            
            if result:
                print(f"🗑️ Deleted session for user {chat_id} from Redis")
            
            return bool(result)
            
        except Exception as e:
            print(f"❌ Error deleting session from Redis: {e}")
            return self._delete_from_file(chat_id)
    
    def cleanup_expired_sessions(self) -> int:
        """Cleanup expired sessions (Redis handles this automatically with TTL)"""
        if not self.redis_client:
            return self._cleanup_file_sessions()
        
        # Redis automatically handles TTL cleanup
        print("🧹 Redis automatically handles session cleanup via TTL")
        return 0
    
    # Fallback file methods
    def _save_to_file(self, chat_id: int, session_data: Dict[str, Any]) -> bool:
        """Fallback: save to file"""
        try:
            sessions = self._load_all_from_file()
            
            # Convert datetime to ISO string
            serializable_data = {}
            for k, v in session_data.items():
                if isinstance(v, datetime):
                    serializable_data[k] = v.isoformat()
                else:
                    serializable_data[k] = v
            
            sessions[str(chat_id)] = serializable_data
            
            with open('active_sessions.json', 'w') as f:
                json.dump(sessions, f, indent=2)
            
            print(f"💾 Saved session for user {chat_id} to file (fallback)")
            return True
            
        except Exception as e:
            print(f"❌ Error saving session to file: {e}")
            return False
    
    def _load_from_file(self, chat_id: int) -> Optional[Dict[str, Any]]:
        """Fallback: load from file"""
        sessions = self._load_all_from_file()
        session_data = sessions.get(str(chat_id))
        
        if session_data and 'start_time' in session_data:
            session_data['start_time'] = datetime.fromisoformat(session_data['start_time'])
        
        return session_data
    
    def _load_all_from_file(self) -> Dict[str, Dict[str, Any]]:
        """Fallback: load all from file"""
        try:
            if not os.path.exists('active_sessions.json'):
                return {}
            
            with open('active_sessions.json', 'r') as f:
                return json.load(f)
                
        except Exception as e:
            print(f"❌ Error loading sessions from file: {e}")
            return {}
    
    def _delete_from_file(self, chat_id: int) -> bool:
        """Fallback: delete from file"""
        try:
            sessions = self._load_all_from_file()
            
            if str(chat_id) in sessions:
                del sessions[str(chat_id)]
                
                if sessions:
                    with open('active_sessions.json', 'w') as f:
                        json.dump(sessions, f, indent=2)
                else:
                    # Remove file if no sessions left
                    if os.path.exists('active_sessions.json'):
                        os.remove('active_sessions.json')
                
                print(f"🗑️ Deleted session for user {chat_id} from file")
                return True
            
            return False
            
        except Exception as e:
            print(f"❌ Error deleting session from file: {e}")
            return False
    
    def cleanup_old_sessions(self, max_age_hours: int = 48) -> int:
        """Clean up sessions older than max_age_hours"""
        if not self.redis_client:
            return self._cleanup_file_sessions()
        
        try:
            cleaned_count = 0
            current_time = datetime.now()
            
            # Get all session keys
            pattern = "telegram_session:*"
            
            for key in self.redis_client.scan_iter(match=pattern):
                try:
                    data = self.redis_client.get(key)
                    if data:
                        session_data = json.loads(data.decode('utf-8'))
                        start_time_str = session_data.get('start_time')
                        
                        if start_time_str:
                            # Parse start time
                            start_time = datetime.fromisoformat(start_time_str)
                            
                            # Check if session is too old
                            age_hours = (current_time - start_time).total_seconds() / 3600
                            
                            if age_hours > max_age_hours:
                                self.redis_client.delete(key)
                                cleaned_count += 1
                                chat_id_str = key.split(':', 1)[1]
                                user_name = session_data.get('user_name', 'Unknown')
                                print(f"🧹 Cleaned old session: {user_name} ({chat_id_str}) - {age_hours:.1f}h old")
                
                except Exception as e:
                    print(f"⚠️ Error cleaning session {key}: {e}")
                    continue
            
            if cleaned_count > 0:
                print(f"🧹 Cleaned {cleaned_count} old sessions (>{max_age_hours}h)")
            
            return cleaned_count
            
        except Exception as e:
            print(f"❌ Error cleaning old sessions: {e}")
            return 0
    
    def _cleanup_file_sessions(self) -> int:
        """Fallback: cleanup old file sessions"""
        try:
            sessions = self._load_all_from_file()
            cleaned = 0
            
            # Remove sessions older than 24 hours
            cutoff = datetime.now() - timedelta(hours=24)
            
            for chat_id, session_data in list(sessions.items()):
                if 'start_time' in session_data:
                    start_time = datetime.fromisoformat(session_data['start_time'])
                    if start_time < cutoff:
                        del sessions[chat_id]
                        cleaned += 1
            
            if cleaned > 0:
                if sessions:
                    with open('active_sessions.json', 'w') as f:
                        json.dump(sessions, f, indent=2)
                else:
                    if os.path.exists('active_sessions.json'):
                        os.remove('active_sessions.json')
                
                print(f"🧹 Cleaned up {cleaned} expired sessions from file")
            
            return cleaned
            
        except Exception as e:
            print(f"❌ Error cleaning up file sessions: {e}")
            return 0
