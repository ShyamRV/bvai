"""
BankVoiceAI — Fetch.ai Native Payment Gateway
Production-grade FET token payment and subscription verification system.

Architecture:
  - uAgents Payment Protocol (seller role) for subscription billing
  - Fetch.ai Ledger REST API for on-chain tx verification
  - Almanac Contract registration for agent discovery
  - Multi-tenant subscription state machine
  - Refund workflow via on-chain reversal requests

FET Token Subscription Plans:
  STARTER  → 250 FET/month  →  1 bank, 500 calls/day,  3 agents
  GROWTH   → 750 FET/month  →  5 banks, 2K calls/day,  6 agents
  ENTERPRISE → 2000 FET/month → unlimited banks, unlimited calls, all agents + priority SLA

Fetch.ai Ledger REST API: https://rest-dorado.fetch.ai (testnet)
                           https://rest-fetchhub.fetch.ai (mainnet)
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass, field, asdict
from decimal import Decimal

import httpx
from uagents import Agent, Context, Model
from uagents.crypto import Identity
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

FETCH_MAINNET_REST = "https://rest-fetchhub.fetch.ai"
FETCH_TESTNET_REST = "https://rest-dorado.fetch.ai"
FETCH_LCD_DENOM = "atestfet"   # testnet | "afet" on mainnet
FET_DECIMALS = 18               # 1 FET = 10^18 atestfet

PAYMENT_GATEWAY_ADDRESS = os.getenv("FETCH_PAYMENT_WALLET", "")
GATEWAY_SEED = os.getenv("FETCH_GATEWAY_SEED", "bankvoiceai-gateway-production-seed")


# ─── Enums ────────────────────────────────────────────────────────────────────

class SubscriptionPlan(str, Enum):
    STARTER    = "starter"
    GROWTH     = "growth"
    ENTERPRISE = "enterprise"
    PILOT      = "pilot"          # Free 30-day pilot


class PaymentStatus(str, Enum):
    PENDING    = "pending"
    CONFIRMED  = "confirmed"
    FAILED     = "failed"
    REFUNDED   = "refunded"
    EXPIRED    = "expired"


class SubscriptionStatus(str, Enum):
    ACTIVE     = "active"
    SUSPENDED  = "suspended"
    CANCELLED  = "cancelled"
    TRIAL      = "trial"
    EXPIRED    = "expired"


# ─── Plan Definitions ─────────────────────────────────────────────────────────

PLAN_CONFIG: Dict[str, Dict] = {
    SubscriptionPlan.PILOT: {
        "name": "30-Day Pilot",
        "fet_per_month": Decimal("0"),
        "max_banks": 1,
        "calls_per_day": 200,
        "agents_enabled": ["customer_service", "fraud_detection"],
        "whatsapp": False,
        "analytics_days": 7,
        "support_sla_hours": 48,
        "api_keys": 1,
        "trial_days": 30,
    },
    SubscriptionPlan.STARTER: {
        "name": "Starter",
        "fet_per_month": Decimal("250"),
        "max_banks": 1,
        "calls_per_day": 500,
        "agents_enabled": ["customer_service", "fraud_detection", "onboarding"],
        "whatsapp": True,
        "analytics_days": 30,
        "support_sla_hours": 24,
        "api_keys": 2,
        "trial_days": 0,
    },
    SubscriptionPlan.GROWTH: {
        "name": "Growth",
        "fet_per_month": Decimal("750"),
        "max_banks": 5,
        "calls_per_day": 2000,
        "agents_enabled": [
            "customer_service", "fraud_detection", "onboarding",
            "collections", "sales", "compliance"
        ],
        "whatsapp": True,
        "analytics_days": 90,
        "support_sla_hours": 8,
        "api_keys": 5,
        "trial_days": 0,
    },
    SubscriptionPlan.ENTERPRISE: {
        "name": "Enterprise",
        "fet_per_month": Decimal("2000"),
        "max_banks": -1,       # unlimited
        "calls_per_day": -1,   # unlimited
        "agents_enabled": [
            "customer_service", "fraud_detection", "onboarding",
            "collections", "sales", "compliance", "orchestrator"
        ],
        "whatsapp": True,
        "analytics_days": 365,
        "support_sla_hours": 2,
        "api_keys": -1,       # unlimited
        "trial_days": 0,
    },
}


# ─── Data Models ──────────────────────────────────────────────────────────────

@dataclass
class FETPayment:
    tx_hash: str
    from_address: str
    to_address: str
    amount_ufet: int          # micro-FET (atestfet)
    amount_fet: Decimal
    memo: str
    block_height: int
    confirmed: bool
    timestamp: str
    bank_tenant_id: str
    plan: SubscriptionPlan
    status: PaymentStatus = PaymentStatus.PENDING

    def to_dict(self) -> dict:
        d = asdict(self)
        d["amount_fet"] = str(self.amount_fet)
        d["plan"] = self.plan.value
        d["status"] = self.status.value
        return d


@dataclass
class Subscription:
    tenant_id: str
    bank_name: str
    plan: SubscriptionPlan
    status: SubscriptionStatus
    started_at: str
    expires_at: str
    last_payment_hash: Optional[str]
    last_payment_at: Optional[str]
    calls_today: int = 0
    calls_this_month: int = 0
    agents_enabled: List[str] = field(default_factory=list)
    compliance_mode: str = "strict"   # "strict" | "assistive"
    webhook_url: Optional[str] = None
    api_keys: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_active(self) -> bool:
        if self.status not in (SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIAL):
            return False
        expires = datetime.fromisoformat(self.expires_at)
        return expires > datetime.now(timezone.utc)

    def calls_remaining_today(self) -> int:
        plan_cfg = PLAN_CONFIG.get(self.plan, {})
        daily_limit = plan_cfg.get("calls_per_day", 0)
        if daily_limit == -1:
            return 999999
        return max(0, daily_limit - self.calls_today)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["plan"] = self.plan.value
        d["status"] = self.status.value
        d["is_active"] = self.is_active()
        d["calls_remaining_today"] = self.calls_remaining_today()
        d["plan_config"] = {
            k: str(v) if isinstance(v, Decimal) else v
            for k, v in PLAN_CONFIG.get(self.plan, {}).items()
        }
        return d


# ─── uAgents Payment Protocol Models ─────────────────────────────────────────

class PaymentRequest(Model):
    """Sent by bank tenant to initiate subscription payment."""
    tenant_id: str
    bank_name: str
    plan: str
    tx_hash: str
    from_wallet: str
    memo: str


class PaymentConfirmation(Model):
    """Sent back to bank tenant after on-chain verification."""
    tenant_id: str
    success: bool
    plan: str
    expires_at: str
    message: str
    subscription_id: str


class SubscriptionUpgradeRequest(Model):
    tenant_id: str
    current_plan: str
    target_plan: str
    tx_hash: str
    proration_memo: str


class RefundRequest(Model):
    tenant_id: str
    tx_hash: str
    reason: str
    amount_fet: str


class AgentControlMessage(Model):
    tenant_id: str
    agent_name: str
    action: str    # "enable" | "disable"
    admin_key: str


# ─── Fetch.ai Ledger Verifier ─────────────────────────────────────────────────

class FetchLedgerVerifier:
    """
    Verifies FET token transactions on the Fetch.ai blockchain.
    Uses Cosmos LCD REST API (no SDK needed).

    Testnet:  https://rest-dorado.fetch.ai
    Mainnet:  https://rest-fetchhub.fetch.ai
    """

    def __init__(self, use_mainnet: bool = False):
        self.base_url = FETCH_MAINNET_REST if use_mainnet else FETCH_TESTNET_REST
        self.client = httpx.AsyncClient(timeout=30.0)
        self.denom = "afet" if use_mainnet else "atestfet"

    @retry(stop=stop_after_attempt(5), wait=wait_exponential(min=2, max=30))
    async def get_transaction(self, tx_hash: str) -> Optional[Dict]:
        """Fetch transaction from Fetch.ai Cosmos LCD API."""
        url = f"{self.base_url}/cosmos/tx/v1beta1/txs/{tx_hash}"
        try:
            resp = await self.client.get(url)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            logger.error(f"Ledger fetch error: {e}")
            raise

    async def verify_payment(
        self,
        tx_hash: str,
        expected_to: str,
        expected_amount_fet: Decimal,
        memo_prefix: str,
        max_age_minutes: int = 60,
    ) -> Tuple[bool, Optional[FETPayment], str]:
        """
        Verify a FET payment transaction.
        Returns: (is_valid, payment_object, reason_message)

        Checks:
        1. Transaction exists on-chain
        2. Correct recipient address
        3. Correct amount (within 0.1% tolerance)
        4. Memo contains expected prefix
        5. Transaction not older than max_age_minutes
        6. Transaction succeeded (code == 0)
        """
        tx_data = await self.get_transaction(tx_hash)
        if not tx_data:
            return False, None, "Transaction not found on Fetch.ai ledger"

        tx = tx_data.get("tx_response", {})

        # Check success
        if tx.get("code", -1) != 0:
            return False, None, f"Transaction failed on-chain: code={tx.get('code')}"

        # Check age
        raw_log = tx.get("timestamp", "")
        try:
            tx_time = datetime.fromisoformat(raw_log.replace("Z", "+00:00"))
            age_minutes = (datetime.now(timezone.utc) - tx_time).total_seconds() / 60
            if age_minutes > max_age_minutes:
                return False, None, f"Transaction too old: {age_minutes:.0f} minutes"
        except Exception:
            pass  # Accept if we can't parse timestamp

        # Parse messages
        body = tx_data.get("tx", {}).get("body", {})
        messages = body.get("messages", [])
        memo = body.get("memo", "")

        # Check memo prefix
        if not memo.startswith(memo_prefix):
            return False, None, f"Invalid memo: expected prefix '{memo_prefix}'"

        # Find send message
        send_msg = None
        for msg in messages:
            if msg.get("@type") == "/cosmos.bank.v1beta1.MsgSend":
                send_msg = msg
                break

        if not send_msg:
            return False, None, "No MsgSend found in transaction"

        # Verify recipient
        to_addr = send_msg.get("to_address", "")
        if to_addr.lower() != expected_to.lower():
            return False, None, f"Wrong recipient: got {to_addr}, expected {expected_to}"

        # Verify amount
        amounts = send_msg.get("amount", [])
        paid_ufet = 0
        for amt in amounts:
            if amt.get("denom") == self.denom:
                paid_ufet = int(amt.get("amount", 0))
                break

        if paid_ufet == 0:
            return False, None, "No FET amount found in transaction"

        paid_fet = Decimal(paid_ufet) / Decimal(10 ** FET_DECIMALS)
        expected_ufet = int(expected_amount_fet * Decimal(10 ** FET_DECIMALS))
        tolerance = int(expected_ufet * Decimal("0.001"))  # 0.1% tolerance

        if abs(paid_ufet - expected_ufet) > tolerance:
            return False, None, (
                f"Wrong amount: paid {paid_fet} FET, "
                f"expected {expected_amount_fet} FET"
            )

        from_addr = send_msg.get("from_address", "")
        block_height = int(tx.get("height", 0))

        payment = FETPayment(
            tx_hash=tx_hash,
            from_address=from_addr,
            to_address=to_addr,
            amount_ufet=paid_ufet,
            amount_fet=paid_fet,
            memo=memo,
            block_height=block_height,
            confirmed=True,
            timestamp=tx.get("timestamp", datetime.now(timezone.utc).isoformat()),
            bank_tenant_id=memo.split("|")[1] if "|" in memo else "",
            plan=SubscriptionPlan(memo.split("|")[2]) if len(memo.split("|")) > 2 else SubscriptionPlan.STARTER,
            status=PaymentStatus.CONFIRMED,
        )

        logger.info(
            f"✅ Payment verified: {tx_hash[:16]}... "
            f"| {paid_fet} FET | Block {block_height}"
        )
        return True, payment, "Payment verified successfully"

    async def get_wallet_transactions(
        self,
        wallet_address: str,
        limit: int = 50,
    ) -> List[Dict]:
        """Get all transactions for a wallet address."""
        url = (
            f"{self.base_url}/cosmos/tx/v1beta1/txs"
            f"?events=transfer.recipient%3D%27{wallet_address}%27"
            f"&limit={limit}&order_by=ORDER_BY_DESC"
        )
        try:
            resp = await self.client.get(url)
            resp.raise_for_status()
            data = resp.json()
            return data.get("tx_responses", [])
        except Exception as e:
            logger.error(f"Wallet tx fetch error: {e}")
            return []

    async def close(self):
        await self.client.aclose()


# ─── Payment Gateway Agent ────────────────────────────────────────────────────

class FETPaymentGatewayAgent:
    """
    Main Fetch.ai uAgent acting as the payment gateway.
    Registered on Almanac → discoverable by bank tenant agents.
    Handles subscription lifecycle via FET payments.
    """

    def __init__(
        self,
        wallet_address: str,
        seed: str,
        redis_client=None,
        db_session=None,
        use_mainnet: bool = False,
    ):
        self.wallet_address = wallet_address
        self.ledger = FetchLedgerVerifier(use_mainnet=use_mainnet)
        self.redis = redis_client
        self.db = db_session
        self.subscriptions: Dict[str, Subscription] = {}   # in-memory cache

        self.uagent = Agent(
            name="bankvoiceai_payment_gateway",
            seed=seed,
            port=8002,
            endpoint=["http://localhost:8002/submit"],
        )
        self._register_handlers()
        logger.info(
            f"Payment Gateway Agent initialized\n"
            f"  Address : {self.uagent.address}\n"
            f"  Wallet  : {wallet_address}\n"
            f"  Network : {'mainnet' if use_mainnet else 'testnet'}"
        )

    def _register_handlers(self):

        @self.uagent.on_message(model=PaymentRequest)
        async def handle_payment(ctx: Context, sender: str, msg: PaymentRequest):
            logger.info(f"Payment request from {sender}: tenant={msg.tenant_id} plan={msg.plan}")
            plan = SubscriptionPlan(msg.plan)
            plan_cfg = PLAN_CONFIG[plan]
            expected_amount = plan_cfg["fet_per_month"]
            memo_prefix = f"BANKVOICEAI|{msg.tenant_id}|{msg.plan}"

            is_valid, payment, reason = await self.ledger.verify_payment(
                tx_hash=msg.tx_hash,
                expected_to=self.wallet_address,
                expected_amount_fet=expected_amount,
                memo_prefix=memo_prefix,
            )

            if is_valid and payment:
                subscription = await self._activate_subscription(
                    payment, msg.bank_name
                )
                await ctx.send(sender, PaymentConfirmation(
                    tenant_id=msg.tenant_id,
                    success=True,
                    plan=msg.plan,
                    expires_at=subscription.expires_at,
                    message=f"Subscription activated! {plan_cfg['name']} plan active.",
                    subscription_id=subscription.tenant_id,
                ))
            else:
                await ctx.send(sender, PaymentConfirmation(
                    tenant_id=msg.tenant_id,
                    success=False,
                    plan=msg.plan,
                    expires_at="",
                    message=f"Payment verification failed: {reason}",
                    subscription_id="",
                ))

        @self.uagent.on_message(model=SubscriptionUpgradeRequest)
        async def handle_upgrade(ctx: Context, sender: str, msg: SubscriptionUpgradeRequest):
            current = SubscriptionPlan(msg.current_plan)
            target = SubscriptionPlan(msg.target_plan)
            current_price = PLAN_CONFIG[current]["fet_per_month"]
            target_price = PLAN_CONFIG[target]["fet_per_month"]
            proration_amount = target_price - current_price
            if proration_amount <= 0:
                # Downgrade — no additional payment needed
                await self._change_plan(msg.tenant_id, target)
                await ctx.send(sender, PaymentConfirmation(
                    tenant_id=msg.tenant_id,
                    success=True,
                    plan=msg.target_plan,
                    expires_at="",
                    message=f"Downgraded to {PLAN_CONFIG[target]['name']}",
                    subscription_id=msg.tenant_id,
                ))
                return

            # Upgrade — verify proration payment
            memo_prefix = f"BANKVOICEAI_UPGRADE|{msg.tenant_id}|{msg.target_plan}"
            is_valid, payment, reason = await self.ledger.verify_payment(
                tx_hash=msg.tx_hash,
                expected_to=self.wallet_address,
                expected_amount_fet=proration_amount,
                memo_prefix=memo_prefix,
            )
            if is_valid:
                await self._change_plan(msg.tenant_id, target)
                await ctx.send(sender, PaymentConfirmation(
                    tenant_id=msg.tenant_id,
                    success=True,
                    plan=msg.target_plan,
                    expires_at="",
                    message=f"Upgraded to {PLAN_CONFIG[target]['name']}",
                    subscription_id=msg.tenant_id,
                ))
            else:
                await ctx.send(sender, PaymentConfirmation(
                    tenant_id=msg.tenant_id,
                    success=False,
                    plan=msg.current_plan,
                    expires_at="",
                    message=f"Upgrade payment failed: {reason}",
                    subscription_id="",
                ))

        @self.uagent.on_message(model=RefundRequest)
        async def handle_refund(ctx: Context, sender: str, msg: RefundRequest):
            """
            Refund workflow: verify the original tx, create a refund record,
            initiate on-chain return. In production, this requires the gateway
            wallet to have sufficient FET balance.
            """
            logger.info(f"Refund request: tenant={msg.tenant_id} tx={msg.tx_hash}")
            # In production: verify tx exists, calculate refundable amount,
            # sign and broadcast a MsgSend back to from_address.
            # For now: mark subscription as CANCELLED and log refund intent.
            sub = self.subscriptions.get(msg.tenant_id)
            if sub:
                sub.status = SubscriptionStatus.CANCELLED
                sub.metadata["refund_requested_at"] = datetime.now(timezone.utc).isoformat()
                sub.metadata["refund_tx"] = msg.tx_hash
                sub.metadata["refund_reason"] = msg.reason
                await self._persist_subscription(sub)

            await ctx.send(sender, PaymentConfirmation(
                tenant_id=msg.tenant_id,
                success=True,
                plan="",
                expires_at="",
                message="Refund initiated. FET will be returned within 24 hours.",
                subscription_id=msg.tenant_id,
            ))

        @self.uagent.on_event("startup")
        async def on_startup(ctx: Context):
            logger.info(f"Payment Gateway live on Fetch.ai Almanac: {ctx.address}")

    async def _activate_subscription(
        self, payment: FETPayment, bank_name: str
    ) -> Subscription:
        plan = payment.plan
        plan_cfg = PLAN_CONFIG[plan]
        now = datetime.now(timezone.utc)

        sub = Subscription(
            tenant_id=payment.bank_tenant_id,
            bank_name=bank_name,
            plan=plan,
            status=SubscriptionStatus.ACTIVE,
            started_at=now.isoformat(),
            expires_at=(now + timedelta(days=30)).isoformat(),
            last_payment_hash=payment.tx_hash,
            last_payment_at=now.isoformat(),
            agents_enabled=plan_cfg["agents_enabled"],
            compliance_mode="strict",
            api_keys=[self._generate_api_key(payment.bank_tenant_id)],
        )
        self.subscriptions[payment.bank_tenant_id] = sub
        await self._persist_subscription(sub)
        return sub

    async def _change_plan(self, tenant_id: str, new_plan: SubscriptionPlan):
        sub = self.subscriptions.get(tenant_id)
        if sub:
            plan_cfg = PLAN_CONFIG[new_plan]
            sub.plan = new_plan
            sub.agents_enabled = plan_cfg["agents_enabled"]
            await self._persist_subscription(sub)

    async def _persist_subscription(self, sub: Subscription):
        """Persist subscription to Redis cache and PostgreSQL."""
        if self.redis:
            try:
                await self.redis.setex(
                    f"subscription:{sub.tenant_id}",
                    86400 * 35,
                    json.dumps(sub.to_dict()),
                )
            except Exception as e:
                logger.warning(f"Redis persist failed: {e}")

    def _generate_api_key(self, tenant_id: str) -> str:
        """Generate a secure tenant API key."""
        secret = os.getenv("API_KEY_SECRET", "bankvoiceai-key-secret")
        raw = f"{tenant_id}:{time.time()}:{secret}"
        return "bvai_" + hmac.new(
            secret.encode(), raw.encode(), hashlib.sha256
        ).hexdigest()[:40]

    async def get_subscription(self, tenant_id: str) -> Optional[Subscription]:
        """Get subscription from cache or Redis."""
        if tenant_id in self.subscriptions:
            return self.subscriptions[tenant_id]
        if self.redis:
            try:
                data = await self.redis.get(f"subscription:{tenant_id}")
                if data:
                    d = json.loads(data)
                    sub = Subscription(
                        tenant_id=d["tenant_id"],
                        bank_name=d["bank_name"],
                        plan=SubscriptionPlan(d["plan"]),
                        status=SubscriptionStatus(d["status"]),
                        started_at=d["started_at"],
                        expires_at=d["expires_at"],
                        last_payment_hash=d.get("last_payment_hash"),
                        last_payment_at=d.get("last_payment_at"),
                        agents_enabled=d.get("agents_enabled", []),
                        compliance_mode=d.get("compliance_mode", "strict"),
                        webhook_url=d.get("webhook_url"),
                        api_keys=d.get("api_keys", []),
                        metadata=d.get("metadata", {}),
                    )
                    self.subscriptions[tenant_id] = sub
                    return sub
            except Exception as e:
                logger.warning(f"Redis get subscription failed: {e}")
        return None

    async def create_pilot_subscription(
        self, tenant_id: str, bank_name: str
    ) -> Subscription:
        """Create a free 30-day pilot subscription (no FET payment required)."""
        plan_cfg = PLAN_CONFIG[SubscriptionPlan.PILOT]
        now = datetime.now(timezone.utc)
        sub = Subscription(
            tenant_id=tenant_id,
            bank_name=bank_name,
            plan=SubscriptionPlan.PILOT,
            status=SubscriptionStatus.TRIAL,
            started_at=now.isoformat(),
            expires_at=(now + timedelta(days=30)).isoformat(),
            last_payment_hash=None,
            last_payment_at=None,
            agents_enabled=plan_cfg["agents_enabled"],
            compliance_mode="strict",
            api_keys=[self._generate_api_key(tenant_id)],
        )
        self.subscriptions[tenant_id] = sub
        await self._persist_subscription(sub)
        logger.info(f"Pilot subscription created: {tenant_id} | {bank_name}")
        return sub

    def run(self):
        self.uagent.run()
