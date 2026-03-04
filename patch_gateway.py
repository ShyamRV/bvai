"""
BankVoiceAI — Gateway Patcher
Run from your bankvoiceai/ folder:
    python patch_gateway.py
"""
import ast
import os

path = "payment_gateway/fet_payment_gateway.py"

if not os.path.exists(path):
    print("ERROR: file not found. Run from bankvoiceai/ folder.")
    exit(1)

with open(path, "r", encoding="utf-8") as f:
    content = f.read()

# Validate it's clean Python first
try:
    ast.parse(content)
    print("File is valid Python — starting patches...")
except SyntaxError as e:
    print(f"ERROR: File has syntax errors before we start: {e}")
    print("Restoring from backup...")
    backup = path.replace(".py", "_OLD.py")
    if os.path.exists(backup):
        with open(backup, "r", encoding="utf-8") as f:
            content = f.read()
        print("Backup restored. Continuing with patches...")
    else:
        print("No backup found. Please re-download the original file.")
        exit(1)

changes = []

# ── FIX 1: add db_url to __init__ ─────────────────────────────────────────
OLD1 = (
    "    def __init__(\n"
    "        self,\n"
    "        wallet_address: str,\n"
    "        seed: str,\n"
    "        redis_client=None,\n"
    "        db_session=None,\n"
    "        use_mainnet: bool = False,\n"
    "    ):"
)
NEW1 = (
    "    def __init__(\n"
    "        self,\n"
    "        wallet_address: str,\n"
    "        seed: str,\n"
    "        redis_client=None,\n"
    "        db_url=None,\n"
    "        db_session=None,\n"
    "        use_mainnet: bool = False,\n"
    "    ):"
)
if "db_url=None" not in content:
    content = content.replace(OLD1, NEW1)
    changes.append("FIX 1: added db_url param to __init__")
else:
    changes.append("FIX 1: db_url already present, skipped")

# ── FIX 2: add days_until_expiry to Subscription ──────────────────────────
DAYS_METHOD = (
    "\n"
    "    def days_until_expiry(self) -> int:\n"
    "        expires = datetime.fromisoformat(self.expires_at)\n"
    "        if expires.tzinfo is None:\n"
    "            expires = expires.replace(tzinfo=timezone.utc)\n"
    "        return max(0, (expires - datetime.now(timezone.utc)).days)\n"
    "\n"
)
if "def days_until_expiry" not in content:
    content = content.replace(
        "    def to_dict(self) -> dict:",
        DAYS_METHOD + "    def to_dict(self) -> dict:"
    )
    changes.append("FIX 2: added days_until_expiry method")
else:
    changes.append("FIX 2: days_until_expiry already present, skipped")

# ── FIX 3: add days_until_expiry to to_dict output ────────────────────────
OLD3 = '        d["is_active"] = self.is_active()'
NEW3 = (
    '        d["is_active"] = self.is_active()\n'
    '        d["days_until_expiry"] = self.days_until_expiry()\n'
    '        d["calls_remaining_today"] = self.calls_remaining_today()'
)
if '"days_until_expiry"' not in content:
    content = content.replace(OLD3, NEW3)
    changes.append("FIX 3: added days_until_expiry to to_dict")
else:
    changes.append("FIX 3: days_until_expiry in to_dict already present, skipped")

# ── FIX 4: add check_agent_access + other missing methods ─────────────────
NEW_METHODS = (
    "\n"
    "    async def check_agent_access(self, api_key, agent_name):\n"
    "        sub = await self.get_subscription_by_api_key(api_key)\n"
    "        if not sub:\n"
    "            return False, 'Invalid API key', None\n"
    "        if not sub.is_active():\n"
    "            return False, 'Subscription expired or inactive. Renew to continue.', sub\n"
    "        if agent_name not in sub.agents_enabled:\n"
    "            return False, f\"Agent '{agent_name}' not in your plan. Upgrade to access.\", sub\n"
    "        if sub.calls_remaining_today() <= 0:\n"
    "            return False, 'Daily call limit reached.', sub\n"
    "        return True, 'OK', sub\n"
    "\n"
    "    async def get_subscription_by_api_key(self, api_key):\n"
    "        for sub in self.subscriptions.values():\n"
    "            if api_key in sub.api_keys:\n"
    "                return sub\n"
    "        return None\n"
    "\n"
    "    async def run_renewal_check(self):\n"
    "        now = datetime.now(timezone.utc)\n"
    "        for sub in list(self.subscriptions.values()):\n"
    "            if not sub.is_active():\n"
    "                continue\n"
    "            days_left = sub.days_until_expiry()\n"
    "            if days_left == 0:\n"
    "                sub.status = SubscriptionStatus.EXPIRED\n"
    "            elif days_left <= 7:\n"
    "                sub.metadata[f'renewal_reminder_{now.date()}'] = f'Expires in {days_left} days'\n"
    "\n"
    "    async def _persist(self, sub):\n"
    "        if self.redis:\n"
    "            try:\n"
    "                import json\n"
    "                data = {\n"
    "                    'tenant_id': sub.tenant_id, 'bank_name': sub.bank_name,\n"
    "                    'plan': sub.plan.value, 'status': sub.status.value,\n"
    "                    'started_at': sub.started_at, 'expires_at': sub.expires_at,\n"
    "                    'last_payment_hash': sub.last_payment_hash,\n"
    "                    'last_payment_at': sub.last_payment_at,\n"
    "                    'agents_enabled': sub.agents_enabled,\n"
    "                    'compliance_mode': sub.compliance_mode,\n"
    "                    'webhook_url': sub.webhook_url,\n"
    "                    'api_keys': sub.api_keys,\n"
    "                    'calls_today': sub.calls_today,\n"
    "                    'calls_this_month': sub.calls_this_month,\n"
    "                    'metadata': sub.metadata,\n"
    "                }\n"
    "                await self.redis.setex(\n"
    "                    f'subscription:{sub.tenant_id}', 86400 * 35, json.dumps(data)\n"
    "                )\n"
    "                for key in sub.api_keys:\n"
    "                    await self.redis.setex(f'apikey:{key}', 86400 * 35, sub.tenant_id)\n"
    "            except Exception:\n"
    "                pass\n"
    "\n"
    "    async def increment_call_count(self, tenant_id):\n"
    "        sub = self.subscriptions.get(tenant_id)\n"
    "        if sub:\n"
    "            sub.calls_today += 1\n"
    "            sub.calls_this_month += 1\n"
    "\n"
)

if "def check_agent_access" not in content:
    if "    def run(self):" in content:
        content = content.replace("    def run(self):", NEW_METHODS + "    def run(self):")
        changes.append("FIX 4: added check_agent_access + other methods")
    else:
        content = content.rstrip() + "\n" + NEW_METHODS
        changes.append("FIX 4: added methods at end of class")
else:
    changes.append("FIX 4: check_agent_access already present, skipped")

# ── FIX 5: add _sub_from_dict at module level ─────────────────────────────
SUB_FROM_DICT = (
    "\n\n"
    "def _sub_from_dict(d: dict):\n"
    "    return Subscription(\n"
    "        tenant_id=d['tenant_id'],\n"
    "        bank_name=d['bank_name'],\n"
    "        plan=SubscriptionPlan(d['plan']),\n"
    "        status=SubscriptionStatus(d['status']),\n"
    "        started_at=d['started_at'],\n"
    "        expires_at=d['expires_at'],\n"
    "        last_payment_hash=d.get('last_payment_hash'),\n"
    "        last_payment_at=d.get('last_payment_at'),\n"
    "        agents_enabled=d.get('agents_enabled', []),\n"
    "        compliance_mode=d.get('compliance_mode', 'strict'),\n"
    "        webhook_url=d.get('webhook_url'),\n"
    "        api_keys=d.get('api_keys', []),\n"
    "        calls_today=d.get('calls_today', 0),\n"
    "        calls_this_month=d.get('calls_this_month', 0),\n"
    "        metadata=d.get('metadata', {}),\n"
    "    )\n"
)

if "_sub_from_dict" not in content:
    content = content.rstrip() + SUB_FROM_DICT
    changes.append("FIX 5: added _sub_from_dict")
else:
    changes.append("FIX 5: _sub_from_dict already present, skipped")

# ── Write file ────────────────────────────────────────────────────────────
with open(path, "w", encoding="utf-8") as f:
    f.write(content)

# ── Final validation ──────────────────────────────────────────────────────
try:
    ast.parse(content)
    print("\nRESULT: File is valid Python")
except SyntaxError as e:
    print(f"\nERROR: Syntax error after patching: {e}")
    exit(1)

print("\nChanges applied:")
for c in changes:
    print(f"  OK  {c}")

print("\nVerifying key symbols exist:")
for symbol in ["_sub_from_dict", "db_url", "days_until_expiry", "check_agent_access", "run_renewal_check", "_persist"]:
    found = symbol in content
    print(f"  {'OK' if found else 'MISSING'}  {symbol}")

print("\nRun: pytest tests/test_payment.py -v")