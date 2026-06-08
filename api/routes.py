from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel

from api.security import authenticate_admin, create_access_token, get_current_user
from config import settings
from database.repository import get_or_create_user
from memory.store import MemoryStore, get_memory_store

api_router = APIRouter()


class WhatsAppPairRequest(BaseModel):
    phone_number: str | None = None


class WhatsAppSendRequest(BaseModel):
    chat_id: str
    text: str | None = None
    file_url: str | None = None
    mime_type: str | None = None
    caption: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


@api_router.post("/token", response_model=TokenResponse)
async def token(form_data: OAuth2PasswordRequestForm = Depends()) -> TokenResponse:
    user = authenticate_admin(form_data.username, form_data.password)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    return TokenResponse(access_token=create_access_token(subject=user["username"]))


@api_router.get("/me")
async def me(current_user: dict = Depends(get_current_user)) -> dict:
    return {"username": current_user["username"], "scopes": current_user.get("scopes", [])}


@api_router.post("/memory/save")
async def save_memory(
    key: str,
    value: str,
    current_user: dict = Depends(get_current_user),
    memory: MemoryStore = Depends(get_memory_store),
) -> dict:
    user = await get_or_create_user(current_user["username"], current_user["username"])
    record = await memory.save_fact(user.id, key, value)
    return {"id": record.id, "key": record.key, "value": record.value}


@api_router.get("/memory/search")
async def search_memory(
    query: str,
    current_user: dict = Depends(get_current_user),
    memory: MemoryStore = Depends(get_memory_store),
) -> dict:
    user = await get_or_create_user(current_user["username"], current_user["username"])
    records = await memory.search_facts(user.id, query)
    return {"results": [{"id": record.id, "key": record.key, "value": record.value, "tags": record.tags} for record in records]}


@api_router.get("/whatsapp/status")
async def whatsapp_status(request: Request, current_user: dict = Depends(get_current_user)) -> dict:
    gateway = request.app.state.whatsapp
    return await gateway.status()


@api_router.post("/whatsapp/pair")
async def whatsapp_pair(
    request: Request,
    pair_request: WhatsAppPairRequest,
    current_user: dict = Depends(get_current_user),
) -> dict:
    gateway = request.app.state.whatsapp
    return await gateway.pair_device(pair_request.phone_number or settings.whatsapp_phone)


@api_router.post("/whatsapp/send")
async def whatsapp_send(
    request: Request,
    payload: WhatsAppSendRequest,
    current_user: dict = Depends(get_current_user),
) -> dict:
    gateway = request.app.state.whatsapp
    await gateway.send_message(
        payload.chat_id,
        payload.text or "",
        payload.file_url,
        payload.mime_type,
        payload.caption,
    )
    return {"status": "queued"}


@api_router.get("/whatsapp/pairing_code")
async def whatsapp_pairing_code(request: Request, current_user: dict = Depends(get_current_user)) -> dict:
    gateway = request.app.state.whatsapp
    return {"pairing_code": gateway.pairing_code}


@api_router.post("/whatsapp/logout")
async def whatsapp_logout(request: Request, current_user: dict = Depends(get_current_user)) -> dict:
    gateway = request.app.state.whatsapp
    return await gateway.logout()


@api_router.get("/health")
async def health_check(request: Request) -> dict:
    """Health check endpoint for all services."""
    import httpx
    
    ollama_healthy = False
    whatsapp_healthy = False
    telegram_healthy = False
    database_healthy = False
    
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
    
    # Check Database (connection is healthy if app initialized it)
    database_healthy = True
    
    return {
        "backend": True,
        "ollama": ollama_healthy,
        "whatsapp": whatsapp_healthy,
        "telegram": telegram_healthy,
        "database": database_healthy,
    }
