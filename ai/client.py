from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import time
from typing import Any

import httpx

from config import settings

logger = logging.getLogger("local_ai_assistant.ai")


class OllamaError(Exception):
    """Base exception for Ollama client errors."""
    pass


class OllamaConnectionError(OllamaError):
    """Raised when Ollama HTTP service is unreachable."""
    pass


class ModelNotFoundError(OllamaError):
    """Raised when the requested model is not installed in Ollama."""
    pass


@dataclass
class ModelInfo:
    """Structured representation of an installed Ollama model."""
    name: str
    size: int = 0
    parameter_size: str | None = None
    quantization_level: str | None = None
    family: str | None = None
    modified_at: str = ""
    digest: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "size": self.size,
            "parameter_size": self.parameter_size,
            "quantization_level": self.quantization_level,
            "family": self.family,
            "modified_at": self.modified_at,
            "digest": self.digest,
            "details": self.details,
        }


class OllamaClient:
    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        cache_ttl: float | None = None,
        max_concurrency: int | None = None,
    ) -> None:
        if model is not None and not isinstance(model, str):
            app_cfg = model
            self.settings = app_cfg
            self.model = str(getattr(app_cfg, "ollama_model", "qwen3:8b"))
            base = getattr(app_cfg, "ollama_url", base_url or "http://127.0.0.1:11434")
            self.base_url = str(base).rstrip("/")
            self.cache_ttl = float(getattr(app_cfg, "ollama_model_cache_ttl", cache_ttl or 60.0))
            concurrency = int(getattr(app_cfg, "ollama_max_concurrency", max_concurrency or 3))
        else:
            self.settings = settings
            self.model = model if model is not None else getattr(settings, "ollama_model", "qwen3:8b")
            base = base_url if base_url is not None else str(getattr(settings, "ollama_url", "http://127.0.0.1:11434"))
            self.base_url = base.rstrip("/")
            self.cache_ttl = cache_ttl if cache_ttl is not None else float(getattr(settings, "ollama_model_cache_ttl", 60.0))
            concurrency = max_concurrency if max_concurrency is not None else int(getattr(settings, "ollama_max_concurrency", 3))

        self.session = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))
        self._http_client = self.session
        self._cached_models: list[ModelInfo] | None = None
        self._cache_timestamp: float = 0.0
        self._cache_lock = asyncio.Lock()
        self.semaphore = asyncio.Semaphore(max(1, concurrency))

    async def is_ollama_available(self) -> bool:
        """Check if the Ollama HTTP endpoint is reachable without starting or loading models."""
        try:
            resp = await self.session.get(f"{self.base_url}/api/tags", timeout=httpx.Timeout(3.0, connect=2.0))
            return resp.status_code == 200
        except Exception:
            return False

    async def get_installed_models(self, refresh: bool = False) -> list[ModelInfo]:
        """Query Ollama GET /api/tags to discover currently installed models with brief caching."""
        now = time.monotonic()
        async with self._cache_lock:
            if not refresh and self._cached_models is not None and (now - self._cache_timestamp < self.cache_ttl):
                return list(self._cached_models)

            try:
                response = await self.session.get(f"{self.base_url}/api/tags", timeout=httpx.Timeout(10.0, connect=5.0))
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                logger.warning("Failed to fetch installed models from Ollama (%s): %s", self.base_url, exc)
                if self._cached_models is not None:
                    return list(self._cached_models)
                return []

            models_data = payload.get("models")
            if not isinstance(models_data, list):
                logger.warning("Ollama /api/tags response did not contain a valid models list")
                self._cached_models = []
                self._cache_timestamp = now
                return []

            parsed_models: list[ModelInfo] = []
            for item in models_data:
                if not isinstance(item, dict):
                    continue
                name = item.get("name") or item.get("model")
                if not name or not isinstance(name, str):
                    continue

                size = item.get("size") if isinstance(item.get("size"), int) else 0
                modified_at = str(item.get("modified_at")) if item.get("modified_at") else ""
                digest = str(item.get("digest")) if item.get("digest") else None

                details = item.get("details")
                param_size = None
                quant_level = None
                family = None
                details_dict = details if isinstance(details, dict) else {}
                if isinstance(details, dict):
                    param_size = str(details.get("parameter_size")) if details.get("parameter_size") else None
                    quant_level = str(details.get("quantization_level")) if details.get("quantization_level") else None
                    family = str(details.get("family")) if details.get("family") else None

                parsed_models.append(
                    ModelInfo(
                        name=name,
                        size=size,
                        parameter_size=param_size,
                        quantization_level=quant_level,
                        family=family,
                        modified_at=modified_at,
                        digest=digest,
                        details=details_dict,
                    )
                )

            self._cached_models = parsed_models
            self._cache_timestamp = now
            return list(parsed_models)

    async def is_model_installed(
        self,
        model_name: str,
        installed_names: list[str] | set[str] | None = None,
        refresh: bool = False,
    ) -> bool:
        """Check if a specific model name is present in installed models."""
        if not model_name:
            return False
        clean = model_name.strip()
        if installed_names is not None:
            names = set(installed_names)
        else:
            models = await self.get_installed_models(refresh=refresh)
            names = {m.name for m in models}
        return clean in names or f"{clean}:latest" in names or (clean.endswith(":latest") and clean[:-7] in names)

    async def get_model_info(self, model_name: str, refresh: bool = False) -> ModelInfo | None:
        """Retrieve metadata for a specific installed model."""
        if not model_name:
            return None
        models = await self.get_installed_models(refresh=refresh)
        for m in models:
            if m.name == model_name:
                return m
        return None

    async def generate(
        self,
        prompt: str,
        model: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 1024,
        validate: bool = True,
    ) -> str:
        """Execute an inference request using the explicitly selected model with bounded concurrency."""
        target_model = model or self.model
        if not target_model:
            return "Error: No model selected or configured. Please select an installed model using /model <name>."

        if validate:
            is_available = await self.is_ollama_available()
            if not is_available:
                logger.error("Ollama service unreachable at %s when generating prompt", self.base_url)
                raise OllamaConnectionError(f"Ollama service is unreachable at {self.base_url}. Please ensure Ollama is running.")

            installed = await self.is_model_installed(target_model)
            if not installed:
                logger.error("Requested model '%s' is not installed in Ollama (%s)", target_model, self.base_url)
                raise ModelNotFoundError(f"Model '{target_model}' is not installed in Ollama. Use /models to list available models.")

        payload = {
            "model": target_model,
            "prompt": prompt,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        try:
            logger.debug("Ollama request - model=%s prompt=%s", target_model, prompt[:500])
        except Exception:
            logger.debug("Ollama request - model=%s (prompt omitted)", target_model)

        async with self.semaphore:
            try:
                response = await self.session.post(f"{self.base_url}/api/generate", json=payload)
                response.raise_for_status()
                data = response.json()
                result = data.get("response", "")
                try:
                    logger.debug("Ollama response (truncated): %s", (result or "")[:1000])
                except Exception:
                    logger.debug("Ollama response received")
                if not result:
                    logger.warning("Ollama returned empty response for prompt")
                return result
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    logger.error("Model '%s' was not found on Ollama server at %s: %s", target_model, self.base_url, exc)
                    raise ModelNotFoundError(f"Model '{target_model}' is not installed in Ollama.")
                logger.error("Ollama HTTP status error: %s", exc)
                raise
            except httpx.ConnectError as exc:
                logger.error("Ollama connection error (server unreachable at %s): %s", self.base_url, exc)
                raise OllamaConnectionError(f"Ollama server unreachable at {self.base_url}")
            except httpx.HTTPError as exc:
                logger.error("Ollama HTTP error: %s", exc)
                raise
            except Exception as exc:
                logger.error("Ollama error: %s", exc)
                raise

    async def stream_generate(
        self,
        prompt: str,
        model: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 1024,
    ) -> Any:
        target_model = model or self.model
        payload = {
            "model": target_model,
            "prompt": prompt,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        try:
            logger.debug("Ollama stream request - model=%s prompt=%s", target_model, prompt[:200])
        except Exception:
            logger.debug("Ollama stream request - model=%s", target_model)
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
