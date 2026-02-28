"""
BankVoiceAI — Base Agent Foundation (Updated Feb 2026)
All agents inherit from this class.
Uses: Fetch.ai uAgents 0.14.x + ASI:ONE free tier LLM
"""

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field

import httpx
from uagents import Agent, Context, Model
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)


# ─── Data Models ──────────────────────────────────────────────────────────────

@dataclass
class ConversationTurn:
    role: str          # "user" | "assistant"
    content: str
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CustomerContext:
    """Enriched customer context from bank systems (or demo mode)."""
    customer_id: Optional[str] = None
    account_number: Optional[str] = None
    full_name: Optional[str] = None
    phone: Optional[str] = None
    language: str = "en-US"
    authenticated: bool = False
    account_balance: Optional[float] = None
    loan_accounts: List[Dict] = field(default_factory=list)
    recent_transactions: List[Dict] = field(default_factory=list)
    fraud_flags: List[str] = field(default_factory=list)
    consent_recorded: bool = False
    call_recording_consent: bool = False
    demo_mode: bool = True


class AgentResponse:
    def __init__(
        self,
        text: str,
        action: Optional[str] = None,
        escalate: bool = False,
        end_call: bool = False,
        twiml: Optional[str] = None,
        metadata: Dict = None,
    ):
        self.text = text
        self.action = action       # e.g. "log_promise_to_pay", "flag_fraud"
        self.escalate = escalate   # Hand off to human agent
        self.end_call = end_call   # Hang up cleanly
        self.twiml = twiml         # Raw TwiML override (optional)
        self.metadata = metadata or {}


# ─── Fetch.ai uAgent Message Models ───────────────────────────────────────────

class TurnRequest(Model):
    session_id: str
    user_input: str
    agent_name: str
    customer_json: str  # JSON-serialized CustomerContext


class TurnResponse(Model):
    session_id: str
    text: str
    escalate: bool
    end_call: bool
    metadata_json: str  # JSON-serialized metadata dict


# ─── Base Agent ───────────────────────────────────────────────────────────────

class BaseAgent(ABC):
    """
    Foundation class for all BankVoiceAI agents.
    Handles: ASI:ONE LLM calls, compliance triggers,
    sentiment escalation, and context management.
    """

    AGENT_NAME: str = "base"
    SYSTEM_PROMPT: str = ""
    MAX_TURNS: int = 50

    # US Compliance Disclosures (CFPB / FDCPA / TCPA)
    REQUIRED_DISCLOSURES = {
        "call_start": (
            "This call may be recorded for quality and compliance purposes. "
            "You are speaking with an AI assistant from {bank_name}. "
            "You may request a human agent at any time by saying 'agent' or pressing zero."
        ),
        "debt_collection": (
            "This is an attempt to collect a debt. "
            "Any information obtained will be used for that purpose. "
            "This communication is from a debt collector."
        ),
        "marketing": (
            "The following is a marketing message from {bank_name}. "
            "You may opt out at any time by saying 'stop' or pressing nine."
        ),
    }

    def __init__(
        self,
        asi_one_api_key: str,
        asi_one_api_url: str,
        bank_name: str = "your bank",
        asi_one_model: str = "asi1-mini",
    ):
        self.asi_one_api_key = asi_one_api_key
        self.asi_one_api_url = asi_one_api_url
        self.bank_name = bank_name
        self.asi_one_model = asi_one_model
        self.http_client = httpx.AsyncClient(timeout=30.0)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
    )
    async def call_asi_one(
        self,
        messages: List[Dict[str, str]],
        system_prompt: str,
        temperature: float = 0.3,
        max_tokens: int = 512,
    ) -> str:
        """Call ASI:ONE LLM (Fetch.ai's free-tier model) with retry logic.
        Free tier: 100K tokens/day at https://asi1.ai
        """
        headers = {
            "Authorization": f"Bearer {self.asi_one_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.asi_one_model,   # "asi1-mini" — free tier
            "messages": [{"role": "system", "content": system_prompt}] + messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        try:
            response = await self.http_client.post(
                f"{self.asi_one_api_url}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]
        except httpx.HTTPStatusError as e:
            logger.error(f"ASI:ONE error {e.response.status_code}: {e.response.text}")
            raise
        except Exception as e:
            logger.error(f"ASI:ONE call failed: {e}")
            raise

    async def call_llm_with_fallback(
        self,
        messages: List[Dict[str, str]],
        system_prompt: str,
        temperature: float = 0.3,
        max_tokens: int = 512,
    ) -> str:
        """Try ASI:ONE first, fall back to OpenAI GPT-4o-mini if needed."""
        try:
            return await self.call_asi_one(
                messages, system_prompt, temperature, max_tokens
            )
        except Exception as e:
            logger.warning(f"ASI:ONE unavailable, using OpenAI fallback: {e}")
            import os
            openai_key = os.getenv("OPENAI_API_KEY", "")
            if not openai_key:
                return "I'm experiencing a technical issue. Let me transfer you to a representative."

            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=openai_key)
            try:
                resp = await client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "system", "content": system_prompt}] + messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                return resp.choices[0].message.content
            except Exception as fallback_err:
                logger.error(f"OpenAI fallback also failed: {fallback_err}")
                return "I'm experiencing a technical issue. Let me connect you with a representative."

    def detect_escalation_request(self, text: str) -> bool:
        """Detect human-agent request (CFPB compliance required)."""
        phrases = [
            "human", "agent", "representative", "person", "supervisor",
            "manager", "real person", "talk to someone", "speak to someone",
            "transfer me", "operator", "live agent", "press 0", "zero",
            "speak with", "talk with", "connect me",
        ]
        text_lower = text.lower()
        return any(p in text_lower for p in phrases)

    def analyze_sentiment(self, text: str) -> str:
        """Rule-based sentiment for auto-escalation."""
        negative = [
            "angry", "furious", "terrible", "ridiculous", "lawsuit",
            "attorney", "lawyer", "complaint", "unacceptable", "incompetent",
            "useless", "disgusting", "fraud", "scam", "stealing",
        ]
        text_lower = text.lower()
        count = sum(1 for w in negative if w in text_lower)
        if count >= 2:
            return "very_negative"
        elif count == 1:
            return "negative"
        return "neutral"

    def build_context_string(self, customer: CustomerContext) -> str:
        """Serialize CustomerContext for LLM system prompt injection."""
        parts = []
        if customer.demo_mode:
            parts.append("MODE: DEMO — Use realistic sample data for responses")
        if customer.full_name:
            parts.append(f"Customer Name: {customer.full_name}")
        if customer.authenticated:
            parts.append("Auth: VERIFIED")
            if customer.account_balance is not None:
                parts.append(f"Balance: ${customer.account_balance:,.2f}")
            if customer.loan_accounts:
                loans = ", ".join(
                    f"{l.get('type','Loan')}: ${l.get('balance',0):,.2f}"
                    for l in customer.loan_accounts[:3]
                )
                parts.append(f"Loans: {loans}")
        else:
            parts.append("Auth: NOT VERIFIED — Do NOT disclose account details")
        if customer.fraud_flags:
            parts.append(f"FRAUD ALERTS: {', '.join(customer.fraud_flags)}")
        parts.append(f"Language: {customer.language}")
        return "\n".join(parts)

    @abstractmethod
    async def handle_turn(
        self,
        user_input: str,
        conversation_history: List[ConversationTurn],
        customer: CustomerContext,
        session_id: str,
    ) -> AgentResponse:
        """Process one conversation turn. Must be implemented by subclasses."""
        pass

    async def get_opening_message(self, customer: CustomerContext) -> str:
        disclosure = self.REQUIRED_DISCLOSURES["call_start"].format(
            bank_name=self.bank_name
        )
        greeting = (
            f"Hello, {customer.full_name.split()[0]}. "
            if customer.full_name
            else "Hello. "
        )
        return f"{disclosure} {greeting}How can I help you today?"

    async def close(self):
        await self.http_client.aclose()
