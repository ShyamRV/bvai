"""
BankVoiceAI — Fix 3 remaining test failures
Run: python patch_gateway2.py
"""
import ast, os, time

path = "payment_gateway/fet_payment_gateway.py"
with open(path, "r", encoding="utf-8") as f:
    content = f.read()

changes = []

# ── FIX 1: run_renewal_check — expired sub not being caught ───────────────
# Bug: is_active() returns False for expired subs, so the loop skips them.
# Fix: check days_until_expiry regardless of is_active status.
OLD_RENEWAL = (
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
)
NEW_RENEWAL = (
    "    async def run_renewal_check(self):\n"
    "        now = datetime.now(timezone.utc)\n"
    "        for sub in list(self.subscriptions.values()):\n"
    "            if sub.status not in (SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIAL):\n"
    "                continue\n"
    "            days_left = sub.days_until_expiry()\n"
    "            if days_left == 0:\n"
    "                sub.status = SubscriptionStatus.EXPIRED\n"
    "            elif days_left <= 7:\n"
    "                sub.metadata[f'renewal_reminder_{now.date()}'] = f'Expires in {days_left} days'\n"
)
content = content.replace(OLD_RENEWAL, NEW_RENEWAL)
changes.append("FIX 1: renewal_check now catches expired subs correctly")

# ── FIX 2: _generate_api_key — not unique because time.time() same call ──
# Bug: called 10x in same millisecond → same hash every time
# Fix: add uuid4() to make each key unique regardless of timing
OLD_KEY = (
    "    def _generate_api_key(self, tenant_id: str) -> str:\n"
    "        \"\"\"Generate a secure tenant API key.\"\"\"\n"
    "        secret = os.getenv(\"API_KEY_SECRET\", \"bankvoiceai-key-secret\")\n"
    "        raw = f\"{tenant_id}:{time.time()}:{secret}\"\n"
    "        return \"bvai_\" + hmac.new(\n"
    "            secret.encode(), raw.encode(), hashlib.sha256\n"
    "        ).hexdigest()[:40]\n"
)
NEW_KEY = (
    "    def _generate_api_key(self, tenant_id: str) -> str:\n"
    "        \"\"\"Generate a secure tenant API key.\"\"\"\n"
    "        import uuid\n"
    "        secret = os.getenv(\"API_KEY_SECRET\", \"bankvoiceai-key-secret\")\n"
    "        raw = f\"{tenant_id}:{time.time()}:{secret}:{uuid.uuid4()}\"\n"
    "        return \"bvai_\" + hmac.new(\n"
    "            secret.encode(), raw.encode(), hashlib.sha256\n"
    "        ).hexdigest()[:40]\n"
)
content = content.replace(OLD_KEY, NEW_KEY)
changes.append("FIX 2: _generate_api_key now always produces unique keys")

# ── FIX 3: create_pilot_subscription — calls old _generate_api_key ────────
# Bug: _persist is never called in original create_pilot_subscription
# so Redis setex never fires → apikey index never created
# Fix: add await self._persist(sub) call after storing in memory
OLD_PILOT = (
    "        self.subscriptions[tenant_id] = sub\n"
    "        await self._persist_subscription(sub)\n"
    "        logger.info(f\"Pilot subscription created: {tenant_id} | {bank_name}\")\n"
    "        return sub\n"
)
NEW_PILOT = (
    "        self.subscriptions[tenant_id] = sub\n"
    "        await self._persist(sub)\n"
    "        logger.info(f\"Pilot subscription created: {tenant_id} | {bank_name}\")\n"
    "        return sub\n"
)
content = content.replace(OLD_PILOT, NEW_PILOT)
changes.append("FIX 3: create_pilot_subscription now calls _persist (Redis indexed)")

# ── Also fix _activate_subscription if it still calls _persist_subscription
if "_persist_subscription" in content:
    content = content.replace("await self._persist_subscription(sub)", "await self._persist(sub)")
    changes.append("FIX 3b: replaced all _persist_subscription calls with _persist")

# ── Write & validate ──────────────────────────────────────────────────────
with open(path, "w", encoding="utf-8") as f:
    f.write(content)

try:
    ast.parse(content)
    print("File is valid Python\n")
except SyntaxError as e:
    print(f"SYNTAX ERROR: {e}")
    exit(1)

for c in changes:
    print(f"  OK  {c}")

print("\nRun: pytest tests/test_payment.py -v")
