"""
BankVoiceAI — Multi-Tenant Subscription Middleware
Enforces subscription gates on every API request:
  - API key validation
  - Tenant isolation
  - Rate limiting per plan
  - Agent access control
  - Compliance mode injection
"""

import hashlib
import hmac
import logging
import time
from typing import Optional, Callable, Awaitable, Dict, Any
from functools import wraps

from fastapi import Request, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from .fet_payment_gateway import (
    FETPaymentGatewayAgent,
    Subscription,
    SubscriptionStatus,
    SubscriptionPlan,
    PLAN_CONFIG,
)

logger = logging.getLogger(__name__)
security = HTTPBearer(auto_error=False)


# ─── API Key Auth ─────────────────────────────────────────────────────────────

class TenantAuth:
    """
    Multi-tenant API key authentication.
    Keys are of the form: bvai_<40-char-hex>
    Each key is tied to a tenant_id → subscription.
    """

    def __init__(self, payment_gateway: FETPaymentGatewayAgent, redis_client=None):
        self.gateway = payment_gateway
        self.redis = redis_client
        # key_map: api_key → tenant_id  (in-memory index)
        self._key_index: Dict[str, str] = {}

    def _build_key_index(self, subscriptions: dict):
        """Rebuild key→tenant index from subscriptions dict."""
        self._key_index = {}
        for tenant_id, sub in subscriptions.items():
            if isinstance(sub, Subscription):
                for key in sub.api_keys:
                    self._key_index[key] = tenant_id

    async def authenticate(self, api_key: str) -> Optional[Subscription]:
        """
        Validate API key and return the associated subscription.
        Checks: format, key-to-tenant mapping, subscription active.
        """
        if not api_key or not api_key.startswith("bvai_"):
            return None

        # Rebuild index from gateway cache
        self._build_key_index(self.gateway.subscriptions)
        tenant_id = self._key_index.get(api_key)

        if not tenant_id:
            # Try Redis lookup
            if self.redis:
                try:
                    tenant_id = await self.redis.get(f"apikey:{api_key}")
                    if tenant_id:
                        tenant_id = tenant_id.decode() if isinstance(tenant_id, bytes) else tenant_id
                except Exception:
                    pass

        if not tenant_id:
            # Last resort: scan all in-memory subscriptions by api_key
            for tid, s in self.gateway.subscriptions.items():
                if isinstance(s, Subscription) and api_key in s.api_keys:
                    tenant_id = tid
                    self._key_index[api_key] = tid  # cache it
                    break

        if not tenant_id:
            return None

        sub = await self.gateway.get_subscription(tenant_id)
        if not sub:
            return None

        if not sub.is_active():
            return None

        return sub

    def generate_new_key(self, tenant_id: str) -> str:
        """Generate a new API key for a tenant (key rotation)."""
        import os
        secret = os.getenv("API_KEY_SECRET", "bankvoiceai-key-secret")
        raw = f"{tenant_id}:{time.time()}:{secret}"
        new_key = "bvai_" + hmac.new(
            secret.encode(), raw.encode(), hashlib.sha256
        ).hexdigest()[:40]
        return new_key


# ─── FastAPI Dependencies ─────────────────────────────────────────────────────

_gateway_instance: Optional[FETPaymentGatewayAgent] = None
_auth_instance: Optional[TenantAuth] = None


def set_gateway(gw: FETPaymentGatewayAgent, auth: TenantAuth):
    global _gateway_instance, _auth_instance
    _gateway_instance = gw
    _auth_instance = auth


async def get_current_subscription(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Subscription:
    """
    FastAPI dependency: validates Bearer token (API key) and returns Subscription.
    Usage: sub = Depends(get_current_subscription)
    """
    api_key = None

    # Try Authorization header
    if credentials and credentials.credentials:
        api_key = credentials.credentials

    # Try X-BankVoiceAI-Key header
    if not api_key:
        api_key = request.headers.get("X-BankVoiceAI-Key")

    # Try query param (for webhooks)
    if not api_key:
        api_key = request.query_params.get("api_key")

    if not api_key or not _auth_instance:
        raise HTTPException(status_code=401, detail="API key required")

    sub = await _auth_instance.authenticate(api_key)
    if not sub:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired API key. Check your subscription status.",
        )

    return sub


async def require_agent(agent_name: str):
    """FastAPI dependency factory: ensures tenant has access to specific agent."""
    async def _check(sub: Subscription = Depends(get_current_subscription)) -> Subscription:
        if agent_name not in sub.agents_enabled:
            plan_cfg = PLAN_CONFIG.get(sub.plan, {})
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Agent '{agent_name}' is not included in your "
                    f"{plan_cfg.get('name', sub.plan)} plan. "
                    f"Upgrade to access this agent."
                ),
            )
        return sub
    return _check


async def check_rate_limit(sub: Subscription, redis_client=None) -> bool:
    """
    Check if tenant has exceeded their daily call limit.
    Returns True if within limit, False if exceeded.
    """
    if not sub.is_active():
        return False

    remaining = sub.calls_remaining_today()
    if remaining <= 0:
        plan_cfg = PLAN_CONFIG.get(sub.plan, {})
        daily_limit = plan_cfg.get("calls_per_day", 0)
        if daily_limit != -1:
            return False

    # Increment counter in Redis
    if redis_client:
        today = time.strftime("%Y%m%d")
        key = f"ratelimit:{sub.tenant_id}:{today}"
        try:
            count = await redis_client.incr(key)
            await redis_client.expire(key, 86400)
            sub.calls_today = count
        except Exception:
            pass

    return True


# ─── Tenant Context ───────────────────────────────────────────────────────────

class TenantContext:
    """
    Injected into every agent call, providing:
    - Tenant isolation (agents can't cross tenant boundaries)
    - Plan-based feature flags
    - Compliance mode
    - Agent enable/disable state
    """

    def __init__(self, subscription: Subscription):
        self.tenant_id = subscription.tenant_id
        self.bank_name = subscription.bank_name
        self.plan = subscription.plan
        self.agents_enabled = set(subscription.agents_enabled)
        self.compliance_mode = subscription.compliance_mode
        self.webhook_url = subscription.webhook_url
        self.plan_config = PLAN_CONFIG.get(subscription.plan, {})

    def can_use_agent(self, agent_name: str) -> bool:
        return agent_name in self.agents_enabled

    def is_strict_compliance(self) -> bool:
        return self.compliance_mode == "strict"

    def max_calls_per_day(self) -> int:
        return self.plan_config.get("calls_per_day", 0)

    def has_whatsapp(self) -> bool:
        return self.plan_config.get("whatsapp", False)

    def analytics_days(self) -> int:
        return self.plan_config.get("analytics_days", 7)

    def to_dict(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "bank_name": self.bank_name,
            "plan": self.plan.value,
            "agents_enabled": list(self.agents_enabled),
            "compliance_mode": self.compliance_mode,
            "has_whatsapp": self.has_whatsapp(),
            "analytics_days": self.analytics_days(),
        }
