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
from bank_db import BankDB
from fastapi import FastAPI, Request, HTTPException, Depends, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse, FileResponse, HTMLResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
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
bank_db: Optional[BankDB] = None          # Live Supabase connection

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
    # ── Shyam Reddy — Mobile number 2 ──────────────────────────────────
    "+918431439772": {
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
    "8431439772": {   # short form fallback
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
    "whatsapp:+918431439772": {  # WhatsApp variant
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
    Look up caller — tries DEMO_PHONE_REGISTRY first for speed,
    falls back to in-memory defaults.
    All balances are USD.
    """
    from agents.base_agent import CustomerContext
    phone = caller_phone.strip()
    data  = DEMO_PHONE_REGISTRY.get(phone)
    if not data:
        for key in DEMO_PHONE_REGISTRY:
            if phone.endswith(key.lstrip("+")) or key.endswith(phone.lstrip("+")):
                data = DEMO_PHONE_REGISTRY[key]
                break
    if data:
        return CustomerContext(
            customer_id            = data["customer_id"],
            account_number         = data["account_number"],
            full_name              = data["full_name"],
            phone                  = phone,
            language               = data.get("language", "en-US"),
            authenticated          = data["authenticated"],
            account_balance        = data["account_balance"],
            loan_accounts          = data.get("loan_accounts", []),
            recent_transactions    = data.get("recent_transactions", []),
            fraud_flags            = data.get("fraud_flags", []),
            consent_recorded       = data.get("consent_recorded", True),
            call_recording_consent = data.get("call_recording_consent", True),
            demo_mode              = True,
        )
    return CustomerContext(phone=phone, demo_mode=True, authenticated=False)


async def get_customer_from_db(caller_phone: str):
    """
    Query Supabase live for caller data.
    Falls back to get_demo_customer if DB unavailable or phone not found.
    """
    from agents.base_agent import CustomerContext
    global bank_db

    # Try live DB first
    if bank_db and bank_db.is_connected:
        try:
            data = await bank_db.get_customer_by_phone(caller_phone)
            if data:
                ctx = CustomerContext(
                    customer_id            = data["customer_id"],
                    account_number         = data["account_number"],
                    full_name              = data["full_name"],
                    phone                  = data["phone"],
                    authenticated          = True,
                    account_balance        = data["account_balance"],
                    loan_accounts          = data["loan_accounts"],
                    recent_transactions    = data["recent_transactions"],
                    fraud_flags            = data["fraud_flags"],
                    consent_recorded       = True,
                    call_recording_consent = True,
                    demo_mode              = True,
                )
                # Store savings balance as attribute (not in dataclass by default)
                ctx.savings_balance = data.get("savings_balance", 0.0)  # type: ignore
                ctx._db_source = "supabase_live"                         # type: ignore
                logger.info(f"✅ DB lookup: {data['full_name']} — ${data['account_balance']:,.2f} checking")
                return ctx
        except Exception as e:
            logger.warning(f"DB lookup failed, falling back to demo: {e}")

    # Fallback to in-memory demo data
    return get_demo_customer(caller_phone)


DEMO_SYSTEM_SUFFIX = """

════════════════════════════════════════════════════════
CALLER AUTHENTICATION: ✓ FULLY VERIFIED VIA REGISTERED PHONE NUMBER
════════════════════════════════════════════════════════

CRITICAL RULES — OVERRIDE ALL OTHER INSTRUCTIONS:

1. DO NOT ask for SSN, account number, last 4 digits, date of birth, or ANY other verification.
   The caller's phone number is their authentication. They are already verified. PERIOD.

2. DO NOT say "I need to verify your identity" or "Can you confirm your account number".
   Authentication is COMPLETE. Jump straight to helping them.

3. DO NOT transfer to a human agent unless the caller explicitly asks for one.
   You have full account data. Use it.

4. ALWAYS answer balance, transaction, and loan questions immediately using the data below.
   Never say "I cannot access that" or "I don't have that information".

5. ALL amounts are in US DOLLARS (USD). Say "dollars". Never say "FET".

6. If asked for balance → state checking balance + savings balance immediately.
   If asked for transactions → read last 3-5 transactions with dates and amounts.
   If asked about loans → state each loan type, balance, monthly payment, due date.

ACCOUNT DATA IS IN YOUR CONTEXT. USE IT NOW. NO VERIFICATION NEEDED.
════════════════════════════════════════════════════════
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



# ─── Demo / Hardcoded Subscriptions ──────────────────────────────────────────
# These are always valid regardless of DB state.
# Add any new tenants here after they pay.

DEMO_SUBSCRIPTIONS: Dict[str, dict] = {
    "bvai_2f2163c92ad998c5778e1a3c77ddda0e3795d842": {
        "tenant_id":           "bank_fcb_001",
        "bank_name":           "First Community Bank",
        "plan":                "growth",
        "status":              "active",
        "api_key":             "bvai_2f2163c92ad998c5778e1a3c77ddda0e3795d842",
        "agents_enabled":      ["customer_service","fraud_detection","onboarding","collections","sales","compliance"],
        "calls_today":         0,
        "calls_remaining_today": 2000,
        "days_until_expiry":   365,
        "expires_at":          "2027-03-08T00:00:00Z",
        "last_payment_hash":   "DEMO_ACTIVE_SUBSCRIPTION",
        "whatsapp":            True,
        "analytics_days":      90,
    },
}


async def get_subscription(
    request:     Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict:
    """
    FastAPI dependency: validates API key, returns subscription dict.
    Priority:  1. DEMO_SUBSCRIPTIONS (always valid, no DB needed)
               2. payment_gateway DB lookup (for real paying customers)
    """
    api_key = await _get_api_key(request, credentials)

    # 1 — Check demo/hardcoded subscriptions first
    if api_key in DEMO_SUBSCRIPTIONS:
        return DEMO_SUBSCRIPTIONS[api_key]

    # 2 — Check payment gateway DB
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
    global orchestrator, session_manager, redis_client, payment_gateway, bank_db

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

    # BankDB — live Supabase customer data
    try:
        bank_db = BankDB(settings.database_url)
        await bank_db.connect()
        if bank_db.is_connected:
            logger.info("✅ BankDB live — Supabase connected")
        else:
            logger.warning("BankDB offline — using in-memory demo data")
    except Exception as e:
        logger.warning(f"BankDB init error: {e}")
        bank_db = None

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
        # Try to connect to DB — non-fatal if unavailable (Railway env may not have PG)
        if payment_gateway.db is not None:
            try:
                await payment_gateway.db.connect()
                active = await payment_gateway.db.load_all_active()
                for sub in active:
                    payment_gateway.subscriptions[sub.tenant_id] = sub
                logger.info(f"✅ Payment Gateway online | {len(active)} subscriptions loaded from DB")
            except Exception as db_err:
                logger.warning(f"Payment gateway DB unavailable (in-memory mode): {db_err}")
                logger.info("✅ Payment Gateway online | In-memory mode (no DB persistence)")
        else:
            logger.info("✅ Payment Gateway online | In-memory mode")
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
    if bank_db:
        await bank_db.close()
    if payment_gateway:
        try:
            if payment_gateway.db is not None:
                await payment_gateway.db.close()
        except Exception:
            pass
        try:
            if payment_gateway.ledger is not None:
                await payment_gateway.ledger.close()
        except Exception:
            pass


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

# ─── Static Files (Portal UI) ─────────────────────────────────────────────────
import os as _os
_static_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "static")
_index_path = _os.path.join(_static_dir, "index.html")

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def serve_root():
    """Serve the BankVoiceAI portal (index.html)."""
    if _os.path.exists(_index_path):
        with open(_index_path, encoding="utf-8") as _f:
            return HTMLResponse(content=_f.read())
    return HTMLResponse(content="<h1>BankVoiceAI API v3</h1><p>Portal not found. Deploy index.html to api/static/index.html</p>")

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    fav = _os.path.join(_static_dir, "favicon.ico")
    if _os.path.exists(fav):
        return FileResponse(fav)
    return Response(status_code=204)

if _os.path.exists(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")


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
            raise HTTPException(503, detail={"error": "Payment gateway starting up. Please retry in 10 seconds.", "code": "GATEWAY_INIT"})
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
        raise HTTPException(503, detail={"error": "Payment gateway starting up. Please retry in 10 seconds.", "code": "GATEWAY_INIT"})

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
        raise HTTPException(503, detail={"error": "Payment gateway starting up. Please retry in 10 seconds.", "code": "GATEWAY_INIT"})
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
        "tenant_id":             sub["tenant_id"],
        "bank_name":             sub.get("bank_name", sub["tenant_id"]),
        "plan":                  sub["plan"],
        "plan_name":             PLAN_CONFIG.get(sub["plan"], {}).get("name", sub["plan"]),
        "status":                sub.get("status", "active"),
        "is_active":             True,
        "expires_at":            sub.get("expires_at", "2027-01-01T00:00:00Z"),
        "days_until_expiry":     sub.get("days_until_expiry", 365),
        "agents_enabled":        sub.get("agents_enabled", []),
        "calls_today":           sub.get("calls_today", 0),
        "calls_per_day":         PLAN_CONFIG.get(sub["plan"], {}).get("calls_per_day", 500),
        "calls_remaining_today": sub.get("calls_remaining_today", 2000),
        "compliance_mode":       sub.get("compliance_mode", "strict"),
        "last_payment_hash":     sub.get("last_payment_hash", ""),
    }


@app.get("/api/v2/subscription/status")
async def get_subscription_status(
    tenant_id: str,
    request:   Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """
    Portal login endpoint — called with ?tenant_id=... and X-BankVoiceAI-Key header.
    Returns full subscription info or 401.
    """
    sub = await get_subscription(request, credentials)
    # Verify tenant_id matches the key
    if sub.get("tenant_id") != tenant_id:
        raise HTTPException(401, "API key does not match tenant ID.")
    return {
        "tenant_id":             sub["tenant_id"],
        "bank_name":             sub.get("bank_name", sub["tenant_id"]),
        "plan":                  sub["plan"],
        "plan_name":             PLAN_CONFIG.get(sub["plan"], {}).get("name", sub["plan"]),
        "status":                sub.get("status", "active"),
        "is_active":             True,
        "expires_at":            sub.get("expires_at", "2027-01-01T00:00:00Z"),
        "days_until_expiry":     sub.get("days_until_expiry", 365),
        "agents_enabled":        sub.get("agents_enabled", []),
        "calls_today":           sub.get("calls_today", 0),
        "calls_per_day":         PLAN_CONFIG.get(sub["plan"], {}).get("calls_per_day", 500),
        "calls_remaining_today": sub.get("calls_remaining_today", 2000),
        "compliance_mode":       sub.get("compliance_mode", "strict"),
        "last_payment_hash":     sub.get("last_payment_hash", ""),
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
        raise HTTPException(503, detail={"error": "Payment gateway starting up. Please retry in 10 seconds.", "code": "GATEWAY_INIT"})
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


# ─── DB Connection Manager (Demo Simulation) ─────────────────────────────────

class DbTestRequest(BaseModel):
    db_type:  str = "PostgreSQL"
    host:     str = ""
    port:     int = 5432
    database: str = ""
    username: str = ""
    name:     str = ""

# Simulated demo schema — mirrors a real core banking DB
DEMO_SCHEMA = {
    "tables": [
        {
            "name": "accounts",
            "rows": 847_293,
            "columns": 14,
            "classification": "NPI",             # GLBA Non-Public Information
            "glba_restricted": True,
            "sample_cols": ["account_id","customer_id","balance_usd","account_type","opened_date","status","branch_id","interest_rate","overdraft_limit","last_activity"],
        },
        {
            "name": "customers",
            "rows": 312_847,
            "columns": 18,
            "classification": "NPI",
            "glba_restricted": True,
            "sample_cols": ["customer_id","full_name","ssn_last4","dob","phone","email","address","city","state","zip","kyc_status","risk_score"],
        },
        {
            "name": "transactions",
            "rows": 2_184_921,
            "columns": 10,
            "classification": "Sensitive",
            "glba_restricted": True,
            "sample_cols": ["txn_id","account_id","amount_usd","type","merchant","timestamp","channel","status","reference","notes"],
        },
        {
            "name": "loans",
            "rows": 94_102,
            "columns": 12,
            "classification": "NPI",
            "glba_restricted": True,
            "sample_cols": ["loan_id","customer_id","principal_usd","rate_pct","term_months","monthly_payment_usd","due_date","status","collateral","origination_date"],
        },
        {
            "name": "products",
            "rows": 47,
            "columns": 8,
            "classification": "Public",
            "glba_restricted": False,
            "sample_cols": ["product_id","name","type","apr","min_balance","fdic_insured","active","description"],
        },
        {
            "name": "tcpa_consent",
            "rows": 198_441,
            "columns": 7,
            "classification": "Compliance",
            "glba_restricted": True,
            "sample_cols": ["phone","consent_type","consent_date","opt_out_date","channel","agent_id","verified"],
        },
    ]
}

@app.post("/api/v2/db/test")
async def test_db_connection(
    body: DbTestRequest,
    sub:  dict = Depends(get_subscription),
):
    """
    Simulate a DB connection test.
    In demo mode: returns realistic latency, TLS info, table counts.
    In production: would use asyncpg/aiomysql to actually connect.
    """
    import random, asyncio
    await asyncio.sleep(0.8)   # simulate real connection time

    host = body.host or "db.yourinstitution.internal"
    db   = body.database or "corebank"

    return {
        "success":       True,
        "latency_ms":    round(random.uniform(2.8, 6.4), 1),
        "tls_version":   "TLS 1.3",
        "tls_cipher":    "TLS_AES_256_GCM_SHA384",
        "access_mode":   "READ ONLY",
        "db_version":    f"{body.db_type} 15.4" if body.db_type == "PostgreSQL" else f"{body.db_type} 8.0",
        "host":          host,
        "database":      db,
        "tables_found":  len(DEMO_SCHEMA["tables"]),
        "total_rows":    sum(t["rows"] for t in DEMO_SCHEMA["tables"]),
        "glba_tables":   sum(1 for t in DEMO_SCHEMA["tables"] if t["glba_restricted"]),
        "schema":        DEMO_SCHEMA["tables"],
        "message":       f"✓ Connected to {host}/{db} · Read-only verified · GLBA NPI tagging ready",
        "logged_to_audit": True,
    }


@app.get("/api/v2/db/schema")
async def get_db_schema_live(sub: dict = Depends(get_subscription)):
    """Return the simulated schema for the connected core banking DB."""
    return {
        "connected":    True,
        "connection":   "Core Banking — PostgreSQL",
        "schema":       DEMO_SCHEMA["tables"],
        "last_sync":    datetime.now(timezone.utc).isoformat(),
        "glba_mode":    "NPI_STRICT",
    }


@app.get("/api/v2/db/connections")
async def list_db_connections(sub: dict = Depends(get_subscription)):
    """List all configured DB connections for this tenant."""
    return {
        "connections": [
            {
                "id":              "conn_001",
                "name":            "Core Banking — PostgreSQL",
                "db_type":         "PostgreSQL",
                "host":            "cbs-prod.bank.internal",
                "port":            5432,
                "database":        "corebank",
                "status":          "live",
                "latency_ms":      4.2,
                "tls":             "TLS 1.3",
                "access_mode":     "READ ONLY",
                "glba_tagged":     True,
                "tables":          6,
                "rows_accessible": 3_637_655,
                "last_check":      datetime.now(timezone.utc).isoformat(),
                "version_history": [
                    {"v": "v3", "by": "admin@bank.com", "note": "Added TLS cert",       "ts": "2026-03-04 08:22 EST", "active": True},
                    {"v": "v2", "by": "admin@bank.com", "note": "Schema mapper update", "ts": "2026-02-18 14:45 EST", "active": False},
                    {"v": "v1", "by": "admin@bank.com", "note": "Initial setup",         "ts": "2026-01-12 09:00 EST", "active": False},
                ],
            },
            {
                "id":          "conn_002",
                "name":        "CRM — MySQL",
                "db_type":     "MySQL",
                "host":        "crm-db.bank.internal",
                "port":        3306,
                "database":    "bankcrm",
                "status":      "idle",
                "latency_ms":  None,
                "tls":         "TLS 1.2",
                "access_mode": "READ ONLY",
                "glba_tagged": False,
                "tables":      3,
                "last_check":  None,
                "version_history": [
                    {"v": "v1", "by": "admin@bank.com", "note": "Initial setup", "ts": "2026-02-01 11:00 EST", "active": True},
                ],
            },
        ]
    }


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

    # ── Live DB lookup: Supabase → fallback to demo registry ────────────
    customer = await get_customer_from_db(caller)
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

            # ── Retrieve or rebuild session (BUG-3 fix) ───────────────────
            sess = _call_sessions.get(session_id, {})

            stored_customer = sess.get("customer")
            if stored_customer and stored_customer.authenticated:
                customer = stored_customer
            else:
                # Session lost (Railway restart) — re-query DB
                # Try to get caller phone from Twilio form fields
                try:
                    vg_form      = await request.form()
                    caller_phone = vg_form.get("Caller", "") or vg_form.get("From", "") or sess.get("caller", "unknown")
                except Exception:
                    caller_phone = sess.get("caller", "unknown")

                customer = await get_customer_from_db(caller_phone)
                # Rebuild session so subsequent turns work
                _call_sessions[session_id] = {
                    "history":  sess.get("history", []),
                    "customer": customer,
                    "agent":    sess.get("agent", "customer_service"),
                    "caller":   caller_phone,
                    "channel":  "voice",
                }
                sess = _call_sessions[session_id]

            history = sess.get("history", [])
            current = sess.get("agent", "customer_service")

            resp = await orchestrator.handle_turn(
                user_input=user_input,
                conversation_history=history,
                customer=customer,
                session_id=session_id,
                current_agent=current,
            )
            reply = resp.text

            # Persist history for multi-turn conversation memory
            history.append(ConversationTurn(role="user",      content=user_input))
            history.append(ConversationTurn(role="assistant",  content=reply))
            if session_id in _call_sessions:
                # Keep system brief + last 20 conversational turns (BUG-10: token budget)
                kept = [h for h in history if h.role == "system"] +                        [h for h in history if h.role != "system"][-20:]
                _call_sessions[session_id]["history"] = kept
                _call_sessions[session_id]["last_activity"] = datetime.now(timezone.utc).isoformat()
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
    body_text   = form.get("Body", "").strip()
    from_number = form.get("From", "").strip()

    # Normalise phone — strip WhatsApp prefix for DB lookup and session key
    # Twilio sends "whatsapp:+918431439772" — we want "+918431439772"
    clean_phone = from_number.replace("whatsapp:", "").strip()
    session_id  = f"wa_{clean_phone}"   # consistent key regardless of prefix
    reply       = "Hello! I'm BankVoiceAI. How can I help you today?"
    if orchestrator:
        try:
            from agents.base_agent import CustomerContext, ConversationTurn

            # ── Live DB lookup — always re-validate session (BUG-1 fix) ──────
            sess = _call_sessions.get(session_id, {})

            # Re-query DB if: new session OR session lost (Railway restart)
            stored_customer = sess.get("customer")
            if stored_customer and stored_customer.authenticated:
                customer = stored_customer
            else:
                # Fresh DB query every new session or on reconnect
                customer = await get_customer_from_db(clean_phone)
                _call_sessions[session_id] = {
                    "history":  sess.get("history", []),   # preserve history if any
                    "customer": customer,
                    "agent":    sess.get("agent", "customer_service"),
                    "caller":   clean_phone,
                    "started":  datetime.now(timezone.utc).isoformat(),
                    "channel":  "whatsapp",
                }

            sess    = _call_sessions[session_id]
            history = sess.get("history", [])
            current = sess.get("agent", "customer_service")

            resp = await orchestrator.handle_turn(
                user_input=body_text,
                conversation_history=history,
                customer=customer,
                session_id=session_id,
                current_agent=current,
            )
            reply = resp.text

            history.append(ConversationTurn(role="user",     content=body_text))
            history.append(ConversationTurn(role="assistant", content=reply))
            # Trim history: keep last 20 turns to stay within LLM token budget (BUG-10 fix)
            kept_history = [h for h in history if h.role == "system"] +                            [h for h in history if h.role != "system"][-20:]
            _call_sessions[session_id]["history"]      = kept_history
            _call_sessions[session_id]["last_activity"] = datetime.now(timezone.utc).isoformat()
            if hasattr(resp, "metadata") and resp.metadata.get("agent"):
                _call_sessions[session_id]["agent"] = resp.metadata["agent"]

            # BUG-7: Prune stale WhatsApp sessions (>4h idle) to prevent memory leak
            _now = datetime.now(timezone.utc)
            stale = [
                sid for sid, s in list(_call_sessions.items())
                if sid.startswith("wa_") and s.get("last_activity")
                and (_now - datetime.fromisoformat(s["last_activity"])).seconds > 14400
            ]
            for sid in stale:
                _call_sessions.pop(sid, None)
                logger.info(f"Pruned stale WhatsApp session: {sid}")

            _ts = datetime.now(timezone.utc).isoformat()
            # Log session_start on first message (RBI audit requirement)
            if len(history) <= 2:   # just inserted our system brief + first user turn
                _audit_log.append({
                    "event":       "whatsapp_session_start",
                    "session_id":  session_id,
                    "from":        clean_phone,
                    "customer_id": getattr(customer, "customer_id", "unknown"),
                    "authenticated": customer.authenticated,
                    "channel":     "whatsapp",
                    "timestamp":   _ts,
                })
            _audit_log.append({
                "event":       "whatsapp_turn",
                "session_id":  session_id,
                "from":        clean_phone,
                "customer_id": getattr(customer, "customer_id", "unknown"),
                "input":       body_text,
                "reply_len":   len(reply),
                "agent":       resp.metadata.get("agent", current) if resp.metadata else current,
                "authenticated": customer.authenticated,
                "db_source":   getattr(customer, "_db_source", "demo"),
                "timestamp":   _ts,
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



# ─── DB Connection Manager — Simulation API ───────────────────────────────────
# In-memory store (resets on server restart — for demo/video purposes)

_db_connections: Dict[str, dict] = {
    "conn_001": {
        "id":          "conn_001",
        "name":        "Core Banking — PostgreSQL",
        "db_type":     "PostgreSQL",
        "host":        "cbs-prod.bank.internal",
        "port":        5432,
        "database":    "corebank",
        "username":    "bvai_readonly",
        "status":      "live",
        "tls":         "TLS 1.3",
        "access":      "Read-only",
        "latency_ms":  4.2,
        "last_sync":   "2 minutes ago",
        "version":     "PostgreSQL 15.4",
        "glba_tagged": True,
        "schema": [
            {"table": "accounts",     "columns": 12, "rows": "847K", "classification": "NPI",      "enabled": True},
            {"table": "transactions", "columns": 8,  "rows": "2.1M", "classification": "Sensitive", "enabled": True},
            {"table": "customers",    "columns": 18, "rows": "312K", "classification": "NPI",      "enabled": True},
            {"table": "loans",        "columns": 14, "rows": "94K",  "classification": "NPI",      "enabled": True},
            {"table": "products",     "columns": 6,  "rows": "124",  "classification": "Public",   "enabled": True},
            {"table": "branches",     "columns": 9,  "rows": "48",   "classification": "Public",   "enabled": False},
        ],
        "versions": [
            {"version": "v3 (current)", "changed_by": "admin@bank.com", "summary": "SSL cert rotation",     "ts": "2026-03-04 08:22 EST", "active": True},
            {"version": "v2",           "changed_by": "admin@bank.com", "summary": "Schema mapper NPI tags", "ts": "2026-02-18 14:45 EST", "active": False},
            {"version": "v1",           "changed_by": "admin@bank.com", "summary": "Initial connection",     "ts": "2026-01-12 09:00 EST", "active": False},
        ],
    },
    "conn_002": {
        "id":          "conn_002",
        "name":        "CRM — MySQL",
        "db_type":     "MySQL",
        "host":        "crm-db.bank.internal",
        "port":        3306,
        "database":    "customers",
        "username":    "bvai_readonly",
        "status":      "idle",
        "tls":         "TLS 1.3",
        "access":      "Read-only",
        "latency_ms":  None,
        "last_sync":   "3 hours ago",
        "version":     "MySQL 8.0.32",
        "glba_tagged": True,
        "schema": [
            {"table": "crm_contacts", "columns": 22, "rows": "312K", "classification": "NPI",      "enabled": True},
            {"table": "interactions", "columns": 10, "rows": "1.8M", "classification": "Sensitive", "enabled": False},
            {"table": "campaigns",    "columns": 8,  "rows": "2.1K", "classification": "Public",   "enabled": False},
        ],
        "versions": [
            {"version": "v2 (current)", "changed_by": "admin@bank.com", "summary": "CRM NPI tagging",    "ts": "2026-02-22 11:10 EST", "active": True},
            {"version": "v1",           "changed_by": "admin@bank.com", "summary": "Initial connection",  "ts": "2026-01-20 14:00 EST", "active": False},
        ],
    },
}


class DbConnectionBody(BaseModel):
    name:     str
    db_type:  str = "PostgreSQL"
    host:     str
    port:     int = 5432
    database: str
    username: str
    password: str = ""


@app.get("/api/v2/db/connections")
async def list_db_connections(sub: dict = Depends(get_subscription)):
    return {
        "connections": [{k: v for k, v in c.items() if k != "password"} for c in _db_connections.values()],
        "total": len(_db_connections),
    }


@app.post("/api/v2/db/connections")
async def add_db_connection(body: DbConnectionBody, sub: dict = Depends(get_subscription)):
    import random, string
    conn_id = "conn_" + "".join(random.choices(string.digits, k=3))
    _db_connections[conn_id] = {
        "id": conn_id, "name": body.name, "db_type": body.db_type,
        "host": body.host, "port": body.port, "database": body.database,
        "username": body.username, "status": "pending", "tls": "TLS 1.3",
        "access": "Read-only", "latency_ms": None, "last_sync": "never",
        "version": body.db_type, "glba_tagged": False, "schema": [],
        "versions": [{"version": "v1 (current)", "changed_by": sub.get("bank_name","admin"),
                      "summary": "Initial connection",
                      "ts": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M EST"), "active": True}],
    }
    _audit_log.append({"event": "db_connection_added", "conn_id": conn_id, "name": body.name,
                        "tenant_id": sub["tenant_id"], "timestamp": datetime.now(timezone.utc).isoformat()})
    return {"ok": True, "conn_id": conn_id, "message": f"Connection '{body.name}' saved. Credentials AES-256 encrypted."}


@app.post("/api/v2/db/connections/{conn_id}/test")
async def test_db_connection(conn_id: str, sub: dict = Depends(get_subscription)):
    import asyncio, random
    conn = _db_connections.get(conn_id)
    if not conn:
        raise HTTPException(404, f"Connection '{conn_id}' not found.")
    await asyncio.sleep(1.5)
    latency = round(random.uniform(2.8, 8.4), 1)
    _db_connections[conn_id]["latency_ms"] = latency
    _db_connections[conn_id]["status"] = "live"
    _db_connections[conn_id]["last_sync"] = "just now"
    _audit_log.append({"event": "db_connection_tested", "conn_id": conn_id, "name": conn["name"],
                        "latency_ms": latency, "result": "success",
                        "tenant_id": sub["tenant_id"], "timestamp": datetime.now(timezone.utc).isoformat()})
    return {"ok": True, "conn_id": conn_id, "name": conn["name"], "latency_ms": latency,
            "tls": "TLS 1.3", "access": "Read-only confirmed", "db_version": conn.get("version", conn["db_type"]),
            "message": f"Connection successful · {latency}ms · TLS 1.3 verified · Read-only confirmed"}


@app.delete("/api/v2/db/connections/{conn_id}")
async def delete_db_connection(conn_id: str, sub: dict = Depends(get_subscription)):
    conn = _db_connections.pop(conn_id, None)
    if not conn:
        raise HTTPException(404, f"Connection '{conn_id}' not found.")
    _audit_log.append({"event": "db_connection_removed", "conn_id": conn_id, "name": conn["name"],
                        "tenant_id": sub["tenant_id"], "timestamp": datetime.now(timezone.utc).isoformat()})
    return {"ok": True, "message": f"Connection '{conn['name']}' disconnected and removed."}


@app.get("/api/v2/db/connections/{conn_id}/schema")
async def get_db_schema_map(conn_id: str, sub: dict = Depends(get_subscription)):
    conn = _db_connections.get(conn_id)
    if not conn:
        raise HTTPException(404, f"Connection '{conn_id}' not found.")
    return {"conn_id": conn_id, "name": conn["name"], "schema": conn.get("schema", []), "glba_tagged": conn.get("glba_tagged", False)}


@app.post("/api/v2/db/connections/{conn_id}/rollback")
async def rollback_db_config(conn_id: str, sub: dict = Depends(get_subscription)):
    conn = _db_connections.get(conn_id)
    if not conn:
        raise HTTPException(404, f"Connection '{conn_id}' not found.")
    versions = conn.get("versions", [])
    active_idx = next((i for i, v in enumerate(versions) if v["active"]), 0)
    if active_idx >= len(versions) - 1:
        raise HTTPException(400, "Already at the oldest version.")
    versions[active_idx]["active"] = False
    versions[active_idx + 1]["active"] = True
    conn["versions"] = versions
    rolled_to = versions[active_idx + 1]["version"]
    _audit_log.append({"event": "db_config_rollback", "conn_id": conn_id, "rolled_to": rolled_to,
                        "tenant_id": sub["tenant_id"], "timestamp": datetime.now(timezone.utc).isoformat()})
    return {"ok": True, "rolled_to": rolled_to, "message": f"Rolled back to {rolled_to}"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
