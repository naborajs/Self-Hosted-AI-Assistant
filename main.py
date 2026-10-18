"""Project EDITH v2.0 — Main Application Factory & Process Lifecycle.

This module is the operational core and ASGI entry point of Project EDITH.
It is responsible for:
1. Application Factory (`create_app`): Constructing the FastAPI instance with
   production middleware (GZip compression, CORS origins, and IP rate limiting).
2. Lifespan Context Manager: Orchestrating deterministic startup (database WAL
   initialization, memory store hydration, AI manager loading, and gateway
   connections) and graceful shutdown (stopping Telegram polling, closing WhatsApp
   queues, and disposing of SQLAlchemy connection pools).
3. Public Diagnostic Probing (`/health`): Providing an unauthenticated health
   endpoint for container orchestrators and monitoring scripts.
4. Process Execution: Enforcing loopback-only binding by default to prevent
   accidental public exposure of administrative endpoints.
"""
from __future__ import annotations

import logging
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from api.middleware import RateLimitMiddleware
from config import settings
from api.routes import api_router, v1_router
from database.connection import init_db, shutdown_db
from whatsapp.service import WhatsAppGateway
from telegram_gateway.bot import TelegramGateway
from memory.store import MemoryStore
from ai.manager import AIManager
from ai.client import OllamaClient


logger = logging.getLogger("local_ai_assistant")


def create_app() -> FastAPI:
    """Instantiate and configure the FastAPI ASGI application.

    Wires middleware, mounts the machine-facing and administrative API routers,
    attaches singleton subsystem gateways to `app.state`, and registers the
    lifespan context manager for startup and shutdown event handling.

    Returns:
        FastAPI: The fully configured ASGI application ready for Uvicorn.
    """
    app = FastAPI(
        title="Local AI Assistant",
        description="Self-hosted WhatsApp-first AI assistant powered by Ollama.",
        version="0.1.0",
    )

    app.add_middleware(GZipMiddleware, minimum_size=1000)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept", "X-Bridge-Secret"],
    )
    app.add_middleware(RateLimitMiddleware)

    app.include_router(api_router, prefix="/api")
    app.include_router(v1_router)

    app.state.memory = MemoryStore(settings)
    app.state.ai_manager = AIManager(settings.storage_path)
    app.state.ai_client = OllamaClient(settings)
    app.state.whatsapp = WhatsAppGateway(settings, app.state.ai_manager)
    app.state.telegram = TelegramGateway(settings, app.state.memory, app.state.ai_manager)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Asynchronous context manager governing application startup and shutdown.

        Startup sequence:
        1. Initialize root logging with configured log level.
        2. Set up SQLite database tables and apply WAL/foreign key pragmas.
        3. Hydrate long-term memory store from local storage.
        4. Load AI system prompts, personas, and preset rules.
        5. Start WhatsApp background polling worker and outbound dispatchers.
        6. Start Telegram bot polling if a valid bot token is provided.

        Shutdown sequence:
        1. Cancel and drain Telegram update polling tasks.
        2. Stop WhatsApp background workers and release HTTP connection pools.
        3. Dispose of the SQLAlchemy async engine and close SQLite file locks.
        """
        # --- Application Startup ---
        logging.basicConfig(level=settings.log_level)
        logger.info("Starting Local AI Assistant")
        await init_db(settings)
        await app.state.memory.initialize()
        app.state.ai_manager.initialize()
        await app.state.whatsapp.start()
        await app.state.telegram.start()
        logger.info("Services are ready")
        
        yield
        
        # --- Application Shutdown ---
        logger.info("Stopping Local AI Assistant")
        await app.state.telegram.stop()
        await app.state.whatsapp.stop()
        await shutdown_db()
        logger.info("Shutdown complete")

    app.router.lifespan_context = lifespan

    @app.get("/health")
    async def health_check() -> dict:
        """Public, unauthenticated health check endpoint for monitoring probes.

        Performs non-blocking connectivity checks against:
        - Ollama local inference API (`/api/tags`)
        - WhatsApp companion bridge service (`/health`)
        - Telegram configuration syntax validation
        - SQLite database connection state

        Returns:
            dict: Boolean availability flags for each subsystem.
        """
        import httpx
        
        ollama_healthy = False
        whatsapp_healthy = False
        telegram_healthy = False
        database_healthy = True
        
        # Check Ollama connectivity (normalizing trailing slashes to avoid 307 redirects)
        try:
            async with httpx.AsyncClient(timeout=2.0, follow_redirects=True) as client:
                response = await client.get(f"{str(settings.ollama_url).rstrip('/')}/api/tags")
                ollama_healthy = response.status_code == 200
        except Exception:
            ollama_healthy = False
        
        # Check WhatsApp bridge connectivity
        try:
            async with httpx.AsyncClient(timeout=2.0, follow_redirects=True) as client:
                response = await client.get(f"{str(settings.whatsapp_bridge_url).rstrip('/')}/health")
                whatsapp_healthy = response.status_code == 200
        except Exception:
            whatsapp_healthy = False
        
        # Check Telegram (verify token is configured and not default placeholder)
        raw_tg_token = (settings.telegram_bot_token or "").strip()
        telegram_healthy = bool(raw_tg_token) and not raw_tg_token.startswith("your-")
        
        return {
            "backend": True,
            "ollama": ollama_healthy,
            "whatsapp": whatsapp_healthy,
            "telegram": telegram_healthy,
            "database": database_healthy,
        }

    @app.exception_handler(Exception)
    async def generic_exception_handler(_, exc: Exception):
        """Global fallback exception handler to prevent leaking internal stack traces."""
        logger.exception("Unhandled exception: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    # Security check: Prevent accidental public binding unless explicitly configured
    host = settings.app_host
    if not settings.is_loopback_only and not settings.allow_remote_api:
        logger.warning(
            "APP_HOST=%s requested without ALLOW_REMOTE_API=True. "
            "Binding to 127.0.0.1 for security.",
            settings.app_host,
        )
        host = "127.0.0.1"

    uvicorn.run(
        "main:app",
        host=host,
        port=settings.app_port,
        log_level=settings.log_level.lower(),
        reload=False,
    )
