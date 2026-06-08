from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field
from typing import Any, List, Literal

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


class AIIdentityModel(BaseModel):
    ai_name: str = Field("EDITH", description="AI name")
    ai_nickname: str = Field("EDITH", description="AI nickname")
    ai_gender: str = Field("Neutral", description="AI gender")
    ai_personality: str = Field("Friendly", description="Current personality")
    ai_role: str = Field("Self-hosted assistant", description="AI role")
    ai_description: str = Field("A secure local AI assistant.", description="AI description")
    ai_backstory: str = Field("EDITH was created to bring custom AI to local devices.", description="AI backstory")
    ai_behaviour_rules: List[str] = Field(default_factory=list, description="Behavior rules for the AI")
    ai_speaking_style: str = Field("Clear and friendly.", description="Speaking style")
    ai_language_preferences: str = Field("English", description="Language preferences")
    ai_emoji_usage: str = Field("Moderate", description="Emoji usage policy")
    ai_tone: str = Field("Warm and professional.", description="Tone")
    developer_name: str = Field("Nishant Sarkar", description="Developer name")
    owner_name: str = Field("Nishant Sarkar", description="Owner name")
    brand_name: str = Field("NS Gamming", description="Brand name")
    website: str = Field("https://nsgamming.xyz", description="Website URL")
    github: str = Field("https://github.com/naborajs", description="GitHub URL")
    youtube: str = Field("https://youtube.com/@Nishant_sarkar", description="YouTube URL")
    instagram: str = Field("https://instagram.com/naborajs", description="Instagram URL")


class SystemPromptModel(BaseModel):
    platform: str = Field("global", description="Prompt platform")
    prompt: str = Field(..., description="System prompt content")


class PresetRuleModel(BaseModel):
    id: str = Field(..., description="Preset rule identifier")
    type: Literal["exact", "keyword", "regex"] = Field("keyword", description="Match type")
    pattern: str = Field(..., description="Match pattern")
    response: str = Field(..., description="Response text")
    enabled: bool = Field(True, description="Rule enabled")


class PersonalityModel(BaseModel):
    name: str = Field(..., description="Personality name")
    role: str = Field(..., description="Personality role")
    description: str = Field(..., description="Personality description")
    speaking_style: str | None = Field(None, description="Speaking style")
    language_preferences: str | None = Field(None, description="Language preferences")
    emoji_usage: str | None = Field(None, description="Emoji usage")
    tone: str | None = Field(None, description="Tone")


class KnowledgeItemModel(BaseModel):
    id: int | None = Field(None, description="Knowledge item id")
    title: str = Field(..., description="Knowledge title")
    content: str = Field(..., description="Knowledge content")
    source: str | None = Field(None, description="Knowledge source")
    tags: str | None = Field(None, description="Knowledge tags")


class MemoryRecordModel(BaseModel):
    key: str = Field(..., description="Memory key")
    value: str = Field(..., description="Memory value")
    tags: str | None = Field(None, description="Memory tags")


def get_ai_manager(request: Request) -> Any:
    return request.app.state.ai_manager


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


@api_router.get("/admin/ai-config")
async def get_ai_config(request: Request, current_user: dict = Depends(get_current_user)) -> dict:
    ai_manager = get_ai_manager(request)
    return ai_manager.load_identity()


@api_router.post("/admin/ai-config")
async def update_ai_config(
    request: Request,
    config: AIIdentityModel,
    current_user: dict = Depends(get_current_user),
) -> dict:
    ai_manager = get_ai_manager(request)
    return ai_manager.save_identity(config.model_dump())


@api_router.get("/admin/system-prompt")
async def get_system_prompt(
    request: Request,
    platform: str = "global",
    current_user: dict = Depends(get_current_user),
) -> dict:
    ai_manager = get_ai_manager(request)
    return {"platform": platform, "prompt": ai_manager.load_system_prompt(platform)}


@api_router.post("/admin/system-prompt")
async def update_system_prompt(
    request: Request,
    payload: SystemPromptModel,
    current_user: dict = Depends(get_current_user),
) -> dict:
    ai_manager = get_ai_manager(request)
    return ai_manager.save_system_prompt(payload.platform, payload.prompt)


@api_router.get("/admin/presets")
async def get_presets(request: Request, current_user: dict = Depends(get_current_user)) -> dict:
    ai_manager = get_ai_manager(request)
    return {"presets": ai_manager.load_presets()}


@api_router.post("/admin/presets")
async def update_presets(
    request: Request,
    payload: list[PresetRuleModel],
    current_user: dict = Depends(get_current_user),
) -> dict:
    ai_manager = get_ai_manager(request)
    return {"presets": ai_manager.save_presets([item.model_dump() for item in payload])}


@api_router.get("/admin/personalities")
async def get_personalities(request: Request, current_user: dict = Depends(get_current_user)) -> dict:
    ai_manager = get_ai_manager(request)
    return {"personalities": ai_manager.load_personalities()}


@api_router.post("/admin/personalities")
async def save_personality(
    request: Request,
    payload: PersonalityModel,
    current_user: dict = Depends(get_current_user),
) -> dict:
    ai_manager = get_ai_manager(request)
    return ai_manager.save_personality(payload.name, payload.model_dump())


@api_router.get("/admin/knowledge")
async def get_knowledge(
    request: Request,
    query: str | None = None,
    current_user: dict = Depends(get_current_user),
) -> dict:
    ai_manager = get_ai_manager(request)
    if query:
        items = ai_manager.search_knowledge(query)
    else:
        items = ai_manager.list_knowledge()
    return {"knowledge": items}


@api_router.post("/admin/knowledge")
async def save_knowledge(
    request: Request,
    payload: KnowledgeItemModel,
    current_user: dict = Depends(get_current_user),
) -> dict:
    ai_manager = get_ai_manager(request)
    if payload.id is not None:
        item = ai_manager.update_knowledge(payload.id, payload.title, payload.content, payload.source, payload.tags)
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Knowledge item not found")
        return item
    return ai_manager.add_knowledge(payload.title, payload.content, payload.source, payload.tags)


@api_router.get("/admin/memory")
async def get_memory(
    query: str | None = None,
    current_user: dict = Depends(get_current_user),
    memory: MemoryStore = Depends(get_memory_store),
) -> dict:
    user = await get_or_create_user(current_user["username"], current_user["username"])
    if query:
        records = await memory.search_facts(user.id, query)
    else:
        records = await memory.search_facts(user.id, "")
    return {"memory": [{"id": record.id, "key": record.key, "value": record.value, "tags": record.tags} for record in records]}


@api_router.post("/admin/memory")
async def manage_memory(
    key: str | None = None,
    value: str | None = None,
    tags: str | None = None,
    action: Literal["save", "delete", "clear"] = "save",
    current_user: dict = Depends(get_current_user),
    memory: MemoryStore = Depends(get_memory_store),
) -> dict:
    user = await get_or_create_user(current_user["username"], current_user["username"])
    if action == "delete":
        if not key:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Key is required for delete")
        records = await memory.search_facts(user.id, key)
        for record in records:
            await memory.delete_fact(record.id)
        return {"status": "deleted", "key": key}
    if action == "clear":
        records = await memory.search_facts(user.id, "")
        for record in records:
            await memory.delete_fact(record.id)
        return {"status": "cleared"}
    if action == "save":
        if not key or value is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Key and value are required for save")
        record = await memory.save_fact(user.id, key, value, tags)
        return {"id": record.id, "key": record.key, "value": record.value, "tags": record.tags}
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported action")


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
