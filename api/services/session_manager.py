"""
BankVoiceAI — Session Manager
Uses Redis for session storage.
Free tier: Upstash Redis (10K commands/day free)
https://upstash.com
"""
import json
import logging
from typing import Optional, Dict, Any
from datetime import datetime, timezone

import redis.asyncio as redis

logger = logging.getLogger(__name__)

DEFAULT_TTL = 3600  # 1 hour


class SessionManager:
    def __init__(self, redis_url: str, ttl: int = DEFAULT_TTL):
        self.redis_url = redis_url
        self.ttl = ttl
        self._client: Optional[redis.Redis] = None

    async def _get_client(self) -> redis.Redis:
        if self._client is None:
            self._client = redis.from_url(
                self.redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=5,
            )
        return self._client

    def _key(self, session_id: str) -> str:
        return f"session:{session_id}"

    async def create_session(
        self,
        session_id: str,
        caller_phone: str,
        channel: str,
        bank_id: str,
    ) -> Dict[str, Any]:
        session = {
            "session_id": session_id,
            "caller_phone": caller_phone,
            "channel": channel,
            "bank_id": bank_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "current_agent": "customer_service",
            "conversation_history": [],
            "customer_context": {},
            "status": "active",
        }
        try:
            client = await self._get_client()
            await client.setex(
                self._key(session_id),
                self.ttl,
                json.dumps(session),
            )
        except Exception as e:
            logger.warning(f"Redis unavailable, using in-memory session: {e}")
        return session

    async def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        try:
            client = await self._get_client()
            data = await client.get(self._key(session_id))
            return json.loads(data) if data else None
        except Exception as e:
            logger.warning(f"Redis get failed: {e}")
            return None

    async def update_session(self, session_id: str, updates: Dict[str, Any]) -> bool:
        try:
            session = await self.get_session(session_id) or {}
            session.update(updates)
            client = await self._get_client()
            await client.setex(
                self._key(session_id),
                self.ttl,
                json.dumps(session),
            )
            return True
        except Exception as e:
            logger.warning(f"Redis update failed: {e}")
            return False

    async def append_turn(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: Dict = None,
    ) -> bool:
        try:
            session = await self.get_session(session_id) or {
                "conversation_history": []
            }
            turn = {
                "role": role,
                "content": content,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "metadata": metadata or {},
            }
            session.setdefault("conversation_history", []).append(turn)
            # Keep last 50 turns max
            session["conversation_history"] = session["conversation_history"][-50:]
            client = await self._get_client()
            await client.setex(
                self._key(session_id),
                self.ttl,
                json.dumps(session),
            )
            return True
        except Exception as e:
            logger.warning(f"Redis append_turn failed: {e}")
            return False

    async def end_session(self, session_id: str, reason: str = "completed") -> bool:
        try:
            session = await self.get_session(session_id) or {}
            session["status"] = "ended"
            session["ended_at"] = datetime.now(timezone.utc).isoformat()
            session["end_reason"] = reason
            client = await self._get_client()
            # Keep ended sessions for 24h for audit logs
            await client.setex(
                self._key(session_id),
                86400,
                json.dumps(session),
            )
            logger.info(f"Session {session_id} ended: {reason}")
            return True
        except Exception as e:
            logger.warning(f"Redis end_session failed: {e}")
            return False

    async def get_active_sessions(self) -> list:
        try:
            client = await self._get_client()
            keys = await client.keys("session:*")
            sessions = []
            for k in keys:
                data = await client.get(k)
                if data:
                    s = json.loads(data)
                    if s.get("status") == "active":
                        sessions.append(s)
            return sessions
        except Exception:
            return []
