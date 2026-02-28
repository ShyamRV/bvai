"""
BankVoiceAI — FastAPI Application (February 2026)
Handles: Voice webhooks, WhatsApp, Admin API, Health checks
Stack: FastAPI 0.115 + Fetch.ai uAgents 0.14 + ASI:ONE free tier
"""
import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


# ─── Settings ─────────────────────────────────────────────────────────────────

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Fetch.ai / ASI:ONE (free tier)
    asi_one_api_key: str = ""
    asi_one_api_url: str = "https://api.asi1.ai/v1"
    asi_one_model: str = "asi1-mini"

    # OpenAI (fallback STT + LLM)
    openai_api_key: str = ""

    # Twilio
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""
    twilio_whatsapp_number: str = ""
    twilio_webhook_base_url: str = "https://your-ngrok-id.ngrok-free.app"

    # Database & Cache (free tiers: Supabase / Upstash)
    database_url: str = "postgresql+asyncpg://localhost/bankvoiceai"
    redis_url: str = "redis://localhost:6379/0"
    session_ttl_seconds: int = 3600

    # Auth
    jwt_secret_key: str = "change-me-at-least-64-chars"
    jwt_algorithm: str = "HS256"

    # App
    bank_name: str = "First Community Bank"
    fetch_agent_seed: str = "bankvoiceai-unique-seed"
    fetch_agent_port: int = 8001
    demo_mode: bool = True
    human_agent_phone: str = "+10000000000"
    log_level: str = "INFO"


settings = Settings()

# Globals (initialized at startup)
orchestrator = None
session_manager = None


# ─── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global orchestrator, session_manager
    logger.info("BankVoiceAI starting up...")

    from agents import OrchestratorAgent
    from api.services.session_manager import SessionManager

    config = {
        "asi_one_api_key": settings.asi_one_api_key,
        "asi_one_api_url": settings.asi_one_api_url,
        "asi_one_model": settings.asi_one_model,
        "bank_name": settings.bank_name,
        "fetch_agent_seed": settings.fetch_agent_seed,
        "fetch_agent_port": settings.fetch_agent_port,
        "demo_mode": settings.demo_mode,
    }

    orchestrator = OrchestratorAgent(config)
    session_manager = SessionManager(settings.redis_url, settings.session_ttl_seconds)

    logger.info(f"BankVoiceAI ready. Bank: {settings.bank_name} | Demo: {settings.demo_mode}")
    yield
    logger.info("BankVoiceAI shutting down.")
    await orchestrator.close()


# ─── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="BankVoiceAI API",
    description="AI Voice Agent Platform for US Banks — Fetch.ai + ASI:ONE",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Health ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "BankVoiceAI", "version": "2.0.0"}


@app.get("/ready")
async def ready():
    checks = {
        "orchestrator": orchestrator is not None,
        "session_manager": session_manager is not None,
    }
    ok = all(checks.values())
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"ready": ok, "checks": checks},
    )


# ─── Voice Webhooks (Twilio) ──────────────────────────────────────────────────

@app.post("/voice/inbound", response_class=Response)
async def voice_inbound(request: Request):
    """Twilio inbound call webhook. Creates session and plays CFPB disclosure."""
    form = await request.form()
    caller = form.get("From", "unknown")
    call_sid = form.get("CallSid", str(uuid.uuid4()))

    await session_manager.create_session(
        session_id=call_sid,
        caller_phone=caller,
        channel="voice",
        bank_id=settings.bank_name,
    )
    logger.info(f"Inbound call: {caller} -> session {call_sid}")

    base = settings.twilio_webhook_base_url
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joanna" language="en-US">
    This call may be recorded for quality and compliance purposes.
    You are speaking with an A I assistant from {settings.bank_name}.
    You may request a human agent at any time by saying agent or pressing zero.
  </Say>
  <Gather input="speech dtmf" timeout="5" speechTimeout="3"
          action="{base}/voice/gather/{call_sid}"
          method="POST" language="en-US" enhanced="true">
    <Say voice="Polly.Joanna">How can I help you today?</Say>
  </Gather>
  <Say voice="Polly.Joanna">I didn't catch that. Please call back if you need help.</Say>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


@app.post("/voice/gather/{session_id}", response_class=Response)
async def voice_gather(session_id: str, request: Request):
    """Twilio speech/DTMF input webhook."""
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

    # DTMF 0 = immediate transfer
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
    ctx_data = session.get("customer_context", {})
    ctx_data["demo_mode"] = settings.demo_mode
    customer = CustomerContext(**{
        k: v for k, v in ctx_data.items()
        if k in CustomerContext.__dataclass_fields__
    })

    try:
        response = await orchestrator.handle_turn(
            user_input=user_input,
            conversation_history=history,
            customer=customer,
            session_id=session_id,
            current_agent=session.get("current_agent", "customer_service"),
        )
        await session_manager.append_turn(
            session_id, "assistant", response.text, metadata=response.metadata
        )

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
  <Say voice="Polly.Joanna">Thank you for calling {settings.bank_name}. Have a great day. Goodbye.</Say>
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
  <Say voice="Polly.Joanna">I didn't catch that. Please call back if you need help.</Say>
</Response>"""
    except Exception as e:
        logger.error(f"Orchestrator error: {e}")
        await session_manager.end_session(session_id, "error")
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joanna">I'm having a technical issue. Let me connect you with a representative.</Say>
  <Dial><Number>{settings.human_agent_phone}</Number></Dial>
</Response>"""

    return Response(content=twiml, media_type="application/xml")


@app.post("/voice/status", response_class=Response)
async def voice_status(request: Request):
    """Twilio call status callback."""
    form = await request.form()
    call_sid = form.get("CallSid")
    call_status = form.get("CallStatus")
    duration = form.get("CallDuration", "0")
    logger.info(f"Call {call_sid} status: {call_status} | duration: {duration}s")

    if call_status in ("completed", "busy", "failed", "no-answer"):
        await session_manager.end_session(call_sid, reason=call_status)
    return Response(content="", status_code=204)


# ─── WhatsApp Webhook ─────────────────────────────────────────────────────────

@app.post("/whatsapp/inbound", response_class=Response)
async def whatsapp_inbound(request: Request):
    """Twilio WhatsApp inbound message webhook."""
    form = await request.form()
    from_number = form.get("From", "")
    body = form.get("Body", "").strip()

    session_key = f"wa_{from_number.replace('+', '').replace(':', '').replace('whatsapp', '')}"
    session = await session_manager.get_session(session_key)
    if not session:
        session = await session_manager.create_session(
            session_id=session_key,
            caller_phone=from_number,
            channel="whatsapp",
            bank_id=settings.bank_name,
        )

    await session_manager.append_turn(session_key, "user", body)

    from agents.base_agent import CustomerContext, ConversationTurn
    history = [
        ConversationTurn(role=t["role"], content=t["content"])
        for t in session.get("conversation_history", [])[-20:]
    ]
    customer = CustomerContext(demo_mode=settings.demo_mode)

    try:
        response = await orchestrator.handle_turn(
            user_input=body,
            conversation_history=history,
            customer=customer,
            session_id=session_key,
            current_agent=session.get("current_agent", "customer_service"),
        )
        await session_manager.append_turn(session_key, "assistant", response.text)
        reply = response.text
        if response.escalate:
            reply += f"\n\n📞 A human representative will contact you shortly at this number."
    except Exception as e:
        logger.error(f"WhatsApp error: {e}")
        reply = "I'm experiencing a technical issue. Please call our main line for immediate assistance."

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response><Message>{reply}</Message></Response>"""
    return Response(content=twiml, media_type="application/xml")


# ─── Admin API ────────────────────────────────────────────────────────────────

@app.get("/api/admin/sessions/active")
async def get_active_sessions():
    sessions = await session_manager.get_active_sessions()
    return {"active_sessions": sessions, "count": len(sessions)}


@app.get("/api/admin/metrics")
async def get_metrics():
    sessions = await session_manager.get_active_sessions()
    return {
        "active_calls": len(sessions),
        "bank_name": settings.bank_name,
        "demo_mode": settings.demo_mode,
        "agents_online": [
            "customer_service", "collections", "sales",
            "fraud_detection", "compliance", "onboarding"
        ],
        "llm_provider": "ASI:ONE (Fetch.ai)",
        "llm_model": settings.asi_one_model,
    }


@app.get("/api/admin/demo-call")
async def demo_call():
    """Simulate a customer call for demo purposes (no Twilio needed)."""
    from agents.base_agent import CustomerContext, ConversationTurn

    demo_turns = [
        "Hi, I'd like to check my account balance",
        "What's my available credit on my credit card?",
        "I'd like to speak with someone about a payment plan",
    ]

    customer = CustomerContext(
        full_name="Jane Demo",
        authenticated=True,
        account_balance=4250.75,
        demo_mode=True,
    )
    history = []
    results = []

    for turn_input in demo_turns:
        conv_history = [
            ConversationTurn(role=t["role"], content=t["content"])
            for t in history
        ]
        response = await orchestrator.handle_turn(
            user_input=turn_input,
            conversation_history=conv_history,
            customer=customer,
            session_id="demo-001",
            current_agent="customer_service" if not history else history[-1].get("agent", "customer_service"),
        )
        history.append({"role": "user", "content": turn_input, "agent": "user"})
        history.append({"role": "assistant", "content": response.text, "agent": response.metadata.get("agent")})
        results.append({
            "input": turn_input,
            "response": response.text,
            "agent": response.metadata.get("agent"),
            "escalate": response.escalate,
        })

    return {"demo_conversation": results}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "api.main:app",
        host=settings.asi_one_api_url and "0.0.0.0" or "127.0.0.1",
        port=8000,
        reload=True,
        log_level=settings.log_level.lower(),
    )
