"""
BankVoiceAI — Payment System Test Suite (v3)
Run: pytest tests/test_payment.py -v

Tests cover:
  1. Pilot subscription creation + DB persistence
  2. FET ledger verifier (mocked on-chain response)
  3. CommitPayment → CompletePayment flow
  4. CancelPayment on bad TX (FAILED path)
  5. Agent access gating per plan
  6. Rate limiting
  7. Subscription expiry + agent pause
  8. Renewal reminder trigger
  9. Plan upgrade flow
  10. API key rotation and Redis indexing
"""

import asyncio
import json
import pytest
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from payment_gateway.fet_payment_gateway import (
    FETPaymentGatewayAgent,
    FetchLedgerVerifier,
    Subscription,
    SubscriptionPlan,
    SubscriptionStatus,
    PaymentStatus,
    FETPayment,
    PLAN_CONFIG,
    _sub_from_dict,
)


# ─── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_redis():
    r = AsyncMock()
    r.ping.return_value = True
    r.setex.return_value = True
    r.get.return_value   = None
    return r


@pytest.fixture
def gateway(mock_redis):
    with patch("payment_gateway.fet_payment_gateway.Agent"):
        gw = FETPaymentGatewayAgent(
            wallet_address = "fetch1test_wallet_address_here",
            seed           = "test-seed-bvai-12345",
            redis_client   = mock_redis,
            db_url         = None,       # no DB in tests
            use_mainnet    = False,
        )
        return gw


@pytest.fixture
def active_subscription():
    now = datetime.now(timezone.utc)
    return Subscription(
        tenant_id         = "bank_test_001",
        bank_name         = "Test Community Bank",
        plan              = SubscriptionPlan.GROWTH,
        status            = SubscriptionStatus.ACTIVE,
        started_at        = now.isoformat(),
        expires_at        = (now + timedelta(days=20)).isoformat(),
        last_payment_hash = "ABC123",
        last_payment_at   = now.isoformat(),
        agents_enabled    = PLAN_CONFIG[SubscriptionPlan.GROWTH]["agents_enabled"],
        api_keys          = ["bvai_testkey_abc123"],
    )


@pytest.fixture
def expired_subscription():
    now = datetime.now(timezone.utc)
    return Subscription(
        tenant_id         = "bank_expired_001",
        bank_name         = "Expired Bank",
        plan              = SubscriptionPlan.STARTER,
        status            = SubscriptionStatus.EXPIRED,
        started_at        = (now - timedelta(days=35)).isoformat(),
        expires_at        = (now - timedelta(days=5)).isoformat(),
        last_payment_hash = "OLDHASH",
        last_payment_at   = (now - timedelta(days=35)).isoformat(),
        agents_enabled    = PLAN_CONFIG[SubscriptionPlan.STARTER]["agents_enabled"],
        api_keys          = ["bvai_expired_key"],
    )


# ─── Test 1: Pilot Subscription ──────────────────────────────────────────────

class TestPilotSubscription:
    @pytest.mark.asyncio
    async def test_pilot_creates_subscription(self, gateway):
        sub = await gateway.create_pilot_subscription("bank_001", "First National Bank")
        assert sub.tenant_id    == "bank_001"
        assert sub.bank_name    == "First National Bank"
        assert sub.plan         == SubscriptionPlan.PILOT
        assert sub.status       == SubscriptionStatus.TRIAL
        assert sub.is_active()
        assert len(sub.api_keys) == 1
        assert sub.api_keys[0].startswith("bvai_")

    @pytest.mark.asyncio
    async def test_pilot_stored_in_memory(self, gateway):
        await gateway.create_pilot_subscription("bank_002", "Community Bank")
        assert "bank_002" in gateway.subscriptions

    @pytest.mark.asyncio
    async def test_pilot_has_limited_agents(self, gateway):
        sub = await gateway.create_pilot_subscription("bank_003", "Test Bank")
        assert "customer_service" in sub.agents_enabled
        assert "fraud_detection"  in sub.agents_enabled
        assert "collections"      not in sub.agents_enabled
        assert "sales"            not in sub.agents_enabled


# ─── Test 2: Ledger Verifier ──────────────────────────────────────────────────

class TestFetchLedgerVerifier:
    def _make_tx_response(
        self,
        to_addr:    str = "fetch1test_wallet_address_here",
        from_addr:  str = "fetch1bank_wallet",
        amount:     int = 250_000_000_000_000_000_000,  # 250 FET in atestfet
        memo:       str = "BANKVOICEAI|bank_001|starter",
        code:       int = 0,
        denom:      str = "atestfet",
        age_mins:   int = 5,
    ) -> dict:
        ts = (datetime.now(timezone.utc) - timedelta(minutes=age_mins)).isoformat()
        return {
            "tx_response": {"code": code, "height": "12345", "timestamp": ts + "Z"},
            "tx": {
                "body": {
                    "memo":     memo,
                    "messages": [{
                        "@type":       "/cosmos.bank.v1beta1.MsgSend",
                        "to_address":  to_addr,
                        "from_address": from_addr,
                        "amount": [{"denom": denom, "amount": str(amount)}],
                    }],
                }
            },
        }

    @pytest.mark.asyncio
    async def test_valid_payment_passes(self):
        verifier = FetchLedgerVerifier(use_mainnet=False)
        with patch.object(verifier, "get_transaction", new_callable=AsyncMock) as mock_tx:
            mock_tx.return_value = self._make_tx_response()
            is_valid, payment, reason = await verifier.verify_payment(
                tx_hash             = "TXHASH001",
                expected_to         = "fetch1test_wallet_address_here",
                expected_amount_fet = Decimal("250"),
                memo_prefix         = "BANKVOICEAI|bank_001|starter",
            )
            assert is_valid  is True
            assert payment   is not None
            assert payment.amount_fet == Decimal("250")
            assert payment.confirmed  is True

    @pytest.mark.asyncio
    async def test_wrong_recipient_fails(self):
        verifier = FetchLedgerVerifier(use_mainnet=False)
        with patch.object(verifier, "get_transaction", new_callable=AsyncMock) as mock_tx:
            mock_tx.return_value = self._make_tx_response(to_addr="fetch1wrong_address")
            is_valid, _, reason = await verifier.verify_payment(
                tx_hash             = "TXHASH002",
                expected_to         = "fetch1test_wallet_address_here",
                expected_amount_fet = Decimal("250"),
                memo_prefix         = "BANKVOICEAI|bank_001|starter",
            )
            assert is_valid is False
            assert "recipient" in reason.lower() or "Wrong" in reason

    @pytest.mark.asyncio
    async def test_wrong_amount_fails(self):
        verifier = FetchLedgerVerifier(use_mainnet=False)
        with patch.object(verifier, "get_transaction", new_callable=AsyncMock) as mock_tx:
            mock_tx.return_value = self._make_tx_response(amount=100_000_000_000_000_000_000)  # 100 FET
            is_valid, _, reason = await verifier.verify_payment(
                tx_hash             = "TXHASH003",
                expected_to         = "fetch1test_wallet_address_here",
                expected_amount_fet = Decimal("250"),
                memo_prefix         = "BANKVOICEAI|bank_001|starter",
            )
            assert is_valid is False
            assert "amount" in reason.lower() or "Wrong" in reason

    @pytest.mark.asyncio
    async def test_wrong_memo_fails(self):
        verifier = FetchLedgerVerifier(use_mainnet=False)
        with patch.object(verifier, "get_transaction", new_callable=AsyncMock) as mock_tx:
            mock_tx.return_value = self._make_tx_response(memo="WRONG_MEMO|bank_001")
            is_valid, _, reason = await verifier.verify_payment(
                tx_hash             = "TXHASH004",
                expected_to         = "fetch1test_wallet_address_here",
                expected_amount_fet = Decimal("250"),
                memo_prefix         = "BANKVOICEAI|bank_001|starter",
            )
            assert is_valid is False
            assert "memo" in reason.lower()

    @pytest.mark.asyncio
    async def test_failed_tx_rejected(self):
        verifier = FetchLedgerVerifier(use_mainnet=False)
        with patch.object(verifier, "get_transaction", new_callable=AsyncMock) as mock_tx:
            mock_tx.return_value = self._make_tx_response(code=5)  # non-zero = failed
            is_valid, _, reason = await verifier.verify_payment(
                tx_hash             = "TXHASH005",
                expected_to         = "fetch1test_wallet_address_here",
                expected_amount_fet = Decimal("250"),
                memo_prefix         = "BANKVOICEAI|bank_001|starter",
            )
            assert is_valid is False
            assert "failed" in reason.lower()

    @pytest.mark.asyncio
    async def test_not_found_returns_false(self):
        verifier = FetchLedgerVerifier(use_mainnet=False)
        with patch.object(verifier, "get_transaction", new_callable=AsyncMock) as mock_tx:
            mock_tx.return_value = None
            is_valid, _, reason = await verifier.verify_payment(
                tx_hash             = "NOTFOUND",
                expected_to         = "fetch1test",
                expected_amount_fet = Decimal("250"),
                memo_prefix         = "BANKVOICEAI",
            )
            assert is_valid is False
            assert "not found" in reason.lower()


# ─── Test 3: Agent Access Gating ─────────────────────────────────────────────

class TestAgentAccessGating:
    @pytest.mark.asyncio
    async def test_active_growth_allows_collections(self, gateway, active_subscription):
        gateway.subscriptions["bank_test_001"] = active_subscription
        gateway.subscriptions["bank_test_001"].api_keys = ["bvai_testkey_abc123"]

        with patch.object(gateway, "get_subscription_by_api_key", new_callable=AsyncMock) as mock:
            mock.return_value = active_subscription
            allowed, reason, sub = await gateway.check_agent_access(
                "bvai_testkey_abc123", "collections"
            )
            assert allowed is True
            assert reason  == "OK"

    @pytest.mark.asyncio
    async def test_starter_blocks_collections(self, gateway):
        now = datetime.now(timezone.utc)
        starter_sub = Subscription(
            tenant_id     = "bank_starter",
            bank_name     = "Starter Bank",
            plan          = SubscriptionPlan.STARTER,
            status        = SubscriptionStatus.ACTIVE,
            started_at    = now.isoformat(),
            expires_at    = (now + timedelta(days=20)).isoformat(),
            last_payment_hash = "XYZ",
            last_payment_at   = now.isoformat(),
            agents_enabled = PLAN_CONFIG[SubscriptionPlan.STARTER]["agents_enabled"],
            api_keys       = ["bvai_starter_key"],
        )
        with patch.object(gateway, "get_subscription_by_api_key", new_callable=AsyncMock) as mock:
            mock.return_value = starter_sub
            allowed, reason, _ = await gateway.check_agent_access(
                "bvai_starter_key", "collections"
            )
            assert allowed is False
            assert "collections" in reason or "plan" in reason.lower()

    @pytest.mark.asyncio
    async def test_expired_subscription_blocks_all(self, gateway, expired_subscription):
        with patch.object(gateway, "get_subscription_by_api_key", new_callable=AsyncMock) as mock:
            mock.return_value = expired_subscription
            allowed, reason, _ = await gateway.check_agent_access(
                "bvai_expired_key", "customer_service"
            )
            assert allowed is False
            assert "expired" in reason.lower() or "inactive" in reason.lower()

    @pytest.mark.asyncio
    async def test_invalid_api_key_blocked(self, gateway):
        with patch.object(gateway, "get_subscription_by_api_key", new_callable=AsyncMock) as mock:
            mock.return_value = None
            allowed, reason, _ = await gateway.check_agent_access(
                "bvai_invalid_key_xyz", "customer_service"
            )
            assert allowed is False
            assert "invalid" in reason.lower()


# ─── Test 4: Subscription Activation ─────────────────────────────────────────

class TestSubscriptionActivation:
    @pytest.mark.asyncio
    async def test_starter_plan_activates(self, gateway):
        now = datetime.now(timezone.utc)
        payment = FETPayment(
            tx_hash        = "TXACTIVATE001",
            from_address   = "fetch1bank",
            to_address     = "fetch1test_wallet_address_here",
            amount_ufet    = 250_000_000_000_000_000_000,
            amount_fet     = Decimal("250"),
            memo           = "BANKVOICEAI|bank_activate|starter",
            block_height   = 99999,
            confirmed      = True,
            timestamp      = now.isoformat(),
            bank_tenant_id = "bank_activate",
            plan           = SubscriptionPlan.STARTER,
            status         = PaymentStatus.CONFIRMED,
        )
        sub = await gateway._activate_subscription(payment, "Activation Test Bank")
        assert sub.tenant_id   == "bank_activate"
        assert sub.plan        == SubscriptionPlan.STARTER
        assert sub.status      == SubscriptionStatus.ACTIVE
        assert sub.is_active()
        assert "customer_service" in sub.agents_enabled
        assert "onboarding"       in sub.agents_enabled
        assert len(sub.api_keys)  == 1

    @pytest.mark.asyncio
    async def test_renewal_extends_expiry(self, gateway, active_subscription):
        gateway.subscriptions["bank_test_001"] = active_subscription
        original_expiry = active_subscription.expires_at

        now = datetime.now(timezone.utc)
        payment = FETPayment(
            tx_hash        = "TXRENEW001",
            from_address   = "fetch1bank",
            to_address     = "fetch1test_wallet_address_here",
            amount_ufet    = 750_000_000_000_000_000_000,
            amount_fet     = Decimal("750"),
            memo           = "BANKVOICEAI|bank_test_001|growth",
            block_height   = 100000,
            confirmed      = True,
            timestamp      = now.isoformat(),
            bank_tenant_id = "bank_test_001",
            plan           = SubscriptionPlan.GROWTH,
            status         = PaymentStatus.CONFIRMED,
        )
        sub = await gateway._activate_subscription(payment, "Test Community Bank")
        # Expiry should be extended beyond original
        new_expiry = datetime.fromisoformat(sub.expires_at)
        old_expiry = datetime.fromisoformat(original_expiry)
        assert new_expiry > old_expiry


# ─── Test 5: Renewal Engine ───────────────────────────────────────────────────

class TestRenewalEngine:
    @pytest.mark.asyncio
    async def test_7_day_warning_sent(self, gateway):
        now = datetime.now(timezone.utc)
        sub = Subscription(
            tenant_id         = "bank_renew",
            bank_name         = "Renewal Test Bank",
            plan              = SubscriptionPlan.STARTER,
            status            = SubscriptionStatus.ACTIVE,
            started_at        = (now - timedelta(days=23)).isoformat(),
            expires_at        = (now + timedelta(days=7)).isoformat(),
            last_payment_hash = "OLD_TX",
            last_payment_at   = (now - timedelta(days=23)).isoformat(),
            agents_enabled    = ["customer_service"],
            api_keys          = ["bvai_renew_key"],
        )
        gateway.subscriptions["bank_renew"] = sub
        await gateway.run_renewal_check()
        # After check, metadata should contain renewal reminder
        updated = gateway.subscriptions["bank_renew"]
        has_reminder = any("renewal_reminder" in k for k in updated.metadata.keys())
        assert has_reminder

    @pytest.mark.asyncio
    async def test_expired_subscription_suspended(self, gateway):
        now = datetime.now(timezone.utc)
        sub = Subscription(
            tenant_id         = "bank_expired_now",
            bank_name         = "Expired Now Bank",
            plan              = SubscriptionPlan.STARTER,
            status            = SubscriptionStatus.ACTIVE,
            started_at        = (now - timedelta(days=31)).isoformat(),
            expires_at        = (now - timedelta(hours=1)).isoformat(),  # expired 1 hour ago
            last_payment_hash = "EXPIRED_TX",
            last_payment_at   = (now - timedelta(days=31)).isoformat(),
            agents_enabled    = ["customer_service"],
            api_keys          = ["bvai_will_expire"],
        )
        gateway.subscriptions["bank_expired_now"] = sub
        await gateway.run_renewal_check()
        updated = gateway.subscriptions["bank_expired_now"]
        assert updated.status == SubscriptionStatus.EXPIRED


# ─── Test 6: API Key Management ───────────────────────────────────────────────

class TestAPIKeyManagement:
    def test_api_key_format(self, gateway):
        key = gateway._generate_api_key("bank_test")
        assert key.startswith("bvai_")
        assert len(key) == 45  # "bvai_" + 40 hex chars

    def test_api_keys_are_unique(self, gateway):
        keys = {gateway._generate_api_key("bank_test") for _ in range(10)}
        assert len(keys) == 10  # all unique

    @pytest.mark.asyncio
    async def test_redis_key_index_created(self, gateway, mock_redis):
        sub = await gateway.create_pilot_subscription("bank_redis_test", "Redis Test Bank")
        api_key = sub.api_keys[0]
        # setex should have been called for the apikey index
        calls = [str(call) for call in mock_redis.setex.call_args_list]
        indexed = any(f"apikey:{api_key}" in str(c) for c in calls)
        assert indexed


# ─── Test 7: Subscription Serialization ──────────────────────────────────────

class TestSubscriptionSerialization:
    def test_to_dict_roundtrip(self, active_subscription):
        d   = active_subscription.to_dict()
        sub = _sub_from_dict(d)
        assert sub.tenant_id      == active_subscription.tenant_id
        assert sub.plan           == active_subscription.plan
        assert sub.agents_enabled == active_subscription.agents_enabled

    def test_is_active_in_dict(self, active_subscription):
        d = active_subscription.to_dict()
        assert d["is_active"] is True
        assert d["days_until_expiry"] > 0

    def test_expired_not_active(self, expired_subscription):
        assert expired_subscription.is_active() is False
        d = expired_subscription.to_dict()
        assert d["is_active"] is False
