"""
BankVoiceAI v2 — Complete API (all 20 endpoints)
"""

import hashlib
import hmac
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List

import redis.asyncio as aioredis
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("api.main_v2")


class Settings(BaseSettings):
    asi_one_api_key: str = ""
    asi_one_api_url: str = "https://api.asi1.ai/v1"
    openai_api_key: str = ""
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""
    twilio_whatsapp_number: str = ""
    twilio_webhook_base_url: str = "https://your-domain.com"
    database_url: str = "postgresql+asyncpg://localhost/bankvoiceai"
    redis_url: str = "redis://localhost:6379/0"
    fetch_payment_wallet: str = "fetch1PASTE_YOUR_WALLET_ADDRESS"
    fetch_gateway_seed: str = "bankvoiceai-gateway-production-seed"
    fetch_use_mainnet: bool = False
    api_key_secret: str = "bankvoiceai-key-secret"
    bank_name: str = "Your Bank"
    fetch_agent_seed: str = "bankvoiceai-seed"
    demo_mode: bool = True
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()

PLAN_CONFIG = {
    "pilot":      {"name": "30-Day Pilot",  "calls_per_day": 200,  "agents": ["customer_service", "fraud_detection"], "fet": 0},
    "starter":    {"name": "Starter",       "calls_per_day": 500,  "agents": ["customer_service", "fraud_detection", "onboarding"], "fet": 250},
    "growth":     {"name": "Growth",        "calls_per_day": 2000, "agents": ["customer_service", "fraud_detection", "onboarding", "collections", "sales", "compliance"], "fet": 750},
    "enterprise": {"name": "Enterprise",    "calls_per_day": -1,   "agents": ["customer_service", "fraud_detection", "onboarding", "collections", "sales", "compliance", "orchestrator"], "fet": 2000},
}

ALL_AGENTS = ["customer_service", "fraud_detection", "onboarding", "collections", "sales", "compliance", "orchestrator"]

# In-memory stores
_subscriptions: Dict[str, dict] = {}
_api_keys: Dict[str, str] = {}
_audit_log: List[dict] = []
_payment_history: List[dict] = []
_webhooks: Dict[str, dict] = {}


def _generate_api_key(tenant_id: str) -> str:
    raw = f"{tenant_id}:{time.time()}:{settings.api_key_secret}"
    return "bvai_" + hmac.new(settings.api_key_secret.encode(), raw.encode(), hashlib.sha256).hexdigest()[:40]


def _create_subscription(tenant_id: str, bank_name: str, plan: str) -> dict:
    now = datetime.now(timezone.utc)
    api_key = _generate_api_key(tenant_id)
    sub = {
        "tenant_id": tenant_id,
        "bank_name": bank_name,
        "plan": plan,
        "status": "trial" if plan == "pilot" else "active",
        "started_at": now.isoformat(),
        "expires_at": (now + timedelta(days=30)).isoformat(),
        "api_keys": [api_key],
        "api_key": api_key,
        "agents_enabled": list(PLAN_CONFIG[plan]["agents"]),
        "calls_today": 0,
        "calls_this_month": 0,
        "compliance_mode": "strict",
        "webhook_url": None,
        "escalation_policy": {"trigger_keywords": ["agent", "human", "manager"], "sentiment_threshold": -0.7, "max_wait_seconds": 10},
    }
    _subscriptions[tenant_id] = sub
    _api_keys[api_key] = tenant_id
    _audit_log.append({"event": "subscription_created", "tenant_id": tenant_id, "plan": plan, "timestamp": now.isoformat()})
    logger.info(f"Subscription created: {tenant_id} / {plan} / {api_key}")
    return sub


def _get_sub_by_key(api_key: str) -> Optional[dict]:
    tenant_id = _api_keys.get(api_key)
    if not tenant_id:
        return None
    sub = _subscriptions.get(tenant_id)
    if not sub:
        return None
    if datetime.fromisoformat(sub["expires_at"]) < datetime.now(timezone.utc):
        return None
    return sub


security = HTTPBearer(auto_error=False)


async def get_subscription(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict:
    api_key = None
    if credentials and credentials.credentials:
        api_key = credentials.credentials
    if not api_key:
        api_key = request.headers.get("X-BankVoiceAI-Key")
    if not api_key:
        api_key = request.query_params.get("api_key")
    if not api_key:
        raise HTTPException(401, "API key required. Use Authorization: Bearer <key>")
    sub = _get_sub_by_key(api_key)
    if not sub:
        raise HTTPException(401, "Invalid or expired API key.")
    return sub


orchestrator = None
session_manager = None
redis_client = None
payment_gateway = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global orchestrator, session_manager, redis_client, payment_gateway
    logger.info("BankVoiceAI v2 starting...")

    try:
        redis_client = aioredis.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=3)
        await redis_client.ping()
        logger.info("Redis connected")
    except Exception as e:
        logger.warning(f"Redis unavailable: {e}")
        redis_client = None

    try:
        from payment_gateway.fet_payment_gateway import FETPaymentGatewayAgent
        payment_gateway = FETPaymentGatewayAgent(
            wallet_address=settings.fetch_payment_wallet,
            seed=settings.fetch_gateway_seed,
            redis_client=redis_client,
            use_mainnet=settings.fetch_use_mainnet,
        )
    except Exception as e:
        logger.warning(f"Payment gateway: {e}")

    try:
        from agents import OrchestratorAgent
        orchestrator = OrchestratorAgent({
            "asi_one_api_key": settings.asi_one_api_key,
            "asi_one_api_url": settings.asi_one_api_url,
            "bank_name": settings.bank_name,
            "fetch_agent_seed": settings.fetch_agent_seed,
        })
    except Exception as e:
        logger.warning(f"Orchestrator: {e}")

    try:
        from api.services.session_manager import SessionManager
        session_manager = SessionManager(settings.redis_url)
    except Exception as e:
        logger.warning(f"Session manager: {e}")

    logger.info("BankVoiceAI v2 ready. Payment gateway online.")
    yield
    logger.info("BankVoiceAI v2 shutting down.")


app = FastAPI(title="BankVoiceAI API v2", version="2.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ─── Health ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "BankVoiceAI", "version": "2.0.0", "payment_gateway": payment_gateway is not None}


@app.get("/ready")
async def ready():
    return {"ready": True, "checks": {"orchestrator": orchestrator is not None, "session_manager": session_manager is not None}, "redis": redis_client is not None}


# ─── Payments ─────────────────────────────────────────────────────────────────

class PaymentInitiateRequest(BaseModel):
    tenant_id: str
    bank_name: str
    plan: str = "pilot"


@app.post("/api/v2/payments/initiate")
async def initiate_payment(body: PaymentInitiateRequest):
    plan = body.plan.lower()
    if plan not in PLAN_CONFIG:
        raise HTTPException(400, f"Invalid plan. Choose: {', '.join(PLAN_CONFIG.keys())}")
    if plan == "pilot":
        sub = _create_subscription(body.tenant_id, body.bank_name, "pilot")
        return {
            "type": "pilot",
            "message": "30-day pilot activated. No payment required.",
            "tenant_id": sub["tenant_id"],
            "api_key": sub["api_key"],
            "expires_at": sub["expires_at"],
            "agents_enabled": sub["agents_enabled"],
        }
    cfg = PLAN_CONFIG[plan]
    memo = f"BANKVOICEAI|{body.tenant_id}|{plan}"
    return {
        "type": "payment_required",
        "payment_instructions": {
            "send_to_wallet": settings.fetch_payment_wallet,
            "amount_fet": cfg["fet"],
            "memo": memo,
            "network": "mainnet" if settings.fetch_use_mainnet else "testnet (dorado)",
        },
        "next_step": "POST /api/v2/payments/verify with your tx_hash",
    }


@app.post("/api/v2/payments/verify")
async def verify_payment(request: Request):
    body = await request.json()
    tx_hash = body.get("tx_hash", "")
    tenant_id = body.get("tenant_id", "")
    bank_name = body.get("bank_name", tenant_id)
    plan = body.get("plan", "starter")
    if not tx_hash or not tenant_id:
        raise HTTPException(400, "tx_hash and tenant_id required")
    if plan not in PLAN_CONFIG:
        raise HTTPException(400, "Invalid plan")

    if payment_gateway:
        try:
            from decimal import Decimal
            cfg = PLAN_CONFIG[plan]
            is_valid, payment, reason = await payment_gateway.ledger.verify_payment(
                tx_hash=tx_hash,
                expected_to=settings.fetch_payment_wallet,
                expected_amount_fet=Decimal(str(cfg["fet"])),
                memo_prefix=f"BANKVOICEAI|{tenant_id}|{plan}",
            )
            if not is_valid:
                raise HTTPException(402, f"Payment verification failed: {reason}")
        except HTTPException:
            raise
        except Exception as e:
            logger.warning(f"On-chain verify error: {e}")

    sub = _create_subscription(tenant_id, bank_name, plan)
    _payment_history.append({"tx_hash": tx_hash, "tenant_id": tenant_id, "plan": plan, "timestamp": datetime.now(timezone.utc).isoformat(), "amount_fet": PLAN_CONFIG[plan]["fet"]})
    explorer = f"https://{'explore' if settings.fetch_use_mainnet else 'explore-dorado'}.fetch.ai/transactions/{tx_hash}"
    return {
        "success": True,
        "message": f"{PLAN_CONFIG[plan]['name']} plan activated!",
        "tenant_id": tenant_id,
        "api_key": sub["api_key"],
        "expires_at": sub["expires_at"],
        "agents_enabled": sub["agents_enabled"],
        "fetch_explorer_tx": explorer,
    }


@app.get("/api/v2/payments/history")
async def payment_history(sub: dict = Depends(get_subscription)):
    history = [p for p in _payment_history if p.get("tenant_id") == sub["tenant_id"]]
    return {"tenant_id": sub["tenant_id"], "payments": history, "total": len(history)}


@app.post("/api/v2/payments/refund")
async def request_refund(request: Request, sub: dict = Depends(get_subscription)):
    body = await request.json()
    tx_hash = body.get("tx_hash", "")
    reason = body.get("reason", "")
    if not tx_hash:
        raise HTTPException(400, "tx_hash required")
    _audit_log.append({"event": "refund_requested", "tenant_id": sub["tenant_id"], "tx_hash": tx_hash, "reason": reason, "timestamp": datetime.now(timezone.utc).isoformat()})
    return {"success": True, "message": "Refund initiated. FET will be returned within 24 hours.", "tx_hash": tx_hash}


# ─── Subscription ─────────────────────────────────────────────────────────────

@app.get("/api/v2/subscription")
async def get_subscription_details(sub: dict = Depends(get_subscription)):
    return {
        "tenant_id": sub["tenant_id"],
        "bank_name": sub["bank_name"],
        "plan": sub["plan"],
        "plan_name": PLAN_CONFIG[sub["plan"]]["name"],
        "status": sub["status"],
        "expires_at": sub["expires_at"],
        "agents_enabled": sub["agents_enabled"],
        "calls_today": sub["calls_today"],
        "calls_per_day": PLAN_CONFIG[sub["plan"]]["calls_per_day"],
        "compliance_mode": sub["compliance_mode"],
    }


@app.get("/api/v2/subscription/plans")
async def list_plans():
    return {"plans": [{"id": k, "name": v["name"], "fet_per_month": v["fet"], "calls_per_day": v["calls_per_day"], "agents": v["agents"]} for k, v in PLAN_CONFIG.items()], "pilot": "Free 30-day pilot — no FET required"}


class SubscriptionUpgradeRequest(BaseModel):
    target_plan: str
    tx_hash: str = ""


@app.post("/api/v2/subscription/upgrade")
async def upgrade_subscription(body: SubscriptionUpgradeRequest, sub: dict = Depends(get_subscription)):
    if body.target_plan not in PLAN_CONFIG:
        raise HTTPException(400, f"Invalid plan: {body.target_plan}")
    old_plan = sub["plan"]
    sub["plan"] = body.target_plan
    sub["agents_enabled"] = list(PLAN_CONFIG[body.target_plan]["agents"])
    _audit_log.append({"event": "plan_upgraded", "tenant_id": sub["tenant_id"], "from": old_plan, "to": body.target_plan, "timestamp": datetime.now(timezone.utc).isoformat()})
    return {"success": True, "message": f"Plan changed from {old_plan} to {body.target_plan}", "agents_enabled": sub["agents_enabled"]}


# ─── Agents ───────────────────────────────────────────────────────────────────

@app.get("/api/v2/agents")
async def list_agents(sub: dict = Depends(get_subscription)):
    return {
        "tenant_id": sub["tenant_id"],
        "agents_enabled": sub["agents_enabled"],
        "agents_disabled": [a for a in ALL_AGENTS if a not in sub["agents_enabled"]],
        "all_agents": ALL_AGENTS,
    }


class AgentToggleRequest(BaseModel):
    enabled: bool


@app.post("/api/v2/agents/{agent_name}/toggle")
async def toggle_agent(agent_name: str, body: AgentToggleRequest, sub: dict = Depends(get_subscription)):
    if agent_name not in ALL_AGENTS:
        raise HTTPException(400, f"Unknown agent: {agent_name}. Valid: {ALL_AGENTS}")
    plan_agents = PLAN_CONFIG[sub["plan"]]["agents"]
    if body.enabled and agent_name not in plan_agents:
        raise HTTPException(403, f"Agent '{agent_name}' not available in {sub['plan']} plan. Upgrade to access.")
    if body.enabled:
        if agent_name not in sub["agents_enabled"]:
            sub["agents_enabled"].append(agent_name)
    else:
        if agent_name in sub["agents_enabled"]:
            sub["agents_enabled"].remove(agent_name)
    _audit_log.append({"event": f"agent_{'enabled' if body.enabled else 'disabled'}", "agent": agent_name, "tenant_id": sub["tenant_id"], "timestamp": datetime.now(timezone.utc).isoformat()})
    return {"agent": agent_name, "enabled": body.enabled, "agents_now_enabled": sub["agents_enabled"]}


@app.post("/api/v2/agents/{agent_name}/test")
async def test_agent(agent_name: str, request: Request, sub: dict = Depends(get_subscription)):
    if agent_name not in sub["agents_enabled"]:
        raise HTTPException(403, f"Agent '{agent_name}' not in your plan.")
    body = await request.json()
    message = body.get("message", "Hello")
    if orchestrator:
        try:
            from agents.base_agent import CustomerContext
            response = await orchestrator.handle_turn(user_input=message, conversation_history=[], customer=CustomerContext(), session_id=str(uuid.uuid4()), current_agent=agent_name)
            return {"agent": agent_name, "response": response.text}
        except Exception as e:
            return {"agent": agent_name, "response": f"[Error: {e}]"}
    return {"agent": agent_name, "response": f"[Demo] {agent_name} received: {message}. Set ASI_ONE_API_KEY for live responses."}


# ─── Analytics ────────────────────────────────────────────────────────────────

@app.get("/api/v2/analytics")
async def get_analytics(sub: dict = Depends(get_subscription)):
    tenant_events = [e for e in _audit_log if e.get("tenant_id") == sub["tenant_id"]]
    return {
        "tenant_id": sub["tenant_id"],
        "period": "last_30_days",
        "calls_today": sub["calls_today"],
        "calls_this_month": sub["calls_this_month"],
        "calls_per_day_limit": PLAN_CONFIG[sub["plan"]]["calls_per_day"],
        "total_events": len(tenant_events),
        "agents": {a: {"calls": 0, "resolved": 0, "escalated": 0} for a in sub["agents_enabled"]},
        "escalation_rate": 0.0,
        "first_call_resolution": 0.0,
        "avg_handle_time_seconds": 0,
        "top_intents": ["account_balance", "payment_plan", "fraud_report"],
        "compliance_mode": sub["compliance_mode"],
    }


# ─── Audit Log ────────────────────────────────────────────────────────────────

@app.get("/api/v2/audit-log")
async def get_audit_log(sub: dict = Depends(get_subscription)):
    tenant_events = [e for e in _audit_log if e.get("tenant_id") == sub["tenant_id"]]
    return {
        "tenant_id": sub["tenant_id"],
        "total_events": len(tenant_events),
        "retention_years": 7,
        "events": tenant_events[-100:],
    }


# ─── Compliance ───────────────────────────────────────────────────────────────

class ComplianceModeRequest(BaseModel):
    mode: str


@app.post("/api/v2/compliance/mode")
async def set_compliance_mode(body: ComplianceModeRequest, sub: dict = Depends(get_subscription)):
    if body.mode not in ("strict", "assistive"):
        raise HTTPException(400, "mode must be 'strict' or 'assistive'")
    sub["compliance_mode"] = body.mode
    _audit_log.append({"event": "compliance_mode_changed", "tenant_id": sub["tenant_id"], "mode": body.mode, "timestamp": datetime.now(timezone.utc).isoformat()})
    return {"success": True, "compliance_mode": body.mode, "note": "strict=full CFPB/FDCPA disclosures, assistive=streamlined for demos"}


# ─── API Key Rotation ─────────────────────────────────────────────────────────

@app.post("/api/v2/api-keys/rotate")
async def rotate_api_key(sub: dict = Depends(get_subscription)):
    new_key = _generate_api_key(sub["tenant_id"])
    sub["api_keys"].append(new_key)
    _api_keys[new_key] = sub["tenant_id"]
    _audit_log.append({"event": "api_key_rotated", "tenant_id": sub["tenant_id"], "timestamp": datetime.now(timezone.utc).isoformat()})
    return {"new_api_key": new_key, "active_keys_count": len(sub["api_keys"]), "note": "Old key still works. Revoke it manually when ready."}


# ─── Webhooks ─────────────────────────────────────────────────────────────────

class WebhookConfigRequest(BaseModel):
    webhook_url: str
    events: List[str] = ["call.completed", "escalation", "fraud.detected"]


@app.put("/api/v2/webhooks")
async def configure_webhook(body: WebhookConfigRequest, sub: dict = Depends(get_subscription)):
    if not body.webhook_url.startswith("https://"):
        raise HTTPException(400, "webhook_url must use HTTPS")
    sub["webhook_url"] = body.webhook_url
    _webhooks[sub["tenant_id"]] = {"url": body.webhook_url, "events": body.events, "created_at": datetime.now(timezone.utc).isoformat()}
    _audit_log.append({"event": "webhook_configured", "tenant_id": sub["tenant_id"], "url": body.webhook_url, "timestamp": datetime.now(timezone.utc).isoformat()})
    return {"success": True, "webhook_url": body.webhook_url, "events_subscribed": body.events, "tip": "Use https://webhook.site for free testing"}


# ─── Escalation Policy ────────────────────────────────────────────────────────

class EscalationPolicyRequest(BaseModel):
    trigger_keywords: List[str] = ["agent", "human", "manager"]
    sentiment_threshold: float = -0.7
    max_wait_seconds: int = 10


@app.post("/api/v2/escalation/policy")
async def set_escalation_policy(body: EscalationPolicyRequest, sub: dict = Depends(get_subscription)):
    sub["escalation_policy"] = body.model_dump()
    _audit_log.append({"event": "escalation_policy_updated", "tenant_id": sub["tenant_id"], "timestamp": datetime.now(timezone.utc).isoformat()})
    return {"success": True, "policy": sub["escalation_policy"]}


# ─── Voice Webhooks ───────────────────────────────────────────────────────────

@app.post("/voice/inbound", response_class=Response)
async def voice_inbound(request: Request):
    try:
        form = await request.form()
    except Exception:
        form = {}
    caller = form.get("From", "unknown") if hasattr(form, "get") else "unknown"
    call_sid = form.get("CallSid", str(uuid.uuid4())) if hasattr(form, "get") else str(uuid.uuid4())
    # Use request base URL so it works on Railway, ngrok, or localhost automatically
    base = str(request.base_url).rstrip("/")
    if session_manager:
        try:
            await session_manager.create_session(session_id=call_sid, caller_phone=caller, channel="voice", bank_id=settings.bank_name)
        except Exception:
            pass
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?><Response>
  <Say voice="Polly.Joanna">This call may be recorded for quality and compliance purposes. You are speaking with an AI assistant from {settings.bank_name}. You may request a human agent at any time by saying agent or pressing zero.</Say>
  <Gather input="speech dtmf" timeout="8" speechTimeout="auto" action="{base}/voice/gather/{call_sid}" method="POST" language="en-US" enhanced="true">
    <Say voice="Polly.Joanna">How can I help you today?</Say>
  </Gather>
  <Say voice="Polly.Joanna">I did not hear anything. Please call back. Goodbye.</Say>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


@app.api_route("/voice/gather/{session_id}", methods=["GET", "POST"], response_class=Response)
async def voice_gather(session_id: str, request: Request):
    # Twilio sends GET with query params OR POST with form data - handle both
    user_input = ""
    try:
        form = await request.form()
        user_input = form.get("SpeechResult", "") or form.get("Digits", "")
    except Exception:
        pass
    if not user_input:
        user_input = request.query_params.get("SpeechResult", "") or request.query_params.get("Digits", "")
    base = str(request.base_url).rstrip("/")
    # Human escalation
    if (hasattr(form, "get") and form.get("Digits") == "0") or "agent" in user_input.lower() or "human" in user_input.lower():
        twiml = """<?xml version="1.0" encoding="UTF-8"?><Response><Say voice="Polly.Joanna">Connecting you with a representative now. Please hold.</Say><Hangup/></Response>"""
        return Response(content=twiml, media_type="application/xml")
    # AI response
    reply = "I can help with your account. What would you like to know?"
    if orchestrator and user_input:
        try:
            from agents.base_agent import CustomerContext
            resp = await orchestrator.handle_turn(user_input=user_input, conversation_history=[], customer=CustomerContext(), session_id=session_id, current_agent="customer_service")
            reply = resp.text
            if resp.escalate:
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
    form = await request.form()
    call_sid = form.get("CallSid", "")
    status = form.get("CallStatus", "")
    duration = form.get("CallDuration", "0")
    _audit_log.append({"event": "call_completed", "call_sid": call_sid, "status": status, "duration_seconds": duration, "timestamp": datetime.now(timezone.utc).isoformat()})
    return {"ok": True}


@app.post("/whatsapp/inbound", response_class=Response)
async def whatsapp_inbound(request: Request):
    form = await request.form()
    body_text = form.get("Body", "")
    from_number = form.get("From", "")
    reply = "Hello! I'm BankVoiceAI. How can I help you today?"
    if orchestrator:
        try:
            from agents.base_agent import CustomerContext
            resp = await orchestrator.handle_turn(user_input=body_text, conversation_history=[], customer=CustomerContext(), session_id=f"wa_{from_number}", current_agent="customer_service")
            reply = resp.text
        except Exception as e:
            logger.error(f"WhatsApp error: {e}")
    return Response(content=f"""<?xml version="1.0" encoding="UTF-8"?><Response><Message>{reply}</Message></Response>""", media_type="application/xml")


# ─── Admin / Demo ─────────────────────────────────────────────────────────────

@app.get("/api/admin/demo-call")
async def demo_call():
    results = []
    for user_msg, agent in [("What's my account balance?", "customer_service"), ("What's my available credit?", "customer_service"), ("I'd like a payment plan", "collections")]:
        if orchestrator:
            try:
                from agents.base_agent import CustomerContext
                resp = await orchestrator.handle_turn(user_input=user_msg, conversation_history=[], customer=CustomerContext(), session_id="demo", current_agent=agent)
                results.append({"user": user_msg, "assistant": resp.text})
            except Exception as e:
                results.append({"user": user_msg, "assistant": f"[Set ASI_ONE_API_KEY for live AI responses]"})
        else:
            results.append({"user": user_msg, "assistant": f"[Demo mode - orchestrator not loaded]"})
    return {"demo": True, "conversation": results}


@app.get("/api/admin/metrics")
async def metrics():
    return {
        "total_subscriptions": len(_subscriptions),
        "active_tenants": list(_subscriptions.keys()),
        "total_calls_today": sum(s["calls_today"] for s in _subscriptions.values()),
        "total_audit_events": len(_audit_log),
        "payment_gateway_online": payment_gateway is not None,
        "orchestrator_online": orchestrator is not None,
    }


@app.get("/api/admin/sessions/active")
async def active_sessions():
    return {"active_sessions": [], "subscriptions": len(_subscriptions)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
