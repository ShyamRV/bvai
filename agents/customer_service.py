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

    def __init__(self, config: dict):
        super().__init__(
            asi_one_api_key = config.get("asi_one_api_key", ""),
            asi_one_api_url = config.get("asi_one_api_url", "https://api.asi1.ai/v1"),
            bank_name       = config.get("bank_name", "your bank"),
            asi_one_model   = config.get("asi_one_model", "asi1-mini"),
        )

    def _build_system_prompt(self, customer: CustomerContext) -> str:
        """
        Build the complete system prompt as a plain string concatenation.
        NO .format() — avoids any risk of KeyError or placeholder not substituting.
        Account data is embedded verbatim so the LLM cannot ignore it.
        """
        bank = self.bank_name

        if not customer.authenticated:
            return (
                f"You are a customer service agent for {bank}.\n"
                "The caller has NOT been verified. "
                "Ask ONLY for the last 4 digits of their account number. "
                "Do NOT reveal any account data."
            )

        name     = customer.full_name      or "Valued Customer"
        acct     = customer.account_number or "on file"
        checking = customer.account_balance or 0.0
        savings  = getattr(customer, "savings_balance", None) or 0.0
        loans    = customer.loan_accounts          or []
        txns     = customer.recent_transactions    or []

        # Build loan lines
        loan_lines = ""
        for ln in loans:
            loan_lines += (
                f"\n  - {ln.get('type','Loan')}: "
                f"Balance ${ln.get('balance',0):,.2f} USD, "
                f"Monthly payment ${ln.get('monthly_payment',0):,.2f} USD, "
                f"Due {ln.get('due_date','N/A')}, "
                f"Status: {ln.get('status','current')}"
            )

        # Build transaction lines
        txn_lines = ""
        for t in txns:
            txn_lines += f"\n  - {t.get('date','')}: {t.get('desc','')} {t.get('amount','')} USD"

        prompt = (
            f"You are a professional customer service agent for {bank}, a US community bank.\n\n"
            "THE FOLLOWING DATA COMES DIRECTLY FROM THE BANK DATABASE. IT IS AUTHORITATIVE.\n"
            "YOU MUST USE THIS DATA EXACTLY. DO NOT INVENT OR MODIFY ANY FIGURES.\n\n"
            "========== VERIFIED CUSTOMER ACCOUNT ==========\n"
            f"Customer Name   : {name}\n"
            f"Account Number  : {acct}\n"
            f"Auth Status     : VERIFIED via registered phone number\n\n"
            "ACCOUNT BALANCES — US DOLLARS ONLY:\n"
            f"  Checking Account : ${checking:,.2f} USD\n"
            f"  Savings Account  : ${savings:,.2f} USD\n"
            f"  Total            : ${checking + savings:,.2f} USD\n"
        )

        if loan_lines:
            prompt += f"\nACTIVE LOANS:{loan_lines}\n"

        if txn_lines:
            prompt += f"\nRECENT TRANSACTIONS:{txn_lines}\n"

        prompt += (
            "\n================================================\n"
            "RULES — NEVER BREAK ANY OF THESE:\n"
            "1. Currency is ALWAYS US DOLLARS. NEVER output rupees, ₹, INR, or any non-USD symbol.\n"
            "2. ONLY quote the numbers above. NEVER invent account numbers, balances, or transactions.\n"
            "3. Customer is VERIFIED. Do NOT ask for any identity check.\n"
            "4. Do NOT say you lack access to account data — the data is above.\n"
            "5. Do NOT escalate to human unless the customer explicitly says 'agent' or 'human'.\n"
            "6. Keep your response under 60 words. Be warm and direct.\n"
            "7. When asked for balance: state BOTH checking and savings in USD.\n"
            "================================================"
        )
        return prompt

    async def handle_turn(
        self,
        user_input:           str,
        conversation_history: List[ConversationTurn],
        customer:             CustomerContext,
        session_id:           str,
    ) -> AgentResponse:

        if self.detect_escalation_request(user_input):
            return AgentResponse(
                text     = "Of course! Transferring you to a customer service representative now. Please hold.",
                escalate = True,
                metadata = {"reason": "customer_request"},
            )

        if self.analyze_sentiment(user_input) == "very_negative":
            return AgentResponse(
                text     = "I sincerely apologize. Let me connect you with a senior representative immediately.",
                escalate = True,
                metadata = {"reason": "negative_sentiment"},
            )

        system = self._build_system_prompt(customer)

        messages = [
            {"role": t.role, "content": t.content}
            for t in conversation_history[-20:]
            if t.role in ("user", "assistant")
        ]
        messages.append({"role": "user", "content": user_input})

        logger.info(
            f"[CS] session={session_id} auth={customer.authenticated} "
            f"checking={customer.account_balance} savings={getattr(customer,'savings_balance',None)}"
        )

        try:
            reply = await self.call_llm_with_fallback(
                messages, system, temperature=0.3, max_tokens=150
            )

            # Safety net: if LLM still outputs rupee symbol, override completely
            if "₹" in reply or "INR" in reply or "rupee" in reply.lower():
                checking = customer.account_balance or 0.0
                savings  = getattr(customer, "savings_balance", None) or 0.0
                reply = (
                    f"Hi {(customer.full_name or 'there').split()[0]}! "
                    f"Your checking account balance is ${checking:,.2f} "
                    f"and your savings account balance is ${savings:,.2f}. "
                    "All amounts are in US dollars. Is there anything else I can help you with?"
                )
                logger.warning(f"[CS] LLM returned rupees — overridden with DB data")

            return AgentResponse(text=reply, metadata={"agent": self.AGENT_NAME})

        except Exception as e:
            logger.error(f"CustomerServiceAgent error: {e}")
            return AgentResponse(
                text     = "I'm sorry, I'm having trouble right now. Let me connect you with a representative.",
                escalate = True,
                metadata = {"error": str(e)},
            )
