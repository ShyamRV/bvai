"""
BankVoiceAI — Customer Service Agent
Handles: balance inquiries, FAQs, transaction history, general support
Free tier: ASI:ONE 100K tokens/day
"""
import logging
from typing import List
from .base_agent import BaseAgent, CustomerContext, ConversationTurn, AgentResponse

logger = logging.getLogger(__name__)


class CustomerServiceAgent(BaseAgent):
    AGENT_NAME = "customer_service"

    SYSTEM_PROMPT = """You are a professional AI customer service representative for {bank_name}, a US community bank.

RULES:
- Be warm, concise, and professional. Responses must be SHORT (under 60 words) for voice delivery.
- NEVER disclose account details unless customer is authenticated (Auth: VERIFIED in context).
- Always offer to escalate to a human if the customer seems frustrated.
- If asked about specific account numbers, SSN, passwords — NEVER provide or confirm them.
- Speak naturally — this will be read aloud by a text-to-speech engine.
- End responses with a brief question to continue helping.

CONTEXT: {context}
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
        system = self.SYSTEM_PROMPT.format(
            bank_name=self.bank_name,
            context=context_str,
        )

        messages = [
            {"role": t.role, "content": t.content}
            for t in conversation_history[-15:]
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
