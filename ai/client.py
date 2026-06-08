from __future__ import annotations

import logging
from typing import Any

import httpx

from config import settings

logger = logging.getLogger("local_ai_assistant.ai")


class OllamaClient:
    def __init__(self, model: str = settings.ollama_model, base_url: str = str(settings.ollama_url)) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.session = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))

    async def generate(self, prompt: str, temperature: float = 0.3, max_tokens: int = 1024) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        try:
            logger.debug("Ollama request - model=%s prompt=%s", self.model, prompt[:500])
        except Exception:
            logger.debug("Ollama request - model=%s (prompt omitted)", self.model)
        try:
            response = await self.session.post(f"{self.base_url}/api/generate", json=payload)
            response.raise_for_status()
            data = response.json()
            # log response content (truncate to avoid excessive logs)
            result = data.get("response", "")
            try:
                logger.debug("Ollama response (truncated): %s", (result or "")[:1000])
            except Exception:
                logger.debug("Ollama response received")
            if not result:
                logger.warning("Ollama returned empty response for prompt")
            return result
        except httpx.HTTPError as exc:
            logger.error("Ollama HTTP error: %s", exc)
            raise
        except Exception as exc:
            logger.error("Ollama connection error: %s", exc)
            raise

    async def stream_generate(self, prompt: str, temperature: float = 0.3, max_tokens: int = 1024) -> Any:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        try:
            logger.debug("Ollama stream request - model=%s prompt=%s", self.model, prompt[:200])
        except Exception:
            logger.debug("Ollama stream request - model=%s", self.model)
        async with self.session.stream("POST", f"{self.base_url}/api/generate", json=payload) as stream:
            async for line in stream.aiter_lines():
                if not line.strip():
                    continue
                try:
                    logger.debug("Ollama stream chunk: %s", line[:500])
                except Exception:
                    pass
                yield line

    async def close(self) -> None:
        await self.session.aclose()
