"""
BankVoiceAI — Test Suite
Run: pytest tests/ -v
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from agents.base_agent import CustomerContext, ConversationTurn, AgentResponse
from agents.customer_service import CustomerServiceAgent
from agents.collections import CollectionsAgent
from agents.fraud_detection import FraudDetectionAgent
from agents.orchestrator import OrchestratorAgent


TEST_CONFIG = {
    "asi_one_api_key": "test-key",
    "asi_one_api_url": "https://api.asi1.ai/v1",
    "asi_one_model": "asi1-mini",
    "bank_name": "Test Bank",
    "fetch_agent_seed": "test-seed-12345",
    "fetch_agent_port": 8099,
    "demo_mode": True,
}

MOCK_LLM_RESPONSE = "I can help you with that. Your account balance is $4,250.75. Is there anything else?"


@pytest.fixture
def customer():
    return CustomerContext(
        full_name="Jane Test",
        authenticated=True,
        account_balance=4250.75,
        demo_mode=True,
    )


@pytest.fixture
def history():
    return []


# ── Customer Service Agent ──────────────────────────────────────────────────

class TestCustomerServiceAgent:
    @pytest.mark.asyncio
    async def test_escalation_request_detected(self, customer, history):
        agent = CustomerServiceAgent(TEST_CONFIG)
        response = await agent.handle_turn(
            "I want to speak to a human agent",
            history, customer, "test-001"
        )
        assert response.escalate is True

    @pytest.mark.asyncio
    async def test_very_negative_sentiment_escalates(self, customer, history):
        agent = CustomerServiceAgent(TEST_CONFIG)
        response = await agent.handle_turn(
            "This is fraud and incompetent service!",
            history, customer, "test-002"
        )
        assert response.escalate is True

    @pytest.mark.asyncio
    async def test_normal_query_calls_llm(self, customer, history):
        agent = CustomerServiceAgent(TEST_CONFIG)
        with patch.object(agent, "call_llm_with_fallback", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = MOCK_LLM_RESPONSE
            response = await agent.handle_turn(
                "What is my account balance?",
                history, customer, "test-003"
            )
            assert response.text == MOCK_LLM_RESPONSE
            assert response.escalate is False
            mock_llm.assert_called_once()


# ── Collections Agent ───────────────────────────────────────────────────────

class TestCollectionsAgent:
    @pytest.mark.asyncio
    async def test_cease_and_desist(self, customer, history):
        agent = CollectionsAgent(TEST_CONFIG)
        response = await agent.handle_turn(
            "Stop calling me, do not contact me again",
            history, customer, "test-004"
        )
        assert response.end_call is True
        assert response.action == "log_cease_and_desist"

    @pytest.mark.asyncio
    async def test_debt_dispute(self, customer, history):
        agent = CollectionsAgent(TEST_CONFIG)
        response = await agent.handle_turn(
            "I dispute this debt, I don't owe this",
            history, customer, "test-005"
        )
        assert response.escalate is True
        assert response.action == "log_debt_dispute"

    def test_mini_miranda_contains_required_text(self):
        agent = CollectionsAgent(TEST_CONFIG)
        miranda = agent.get_mini_miranda()
        assert "attempt to collect a debt" in miranda.lower()
        assert "debt collector" in miranda.lower()


# ── Fraud Detection Agent ───────────────────────────────────────────────────

class TestFraudDetectionAgent:
    @pytest.mark.asyncio
    async def test_lost_card_triggers_block(self, customer, history):
        agent = FraudDetectionAgent(TEST_CONFIG)
        response = await agent.handle_turn(
            "My card was stolen",
            history, customer, "test-006"
        )
        assert response.action == "block_card"

    @pytest.mark.asyncio
    async def test_unauthorized_charges_escalate(self, customer, history):
        agent = FraudDetectionAgent(TEST_CONFIG)
        response = await agent.handle_turn(
            "There are unauthorized charges on my account",
            history, customer, "test-007"
        )
        assert response.escalate is True
        assert response.action == "flag_fraud"


# ── Orchestrator ─────────────────────────────────────────────────────────────

class TestOrchestratorAgent:
    @pytest.mark.asyncio
    async def test_immediate_escalation_on_request(self, customer, history):
        orch = OrchestratorAgent(TEST_CONFIG)
        response = await orch.handle_turn(
            "I need a human agent right now",
            history, customer, "test-orch-001"
        )
        assert response.escalate is True

    @pytest.mark.asyncio
    async def test_routes_to_fraud_on_lost_card(self, customer, history):
        orch = OrchestratorAgent(TEST_CONFIG)
        with patch.object(orch, "classify_intent", new_callable=AsyncMock) as mock_intent:
            mock_intent.return_value = "lost_card"
            with patch.object(
                orch.agents["fraud_detection"], "handle_turn", new_callable=AsyncMock
            ) as mock_fraud:
                mock_fraud.return_value = AgentResponse(
                    text="Blocking your card now.",
                    action="block_card",
                    metadata={"agent": "fraud_detection"},
                )
                response = await orch.handle_turn(
                    "I lost my card",
                    history, customer, "test-orch-002"
                )
                mock_fraud.assert_called_once()
                assert response.action == "block_card"

    @pytest.mark.asyncio
    async def test_classify_intent_returns_valid(self, customer, history):
        orch = OrchestratorAgent(TEST_CONFIG)
        with patch.object(orch, "call_asi_one", new_callable=AsyncMock) as mock_asi:
            mock_asi.return_value = "balance_inquiry"
            intent = await orch.classify_intent("What is my balance?")
            assert intent in orch.INTENT_ROUTING.keys()
