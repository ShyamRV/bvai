"""
BankVoiceAI — Onboarding Agent
Handles: new account applications, KYC, account setup
"""
import logging
from typing import List
from .base_agent import BaseAgent, CustomerContext, ConversationTurn, AgentResponse

logger = logging.getLogger(__name__)


class OnboardingAgent(BaseAgent):
    AGENT_NAME = "onboarding"

    SYSTEM_PROMPT = """You are an account onboarding AI specialist for {bank_name}.

RULES:
- Guide new customers through account opening in a friendly, step-by-step manner.
- You CANNOT complete the application — you collect info and hand off to a human banker.
- Information to collect: Full name, address, SSN (last 4 only over phone), date of birth, email.
- NEVER store or repeat full SSNs. Only confirm last 4 digits.
- After collecting basics, transfer to human banker to complete the KYC and application.
- GLBA compliance: explain how {bank_name} handles customer data privacy.
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
        if self.detect_escalation_request(user_input):
            return AgentResponse(
                text="I'll connect you with a personal banker who can complete your application right away. Please hold.",
                escalate=True,
            )

        # After 6+ turns, transfer to human to complete KYC
        if len(conversation_history) >= 6:
            return AgentResponse(
                text="Great, I have your preliminary information. Let me transfer you to a banker who will complete your application and get your account opened today.",
                escalate=True,
                action="transfer_to_onboarding_banker",
                metadata={"agent": self.AGENT_NAME, "reason": "kyc_handoff"},
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
                messages, system, temperature=0.3, max_tokens=150
            )
            return AgentResponse(
                text=response_text,
                metadata={"agent": self.AGENT_NAME},
            )
        except Exception as e:
            logger.error(f"OnboardingAgent error: {e}")
            return AgentResponse(
                text="Let me connect you with a banker to complete your application.",
                escalate=True,
            )
