"""
BankVoiceAI — Compliance Agent
Handles: CFPB complaints, data privacy requests, regulatory escalations
"""
import logging
from typing import List
from .base_agent import BaseAgent, CustomerContext, ConversationTurn, AgentResponse

logger = logging.getLogger(__name__)


class ComplianceAgent(BaseAgent):
    AGENT_NAME = "compliance"

    SYSTEM_PROMPT = """You are a compliance specialist AI for {bank_name}, trained on US banking regulations.

RULES:
- Handle complaints with empathy, document everything, escalate to human compliance officer.
- For CFPB complaints: acknowledge, apologize, log it, and ensure human follow-up within 60 days (regulatory requirement).
- For data privacy (GLBA) requests: acknowledge the right, explain the bank's privacy policy, and transfer to compliance team.
- NEVER dismiss or minimize a complaint — take every concern seriously.
- For formal complaints, always provide a reference number (use session_id).
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
                text="I'll connect you with our compliance officer immediately.",
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
                action="log_compliance_event",
                metadata={"agent": self.AGENT_NAME, "ref": session_id},
            )
        except Exception as e:
            logger.error(f"ComplianceAgent error: {e}")
            return AgentResponse(
                text="I'm connecting you with our compliance team directly.",
                escalate=True,
            )
