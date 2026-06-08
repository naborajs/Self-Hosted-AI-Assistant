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
from api.routes import api_router
from database.connection import init_db, shutdown_db
from whatsapp.service import WhatsAppGateway
from telegram_gateway.bot import TelegramGateway
from memory.store import MemoryStore
from ai.manager import AIManager


logger = logging.getLogger("local_ai_assistant")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Local AI Assistant",
        description="Self-hosted WhatsApp-first AI assistant powered by Ollama.",
        version="0.1.0",
    )

    app.add_middleware(GZipMiddleware, minimum_size=1000)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RateLimitMiddleware)

    app.include_router(api_router, prefix="/api")

    app.state.memory = MemoryStore(settings)
    app.state.ai_manager = AIManager(settings.storage_path)
    app.state.whatsapp = WhatsAppGateway(settings, app.state.ai_manager)
    app.state.telegram = TelegramGateway(settings, app.state.memory, app.state.ai_manager)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup
        logging.basicConfig(level=settings.log_level)
        logger.info("Starting Local AI Assistant")
        await init_db(settings)
        await app.state.memory.initialize()
        app.state.ai_manager.initialize()
        await app.state.whatsapp.start()
        await app.state.telegram.start()
        logger.info("Services are ready")
        
        yield
        
        # Shutdown
        logger.info("Stopping Local AI Assistant")
        await app.state.telegram.stop()
        await app.state.whatsapp.stop()
        await shutdown_db()
        logger.info("Shutdown complete")

    app.router.lifespan_context = lifespan

    @app.get("/health")
    async def health_check() -> dict:
        """Public health check endpoint."""
        import httpx
        
        ollama_healthy = False
        whatsapp_healthy = False
        telegram_healthy = False
        database_healthy = True
        
        # Check Ollama
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(f"{settings.ollama_url}/api/tags")
                ollama_healthy = response.status_code == 200
        except Exception:
            ollama_healthy = False
        
        # Check WhatsApp bridge
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(f"{settings.whatsapp_bridge_url}/health")
                whatsapp_healthy = response.status_code == 200
        except Exception:
            whatsapp_healthy = False
        
        # Check Telegram (just verify token is configured)
        telegram_healthy = bool(settings.telegram_bot_token)
        
        return {
            "backend": True,
            "ollama": ollama_healthy,
            "whatsapp": whatsapp_healthy,
            "telegram": telegram_healthy,
            "database": database_healthy,
        }

    @app.exception_handler(Exception)
    async def generic_exception_handler(_, exc: Exception):
        logger.exception("Unhandled exception: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.app_host,
        port=settings.app_port,
        log_level=settings.log_level.lower(),
        reload=False,
    )
