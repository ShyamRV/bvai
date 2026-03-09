"""
BankVoiceAI — Customer Service Agent
Handles: balance inquiries, FAQs, transaction history, general support
Free tier: ASI:ONE 100K tokens/day
"""
import logging
from typing import List
from .base_agent import BaseAgent, _safe_format, CustomerContext, ConversationTurn, AgentResponse

logger = logging.getLogger(__name__)


class CustomerServiceAgent(BaseAgent):
    AGENT_NAME = "customer_service"

    SYSTEM_PROMPT = """You are a professional AI customer service agent for {bank_name}.

THE FOLLOWING ACCOUNT DATA IS FROM THE BANK DATABASE. IT IS 100% ACCURATE. USE IT EXACTLY:

{context}

ABSOLUTE RULES — NEVER BREAK THESE:
1. ALL amounts are in US DOLLARS (USD). NEVER use ₹ or rupees or any other currency.
2. ONLY quote the balances and transactions shown above. NEVER invent or hallucinate figures.
3. The customer is VERIFIED. Do NOT ask for any identity verification whatsoever.
4. Do NOT say "I don't have access" or "I cannot see your account" — the data is above.
5. Do NOT transfer to a human unless the customer explicitly says "human" or "agent".
6. Keep responses under 60 words. Speak naturally for voice/WhatsApp delivery.
7. If asked for balance: state CHECKING and SAVINGS from the data above, in USD.
8. If asked for transactions: read the RECENT TRANSACTIONS from the data above.
9. If asked about loans: read the LOANS section from the data above.

BANK: {bank_name}"""

    def __init__(self, config: dict):
        super().__init__(
            asi_one_api_key=config.get("asi_one_api_key", ""),
            asi_one_api_url=config.get("asi_one_api_url", "https://api.asi1.ai/v1"),
            bank_name=config.get("bank_name", "your bank"),
            asi_one_model=config.get("asi_one_model", "asi1-mini"),
        )

    async def handle_turn(
        self,
        user_input: str,
        conversation_history: List[ConversationTurn],
        customer: CustomerContext,
        session_id: str,
    ) -> AgentResponse:
        # Check escalation triggers first
        if self.detect_escalation_request(user_input):
            return AgentResponse(
                text="Of course! Let me transfer you to a customer service representative right away. Please hold.",
                escalate=True,
                metadata={"reason": "customer_request"},
            )

        sentiment = self.analyze_sentiment(user_input)
        if sentiment == "very_negative":
            return AgentResponse(
                text="I sincerely apologize for the difficulty you're experiencing. Let me connect you with a senior representative immediately.",
                escalate=True,
                metadata={"reason": "negative_sentiment"},
            )

        context_str = self.build_context_string(customer)
        system = _safe_format(self.SYSTEM_PROMPT,
            bank_name=self.bank_name,
            context=context_str,
        )

        # Only user/assistant roles — system roles stripped here AND in call_asi_one
        messages = [
            {"role": t.role, "content": t.content}
            for t in conversation_history[-20:]
            if t.role in ("user", "assistant")
        ]
        messages.append({"role": "user", "content": user_input})

        try:
            response_text = await self.call_llm_with_fallback(
                messages, system, temperature=0.4, max_tokens=150
            )
            return AgentResponse(
                text=response_text,
                metadata={"agent": self.AGENT_NAME, "sentiment": sentiment},
            )
        except Exception as e:
            logger.error(f"CustomerServiceAgent error: {e}")
            return AgentResponse(
                text="I'm sorry, I'm having trouble right now. Let me connect you with a representative.",
                escalate=True,
                metadata={"error": str(e)},
            )
