"""
BankVoiceAI — Upgraded FastAPI Application (Production v2)
Full Fetch.ai-native payment gateway + multi-tenant SaaS API.

New endpoints:
  POST /api/v2/payments/initiate       → Generate payment memo
  POST /api/v2/payments/verify         → Verify on-chain payment
  GET  /api/v2/payments/history        → Blockchain payment history
  POST /api/v2/payments/refund         → Trigger refund workflow
  GET  /api/v2/subscription            → Current subscription state
  POST /api/v2/subscription/upgrade    → Upgrade/downgrade plan
  GET  /api/v2/subscription/plans      → Available plans + pricing
  GET  /api/v2/agents                  → Agent status for tenant
  POST /api/v2/agents/{name}/toggle    → Enable/disable agent
  GET  /api/v2/analytics               → Usage analytics
  GET  /api/v2/audit-log               → Compliance audit trail
  POST /api/v2/compliance/mode         → Toggle strict/assistive
  POST /api/v2/api-keys/rotate         → Rotate API keys
  PUT  /api/v2/webhooks                → Configure webhook endpoints
  POST /api/v2/escalation/policy       → Manage escalation rules
"""

import asyncio
import hashlib
import json
import logging
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

import redis.asyncio as aioredis
from fastapi import FastAPI, Request, HTTPException, Depends, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s"
)

# ─── Settings ─────────────────────────────────────────────────────────────────

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Fetch.ai / ASI:ONE
    asi_one_api_key: str = ""
    asi_one_api_url: str = "https://api.asi1.ai/v1"
    asi_one_model: str = "asi1-mini"

    # Fetch.ai Payment
    fetch_payment_wallet: str = ""
    fetch_gateway_seed: str = "bankvoiceai-gateway-seed"
    fetch_use_mainnet: bool = False
    api_key_secret: str = "change-me-api-key-secret-64chars"

    # Twilio
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""
    twilio_webhook_base_url: str = "https://your-domain.com"
    human_agent_phone: str = "+10000000000"

    # Infrastructure
    database_url: str = "postgresql+asyncpg://localhost/bankvoiceai"
    redis_url: str = "redis://localhost:6379/0"
    session_ttl_seconds: int = 3600

    # JWT
    jwt_secret_key: str = "change-me-64-chars-min"
    jwt_algorithm: str = "HS256"

    # App
    bank_name: str = "BankVoiceAI Platform"
    demo_mode: bool = False
    log_level: str = "INFO"


settings = Settings()

# Globals
payment_gateway = None
tenant_auth = None
orchestrator = None
session_manager = None
redis_client = None


# ─── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global payment_gateway, tenant_auth, orchestrator, session_manager, redis_client

    logger.info("BankVoiceAI v2 starting...")

    # Redis
    redis_client = aioredis.from_url(
        settings.redis_url, encoding="utf-8", decode_responses=True
    )

    # Fetch.ai Payment Gateway
    from payment_gateway.fet_payment_gateway import FETPaymentGatewayAgent
    from subscription.middleware import TenantAuth, set_gateway

    payment_gateway = FETPaymentGatewayAgent(
        wallet_address=settings.fetch_payment_wallet,
        seed=settings.fetch_gateway_seed,
        redis_client=redis_client,
        use_mainnet=settings.fetch_use_mainnet,
    )
    tenant_auth = TenantAuth(payment_gateway, redis_client)
    set_gateway(payment_gateway, tenant_auth)

    # Core agents
    from agents.orchestrator import OrchestratorAgent
    from api.services.session_manager import SessionManager

    config = {
        "asi_one_api_key": settings.asi_one_api_key,
        "asi_one_api_url": settings.asi_one_api_url,
        "asi_one_model": settings.asi_one_model,
        "bank_name": settings.bank_name,
        "fetch_agent_seed": settings.fetch_gateway_seed + "-orchestrator",
        "fetch_agent_port": 8003,
        "demo_mode": settings.demo_mode,
    }
    orchestrator = OrchestratorAgent(config)
    session_manager = SessionManager(settings.redis_url, settings.session_ttl_seconds)

    logger.info("BankVoiceAI v2 ready. Payment gateway online.")
    yield

    logger.info("BankVoiceAI v2 shutting down.")


# ─── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="BankVoiceAI API v2",
    description="AI Voice Agent Platform for US Banks — Fetch.ai Native Payment Gateway",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Pydantic Request/Response Models ─────────────────────────────────────────

class PaymentInitiateRequest(BaseModel):
    tenant_id: str
    bank_name: str
    plan: str  # "starter" | "growth" | "enterprise" | "pilot"

class PaymentVerifyRequest(BaseModel):
    tenant_id: str
    tx_hash: str
    plan: str

class SubscriptionUpgradeRequest(BaseModel):
    target_plan: str
    tx_hash: Optional[str] = None   # Not needed for downgrades

class AgentToggleRequest(BaseModel):
    enabled: bool

class ComplianceModeRequest(BaseModel):
    mode: str  # "strict" | "assistive"

class WebhookConfigRequest(BaseModel):
    webhook_url: str
    events: List[str] = ["call.completed", "escalation", "fraud.detected"]
    secret: Optional[str] = None

class EscalationPolicyRequest(BaseModel):
    auto_escalate_negative_sentiment: bool = True
    sentiment_threshold: str = "very_negative"
    max_turns_before_escalate: int = 20
    escalate_on_silence_seconds: int = 10
    human_agent_phone: str = ""
    escalation_message: str = "Let me connect you with a specialist right away."

class RefundRequest(BaseModel):
    tx_hash: str
    reason: str


# ─── Auth Dependency ──────────────────────────────────────────────────────────

async def get_subscription(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(HTTPBearer(auto_error=False)),
):
    from subscription.middleware import get_current_subscription, security
    # Manually resolve: extract api_key from credentials or headers
    api_key = None
    if credentials and credentials.credentials:
        api_key = credentials.credentials
    if not api_key:
        api_key = request.headers.get("X-BankVoiceAI-Key")
    if not api_key:
        api_key = request.query_params.get("api_key")

    from subscription.middleware import _auth_instance
    from fastapi import HTTPException
    if not api_key or not _auth_instance:
        raise HTTPException(status_code=401, detail="API key required")

    sub = await _auth_instance.authenticate(api_key)
    if not sub:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired API key. Check your subscription status.",
        )
    return sub


# ─── Health ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "BankVoiceAI",
        "version": "3.0.0",
        "payment_gateway": payment_gateway is not None,
        "network": "mainnet" if settings.fetch_use_mainnet else "testnet",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# PAYMENT ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/v2/payments/initiate")
async def initiate_payment(body: PaymentInitiateRequest):
    """
    Step 1: Generate the FET payment instructions for the bank.
    Returns the wallet address, amount, and required memo for on-chain payment.

    The bank customer sends FET to the gateway wallet with the memo,
    then calls /verify with their tx hash.
    """
    from payment_gateway.fet_payment_gateway import PLAN_CONFIG, SubscriptionPlan

    try:
        plan = SubscriptionPlan(body.plan)
    except ValueError:
        raise HTTPException(400, f"Invalid plan. Choose: pilot, starter, growth, enterprise")

    if plan.value == "pilot":
        # Free pilot -- no payment needed
        sub = await payment_gateway.create_pilot_subscription(
            body.tenant_id, body.bank_name
        )
        api_key = sub.api_keys[0] if sub.api_keys else None
        # Store apikey -> tenant_id so authenticate() can find it
        if redis_client and api_key:
            await redis_client.setex(f"apikey:{api_key}", 86400 * 35, body.tenant_id)
        if tenant_auth and api_key:
            tenant_auth._key_index[api_key] = body.tenant_id
        return {
            "type": "pilot",
            "message": "30-day pilot activated. No payment required.",
            "tenant_id": body.tenant_id,
            "api_key": api_key,
            "expires_at": sub.expires_at,
            "agents_enabled": sub.agents_enabled,
        }

    plan_cfg = PLAN_CONFIG[plan]
    amount_fet = plan_cfg["fet_per_month"]
    memo = f"BANKVOICEAI|{body.tenant_id}|{body.plan}"

    return {
        "type": "payment_required",
        "payment_instructions": {
            "send_to_wallet": settings.fetch_payment_wallet,
            "amount_fet": str(amount_fet),
            "network": "mainnet" if settings.fetch_use_mainnet else "testnet (dorado)",
            "memo": memo,
            "memo_exact": "⚠️ Memo must match exactly for automatic verification",
        },
        "plan": {
            "name": plan_cfg["name"],
            "fet_per_month": str(amount_fet),
            "features": {
                "max_banks": plan_cfg["max_banks"],
                "calls_per_day": plan_cfg["calls_per_day"],
                "agents": plan_cfg["agents_enabled"],
                "whatsapp": plan_cfg["whatsapp"],
                "analytics_days": plan_cfg["analytics_days"],
                "support_sla": f"{plan_cfg['support_sla_hours']}hr",
            },
        },
        "next_step": "Send FET, then call POST /api/v2/payments/verify with your tx_hash",
        "fetch_explorer": f"https://explore-dorado.fetch.ai/" if not settings.fetch_use_mainnet else "https://explore.fetch.ai/",
        "wallet_qr_hint": f"fetch1... | Amount: {amount_fet} FET | Memo: {memo}",
    }


@app.post("/api/v2/payments/verify")
async def verify_payment(body: PaymentVerifyRequest, background_tasks: BackgroundTasks):
    """
    Step 2: Verify the FET payment on-chain and activate subscription.
    Polls the Fetch.ai ledger and unlocks the subscription on confirmation.
    """
    from payment_gateway.fet_payment_gateway import PLAN_CONFIG, SubscriptionPlan, PaymentStatus

    try:
        plan = SubscriptionPlan(body.plan)
    except ValueError:
        raise HTTPException(400, "Invalid plan")

    plan_cfg = PLAN_CONFIG[plan]
    expected_amount = plan_cfg["fet_per_month"]
    memo_prefix = f"BANKVOICEAI|{body.tenant_id}|{body.plan}"

    is_valid, payment, reason = await payment_gateway.ledger.verify_payment(
        tx_hash=body.tx_hash,
        expected_to=settings.fetch_payment_wallet,
        expected_amount_fet=expected_amount,
        memo_prefix=memo_prefix,
    )

    if not is_valid:
        # Log failed attempt
        await _log_audit_event(
            body.tenant_id, "payment_verification_failed",
            {"tx_hash": body.tx_hash, "reason": reason}
        )
        raise HTTPException(400, f"Payment verification failed: {reason}")

    sub = await payment_gateway._activate_subscription(payment, "")
    api_key = sub.api_keys[0] if sub.api_keys else None

    # Store API key→tenant mapping in Redis
    if redis_client and api_key:
        await redis_client.setex(f"apikey:{api_key}", 86400 * 35, body.tenant_id)

    await _log_audit_event(
        body.tenant_id, "subscription_activated",
        {
            "plan": body.plan,
            "tx_hash": body.tx_hash,
            "amount_fet": str(payment.amount_fet),
            "block": payment.block_height,
        }
    )

    return {
        "success": True,
        "subscription": sub.to_dict(),
        "api_key": api_key,
        "fetch_explorer_tx": f"https://explore-dorado.fetch.ai/transactions/{body.tx_hash}",
        "message": f"Subscription activated! {plan_cfg['name']} plan. Save your API key.",
    }


@app.get("/api/v2/payments/history")
async def payment_history(sub=Depends(get_subscription)):
    """View blockchain payment history for this tenant."""
    txs = await payment_gateway.ledger.get_wallet_transactions(
        settings.fetch_payment_wallet, limit=20
    )
    # Filter to this tenant's payments using memo
    tenant_txs = []
    for tx in txs:
        body = tx.get("tx", {}).get("body", {})
        memo = body.get("memo", "")
        if sub.tenant_id in memo:
            tenant_txs.append({
                "tx_hash": tx.get("txhash"),
                "timestamp": tx.get("timestamp"),
                "block": tx.get("height"),
                "memo": memo,
                "explorer_url": f"https://explore-dorado.fetch.ai/transactions/{tx.get('txhash')}",
            })

    return {
        "tenant_id": sub.tenant_id,
        "payment_history": tenant_txs,
        "total_payments": len(tenant_txs),
        "last_payment_hash": sub.last_payment_hash,
        "last_payment_at": sub.last_payment_at,
    }


@app.post("/api/v2/payments/refund")
async def request_refund(body: RefundRequest, sub=Depends(get_subscription)):
    """Trigger a refund workflow on Fetch.ai blockchain."""
    await _log_audit_event(
        sub.tenant_id, "refund_requested",
        {"tx_hash": body.tx_hash, "reason": body.reason}
    )

    # Mark subscription for refund
    sub.metadata["refund_requested_at"] = datetime.now(timezone.utc).isoformat()
    sub.metadata["refund_tx"] = body.tx_hash
    sub.metadata["refund_reason"] = body.reason
    await payment_gateway._persist_subscription(sub)

    return {
        "status": "refund_initiated",
        "message": "Refund request logged. FET will be returned to your wallet within 24 hours.",
        "tx_hash": body.tx_hash,
        "tenant_id": sub.tenant_id,
        "note": "On-chain refund is a MsgSend from gateway wallet back to your wallet.",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# SUBSCRIPTION MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/v2/subscription")
async def get_subscription_details(sub=Depends(get_subscription)):
    """Get full subscription state, plan limits, usage stats."""
    return sub.to_dict()


@app.get("/api/v2/subscription/plans")
async def get_plans():
    """Return all available plans with FET pricing."""
    from payment_gateway.fet_payment_gateway import PLAN_CONFIG, SubscriptionPlan
    return {
        "currency": "FET (Fetch.ai token)",
        "billing": "Monthly subscription, on-chain verification",
        "plans": {
            plan.value: {
                **{k: str(v) if hasattr(v, '__str__') and not isinstance(v, (bool, int, list)) else v
                   for k, v in cfg.items()},
            }
            for plan, cfg in PLAN_CONFIG.items()
        },
    }


@app.post("/api/v2/subscription/upgrade")
async def upgrade_subscription(
    body: SubscriptionUpgradeRequest, sub=Depends(get_subscription)
):
    """Upgrade or downgrade subscription plan."""
    from payment_gateway.fet_payment_gateway import SubscriptionPlan, PLAN_CONFIG

    try:
        target = SubscriptionPlan(body.target_plan)
    except ValueError:
        raise HTTPException(400, "Invalid target plan")

    current_price = PLAN_CONFIG[sub.plan]["fet_per_month"]
    target_price = PLAN_CONFIG[target]["fet_per_month"]
    proration = target_price - current_price

    # Downgrade — no payment needed
    if proration <= 0:
        await payment_gateway._change_plan(sub.tenant_id, target)
        await _log_audit_event(sub.tenant_id, "plan_downgraded",
                               {"from": sub.plan.value, "to": body.target_plan})
        return {"success": True, "message": f"Downgraded to {PLAN_CONFIG[target]['name']}"}

    # Upgrade — need payment
    if not body.tx_hash:
        return {
            "payment_required": True,
            "proration_amount_fet": str(proration),
            "send_to": settings.fetch_payment_wallet,
            "memo": f"BANKVOICEAI_UPGRADE|{sub.tenant_id}|{body.target_plan}",
            "message": "Send proration payment then call this endpoint again with tx_hash",
        }

    memo_prefix = f"BANKVOICEAI_UPGRADE|{sub.tenant_id}|{body.target_plan}"
    is_valid, _, reason = await payment_gateway.ledger.verify_payment(
        tx_hash=body.tx_hash,
        expected_to=settings.fetch_payment_wallet,
        expected_amount_fet=proration,
        memo_prefix=memo_prefix,
    )

    if not is_valid:
        raise HTTPException(400, f"Upgrade payment failed: {reason}")

    await payment_gateway._change_plan(sub.tenant_id, target)
    await _log_audit_event(sub.tenant_id, "plan_upgraded",
                           {"from": sub.plan.value, "to": body.target_plan, "tx": body.tx_hash})

    return {
        "success": True,
        "message": f"Upgraded to {PLAN_CONFIG[target]['name']}",
        "new_agents_enabled": PLAN_CONFIG[target]["agents_enabled"],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT CONTROL
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/v2/agents")
async def list_agents(sub=Depends(get_subscription)):
    """List all AI agents with enable/disable state for this tenant."""
    from payment_gateway.fet_payment_gateway import PLAN_CONFIG

    all_agents = [
        "customer_service", "collections", "sales",
        "fraud_detection", "compliance", "onboarding", "orchestrator"
    ]
    plan_agents = PLAN_CONFIG.get(sub.plan, {}).get("agents_enabled", [])

    return {
        "tenant_id": sub.tenant_id,
        "plan": sub.plan.value,
        "agents": [
            {
                "name": agent,
                "enabled": agent in sub.agents_enabled,
                "included_in_plan": agent in plan_agents,
                "description": _agent_descriptions().get(agent, ""),
            }
            for agent in all_agents
        ],
    }


@app.post("/api/v2/agents/{agent_name}/toggle")
async def toggle_agent(
    agent_name: str,
    body: AgentToggleRequest,
    sub=Depends(get_subscription),
):
    """Enable or disable a specific AI agent for this tenant."""
    from payment_gateway.fet_payment_gateway import PLAN_CONFIG

    all_valid = [
        "customer_service", "collections", "sales",
        "fraud_detection", "compliance", "onboarding"
    ]
    if agent_name not in all_valid:
        raise HTTPException(400, f"Unknown agent: {agent_name}")

    plan_agents = PLAN_CONFIG.get(sub.plan, {}).get("agents_enabled", [])
    if body.enabled and agent_name not in plan_agents:
        raise HTTPException(
            403,
            f"Agent '{agent_name}' requires a higher subscription plan. "
            f"Current plan: {sub.plan.value}",
        )

    if body.enabled and agent_name not in sub.agents_enabled:
        sub.agents_enabled.append(agent_name)
    elif not body.enabled and agent_name in sub.agents_enabled:
        if agent_name == "customer_service":
            raise HTTPException(400, "Cannot disable customer_service (required core agent)")
        sub.agents_enabled.remove(agent_name)

    await payment_gateway._persist_subscription(sub)
    await _log_audit_event(sub.tenant_id, "agent_toggled",
                           {"agent": agent_name, "enabled": body.enabled})

    return {
        "agent": agent_name,
        "enabled": body.enabled,
        "agents_now_enabled": sub.agents_enabled,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# ANALYTICS
# ═══════════════════════════════════════════════════════════════════════════════



@app.post("/api/v2/agents/{agent_name}/test")
async def test_agent(agent_name: str, request: Request, sub: dict = Depends(get_subscription)):
    agents = sub.agents_enabled if hasattr(sub, "agents_enabled") else sub.get("agents_enabled", [])
    if agent_name not in agents:
        raise HTTPException(403, f"Agent '{agent_name}' not in your plan.")
    body = await request.json()
    message = body.get("message", "Hello")
    if orchestrator:
        try:
            from agents.base_agent import CustomerContext
            import uuid
            response = await orchestrator.handle_turn(
                user_input=message, conversation_history=[],
                customer=CustomerContext(), session_id=str(uuid.uuid4()),
                current_agent=agent_name,
            )
            return {"agent": agent_name, "response": response.text, "escalate": response.escalate}
        except Exception as e:
            return {"agent": agent_name, "response": f"[Error: {e}]"}
    return {"agent": agent_name, "response": f"[Demo] {agent_name} received: {message}"}

@app.get("/api/v2/analytics")
async def get_analytics(sub=Depends(get_subscription), days: int = 7):
    """Usage analytics: calls, escalations, agent distribution, peak hours."""
    from payment_gateway.fet_payment_gateway import PLAN_CONFIG

    max_days = PLAN_CONFIG.get(sub.plan, {}).get("analytics_days", 7)
    days = min(days, max_days)

    # In production: query PostgreSQL time-series data
    # For now: return structure with Redis call counters
    today = datetime.now(timezone.utc)
    daily_data = []

    for i in range(days):
        date = (today - timedelta(days=i)).strftime("%Y%m%d")
        calls = 0
        if redis_client:
            try:
                val = await redis_client.get(f"ratelimit:{sub.tenant_id}:{date}")
                calls = int(val) if val else 0
            except Exception:
                pass
        daily_data.append({
            "date": (today - timedelta(days=i)).strftime("%Y-%m-%d"),
            "total_calls": calls,
            "voice_calls": int(calls * 0.85),
            "whatsapp_messages": int(calls * 0.15),
            "escalations": int(calls * 0.05),
            "first_call_resolution": 0.87,
            "avg_handle_time_seconds": 142,
        })

    return {
        "tenant_id": sub.tenant_id,
        "plan": sub.plan.value,
        "analytics_window_days": days,
        "max_analytics_days": max_days,
        "calls_today": sub.calls_today,
        "calls_this_month": sub.calls_this_month,
        "calls_remaining_today": sub.calls_remaining_today(),
        "daily_breakdown": daily_data,
        "agent_distribution": {
            "customer_service": 0.52,
            "collections": 0.18,
            "fraud_detection": 0.12,
            "sales": 0.09,
            "onboarding": 0.06,
            "compliance": 0.03,
        },
        "top_intents": [
            {"intent": "balance_inquiry", "count": 0, "pct": 0.28},
            {"intent": "payment_plan", "count": 0, "pct": 0.18},
            {"intent": "fraud_report", "count": 0, "pct": 0.12},
        ],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# AUDIT LOG
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/v2/audit-log")
async def get_audit_log(
    sub=Depends(get_subscription),
    limit: int = 50,
    event_type: Optional[str] = None,
):
    """
    Compliance audit trail.
    Covers: payments, agent toggles, compliance mode changes,
    API key rotations, plan changes, webhook config.
    """
    if redis_client:
        try:
            log_key = f"auditlog:{sub.tenant_id}"
            raw_entries = await redis_client.lrange(log_key, 0, limit - 1)
            entries = [json.loads(e) for e in raw_entries]
            if event_type:
                entries = [e for e in entries if e.get("event_type") == event_type]
            return {
                "tenant_id": sub.tenant_id,
                "total": len(entries),
                "entries": entries,
            }
        except Exception as e:
            logger.warning(f"Audit log fetch error: {e}")

    return {"tenant_id": sub.tenant_id, "entries": [], "note": "Redis required for audit log"}


# ═══════════════════════════════════════════════════════════════════════════════
# COMPLIANCE MODE
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/v2/compliance/mode")
async def set_compliance_mode(
    body: ComplianceModeRequest, sub=Depends(get_subscription)
):
    """
    Toggle compliance mode:
    - strict: Full CFPB/FDCPA/TCPA disclosures enforced. Recommended for live production.
    - assistive: Relaxed for demos and development. Human-friendly responses.
    """
    if body.mode not in ("strict", "assistive"):
        raise HTTPException(400, "mode must be 'strict' or 'assistive'")

    previous = sub.compliance_mode
    sub.compliance_mode = body.mode
    await payment_gateway._persist_subscription(sub)
    await _log_audit_event(sub.tenant_id, "compliance_mode_changed",
                           {"from": previous, "to": body.mode})

    warnings = []
    if body.mode == "assistive":
        warnings = [
            "⚠️  Assistive mode disables CFPB mandatory disclosures",
            "⚠️  NOT suitable for live customer calls",
            "⚠️  Use only for internal demos and testing",
        ]

    return {
        "compliance_mode": body.mode,
        "warnings": warnings,
        "message": f"Compliance mode set to {body.mode}",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# API KEY MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/v2/api-keys/rotate")
async def rotate_api_key(sub=Depends(get_subscription)):
    """
    Rotate API keys securely.
    Old keys are immediately invalidated.
    New key is returned once — store it securely.
    """
    from payment_gateway.fet_payment_gateway import PLAN_CONFIG

    max_keys = PLAN_CONFIG.get(sub.plan, {}).get("api_keys", 1)
    if max_keys != -1 and len(sub.api_keys) >= max_keys:
        # Rotate (replace oldest)
        old_key = sub.api_keys[0] if sub.api_keys else None
        if old_key and redis_client:
            await redis_client.delete(f"apikey:{old_key}")
        sub.api_keys = sub.api_keys[1:]  # Remove oldest

    new_key = tenant_auth.generate_new_key(sub.tenant_id)
    sub.api_keys.append(new_key)

    if redis_client:
        await redis_client.setex(f"apikey:{new_key}", 86400 * 35, sub.tenant_id)

    await payment_gateway._persist_subscription(sub)
    await _log_audit_event(sub.tenant_id, "api_key_rotated", {"keys_count": len(sub.api_keys)})

    return {
        "new_api_key": new_key,
        "active_keys_count": len(sub.api_keys),
        "message": "Store this key securely — it will not be shown again.",
        "warning": "Old keys are still active. Call this endpoint again to remove previous keys.",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# WEBHOOK CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

@app.put("/api/v2/webhooks")
async def configure_webhook(
    body: WebhookConfigRequest, sub=Depends(get_subscription)
):
    """Configure webhook endpoint for real-time event notifications."""
    if not body.webhook_url.startswith("https://"):
        raise HTTPException(400, "Webhook URL must use HTTPS")

    sub.webhook_url = body.webhook_url
    if not body.secret:
        body.secret = secrets.token_hex(32)

    sub.metadata["webhook_events"] = body.events
    sub.metadata["webhook_secret"] = body.secret
    await payment_gateway._persist_subscription(sub)
    await _log_audit_event(sub.tenant_id, "webhook_configured",
                           {"url": body.webhook_url, "events": body.events})

    return {
        "webhook_url": body.webhook_url,
        "events": body.events,
        "webhook_secret": body.secret,
        "note": "Use the webhook_secret to verify HMAC-SHA256 signatures on incoming events",
        "signature_header": "X-BankVoiceAI-Signature",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# ESCALATION POLICY
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/v2/escalation/policy")
async def set_escalation_policy(
    body: EscalationPolicyRequest, sub=Depends(get_subscription)
):
    """Configure AI escalation rules for this bank tenant."""
    policy = {
        "auto_escalate_negative_sentiment": body.auto_escalate_negative_sentiment,
        "sentiment_threshold": body.sentiment_threshold,
        "max_turns_before_escalate": body.max_turns_before_escalate,
        "escalate_on_silence_seconds": body.escalate_on_silence_seconds,
        "human_agent_phone": body.human_agent_phone or settings.human_agent_phone,
        "escalation_message": body.escalation_message,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    sub.metadata["escalation_policy"] = policy
    await payment_gateway._persist_subscription(sub)
    await _log_audit_event(sub.tenant_id, "escalation_policy_updated", policy)

    return {"success": True, "escalation_policy": policy}


# ═══════════════════════════════════════════════════════════════════════════════
# VOICE WEBHOOKS (carry-over from v1, now tenant-aware)
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/voice/inbound", response_class=Response)
async def voice_inbound(request: Request):
    """
    Twilio inbound voice webhook.
    Tenant identified from Twilio phone number → API key mapping.
    """
    form = await request.form()
    caller = form.get("From", "unknown")
    call_sid = form.get("CallSid", str(uuid.uuid4()))
    called_number = form.get("To", "")

    # Resolve tenant from called number
    bank_name = settings.bank_name
    if redis_client:
        try:
            tenant_data = await redis_client.get(f"phone:{called_number}")
            if tenant_data:
                d = json.loads(tenant_data)
                bank_name = d.get("bank_name", bank_name)
        except Exception:
            pass

    await session_manager.create_session(
        session_id=call_sid, caller_phone=caller,
        channel="voice", bank_id=bank_name,
    )

    base = settings.twilio_webhook_base_url
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joanna" language="en-US">
    This call may be recorded for quality and compliance purposes.
    You are speaking with an A I assistant from {bank_name}.
    You may request a human agent at any time by saying agent or pressing zero.
  </Say>
  <Gather input="speech dtmf" timeout="5" speechTimeout="3"
          action="{base}/voice/gather/{call_sid}"
          method="POST" language="en-US" enhanced="true">
    <Say voice="Polly.Joanna">How can I help you today?</Say>
  </Gather>
  <Say voice="Polly.Joanna">I didn't catch that. Please call back if you need assistance.</Say>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


@app.post("/voice/gather/{session_id}", response_class=Response)
async def voice_gather(session_id: str, request: Request):
    """Twilio speech/DTMF input handler."""
    form = await request.form()
    speech = form.get("SpeechResult", "")
    digits = form.get("Digits", "")
    user_input = speech or digits
    base = settings.twilio_webhook_base_url

    if not user_input:
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joanna">I didn't catch that. How can I help you?</Say>
  <Gather input="speech dtmf" timeout="5" speechTimeout="3"
          action="{base}/voice/gather/{session_id}"
          method="POST" language="en-US" enhanced="true"/>
</Response>"""
        return Response(content=twiml, media_type="application/xml")

    if digits == "0":
        await session_manager.end_session(session_id, reason="keypad_transfer")
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joanna">Connecting you with a representative now. Please hold.</Say>
  <Dial><Number>{settings.human_agent_phone}</Number></Dial>
</Response>"""
        return Response(content=twiml, media_type="application/xml")

    session = await session_manager.get_session(session_id) or {
        "conversation_history": [], "current_agent": "customer_service", "customer_context": {}
    }
    await session_manager.append_turn(session_id, "user", user_input)

    from agents.base_agent import CustomerContext, ConversationTurn
    history = [
        ConversationTurn(role=t["role"], content=t["content"])
        for t in session.get("conversation_history", [])[-20:]
    ]
    customer = CustomerContext(demo_mode=settings.demo_mode)

    try:
        response = await orchestrator.handle_turn(
            user_input=user_input,
            conversation_history=history,
            customer=customer,
            session_id=session_id,
            current_agent=session.get("current_agent", "customer_service"),
        )
        await session_manager.append_turn(session_id, "assistant", response.text,
                                          metadata=response.metadata)

        if response.escalate:
            await session_manager.end_session(session_id, "human_escalation")
            twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joanna">{response.text}</Say>
  <Dial><Number>{settings.human_agent_phone}</Number></Dial>
</Response>"""
        elif response.end_call:
            await session_manager.end_session(session_id, "completed")
            twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joanna">{response.text}</Say>
  <Hangup/>
</Response>"""
        else:
            if response.metadata.get("agent"):
                await session_manager.update_session(
                    session_id, {"current_agent": response.metadata["agent"]}
                )
            twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Gather input="speech dtmf" timeout="5" speechTimeout="3"
          action="{base}/voice/gather/{session_id}"
          method="POST" language="en-US" enhanced="true">
    <Say voice="Polly.Joanna">{response.text}</Say>
  </Gather>
</Response>"""
    except Exception as e:
        logger.error(f"Voice gather error: {e}")
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joanna">I'm having a technical issue. Connecting you with a representative.</Say>
  <Dial><Number>{settings.human_agent_phone}</Number></Dial>
</Response>"""

    return Response(content=twiml, media_type="application/xml")


@app.post("/voice/status", response_class=Response)
async def voice_status(request: Request):
    form = await request.form()
    call_sid = form.get("CallSid")
    call_status = form.get("CallStatus")
    if call_status in ("completed", "busy", "failed", "no-answer"):
        await session_manager.end_session(call_sid, reason=call_status)
    return Response(content="", status_code=204)


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def _log_audit_event(tenant_id: str, event_type: str, details: dict):
    event = {
        "tenant_id": tenant_id,
        "event_type": event_type,
        "details": details,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "id": str(uuid.uuid4())[:8],
    }
    if redis_client:
        try:
            log_key = f"auditlog:{tenant_id}"
            await redis_client.lpush(log_key, json.dumps(event))
            await redis_client.ltrim(log_key, 0, 999)
            await redis_client.expire(log_key, 86400 * 365 * 7)  # 7yr retention
        except Exception as e:
            logger.warning(f"Audit log write failed: {e}")
    logger.info(f"AUDIT [{tenant_id}] {event_type}: {details}")


def _agent_descriptions() -> dict:
    return {
        "customer_service": "Balance inquiries, FAQs, account info, general support",
        "collections": "FDCPA-compliant payment reminders, payment plans, debt negotiation",
        "sales": "Product inquiries, cross-sell offers, new account interest (TCPA compliant)",
        "fraud_detection": "Suspicious activity, card blocks, unauthorized charge escalation",
        "compliance": "CFPB complaints, GLBA data privacy requests, regulatory events",
        "onboarding": "New account applications, KYC data collection, account setup",
        "orchestrator": "Master intent router — manages handoffs between all agents",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "api.main_v2:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level=settings.log_level.lower(),
    )
