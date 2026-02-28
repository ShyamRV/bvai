"""
BankVoiceAI — ASI:ONE Service
Fetch.ai's LLM engine. Free tier: 100K tokens/day
Sign up: https://asi1.ai
"""
import logging
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)


class ASIOneService:
    """Thin wrapper around ASI:ONE (Fetch.ai's LLM API)."""

    BASE_URL = "https://api.asi1.ai/v1"

    def __init__(self, api_key: str, model: str = "asi1-mini"):
        self.api_key = api_key
        self.model = model
        self.client = httpx.AsyncClient(timeout=30.0)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def complete(
        self,
        messages: list,
        system_prompt: str = "",
        temperature: float = 0.3,
        max_tokens: int = 512,
    ) -> str:
        full_messages = []
        if system_prompt:
            full_messages.append({"role": "system", "content": system_prompt})
        full_messages.extend(messages)

        response = await self.client.post(
            f"{self.BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "messages": full_messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": False,
            },
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

    async def close(self):
        await self.client.aclose()
