"""
BankVoiceAI — Collections Agent
Handles: payment reminders, payment plans, loan inquiries
FDCPA compliant: Mini-Miranda disclosure on every call
"""
import logging
from typing import List
from .base_agent import BaseAgent, _safe_format, CustomerContext, ConversationTurn, AgentResponse

logger = logging.getLogger(__name__)


class CollectionsAgent(BaseAgent):
    AGENT_NAME = "collections"

    SYSTEM_PROMPT = """You are a compliant debt collection AI agent for {bank_name}.

AUTHENTICATION: If context shows "Auth: ✓ VERIFIED", caller is authenticated via registered
phone number. Do NOT ask for SSN or account verification. Proceed directly to helping them.

FDCPA COMPLIANCE (MANDATORY):
- You MUST deliver the Mini-Miranda disclosure at the start of this collections call.
- NEVER threaten illegal actions (jail, criminal charges for non-payment).
- NEVER call before 8am or after 9pm (system enforces this).
- NEVER discuss the debt with third parties.
- If customer invokes their right to cease communication, acknowledge and end call.
- If customer disputes the debt, note it and escalate to human agent.

GOALS:
1. If VERIFIED: skip identity check — go straight to payment options.
   If NOT VERIFIED: ask for last 4 of account number only (not SSN).
2. Offer payment options: full payment, payment plan, hardship program.
3. Arrange a promise-to-pay if customer agrees.
4. Keep responses SHORT (under 60 words) — this is voice.

CONTEXT: {context}
BANK: {bank_name}"""

    MINI_MIRANDA = (
        "This is an attempt to collect a debt. "
        "Any information obtained will be used for that purpose. "
        "This communication is from {bank_name}, a debt collector."
    )

    def __init__(self, config: dict):
        super().__init__(
            asi_one_api_key=config.get("asi_one_api_key", ""),
            asi_one_api_url=config.get("asi_one_api_url", "https://api.asi1.ai/v1"),
            bank_name=config.get("bank_name", "your bank"),
            asi_one_model=config.get("asi_one_model", "asi1-mini"),
        )

    def get_mini_miranda(self) -> str:
        return self.MINI_MIRANDA.format(bank_name=self.bank_name)

    async def handle_turn(
        self,
        user_input: str,
        conversation_history: List[ConversationTurn],
        customer: CustomerContext,
        session_id: str,
    ) -> AgentResponse:
        # Detect cease-and-desist invocation
        cease_phrases = ["stop calling", "cease", "do not contact", "don't contact", "stop contacting"]
        if any(p in user_input.lower() for p in cease_phrases):
            return AgentResponse(
                text="We will honor your request to cease communication. A written notice will be sent to confirm. Have a good day.",
                end_call=True,
                action="log_cease_and_desist",
                metadata={"compliance_action": "cease_and_desist", "session_id": session_id},
            )

        # Debt dispute
        dispute_phrases = ["i dispute", "not my debt", "wrong amount", "don't owe", "do not owe"]
        if any(p in user_input.lower() for p in dispute_phrases):
            return AgentResponse(
                text="I understand you're disputing this debt. I'm noting your dispute and connecting you with a specialist who can provide written debt validation.",
                escalate=True,
                action="log_debt_dispute",
                metadata={"compliance_action": "debt_dispute"},
            )

        if self.detect_escalation_request(user_input):
            return AgentResponse(
                text="I'll connect you with a human representative now. Please hold.",
                escalate=True,
            )

        # Mini-Miranda only on very first turn AND only if not a service call
        is_first_turn = len(conversation_history) == 0
        context_str   = self.build_context_string(customer)
        system        = _safe_format(self.SYSTEM_PROMPT,
            bank_name = self.bank_name,
            context   = context_str,
        )

        messages = [
            {"role": t.role, "content": t.content}
            for t in conversation_history[-15:]
        ]
        # Only inject Mini-Miranda on the very first turn for collections calls
        # For verified callers asking about balances/transactions — skip it
        balance_words = ["balance","transaction","history","statement","loan","payment","account"]
        is_collections_call = not any(w in user_input.lower() for w in balance_words)
        if is_first_turn and is_collections_call:
            messages.insert(0, {
                "role": "system",
                "content": f"IMPORTANT: Begin your response with the Mini-Miranda disclosure: '{self.get_mini_miranda()}'"
            })
        messages.append({"role": "user", "content": user_input})

        try:
            response_text = await self.call_llm_with_fallback(
                messages, system, temperature=0.3, max_tokens=200
            )
            return AgentResponse(
                text=response_text,
                metadata={"agent": self.AGENT_NAME},
            )
        except Exception as e:
            logger.error(f"CollectionsAgent error: {e}")
            return AgentResponse(
                text="I'm having a technical issue. A collections specialist will call you back within one business day.",
                end_call=True,
                metadata={"error": str(e)},
            )
