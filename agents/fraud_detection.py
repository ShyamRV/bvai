"""
BankVoiceAI — Fraud Detection Agent
Handles: suspicious activity reports, card blocks, fraud escalation
"""
import logging
from typing import List
from .base_agent import BaseAgent, CustomerContext, ConversationTurn, AgentResponse

logger = logging.getLogger(__name__)


class FraudDetectionAgent(BaseAgent):
    AGENT_NAME = "fraud_detection"

    SYSTEM_PROMPT = """You are a fraud prevention specialist AI for {bank_name}.

RULES:
- Treat every fraud report as URGENT — customer safety is the top priority.
- NEVER ask for full card numbers, PINs, or passwords over the phone.
- You can ask for partial info (last 4 digits) to verify identity.
- For active fraud: immediately block the card (trigger action="block_card").
- For suspicious activity: confirm details and escalate to human fraud team.
- Be calm, reassuring, and efficient. Customers are often distressed.
- Responses must be SHORT (under 60 words) — this is voice.

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
        text_lower = user_input.lower()

        # Immediate card block trigger (UPDATED)
        block_phrases = [
            "block my card",
            "cancel my card",
            "lost my card",
            "card lost",
            "stolen card",
            "card stolen",
            "my card was stolen",
            "my card is stolen",
            "someone stole my card",
        ]

        if any(p in text_lower for p in block_phrases):
            return AgentResponse(
                text="I'm blocking your card immediately for your protection. A replacement card will arrive in 5 to 7 business days. Can you confirm the last four digits of the affected card?",
                action="block_card",
                metadata={
                    "agent": self.AGENT_NAME,
                    "action_taken": "card_block_initiated",
                },
            )

        # Escalate active fraud to human
        active_fraud_phrases = [
            "unauthorized",
            "didn't make",
            "didn't authorize",
            "fraud charge",
            "fraudulent",
            "someone used",
        ]

        if any(p in text_lower for p in active_fraud_phrases):
            return AgentResponse(
                text="I understand there are unauthorized charges on your account. I'm connecting you with our fraud specialist immediately. They have the authority to reverse charges and secure your account.",
                escalate=True,
                action="flag_fraud",
                metadata={
                    "agent": self.AGENT_NAME,
                    "fraud_type": "unauthorized_charges",
                },
            )

        if self.detect_escalation_request(user_input):
            return AgentResponse(
                text="Connecting you with our fraud team now. Please stay on the line.",
                escalate=True,
            )

        context_str = self.build_context_string(customer)
        system = self.SYSTEM_PROMPT.format(
            bank_name=self.bank_name, context=context_str
        )

        messages = [
            {"role": t.role, "content": t.content}
            for t in conversation_history[-10:]
        ]
        messages.append({"role": "user", "content": user_input})

        try:
            response_text = await self.call_llm_with_fallback(
                messages, system, temperature=0.2, max_tokens=150
            )
            return AgentResponse(
                text=response_text,
                metadata={"agent": self.AGENT_NAME},
            )
        except Exception as e:
            logger.error(f"FraudDetectionAgent error: {e}")
            return AgentResponse(
                text="I'm connecting you directly to our fraud prevention team.",
                escalate=True,
            )