import asyncio, sys
sys.path.insert(0, '.')
from api.main_v2 import app
from httpx import AsyncClient, ASGITransport

async def test():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/v2/payments/initiate", json={"tenant_id":"test","bank_name":"Test","plan":"pilot"})
        print("STATUS:", r.status_code)
        print("BODY:", r.text)

asyncio.run(test())
