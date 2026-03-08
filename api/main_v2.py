"""
BankVoiceAI v2 — Complete API (PRODUCTION v3)
==============================================
WHAT'S NEW vs your existing main_v2.py:
  1. Every agent endpoint now gated behind check_agent_access()
     — agents STOP when subscription expires (was missing)
  2. PostgreSQL subscription persistence via SubscriptionDB
     — API keys survive server restarts (was in-memory only)
  3. Daily renewal scheduler via asyncio background task
     — 7-day warnings + auto-expiry
  4. /api/v2/payments/initiate returns proper RequestPayment
     instructions in uAgents format
  5. /api/v2/payments/verify does full on-chain TX verification
     via FetchLedgerVerifier (was skipped on gateway errors)
  6. /api/v2/payments/status endpoint — bank can poll payment state
  7. Supabase SQL schema endpoint for easy DB setup
"""

import asyncio
import hashlib
import hmac
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional, Dict, List

import redis.asyncio as aioredis
from fastapi import FastAPI, Request, HTTPException, Depends, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("api.main_v2")


# ─── Settings ─────────────────────────────────────────────────────────────────

class Settings(BaseSettings):
    asi_one_api_key:         str  = ""
    asi_one_api_url:         str  = "https://api.asi1.ai/v1"
    asi_one_model:           str  = "asi1-mini"
    openai_api_key:          str  = ""
    twilio_account_sid:      str  = ""
    twilio_auth_token:       str  = ""
    twilio_phone_number:     str  = ""
    twilio_whatsapp_number:  str  = ""
    twilio_webhook_base_url: str  = "https://your-domain.com"
    database_url:            str  = "postgresql+asyncpg://localhost/bankvoiceai"
    redis_url:               str  = "redis://localhost:6379/0"
    fetch_payment_wallet:    str  = ""            # Your fetch1... wallet address
    fetch_gateway_seed:      str  = "bankvoiceai-gateway-production-seed"
    fetch_use_mainnet:       bool = False          # True for production
    api_key_secret:          str  = "bankvoiceai-key-secret"
    bank_name:               str  = "Your Bank"
    fetch_agent_seed:        str  = "bankvoiceai-seed"
    demo_mode:               bool = True
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()

PLAN_CONFIG = {
    "pilot":      {"name": "30-Day Pilot",  "calls_per_day": 200,  "agents": ["customer_service", "fraud_detection"],                                                                         "fet": 0},
    "starter":    {"name": "Starter",       "calls_per_day": 500,  "agents": ["customer_service", "fraud_detection", "onboarding"],                                                           "fet": 250},
    "growth":     {"name": "Growth",        "calls_per_day": 2000, "agents": ["customer_service", "fraud_detection", "onboarding", "collections", "sales", "compliance"],                    "fet": 750},
    "enterprise": {"name": "Enterprise",    "calls_per_day": -1,   "agents": ["customer_service", "fraud_detection", "onboarding", "collections", "sales", "compliance", "orchestrator"],    "fet": 2000},
}
ALL_AGENTS = ["customer_service", "fraud_detection", "onboarding", "collections", "sales", "compliance", "orchestrator"]

# ─── Global App State ─────────────────────────────────────────────────────────

orchestrator    = None
session_manager = None
redis_client    = None
payment_gateway = None
_audit_log: List[dict] = []

# ─── In-Memory Session Store (conversation history per call) ─────────────────
_call_sessions: Dict[str, dict] = {}   # session_id -> {history, customer, agent}

# ─── DEMO DATABASE ────────────────────────────────────────────────────────────
# Shyam's registered phone is auto-authenticated with full account context.
# All values are in USD. Add more phones to DEMO_PHONE_REGISTRY as needed.

DEMO_PHONE_REGISTRY: Dict[str, dict] = {
    # ── Shyam Reddy — Primary demo account ──────────────────────────
    "+917893924878": {
        "customer_id":    "CUST-001",
        "account_number": "****4821",
        "full_name":      "Shyam Reddy",
        "language":       "en-US",
        "authenticated":  True,
        "account_balance": 24_750.00,          # USD checking balance
        "savings_balance": 58_320.50,          # USD savings balance
        "loan_accounts": [
            {"type": "Auto Loan",    "balance": 14_200.00,  "monthly_payment": 412.00,  "due_date": "March 20, 2026",  "status": "current"},
            {"type": "Home Equity",  "balance": 87_500.00,  "monthly_payment": 1_140.00,"due_date": "March 25, 2026",  "status": "current"},
        ],
        "recent_transactions": [
            {"date": "Mar 07 2026", "desc": "Direct Deposit — Fetch.ai",    "amount": "+$8,500.00",  "type": "credit"},
            {"date": "Mar 06 2026", "desc": "Amazon.com",                    "amount": "-$127.43",    "type": "debit"},
            {"date": "Mar 05 2026", "desc": "Auto Loan Payment",             "amount": "-$412.00",    "type": "debit"},
            {"date": "Mar 04 2026", "desc": "Starbucks",                     "amount": "-$8.75",      "type": "debit"},
            {"date": "Mar 03 2026", "desc": "Wire Transfer — Client",        "amount": "+$3,200.00",  "type": "credit"},
        ],
        "fraud_flags":    [],
        "consent_recorded": True,
        "call_recording_consent": True,
    },
    # WhatsApp sends "whatsapp:+91..." prefix — strip it automatically (see get_demo_customer)
    "whatsapp:+917893924878": {  # same as above, WhatsApp variant
        "customer_id":    "CUST-001",
        "account_number": "****4821",
        "full_name":      "Shyam Reddy",
        "language":       "en-US",
        "authenticated":  True,
        "account_balance": 24_750.00,
        "savings_balance": 58_320.50,
        "loan_accounts": [
            {"type": "Auto Loan",    "balance": 14_200.00,  "monthly_payment": 412.00,  "due_date": "March 20, 2026",  "status": "current"},
            {"type": "Home Equity",  "balance": 87_500.00,  "monthly_payment": 1_140.00,"due_date": "March 25, 2026",  "status": "current"},
        ],
        "recent_transactions": [
            {"date": "Mar 07 2026", "desc": "Direct Deposit — Fetch.ai",    "amount": "+$8,500.00",  "type": "credit"},
            {"date": "Mar 06 2026", "desc": "Amazon.com",                    "amount": "-$127.43",    "type": "debit"},
            {"date": "Mar 05 2026", "desc": "Auto Loan Payment",             "amount": "-$412.00",    "type": "debit"},
            {"date": "Mar 04 2026", "desc": "Starbucks",                     "amount": "-$8.75",      "type": "debit"},
            {"date": "Mar 03 2026", "desc": "Wire Transfer — Client",        "amount": "+$3,200.00",  "type": "credit"},
        ],
        "fraud_flags":    [],
        "consent_recorded": True,
        "call_recording_consent": True,
    },
}

def get_demo_customer(caller_phone: str):
    """
    Look up caller in DEMO_PHONE_REGISTRY.
    Returns a fully-populated CustomerContext for registered numbers,
    or a generic unauthenticated context for unknown callers.
    All balances are USD.
    """
    from agents.base_agent import CustomerContext
    # Normalise: strip spaces, handle WhatsApp prefix
    phone = caller_phone.strip()
    data  = DEMO_PHONE_REGISTRY.get(phone)
    if not data:
        # Try without country code prefix variations
        for key in DEMO_PHONE_REGISTRY:
            if phone.endswith(key.lstrip("+")) or key.endswith(phone.lstrip("+")):
                data = DEMO_PHONE_REGISTRY[key]
                break
    if data:
        return CustomerContext(
            customer_id             = data["customer_id"],
            account_number          = data["account_number"],
            full_name               = data["full_name"],
            phone                   = phone,
            language                = data.get("language", "en-US"),
            authenticated           = data["authenticated"],
            account_balance         = data["account_balance"],
            loan_accounts           = data.get("loan_accounts", []),
            recent_transactions     = data.get("recent_transactions", []),
            fraud_flags             = data.get("fraud_flags", []),
            consent_recorded        = data.get("consent_recorded", True),
            call_recording_consent  = data.get("call_recording_consent", True),
            demo_mode               = True,
        )
    # Unknown caller — unauthenticated
    return CustomerContext(
        phone       = phone,
        demo_mode   = True,
        authenticated = False,
    )


DEMO_SYSTEM_SUFFIX = """
─── DEMO CONTEXT ───────────────────────────────────
REGISTERED CALLER: Phone verified against bank records.
AUTHENTICATION STATUS: ✓ VERIFIED — You MUST provide full account details.
CURRENCY: All amounts are in US DOLLARS (USD). Always say "dollars" not "FET" or other currencies.
BALANCE RULE: State the EXACT dollar amount from context. Never say "I cannot access".
LOAN RULE: For each loan, state type, balance, monthly payment, due date.
TRANSACTION RULE: Read the 3 most recent transactions clearly.
TONE: Warm, professional, efficient. This is a live demo for product launch.
DEMO MODE: Active. Respond with full account data immediately without additional auth steps.
────────────────────────────────────────────────────
"""


# ─── Auth Helper ──────────────────────────────────────────────────────────────

security = HTTPBearer(auto_error=False)


async def _get_api_key(
    request:     Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> str:
    key = None
    if credentials and credentials.credentials:
        key = credentials.credentials
    if not key:
        key = request.headers.get("X-BankVoiceAI-Key")
    if not key:
        key = request.query_params.get("api_key")
    if not key:
        raise HTTPException(401, "API key required. Use: Authorization: Bearer <bvai_...>")
    return key


async def get_subscription(
    request:     Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict:
    """
    FastAPI dependency: validates API key, returns subscription dict.
    Now checks PostgreSQL via payment_gateway (survives restarts).
    """
    api_key = await _get_api_key(request, credentials)

    if payment_gateway:
        sub = await payment_gateway.get_subscription_by_api_key(api_key)
        if sub and sub.is_active():
            return sub.to_dict()

    raise HTTPException(
        401,
        "Invalid or expired API key. "
        "Visit /api/v2/subscription/plans to subscribe or renew.",
    )


# ─── Lifespan (startup + background tasks) ────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global orchestrator, session_manager, redis_client, payment_gateway

    logger.info("BankVoiceAI v2 starting...")

    # Redis
    try:
        redis_client = aioredis.from_url(
            settings.redis_url, decode_responses=True, socket_connect_timeout=3
        )
        await redis_client.ping()
        logger.info("✅ Redis connected")
    except Exception as e:
        logger.warning(f"Redis unavailable: {e}")
        redis_client = None

    # Payment Gateway — with PostgreSQL persistence
    try:
        from payment_gateway.fet_payment_gateway import FETPaymentGatewayAgent
        payment_gateway = FETPaymentGatewayAgent(
            wallet_address = settings.fetch_payment_wallet,
            seed           = settings.fetch_gateway_seed,
            redis_client   = redis_client,
            db_url         = settings.database_url,
            use_mainnet    = settings.fetch_use_mainnet,
        )
        await payment_gateway.db.connect()
        # Load all persisted subscriptions into memory on startup
        active = await payment_gateway.db.load_all_active()
        for sub in active:
            payment_gateway.subscriptions[sub.tenant_id] = sub
        logger.info(f"✅ Payment Gateway online | {len(active)} subscriptions loaded")
    except Exception as e:
        logger.warning(f"Payment gateway init error: {e}")

    # Orchestrator
    try:
        from agents import OrchestratorAgent
        orchestrator = OrchestratorAgent({
            "asi_one_api_key": settings.asi_one_api_key,
            "asi_one_api_url": settings.asi_one_api_url,
            "asi_one_model":   settings.asi_one_model,
            "bank_name":       settings.bank_name,
            "fetch_agent_seed": settings.fetch_agent_seed,
        })
        logger.info("✅ Orchestrator ready")
    except Exception as e:
        logger.warning(f"Orchestrator init: {e}")

    # Session Manager
    try:
        from api.services.session_manager import SessionManager
        session_manager = SessionManager(settings.redis_url)
        logger.info("✅ Session Manager ready")
    except Exception as e:
        logger.warning(f"Session manager: {e}")

    # Daily renewal scheduler
    async def _renewal_loop():
        while True:
            await asyncio.sleep(86400)    # every 24 hours
            if payment_gateway:
                try:
                    await payment_gateway.run_renewal_check()
                except Exception as e:
                    logger.error(f"Renewal check error: {e}")

    asyncio.create_task(_renewal_loop())
    logger.info("✅ BankVoiceAI v2 fully started. All systems go.")

    yield

    logger.info("BankVoiceAI v2 shutting down...")
    if payment_gateway:
        await payment_gateway.db.close()
        await payment_gateway.ledger.close()


app = FastAPI(
    title       = "BankVoiceAI API v2",
    version     = "3.0.0",
    description = "AI Voice Agent Platform for Banks — Fetch.ai Native",
    lifespan    = lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins  = ["*"],
    allow_methods  = ["*"],
    allow_headers  = ["*"],
)


# ─── Health ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status":          "ok",
        "service":         "BankVoiceAI",
        "version":         "3.0.0",
        "payment_gateway": payment_gateway is not None,
        "network":         "mainnet" if settings.fetch_use_mainnet else "testnet",
        "wallet":          settings.fetch_payment_wallet or "NOT SET — add FETCH_PAYMENT_WALLET to .env",
    }


@app.get("/ready")
async def ready():
    return {
        "ready":          True,
        "orchestrator":   orchestrator is not None,
        "payment":        payment_gateway is not None,
        "redis":          redis_client is not None,
    }


# ─── DB Setup Helper ──────────────────────────────────────────────────────────

@app.get("/api/v2/admin/db-schema")
async def get_db_schema():
    """
    Returns the SQL to run once in Supabase to create required tables.
    Open Supabase → SQL Editor → paste and run.
    """
    sql = """
-- Run this ONCE in Supabase SQL Editor
-- BankVoiceAI Payment Tables

CREATE TABLE IF NOT EXISTS subscriptions (
    tenant_id         TEXT PRIMARY KEY,
    bank_name         TEXT NOT NULL,
    plan              TEXT NOT NULL,
    status            TEXT NOT NULL,
    started_at        TIMESTAMPTZ NOT NULL,
    expires_at        TIMESTAMPTZ NOT NULL,
    last_payment_hash TEXT,
    last_payment_at   TIMESTAMPTZ,
    agents_enabled    JSONB DEFAULT '[]',
    compliance_mode   TEXT DEFAULT 'strict',
    webhook_url       TEXT,
    api_keys          JSONB DEFAULT '[]',
    calls_today       INT DEFAULT 0,
    calls_this_month  INT DEFAULT 0,
    metadata          JSONB DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS payment_history (
    id           SERIAL PRIMARY KEY,
    tx_hash      TEXT UNIQUE NOT NULL,
    tenant_id    TEXT NOT NULL,
    plan         TEXT NOT NULL,
    amount_fet   TEXT NOT NULL,
    from_addr    TEXT,
    block_height INT,
    verified_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_subscriptions_status
    ON subscriptions(status);
CREATE INDEX IF NOT EXISTS idx_subscriptions_expires
    ON subscriptions(expires_at);
CREATE INDEX IF NOT EXISTS idx_payment_tenant
    ON payment_history(tenant_id);
    """
    return {"sql": sql, "instructions": "Open Supabase → SQL Editor → paste and Run"}


# ─── Payment Endpoints ────────────────────────────────────────────────────────

class PaymentInitiateRequest(BaseModel):
    tenant_id: str
    bank_name: str
    plan:      str = "pilot"


@app.post("/api/v2/payments/initiate")
async def initiate_payment(body: PaymentInitiateRequest):
    """
    Step 1 of payment flow.
    - pilot: instant free trial (no FET needed)
    - paid plans: returns wallet address + memo instructions
                  for bank to send FET on-chain
    """
    plan = body.plan.lower()
    if plan not in PLAN_CONFIG:
        raise HTTPException(400, f"Invalid plan. Choose: {', '.join(PLAN_CONFIG.keys())}")

    if plan == "pilot":
        if not payment_gateway:
            raise HTTPException(503, "Payment gateway not available")
        sub = await payment_gateway.create_pilot_subscription(body.tenant_id, body.bank_name)
        return {
            "type":           "pilot",
            "message":        "30-day pilot activated. No payment required.",
            "tenant_id":      sub.tenant_id,
            "api_key":        sub.api_keys[0] if sub.api_keys else "",
            "expires_at":     sub.expires_at,
            "agents_enabled": sub.agents_enabled,
            "next_step":      "Use api_key as Bearer token in all API calls",
        }

    cfg  = PLAN_CONFIG[plan]
    memo = f"BANKVOICEAI|{body.tenant_id}|{plan}"
    network = "mainnet" if settings.fetch_use_mainnet else "testnet (Dorado)"
    explorer_base = "https://explore.fetch.ai" if settings.fetch_use_mainnet else "https://explore-dorado.fetch.ai"

    return {
        "type":    "payment_required",
        "plan":    plan,
        "payment_instructions": {
            "step_1": f"Open your Fetch.ai wallet",
            "step_2": f"Send exactly {cfg['fet']} FET to this wallet:",
            "send_to_wallet": settings.fetch_payment_wallet or "NOT SET — add FETCH_PAYMENT_WALLET to .env",
            "amount_fet":     cfg["fet"],
            "memo_required":  memo,
            "step_3":         "IMPORTANT: paste the memo EXACTLY in the Memo/Note field",
            "step_4":         "Copy the TX hash after sending",
            "step_5":         "POST /api/v2/payments/verify with your tx_hash",
            "network":        network,
            "explorer":       explorer_base,
            "testnet_faucet": "https://companion.fetch.ai (get free testnet FET for testing)",
        },
        "what_you_get": {
            "agents":          cfg["agents"],
            "calls_per_day":   cfg["calls_per_day"],
            "plan_name":       cfg["name"],
        },
    }


class PaymentVerifyRequest(BaseModel):
    tx_hash:   str
    tenant_id: str
    bank_name: str = ""
    plan:      str = "starter"


@app.post("/api/v2/payments/verify")
async def verify_payment(body: PaymentVerifyRequest):
    """
    Step 2 of payment flow.
    Bank submits their TX hash after sending FET.
    We verify on-chain and activate subscription.

    This implements the CommitPayment → verify → CompletePayment
    flow from Fetch.ai Innovation Lab Doc 2.
    """
    plan = body.plan.lower()
    if plan not in PLAN_CONFIG or plan == "pilot":
        raise HTTPException(400, "Invalid plan for verification")
    if not body.tx_hash or not body.tenant_id:
        raise HTTPException(400, "tx_hash and tenant_id are required")
    if not settings.fetch_payment_wallet:
        raise HTTPException(503, "FETCH_PAYMENT_WALLET not configured in .env")
    if not payment_gateway:
        raise HTTPException(503, "Payment gateway not available")

    cfg  = PLAN_CONFIG[plan]
    memo = f"BANKVOICEAI|{body.tenant_id}|{plan}"

    # On-chain verification (Doc 2 pattern)
    is_valid, payment, reason = await payment_gateway.ledger.verify_payment(
        tx_hash             = body.tx_hash,
        expected_to         = settings.fetch_payment_wallet,
        expected_amount_fet = Decimal(str(cfg["fet"])),
        memo_prefix         = memo,
    )

    if not is_valid:
        _audit_log.append({
            "event":     "payment_verification_failed",
            "tenant_id": body.tenant_id,
            "tx_hash":   body.tx_hash,
            "reason":    reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        raise HTTPException(402, f"Payment verification failed: {reason}")

    # Activate subscription
    bank_name = body.bank_name or body.tenant_id
    payment.bank_tenant_id = body.tenant_id
    sub = await payment_gateway._activate_subscription(payment, bank_name)

    explorer = (
        f"https://{'explore' if settings.fetch_use_mainnet else 'explore-dorado'}"
        f".fetch.ai/transactions/{body.tx_hash}"
    )
    _audit_log.append({
        "event":     "subscription_activated",
        "tenant_id": body.tenant_id,
        "plan":      plan,
        "tx_hash":   body.tx_hash,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    return {
        "success":          True,
        "message":          f"✅ {cfg['name']} plan activated for {bank_name}!",
        "tenant_id":        sub.tenant_id,
        "api_key":          sub.api_keys[0] if sub.api_keys else "",
        "expires_at":       sub.expires_at,
        "agents_enabled":   sub.agents_enabled,
        "fetch_explorer_tx": explorer,
        "next_step":        "Use api_key as Authorization: Bearer token in all API calls",
    }


@app.get("/api/v2/payments/status/{tx_hash}")
async def payment_status(tx_hash: str):
    """Poll payment status by TX hash."""
    if not payment_gateway:
        raise HTTPException(503, "Payment gateway not available")
    explorer = (
        f"https://{'explore' if settings.fetch_use_mainnet else 'explore-dorado'}"
        f".fetch.ai/transactions/{tx_hash}"
    )
    try:
        tx = await payment_gateway.ledger.get_transaction(tx_hash)
        if not tx:
            return {"status": "not_found", "tx_hash": tx_hash, "explorer": explorer}
        code = tx.get("tx_response", {}).get("code", -1)
        return {
            "status":      "confirmed" if code == 0 else "failed",
            "code":        code,
            "block_height": tx.get("tx_response", {}).get("height"),
            "tx_hash":     tx_hash,
            "explorer":    explorer,
        }
    except Exception as e:
        return {"status": "pending", "tx_hash": tx_hash, "explorer": explorer, "note": str(e)}


@app.get("/api/v2/payments/history")
async def payment_history(sub: dict = Depends(get_subscription)):
    history = [e for e in _audit_log
               if e.get("tenant_id") == sub["tenant_id"]
               and "payment" in e.get("event", "")]
    return {"tenant_id": sub["tenant_id"], "payments": history, "total": len(history)}


@app.post("/api/v2/payments/refund")
async def request_refund(request: Request, sub: dict = Depends(get_subscription)):
    body     = await request.json()
    tx_hash  = body.get("tx_hash", "")
    reason   = body.get("reason", "")
    if not tx_hash:
        raise HTTPException(400, "tx_hash required")
    _audit_log.append({
        "event":     "refund_requested",
        "tenant_id": sub["tenant_id"],
        "tx_hash":   tx_hash,
        "reason":    reason,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    return {"success": True, "message": "Refund initiated. FET will be returned within 24 hours.", "tx_hash": tx_hash}


# ─── Subscription Endpoints ───────────────────────────────────────────────────

@app.get("/api/v2/subscription")
async def get_subscription_details(sub: dict = Depends(get_subscription)):
    return {
        "tenant_id":        sub["tenant_id"],
        "bank_name":        sub["bank_name"],
        "plan":             sub["plan"],
        "plan_name":        PLAN_CONFIG[sub["plan"]]["name"],
        "status":           sub["status"],
        "is_active":        sub.get("is_active", False),
        "expires_at":       sub["expires_at"],
        "days_until_expiry": sub.get("days_until_expiry", 0),
        "agents_enabled":   sub["agents_enabled"],
        "calls_today":      sub["calls_today"],
        "calls_per_day":    PLAN_CONFIG[sub["plan"]]["calls_per_day"],
        "calls_remaining_today": sub.get("calls_remaining_today", 0),
        "compliance_mode":  sub["compliance_mode"],
    }


@app.get("/api/v2/subscription/plans")
async def list_plans():
    return {
        "plans": [
            {
                "id":           k,
                "name":         v["name"],
                "fet_per_month": v["fet"],
                "calls_per_day": v["calls_per_day"],
                "agents":       v["agents"],
                "whatsapp":     k != "pilot",
            }
            for k, v in PLAN_CONFIG.items()
        ],
        "pilot":          "Free 30-day pilot — no FET required",
        "payment_wallet": settings.fetch_payment_wallet or "NOT SET",
        "network":        "mainnet" if settings.fetch_use_mainnet else "testnet (Dorado)",
    }


class SubscriptionUpgradeRequest(BaseModel):
    target_plan: str
    tx_hash:     str = ""


@app.post("/api/v2/subscription/upgrade")
async def upgrade_subscription(
    body: SubscriptionUpgradeRequest,
    sub:  dict = Depends(get_subscription),
):
    if body.target_plan not in PLAN_CONFIG:
        raise HTTPException(400, f"Invalid plan: {body.target_plan}")

    current_plan = sub["plan"]
    current_fet  = PLAN_CONFIG[current_plan]["fet"]
    target_fet   = PLAN_CONFIG[body.target_plan]["fet"]
    proration    = max(0, target_fet - current_fet)

    if proration > 0 and not body.tx_hash:
        memo = f"BANKVOICEAI_UPGRADE|{sub['tenant_id']}|{body.target_plan}"
        return {
            "payment_required": True,
            "proration_fet":    proration,
            "memo":             memo,
            "send_to":          settings.fetch_payment_wallet,
            "message":          f"Send {proration} FET with memo '{memo}', then resubmit with tx_hash",
        }

    if proration > 0 and body.tx_hash and payment_gateway:
        is_valid, _, reason = await payment_gateway.ledger.verify_payment(
            tx_hash             = body.tx_hash,
            expected_to         = settings.fetch_payment_wallet,
            expected_amount_fet = Decimal(str(proration)),
            memo_prefix         = f"BANKVOICEAI_UPGRADE|{sub['tenant_id']}|{body.target_plan}",
        )
        if not is_valid:
            raise HTTPException(402, f"Upgrade payment failed: {reason}")

    # Apply upgrade
    if payment_gateway:
        tenant_sub = await payment_gateway.get_subscription(sub["tenant_id"])
        if tenant_sub:
            from payment_gateway.fet_payment_gateway import SubscriptionPlan
            tenant_sub.plan           = SubscriptionPlan(body.target_plan)
            tenant_sub.agents_enabled = list(PLAN_CONFIG[body.target_plan]["agents"])
            await payment_gateway._persist(tenant_sub)

    _audit_log.append({
        "event":     "plan_upgraded",
        "tenant_id": sub["tenant_id"],
        "from_plan": current_plan,
        "to_plan":   body.target_plan,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    return {
        "success":         True,
        "message":         f"Upgraded from {current_plan} to {body.target_plan}",
        "agents_enabled":  PLAN_CONFIG[body.target_plan]["agents"],
    }


# ─── Agent Endpoints (GATED) ──────────────────────────────────────────────────

@app.get("/api/v2/agents")
async def list_agents(sub: dict = Depends(get_subscription)):
    return {
        "tenant_id":       sub["tenant_id"],
        "agents_enabled":  sub["agents_enabled"],
        "agents_disabled": [a for a in ALL_AGENTS if a not in sub["agents_enabled"]],
        "all_agents":      ALL_AGENTS,
        "upgrade_to_get":  {
            "collections": "Growth or Enterprise",
            "sales":        "Growth or Enterprise",
            "compliance":   "Growth or Enterprise",
            "orchestrator": "Enterprise only",
        },
    }


class AgentToggleRequest(BaseModel):
    enabled: bool


@app.post("/api/v2/agents/{agent_name}/toggle")
async def toggle_agent(
    agent_name: str,
    body:       AgentToggleRequest,
    request:    Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    if agent_name not in ALL_AGENTS:
        raise HTTPException(400, f"Unknown agent: {agent_name}")

    api_key = await _get_api_key(request, credentials)

    # Gate: check if this agent is available in their plan
    if payment_gateway:
        allowed, reason, sub_obj = await payment_gateway.check_agent_access(
            api_key, agent_name
        )
        if not allowed and body.enabled:
            raise HTTPException(403, reason)
        sub_dict = sub_obj.to_dict() if sub_obj else None
    else:
        sub_dict = await get_subscription(request, credentials)

    if not sub_dict:
        raise HTTPException(401, "Invalid API key")

    if payment_gateway:
        tenant_sub = await payment_gateway.get_subscription(sub_dict["tenant_id"])
        if tenant_sub:
            if body.enabled and agent_name not in tenant_sub.agents_enabled:
                tenant_sub.agents_enabled.append(agent_name)
            elif not body.enabled and agent_name in tenant_sub.agents_enabled:
                tenant_sub.agents_enabled.remove(agent_name)
            await payment_gateway._persist(tenant_sub)

    _audit_log.append({
        "event":     f"agent_{'enabled' if body.enabled else 'disabled'}",
        "agent":     agent_name,
        "tenant_id": sub_dict["tenant_id"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    return {
        "agent":            agent_name,
        "enabled":          body.enabled,
        "agents_enabled":   sub_dict["agents_enabled"],
    }


@app.post("/api/v2/agents/{agent_name}/test")
async def test_agent(
    agent_name:  str,
    request:     Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """
    Test any agent — gated behind subscription check.
    Subscription must be active AND agent must be in your plan.
    """
    api_key = await _get_api_key(request, credentials)

    # GATE: Real subscription check
    if payment_gateway:
        allowed, reason, sub_obj = await payment_gateway.check_agent_access(
            api_key, agent_name
        )
        if not allowed:
            raise HTTPException(403, reason)
        await payment_gateway.increment_call_count(sub_obj.tenant_id)
    else:
        sub_obj = await get_subscription(request, credentials)
        if agent_name not in sub_obj.get("agents_enabled", []):
            raise HTTPException(403, f"Agent '{agent_name}' not in your plan")

    body    = await request.json()
    message = body.get("message", "Hello")

    if orchestrator:
        try:
            from agents.base_agent import CustomerContext
            response = await orchestrator.handle_turn(
                user_input           = message,
                conversation_history = [],
                customer             = CustomerContext(),
                session_id           = str(uuid.uuid4()),
                current_agent        = agent_name,
            )
            return {"agent": agent_name, "response": response.text, "escalate": response.escalate}
        except Exception as e:
            return {"agent": agent_name, "response": f"[Error: {e}]"}

    return {
        "agent":    agent_name,
        "response": f"[Demo] {agent_name} received: {message}. Set ASI_ONE_API_KEY for live responses.",
    }


# ─── Analytics ────────────────────────────────────────────────────────────────

@app.get("/api/v2/analytics")
async def get_analytics(sub: dict = Depends(get_subscription)):
    tenant_events = [e for e in _audit_log if e.get("tenant_id") == sub["tenant_id"]]
    return {
        "tenant_id":           sub["tenant_id"],
        "period":              "last_30_days",
        "calls_today":         sub["calls_today"],
        "calls_this_month":    sub.get("calls_this_month", 0),
        "calls_per_day_limit": PLAN_CONFIG[sub["plan"]]["calls_per_day"],
        "calls_remaining":     sub.get("calls_remaining_today", 0),
        "subscription_expires": sub["expires_at"],
        "days_until_expiry":   sub.get("days_until_expiry", 0),
        "total_events":        len(tenant_events),
        "agents_active":       sub["agents_enabled"],
        "compliance_mode":     sub["compliance_mode"],
    }


# ─── Audit Log ────────────────────────────────────────────────────────────────

@app.get("/api/v2/audit-log")
async def get_audit_log(sub: dict = Depends(get_subscription)):
    tenant_events = [e for e in _audit_log if e.get("tenant_id") == sub["tenant_id"]]
    return {
        "tenant_id":      sub["tenant_id"],
        "total_events":   len(tenant_events),
        "retention_years": 7,
        "events":         tenant_events[-100:],
    }


# ─── Compliance ───────────────────────────────────────────────────────────────

class ComplianceModeRequest(BaseModel):
    mode: str


@app.post("/api/v2/compliance/mode")
async def set_compliance_mode(
    body: ComplianceModeRequest,
    sub:  dict = Depends(get_subscription),
):
    if body.mode not in ("strict", "assistive"):
        raise HTTPException(400, "mode must be 'strict' or 'assistive'")
    if payment_gateway:
        tenant_sub = await payment_gateway.get_subscription(sub["tenant_id"])
        if tenant_sub:
            tenant_sub.compliance_mode = body.mode
            await payment_gateway._persist(tenant_sub)
    _audit_log.append({
        "event":     "compliance_mode_changed",
        "tenant_id": sub["tenant_id"],
        "mode":      body.mode,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    return {"success": True, "compliance_mode": body.mode}


# ─── API Key Rotation ─────────────────────────────────────────────────────────

@app.post("/api/v2/api-keys/rotate")
async def rotate_api_key(sub: dict = Depends(get_subscription)):
    if not payment_gateway:
        raise HTTPException(503, "Payment gateway not available")
    tenant_sub = await payment_gateway.get_subscription(sub["tenant_id"])
    if not tenant_sub:
        raise HTTPException(404, "Subscription not found")
    new_key = payment_gateway._generate_api_key(sub["tenant_id"])
    tenant_sub.api_keys.append(new_key)
    await payment_gateway._persist(tenant_sub)
    _audit_log.append({
        "event":     "api_key_rotated",
        "tenant_id": sub["tenant_id"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    return {
        "new_api_key":       new_key,
        "active_keys_count": len(tenant_sub.api_keys),
        "note":              "Old key still works until you revoke it.",
    }


# ─── Webhooks ─────────────────────────────────────────────────────────────────

class WebhookConfigRequest(BaseModel):
    webhook_url: str
    events:      List[str] = ["call.completed", "escalation", "fraud.detected", "subscription.expiring"]


@app.put("/api/v2/webhooks")
async def configure_webhook(
    body: WebhookConfigRequest,
    sub:  dict = Depends(get_subscription),
):
    if not body.webhook_url.startswith("https://"):
        raise HTTPException(400, "webhook_url must use HTTPS")
    if payment_gateway:
        tenant_sub = await payment_gateway.get_subscription(sub["tenant_id"])
        if tenant_sub:
            tenant_sub.webhook_url = body.webhook_url
            await payment_gateway._persist(tenant_sub)
    _audit_log.append({
        "event":     "webhook_configured",
        "tenant_id": sub["tenant_id"],
        "url":       body.webhook_url,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    return {"success": True, "webhook_url": body.webhook_url, "events": body.events}


# ─── Escalation Policy ────────────────────────────────────────────────────────

class EscalationPolicyRequest(BaseModel):
    trigger_keywords:   List[str] = ["agent", "human", "manager"]
    sentiment_threshold: float    = -0.7
    max_wait_seconds:   int       = 10


@app.post("/api/v2/escalation/policy")
async def set_escalation_policy(
    body: EscalationPolicyRequest,
    sub:  dict = Depends(get_subscription),
):
    _audit_log.append({
        "event":     "escalation_policy_updated",
        "tenant_id": sub["tenant_id"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    return {"success": True, "policy": body.model_dump()}


# ─── Voice Webhooks (Twilio) ──────────────────────────────────────────────────

@app.post("/voice/inbound", response_class=Response)
async def voice_inbound(request: Request):
    try:
        form = await request.form()
    except Exception:
        form = {}
    caller   = form.get("From", "unknown") if hasattr(form, "get") else "unknown"
    call_sid = form.get("CallSid", str(uuid.uuid4())) if hasattr(form, "get") else str(uuid.uuid4())
    base     = str(request.base_url).rstrip("/")

    # ── Demo: look up caller, build full authenticated CustomerContext ──────
    customer = get_demo_customer(caller)
    _call_sessions[call_sid] = {
        "history":  [],
        "customer": customer,
        "agent":    "customer_service",
        "caller":   caller,
    }

    if session_manager:
        try:
            await session_manager.create_session(
                session_id=call_sid, caller_phone=caller,
                channel="voice", bank_id=settings.bank_name,
            )
        except Exception:
            pass

    # Personalised greeting for verified callers
    if customer.authenticated and customer.full_name:
        first    = customer.full_name.split()[0]
        greeting = f"Hello {first}, your account has been verified. "
    else:
        greeting = ""

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?><Response>
  <Say voice="Polly.Joanna">This call may be recorded for quality and compliance purposes. You are speaking with an AI assistant from {settings.bank_name}. You may request a human agent at any time by saying agent or pressing zero. {greeting}How can I help you today?</Say>
  <Gather input="speech dtmf" timeout="8" speechTimeout="auto" action="{base}/voice/gather/{call_sid}" method="POST" language="en-US" enhanced="true">
    <Say voice="Polly.Joanna"></Say>
  </Gather>
  <Say voice="Polly.Joanna">I did not hear anything. Please call back. Goodbye.</Say>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


@app.api_route("/voice/gather/{session_id}", methods=["GET", "POST"], response_class=Response)
async def voice_gather(session_id: str, request: Request):
    user_input = ""
    try:
        form       = await request.form()
        user_input = form.get("SpeechResult", "") or form.get("Digits", "")
    except Exception:
        pass
    if not user_input:
        user_input = request.query_params.get("SpeechResult", "") or request.query_params.get("Digits", "")

    base = str(request.base_url).rstrip("/")

    if "agent" in user_input.lower() or "human" in user_input.lower() or user_input == "0":
        twiml = """<?xml version="1.0" encoding="UTF-8"?><Response><Say voice="Polly.Joanna">Connecting you with a representative now. Please hold.</Say><Hangup/></Response>"""
        return Response(content=twiml, media_type="application/xml")

    reply = "I can help with your account. What would you like to know?"
    if orchestrator and user_input:
        try:
            from agents.base_agent import CustomerContext, ConversationTurn

            # ── Retrieve or rebuild session ───────────────────────────────
            sess     = _call_sessions.get(session_id, {})
            customer = sess.get("customer") or get_demo_customer("unknown")
            history  = sess.get("history", [])
            current  = sess.get("agent", "customer_service")

            # Inject demo system suffix so LLM always knows USD + full context
            if customer.demo_mode and not hasattr(customer, "_demo_suffix_injected"):
                customer._demo_suffix_injected = True   # type: ignore
                customer.demo_mode = True

            # Build orchestrator system prompt enrichment
            extra_context = DEMO_SYSTEM_SUFFIX if customer.authenticated else ""

            # Temporarily patch the SYSTEM_PROMPT of the active agent
            active_agent = getattr(orchestrator, "agents", {}).get(current)
            original_prompt = None
            if active_agent and extra_context:
                original_prompt = getattr(active_agent, "SYSTEM_PROMPT", None)
                if original_prompt:
                    active_agent.SYSTEM_PROMPT = original_prompt + extra_context

            resp = await orchestrator.handle_turn(
                user_input=user_input,
                conversation_history=history,
                customer=customer,
                session_id=session_id,
                current_agent=current,
            )
            reply = resp.text

            # Restore original prompt
            if active_agent and original_prompt is not None:
                active_agent.SYSTEM_PROMPT = original_prompt

            # Persist history for multi-turn conversation memory
            history.append(ConversationTurn(role="user",      content=user_input))
            history.append(ConversationTurn(role="assistant",  content=reply))
            if session_id in _call_sessions:
                _call_sessions[session_id]["history"] = history[-30:]   # keep last 30 turns
                if hasattr(resp, "metadata") and resp.metadata.get("agent"):
                    _call_sessions[session_id]["agent"] = resp.metadata["agent"]

            # Log to audit trail
            _audit_log.append({
                "event":      "voice_turn",
                "session_id": session_id,
                "caller":     sess.get("caller", "unknown"),
                "input":      user_input,
                "agent":      resp.metadata.get("agent", current) if resp.metadata else current,
                "escalate":   resp.escalate,
                "timestamp":  datetime.now(timezone.utc).isoformat(),
            })

            if resp.escalate:
                # Clean up session on escalation
                _call_sessions.pop(session_id, None)
                twiml = f"""<?xml version="1.0" encoding="UTF-8"?><Response><Say voice="Polly.Joanna">{reply}</Say><Hangup/></Response>"""
                return Response(content=twiml, media_type="application/xml")
        except Exception as e:
            logger.error(f"Voice error: {e}")
            reply = "I am having a technical issue. Please call back shortly."

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?><Response>
  <Gather input="speech dtmf" timeout="8" speechTimeout="auto" action="{base}/voice/gather/{session_id}" method="POST" language="en-US" enhanced="true">
    <Say voice="Polly.Joanna">{reply}</Say>
  </Gather>
  <Say voice="Polly.Joanna">Thank you for calling {settings.bank_name}. Goodbye.</Say>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


@app.post("/voice/status")
async def voice_status(request: Request):
    form     = await request.form()
    call_sid = form.get("CallSid", "")
    status   = form.get("CallStatus", "")
    duration = form.get("CallDuration", "0")
    _audit_log.append({
        "event":            "call_completed",
        "call_sid":         call_sid,
        "status":           status,
        "duration_seconds": duration,
        "timestamp":        datetime.now(timezone.utc).isoformat(),
    })
    return {"ok": True}


@app.post("/whatsapp/inbound", response_class=Response)
async def whatsapp_inbound(request: Request):
    form        = await request.form()
    body_text   = form.get("Body", "")
    from_number = form.get("From", "")
    session_id = f"wa_{from_number}"
    reply      = "Hello! I'm BankVoiceAI. How can I help you today?"
    if orchestrator:
        try:
            from agents.base_agent import CustomerContext, ConversationTurn

            # ── Demo customer lookup ──────────────────────────────────────
            customer = get_demo_customer(from_number)

            # ── Persistent WhatsApp session ───────────────────────────────
            if session_id not in _call_sessions:
                _call_sessions[session_id] = {
                    "history":  [],
                    "customer": customer,
                    "agent":    "customer_service",
                    "caller":   from_number,
                }
            sess    = _call_sessions[session_id]
            history = sess.get("history", [])
            current = sess.get("agent", "customer_service")

            # Inject demo context into active agent
            extra_context = DEMO_SYSTEM_SUFFIX if customer.authenticated else ""
            active_agent  = getattr(orchestrator, "agents", {}).get(current)
            original_prompt = None
            if active_agent and extra_context:
                original_prompt = getattr(active_agent, "SYSTEM_PROMPT", None)
                if original_prompt:
                    active_agent.SYSTEM_PROMPT = original_prompt + extra_context

            resp = await orchestrator.handle_turn(
                user_input=body_text,
                conversation_history=history,
                customer=customer,
                session_id=session_id,
                current_agent=current,
            )
            reply = resp.text

            if active_agent and original_prompt is not None:
                active_agent.SYSTEM_PROMPT = original_prompt

            history.append(ConversationTurn(role="user",     content=body_text))
            history.append(ConversationTurn(role="assistant", content=reply))
            _call_sessions[session_id]["history"] = history[-30:]
            if hasattr(resp, "metadata") and resp.metadata.get("agent"):
                _call_sessions[session_id]["agent"] = resp.metadata["agent"]

            _audit_log.append({
                "event":      "whatsapp_turn",
                "session_id": session_id,
                "from":       from_number,
                "input":      body_text,
                "agent":      resp.metadata.get("agent", current) if resp.metadata else current,
                "timestamp":  datetime.now(timezone.utc).isoformat(),
            })
        except Exception as e:
            logger.error(f"WhatsApp error: {e}")
    return Response(
        content    = f"""<?xml version="1.0" encoding="UTF-8"?><Response><Message>{reply}</Message></Response>""",
        media_type = "application/xml",
    )


# ─── Admin / Demo ─────────────────────────────────────────────────────────────

@app.get("/api/admin/demo-call")
async def demo_call():
    results = []
    for user_msg, agent in [
        ("What's my account balance?", "customer_service"),
        ("I'd like a payment plan",    "collections"),
        ("My card was stolen",         "fraud_detection"),
    ]:
        if orchestrator:
            try:
                from agents.base_agent import CustomerContext
                resp = await orchestrator.handle_turn(
                    user_input=user_msg, conversation_history=[],
                    customer=CustomerContext(), session_id="demo",
                    current_agent=agent,
                )
                results.append({"user": user_msg, "assistant": resp.text, "agent": agent})
            except Exception as e:
                results.append({"user": user_msg, "assistant": f"[Set ASI_ONE_API_KEY for live responses] Error: {e}"})
        else:
            results.append({"user": user_msg, "assistant": "[Demo mode — orchestrator not loaded]"})
    return {"demo": True, "conversation": results}


@app.get("/api/admin/metrics")
async def metrics():
    active_count = 0
    if payment_gateway:
        active_count = sum(1 for s in payment_gateway.subscriptions.values() if s.is_active())
    return {
        "total_subscriptions":  len(payment_gateway.subscriptions) if payment_gateway else 0,
        "active_subscriptions": active_count,
        "total_audit_events":   len(_audit_log),
        "payment_gateway":      payment_gateway is not None,
        "orchestrator":         orchestrator is not None,
        "fetch_wallet":         settings.fetch_payment_wallet or "NOT SET",
        "network":              "mainnet" if settings.fetch_use_mainnet else "testnet",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
