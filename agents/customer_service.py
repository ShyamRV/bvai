"""
BankVoiceAI — Customer Service Agent
Handles: balance inquiries, transaction history, account info, general support
"""
import logging
from typing import List
from .base_agent import BaseAgent, CustomerContext, ConversationTurn, AgentResponse

logger = logging.getLogger(__name__)


class CustomerServiceAgent(BaseAgent):
    AGENT_NAME = "customer_service"

    SYSTEM_PROMPT = """You are a professional AI customer service agent for {bank_name}, a US community bank.

{account_brief}

RESPONSE STYLE:
- Warm, concise, professional. Under 60 words for voice/WhatsApp delivery.
- Speak naturally — text-to-speech will read this aloud.
- End with a short follow-up question like "Is there anything else I can help you with?"

WHAT TO DO WHEN ASKED ABOUT BALANCE:
Say both checking and savings amounts in US dollars from the data above.
Example: "Your checking account has $24,750.00 and your savings account has $58,320.50."

WHAT TO DO WHEN ASKED ABOUT TRANSACTIONS:
Read the recent transactions listed above with dates and amounts in dollars.

WHAT TO DO WHEN ASKED ABOUT LOANS:
State each loan type, balance, monthly payment, and due date from the data above."""

    def __init__(self, config: dict):
        super().__init__(
            asi_one_api_key = config.get("asi_one_api_key", ""),
            asi_one_api_url = config.get("asi_one_api_url", "https://api.asi1.ai/v1"),
            bank_name       = config.get("bank_name", "your bank"),
            asi_one_model   = config.get("asi_one_model", "asi1-mini"),
        )

    async def handle_turn(
        self,
        user_input:           str,
        conversation_history: List[ConversationTurn],
        customer:             CustomerContext,
        session_id:           str,
    ) -> AgentResponse:

        if self.detect_escalation_request(user_input):
            return AgentResponse(
                text="Of course! Let me transfer you to a customer service representative right away. Please hold.",
                escalate=True,
                metadata={"reason": "customer_request"},
            )

        if self.analyze_sentiment(user_input) == "very_negative":
            return AgentResponse(
                text="I sincerely apologize for the difficulty. Let me connect you with a senior representative immediately.",
                escalate=True,
                metadata={"reason": "negative_sentiment"},
            )

        # Build system prompt with full account brief embedded
        account_brief = self.build_account_brief(customer)
        system = self.SYSTEM_PROMPT.format(
            bank_name     = self.bank_name,
            account_brief = account_brief,
        )

        # Only user/assistant turns in history — system role not allowed mid-array
        messages = [
            {"role": t.role, "content": t.content}
            for t in conversation_history[-20:]
            if t.role in ("user", "assistant")
        ]
        messages.append({"role": "user", "content": user_input})

        try:
            reply = await self.call_llm_with_fallback(
                messages, system, temperature=0.3, max_tokens=150
            )
            return AgentResponse(
                text     = reply,
                metadata = {"agent": self.AGENT_NAME},
            )
        except Exception as e:
            logger.error(f"CustomerServiceAgent error: {e}")
            return AgentResponse(
                text     = "I'm sorry, I'm having trouble right now. Let me connect you with a representative.",
                escalate = True,
                metadata = {"error": str(e)},
            )
