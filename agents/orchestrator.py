"""
BankVoiceAI — Orchestrator Agent (Fetch.ai uAgents 0.14.x)
Master router: receives all contacts, classifies intent,
dispatches to specialist agents, manages human escalation.
"""
import asyncio
import logging
from typing import Optional, Dict, Any, List

from uagents import Agent, Context, Model

from .base_agent import BaseAgent, CustomerContext, ConversationTurn, AgentResponse
from .customer_service import CustomerServiceAgent
from .collections import CollectionsAgent
from .sales import SalesAgent
from .fraud_detection import FraudDetectionAgent
from .compliance import ComplianceAgent
from .onboarding import OnboardingAgent

logger = logging.getLogger(__name__)


# ─── Fetch.ai uAgent Message Models ───────────────────────────────────────────

class IncomingCallMessage(Model):
    session_id: str
    caller_phone: str
    channel: str        # "voice" | "whatsapp"
    initial_input: Optional[str] = None
    bank_id: str


class AgentHandoffMessage(Model):
    session_id: str
    from_agent: str
    to_agent: str
    reason: str


# ─── Orchestrator ─────────────────────────────────────────────────────────────

class OrchestratorAgent(BaseAgent):
    """
    Master orchestration agent.
    1. Classifies intent using ASI:ONE (free tier)
    2. Routes to correct specialist agent
    3. Manages escalation to human agents
    4. Broadcasts presence on Fetch.ai Almanac (free)
    """

    AGENT_NAME = "orchestrator"

    INTENT_ROUTING = {
        "balance_inquiry":      "customer_service",
        "transaction_history":  "customer_service",
        "account_info":         "customer_service",
        "general_faq":          "customer_service",
        "payment_reminder":     "collections",
        "loan_payment":         "collections",
        "payment_plan":         "collections",
        "debt_inquiry":         "collections",
        "product_inquiry":      "sales",
        "new_account":          "onboarding",
        "credit_card_inquiry":  "sales",
        "fraud_report":         "fraud_detection",
        "suspicious_activity":  "fraud_detection",
        "lost_card":            "fraud_detection",
        "kyc_update":           "compliance",
        "complaint":            "compliance",
        "data_privacy":         "compliance",
    }

    def __init__(self, config: Dict[str, Any]):
        super().__init__(
            asi_one_api_key=config.get("asi_one_api_key", ""),
            asi_one_api_url=config.get("asi_one_api_url", "https://api.asi1.ai/v1"),
            bank_name=config.get("bank_name", "your bank"),
            asi_one_model=config.get("asi_one_model", "asi1-mini"),
        )
        self.config = config

        # Initialize all specialist agents
        self.agents: Dict[str, BaseAgent] = {
            "customer_service": CustomerServiceAgent(config),
            "collections":      CollectionsAgent(config),
            "sales":            SalesAgent(config),
            "fraud_detection":  FraudDetectionAgent(config),
            "compliance":       ComplianceAgent(config),
            "onboarding":       OnboardingAgent(config),
        }

        # Fetch.ai uAgent — registers on the Almanac (free)
        # Port 8001 is used for agent-to-agent comms
        agent_seed = config.get("fetch_agent_seed", "bankvoiceai-default-seed")
        agent_port = int(config.get("fetch_agent_port", 8001))

        self.uagent = Agent(
            name="bankvoiceai_orchestrator",
            seed=agent_seed,
            port=agent_port,
            endpoint=[f"http://localhost:{agent_port}/submit"],
        )
        self._register_uagent_handlers()

    def _register_uagent_handlers(self):
        """Register Fetch.ai uAgent message handlers."""

        @self.uagent.on_message(model=IncomingCallMessage)
        async def handle_incoming(ctx: Context, sender: str, msg: IncomingCallMessage):
            logger.info(
                f"uAgent received: session={msg.session_id} "
                f"channel={msg.channel} from={sender}"
            )

        @self.uagent.on_event("startup")
        async def on_startup(ctx: Context):
            logger.info(
                f"BankVoiceAI Orchestrator uAgent started\n"
                f"  Address : {ctx.address}\n"
                f"  Almanac : Registered (free)\n"
                f"  Agents  : {list(self.agents.keys())}"
            )

    async def classify_intent(self, user_input: str) -> str:
        """Use ASI:ONE free tier to classify customer intent."""
        system_prompt = (
            "You are an intent classifier for a US bank IVR system. "
            "Classify the message into exactly ONE of these intents:\n"
            "balance_inquiry, transaction_history, account_info, general_faq, "
            "payment_reminder, loan_payment, payment_plan, debt_inquiry, "
            "product_inquiry, new_account, credit_card_inquiry, "
            "fraud_report, suspicious_activity, lost_card, "
            "kyc_update, complaint, data_privacy\n"
            "Respond with ONLY the intent label. Nothing else."
        )
        messages = [{"role": "user", "content": user_input}]
        try:
            intent = await self.call_asi_one(
                messages, system_prompt, temperature=0.0, max_tokens=15
            )
            intent = intent.strip().lower().replace(" ", "_")
            return intent if intent in self.INTENT_ROUTING else "general_faq"
        except Exception:
            return "general_faq"

    async def handle_turn(
        self,
        user_input: str,
        conversation_history: List[ConversationTurn],
        customer: CustomerContext,
        session_id: str,
        current_agent: str = "customer_service",
    ) -> AgentResponse:
        """Main orchestration — called on every conversation turn."""

        # CFPB: Always honor human-agent requests immediately
        if self.detect_escalation_request(user_input):
            return AgentResponse(
                text="I'll transfer you to a human representative right away. Please hold.",
                escalate=True,
                metadata={"escalation_reason": "customer_request"},
            )

        # Auto-escalate on very negative sentiment
        if self.analyze_sentiment(user_input) == "very_negative":
            return AgentResponse(
                text="I understand your frustration and I sincerely apologize. "
                     "Let me connect you with a senior representative immediately.",
                escalate=True,
                metadata={"escalation_reason": "negative_sentiment"},
            )

        # Route: first turn classifies intent; subsequent turns stay on current agent
        if len(conversation_history) <= 1:
            intent = await self.classify_intent(user_input)
            target_name = self.INTENT_ROUTING.get(intent, "customer_service")
        else:
            target_name = current_agent
            intent = "continuation"

        target_agent = self.agents.get(target_name, self.agents["customer_service"])
        logger.info(f"Session {session_id}: {target_name} | intent={intent}")

        try:
            response = await target_agent.handle_turn(
                user_input, conversation_history, customer, session_id
            )
            response.metadata["agent"] = target_name
            response.metadata["intent"] = intent
            return response
        except Exception as e:
            logger.error(f"Agent {target_name} failed: {e}")
            return AgentResponse(
                text="I'm experiencing a technical issue. Let me connect you with a representative.",
                escalate=True,
                metadata={"error": str(e)},
            )

    def run_uagent(self):
        """Run the Fetch.ai uAgent (separate thread/process)."""
        self.uagent.run()
