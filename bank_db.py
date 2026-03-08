"""
BankVoiceAI — Live Database Layer
Connects to Supabase PostgreSQL and queries real customer data.
Called by voice_gather to build CustomerContext before LLM call.

Tables created on first startup if they don't exist.
"""

import asyncio
import logging
from typing import Optional, Dict, Any
from datetime import datetime, timezone

logger = logging.getLogger("bank_db")

# asyncpg is the raw async PostgreSQL driver — already pulled in via sqlalchemy+asyncpg
try:
    import asyncpg
    HAS_ASYNCPG = True
except ImportError:
    HAS_ASYNCPG = False
    logger.warning("asyncpg not installed — DB queries disabled")


# ── SQL: create demo tables once ─────────────────────────────────────────────

SETUP_SQL = """
-- BankVoiceAI customer tables
-- Run once on Supabase. Safe to re-run (IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS bvai_customers (
    customer_id     TEXT PRIMARY KEY,
    full_name       TEXT NOT NULL,
    phone           TEXT NOT NULL,
    phone_alt       TEXT,
    account_number  TEXT NOT NULL,
    email           TEXT,
    kyc_verified    BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_customers_phone ON bvai_customers(phone);

CREATE TABLE IF NOT EXISTS bvai_accounts (
    account_id      TEXT PRIMARY KEY,
    customer_id     TEXT REFERENCES bvai_customers(customer_id),
    account_type    TEXT NOT NULL,   -- checking | savings | money_market
    balance_usd     NUMERIC(15,2) NOT NULL DEFAULT 0,
    status          TEXT DEFAULT 'active',
    opened_date     DATE,
    interest_rate   NUMERIC(5,4) DEFAULT 0
);

CREATE TABLE IF NOT EXISTS bvai_loans (
    loan_id             TEXT PRIMARY KEY,
    customer_id         TEXT REFERENCES bvai_customers(customer_id),
    loan_type           TEXT NOT NULL,   -- Auto Loan | Home Equity | Personal
    principal_usd       NUMERIC(15,2),
    balance_usd         NUMERIC(15,2),
    monthly_payment_usd NUMERIC(10,2),
    interest_rate       NUMERIC(5,4),
    due_date            TEXT,
    status              TEXT DEFAULT 'current'
);

CREATE TABLE IF NOT EXISTS bvai_transactions (
    txn_id          TEXT PRIMARY KEY,
    customer_id     TEXT REFERENCES bvai_customers(customer_id),
    amount_usd      NUMERIC(15,2),
    txn_type        TEXT,   -- credit | debit
    description     TEXT,
    merchant        TEXT,
    txn_date        DATE,
    status          TEXT DEFAULT 'posted'
);

CREATE INDEX IF NOT EXISTS idx_txn_customer ON bvai_transactions(customer_id, txn_date DESC);

-- ── Seed Shyam Reddy demo data ────────────────────────────────────────────────

INSERT INTO bvai_customers (customer_id, full_name, phone, phone_alt, account_number, email)
VALUES
  ('CUST-001', 'Shyam Reddy', '+918431439772', '+917893924878', '****4821', 'shyamji211105@gmail.com')
ON CONFLICT (customer_id) DO UPDATE
  SET full_name = EXCLUDED.full_name,
      phone     = EXCLUDED.phone,
      phone_alt = EXCLUDED.phone_alt;

INSERT INTO bvai_accounts (account_id, customer_id, account_type, balance_usd, status, opened_date)
VALUES
  ('ACC-001-CHK', 'CUST-001', 'checking', 24750.00, 'active', '2022-03-15'),
  ('ACC-001-SAV', 'CUST-001', 'savings',  58320.50, 'active', '2022-03-15')
ON CONFLICT (account_id) DO UPDATE
  SET balance_usd = EXCLUDED.balance_usd;

INSERT INTO bvai_loans (loan_id, customer_id, loan_type, balance_usd, monthly_payment_usd, interest_rate, due_date, status)
VALUES
  ('LOAN-001-AUTO', 'CUST-001', 'Auto Loan',   14200.00, 412.00,  0.0649, 'March 20, 2026', 'current'),
  ('LOAN-001-HELOC','CUST-001', 'Home Equity',  87500.00, 1140.00, 0.0725, 'March 25, 2026', 'current')
ON CONFLICT (loan_id) DO UPDATE
  SET balance_usd = EXCLUDED.balance_usd,
      monthly_payment_usd = EXCLUDED.monthly_payment_usd;

INSERT INTO bvai_transactions (txn_id, customer_id, amount_usd, txn_type, description, merchant, txn_date)
VALUES
  ('TXN-001', 'CUST-001',  8500.00, 'credit', 'Direct Deposit',     'Fetch.ai Inc',    '2026-03-07'),
  ('TXN-002', 'CUST-001',  -127.43, 'debit',  'Purchase',           'Amazon.com',      '2026-03-06'),
  ('TXN-003', 'CUST-001',  -412.00, 'debit',  'Auto Loan Payment',  'First Community', '2026-03-05'),
  ('TXN-004', 'CUST-001',    -8.75, 'debit',  'Purchase',           'Starbucks',       '2026-03-04'),
  ('TXN-005', 'CUST-001',  3200.00, 'credit', 'Wire Transfer',      'Client Payment',  '2026-03-03')
ON CONFLICT (txn_id) DO NOTHING;
"""


# ── Main DB class ─────────────────────────────────────────────────────────────

class BankDB:
    """
    Async PostgreSQL client for BankVoiceAI.
    One shared connection pool for the whole app.
    """

    def __init__(self, dsn: str):
        # Convert SQLAlchemy URL to raw asyncpg DSN
        # postgresql+asyncpg://user:pass@host/db  →  postgresql://user:pass@host/db
        self.dsn  = dsn.replace("postgresql+asyncpg://", "postgresql://")
        self.pool: Optional[asyncpg.Pool] = None

    async def connect(self):
        if not HAS_ASYNCPG:
            logger.warning("asyncpg missing — BankDB in offline mode")
            return
        try:
            self.pool = await asyncpg.create_pool(
                self.dsn,
                min_size        = 2,
                max_size        = 10,
                command_timeout = 10,
                ssl             = "require",   # Supabase requires SSL
            )
            logger.info("✅ BankDB connected to Supabase")
            await self._setup_tables()
        except Exception as e:
            logger.warning(f"BankDB connect failed (demo mode): {e}")
            self.pool = None

    async def _setup_tables(self):
        """Create tables and seed demo data on first connect."""
        if not self.pool:
            return
        try:
            async with self.pool.acquire() as conn:
                # Split on semicolons and run each statement
                for stmt in SETUP_SQL.split(";"):
                    stmt = stmt.strip()
                    if stmt:
                        await conn.execute(stmt)
            logger.info("✅ BankDB tables ready + demo data seeded")
        except Exception as e:
            logger.warning(f"BankDB setup error: {e}")

    async def close(self):
        if self.pool:
            await self.pool.close()

    async def get_customer_by_phone(self, phone: str) -> Optional[Dict]:
        """
        Full customer lookup by phone number.
        Returns dict with all account data ready for CustomerContext,
        or None if not found.
        """
        if not self.pool:
            return None

        # Normalise phone — try exact, then last-10-digits match
        phone_variants = self._phone_variants(phone)

        try:
            async with self.pool.acquire() as conn:
                # 1. Find customer
                customer = None
                for p in phone_variants:
                    row = await conn.fetchrow(
                        """SELECT * FROM bvai_customers
                           WHERE phone = $1 OR phone_alt = $1
                           LIMIT 1""",
                        p
                    )
                    if row:
                        customer = dict(row)
                        break

                if not customer:
                    logger.info(f"Phone {phone} not found in bvai_customers")
                    return None

                cid = customer["customer_id"]

                # 2. Accounts
                accounts = await conn.fetch(
                    "SELECT * FROM bvai_accounts WHERE customer_id = $1 AND status = 'active'",
                    cid
                )

                # 3. Loans
                loans = await conn.fetch(
                    "SELECT * FROM bvai_loans WHERE customer_id = $1 ORDER BY loan_type",
                    cid
                )

                # 4. Recent transactions (last 5)
                txns = await conn.fetch(
                    """SELECT * FROM bvai_transactions
                       WHERE customer_id = $1
                       ORDER BY txn_date DESC, txn_id DESC
                       LIMIT 5""",
                    cid
                )

            # Build balances
            checking = next((float(a["balance_usd"]) for a in accounts if a["account_type"] == "checking"), 0.0)
            savings  = next((float(a["balance_usd"]) for a in accounts if a["account_type"] == "savings"),  0.0)

            # Format loans
            loan_list = [
                {
                    "type":            r["loan_type"],
                    "balance":         float(r["balance_usd"]),
                    "monthly_payment": float(r["monthly_payment_usd"]),
                    "due_date":        r["due_date"],
                    "status":          r["status"],
                }
                for r in loans
            ]

            # Format transactions
            txn_list = []
            for t in txns:
                amt = float(t["amount_usd"])
                sign = "+" if amt >= 0 else ""
                txn_list.append({
                    "date":   t["txn_date"].strftime("%b %d %Y") if hasattr(t["txn_date"], "strftime") else str(t["txn_date"]),
                    "desc":   t["description"] + (f" — {t['merchant']}" if t.get("merchant") else ""),
                    "amount": f"{sign}${abs(amt):,.2f}",
                    "type":   t["txn_type"],
                })

            logger.info(f"✅ BankDB: loaded {cid} — checking=${checking:,.2f} loans={len(loan_list)} txns={len(txn_list)}")

            return {
                "customer_id":      customer["customer_id"],
                "account_number":   customer["account_number"],
                "full_name":        customer["full_name"],
                "phone":            phone,
                "email":            customer.get("email", ""),
                "authenticated":    True,
                "account_balance":  checking,
                "savings_balance":  savings,
                "loan_accounts":    loan_list,
                "recent_transactions": txn_list,
                "fraud_flags":      [],
                "consent_recorded": True,
                "call_recording_consent": True,
                "demo_mode":        True,
                "_source":          "supabase_live",
            }

        except Exception as e:
            logger.error(f"BankDB query error: {e}")
            return None

    def _phone_variants(self, phone: str):
        """Generate all phone variants to try against the DB."""
        phone = phone.replace("whatsapp:", "").strip()
        variants = {phone}
        # Remove leading +
        variants.add(phone.lstrip("+"))
        # Add +91 prefix if 10 digits
        digits = "".join(c for c in phone if c.isdigit())
        if len(digits) == 10:
            variants.add(f"+91{digits}")
            variants.add(f"91{digits}")
        # Add + prefix to 12-digit numbers
        if len(digits) == 12 and digits.startswith("91"):
            variants.add(f"+{digits}")
        return list(variants)

    async def update_balance(self, customer_id: str, account_type: str, new_balance: float):
        """Update a customer's account balance (for demo transactions)."""
        if not self.pool:
            return
        async with self.pool.acquire() as conn:
            await conn.execute(
                """UPDATE bvai_accounts SET balance_usd = $1
                   WHERE customer_id = $2 AND account_type = $3""",
                new_balance, customer_id, account_type
            )

    async def add_transaction(self, customer_id: str, amount: float,
                               txn_type: str, description: str, merchant: str = ""):
        """Log a new transaction."""
        if not self.pool:
            return
        import uuid as _uuid
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO bvai_transactions
                   (txn_id, customer_id, amount_usd, txn_type, description, merchant, txn_date)
                   VALUES ($1,$2,$3,$4,$5,$6,CURRENT_DATE)""",
                f"TXN-{_uuid.uuid4().hex[:8].upper()}",
                customer_id, amount, txn_type, description, merchant
            )

    @property
    def is_connected(self) -> bool:
        return self.pool is not None
