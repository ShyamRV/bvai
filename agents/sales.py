"""
BankVoiceAI — Sales Agent
Handles: product inquiries, cross-sell, new account interest
TCPA compliant: only contacts customers with prior consent
"""
import logging
from typing import List
from .base_agent import BaseAgent, CustomerContext, ConversationTurn, AgentResponse

logger = logging.getLogger(__name__)


class SalesAgent(BaseAgent):
    AGENT_NAME = "sales"

    SYSTEM_PROMPT = """You are a consultative banking sales AI for {bank_name}.

RULES:
- TCPA compliance: This customer has given prior express written consent to receive sales calls.
- Be helpful and consultative, NOT pushy. Never pressure the customer.
- Focus on ONE product per call — don't overwhelm with options.
- If customer shows interest, offer to transfer to a human banker to complete the application.
- Keep responses SHORT (under 60 words) — this is voice.

PRODUCTS YOU CAN DISCUSS:
- High-yield savings account (APY: 4.85%, no minimum balance, FDIC insured)
- Personal checking account (no monthly fees, 55K ATMs free)
- Auto loans (rates from 6.49% APR, 72-hour approval)
- Home equity line of credit (rates from 7.25% APR, up to $500K)
- Business checking (no transaction fees, free bill pay)
- Credit cards (1.5% cash back, no annual fee, 0% intro APR 15 months)

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
                text="I'll connect you with one of our personal bankers who can walk you through everything and get you started today. Please hold.",
                escalate=True,
                metadata={"reason": "sales_handoff"},
            )

        # Opt-out handling (TCPA)
        opt_out_phrases = ["not interested", "remove me", "stop calling", "opt out", "don't call"]
        if any(p in user_input.lower() for p in opt_out_phrases):
            return AgentResponse(
                text="Absolutely, I'll remove you from our outreach list right away. We apologize for any inconvenience. Is there anything else I can help you with today?",
                action="opt_out_sales",
                end_call=True,
                metadata={"agent": self.AGENT_NAME, "action": "tcpa_opt_out"},
            )

        context_str = self.build_context_string(customer)
        system = self.SYSTEM_PROMPT.format(
            bank_name=self.bank_name, context=context_str
        )
        messages = [
            {"role": t.role, "content": t.content}
            for t in conversation_history[-15:]
        ]
        messages.append({"role": "user", "content": user_input})

        try:
            response_text = await self.call_llm_with_fallback(
                messages, system, temperature=0.5, max_tokens=150
            )
            return AgentResponse(
                text=response_text,
                metadata={"agent": self.AGENT_NAME},
            )
        except Exception as e:
            logger.error(f"SalesAgent error: {e}")
            return AgentResponse(
                text="Let me connect you with one of our bankers who can better assist you.",
                escalate=True,
            )
