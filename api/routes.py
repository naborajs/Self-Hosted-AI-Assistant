from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field
from typing import Any, List, Literal

from api.security import authenticate_admin, create_access_token, get_current_user
from ai.client import OllamaClient
from config import settings
from database.repository import (
    get_or_create_user,
    get_system_setting,
    get_user_by_id,
    get_user_preference,
    resolve_effective_model,
    set_system_setting,
    set_user_preference,
)
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
    website: str = Field("https://naborajs.me", description="Website URL")
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


class ModelInfoResponse(BaseModel):
    name: str
    size: int
    modified_at: str
    digest: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ModelsListResponse(BaseModel):
    models: list[ModelInfoResponse]
    ollama_reachable: bool
    count: int


class ModelConfigResponse(BaseModel):
    global_default_model: str | None
    fallback_default_model: str
    effective_default_model: str | None
    ollama_healthy: bool
    allow_user_model_selection: bool


class UpdateModelConfigRequest(BaseModel):
    global_default_model: str | None = None


class UserPreferenceResponse(BaseModel):
    user_id: int
    preferred_model: str | None = None
    preferred_language: str | None = None
    response_style: str | None = None
    effective_model: str | None = None
    effective_model_source: str = "none"
    effective_model_error: str | None = None


class UpdateUserPreferenceRequest(BaseModel):
    preferred_model: str | None = None
    preferred_language: str | None = None
    response_style: str | None = None


def is_admin_user(current_user: dict) -> bool:
    username = current_user.get("username", "")
    allowed = [item.strip() for item in settings.admin_list if item.strip()]
    return username in allowed


def get_ollama_client(request: Request) -> OllamaClient:
    client = getattr(request.app.state, "ai_client", None)
    if client is None:
        client = OllamaClient(settings)
        request.app.state.ai_client = client
    return client


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


@api_router.get("/whatsapp/qr")
async def whatsapp_qr(request: Request, current_user: dict = Depends(get_current_user)) -> Response:
    gateway = request.app.state.whatsapp
    qr_bytes = await gateway.get_qr()
    if not qr_bytes:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="QR code not available")
    return Response(content=qr_bytes, media_type="image/png")


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


@api_router.get("/models", response_model=ModelsListResponse)
async def list_models(
    current_user: dict = Depends(get_current_user),
    client: OllamaClient = Depends(get_ollama_client),
) -> ModelsListResponse:
    if not is_admin_user(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privilege required")
    reachable = await client.is_ollama_available()
    models = await client.get_installed_models(refresh=False)
    return ModelsListResponse(
        models=[
            ModelInfoResponse(
                name=m.name,
                size=m.size,
                modified_at=m.modified_at,
                digest=m.digest,
                details=m.details,
            )
            for m in models
        ],
        ollama_reachable=reachable,
        count=len(models),
    )


@api_router.post("/models/refresh", response_model=ModelsListResponse)
async def refresh_models(
    current_user: dict = Depends(get_current_user),
    client: OllamaClient = Depends(get_ollama_client),
) -> ModelsListResponse:
    if not is_admin_user(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privilege required")
    reachable = await client.is_ollama_available()
    models = await client.get_installed_models(refresh=True)
    return ModelsListResponse(
        models=[
            ModelInfoResponse(
                name=m.name,
                size=m.size,
                modified_at=m.modified_at,
                digest=m.digest,
                details=m.details,
            )
            for m in models
        ],
        ollama_reachable=reachable,
        count=len(models),
    )


@api_router.get("/models/config", response_model=ModelConfigResponse)
async def get_model_config(
    current_user: dict = Depends(get_current_user),
    client: OllamaClient = Depends(get_ollama_client),
) -> ModelConfigResponse:
    if not is_admin_user(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privilege required")
    global_def = await get_system_setting("global_default_model")
    reachable = await client.is_ollama_available()
    installed_models = await client.get_installed_models(refresh=False)
    installed_names = [m.name for m in installed_models]

    effective = None
    if global_def and await client.is_model_installed(global_def, installed_names):
        effective = global_def
    elif await client.is_model_installed(settings.ollama_model, installed_names):
        effective = settings.ollama_model

    return ModelConfigResponse(
        global_default_model=global_def,
        fallback_default_model=settings.ollama_model,
        effective_default_model=effective,
        ollama_healthy=reachable,
        allow_user_model_selection=settings.allow_user_model_selection,
    )


@api_router.post("/models/config")
async def update_model_config(
    payload: UpdateModelConfigRequest,
    current_user: dict = Depends(get_current_user),
    client: OllamaClient = Depends(get_ollama_client),
) -> dict:
    if not is_admin_user(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privilege required")
    if payload.global_default_model:
        requested = payload.global_default_model.strip()
        installed_models = await client.get_installed_models(refresh=True)
        installed_names = [m.name for m in installed_models]
        if not await client.is_model_installed(requested, installed_names):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Model '{requested}' is not installed in Ollama. Available: {', '.join(installed_names) or 'none'}",
            )
        await set_system_setting("global_default_model", requested)
        return {"status": "updated", "global_default_model": requested}
    else:
        await set_system_setting("global_default_model", "")
        return {"status": "cleared", "global_default_model": None}


@api_router.get("/users/{user_id}/preferences", response_model=UserPreferenceResponse)
async def get_user_preferences_endpoint(
    user_id: int,
    current_user: dict = Depends(get_current_user),
    client: OllamaClient = Depends(get_ollama_client),
) -> UserPreferenceResponse:
    user = await get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if not is_admin_user(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required to access user preferences",
        )

    pref = await get_user_preference(user_id)
    is_reachable = await client.is_ollama_available()
    installed_models = await client.get_installed_models(refresh=False)
    installed_names = [m.name for m in installed_models]
    eff_model, source, error = await resolve_effective_model(
        user_id=user_id,
        installed_model_names=installed_names,
        default_env_model=settings.ollama_model,
        ollama_reachable=is_reachable,
    )
    return UserPreferenceResponse(
        user_id=user_id,
        preferred_model=pref.preferred_model if pref else None,
        preferred_language=pref.preferred_language if pref else None,
        response_style=pref.response_style if pref else None,
        effective_model=eff_model,
        effective_model_source=source,
        effective_model_error=error,
    )


@api_router.put("/users/{user_id}/preferences", response_model=UserPreferenceResponse)
async def update_user_preferences_endpoint(
    user_id: int,
    payload: UpdateUserPreferenceRequest,
    current_user: dict = Depends(get_current_user),
    client: OllamaClient = Depends(get_ollama_client),
) -> UserPreferenceResponse:
    user = await get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if not is_admin_user(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required to modify user preferences",
        )

    # Validate response_style
    clear_style = False
    valid_style = None
    if payload.response_style is not None:
        style_clean = payload.response_style.strip().lower()
        if style_clean in ("", "null", "none"):
            clear_style = True
        elif style_clean in ("concise", "detailed"):
            valid_style = style_clean
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid response_style. Must be 'concise', 'detailed', or null/empty.",
            )

    # Validate preferred_model
    clear_model = False
    valid_model = None
    if payload.preferred_model is not None:
        model_clean = payload.preferred_model.strip()
        if model_clean in ("", "null", "none"):
            clear_model = True
        else:
            installed_models = await client.get_installed_models(refresh=True)
            installed_names = [m.name for m in installed_models]
            if not await client.is_model_installed(model_clean, installed_names):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Model '{model_clean}' is not installed in Ollama. Available: {', '.join(installed_names) or 'none'}",
                )
            valid_model = model_clean

    # Validate preferred_language
    clear_lang = False
    valid_lang = None
    if payload.preferred_language is not None:
        lang_clean = payload.preferred_language.strip()
        if lang_clean in ("", "null", "none"):
            clear_lang = True
        else:
            valid_lang = lang_clean

    pref = await set_user_preference(
        user_id=user_id,
        preferred_model=valid_model,
        clear_model=clear_model,
        preferred_language=valid_lang,
        clear_language=clear_lang,
        response_style=valid_style,
        clear_style=clear_style,
    )

    installed_models = await client.get_installed_models(refresh=False)
    installed_names = [m.name for m in installed_models]
    eff_model, source, error = await resolve_effective_model(
        user_id=user_id,
        installed_model_names=installed_names,
        default_env_model=settings.ollama_model,
    )
    return UserPreferenceResponse(
        user_id=user_id,
        preferred_model=pref.preferred_model,
        preferred_language=pref.preferred_language,
        response_style=pref.response_style,
        effective_model=eff_model,
        effective_model_source=source,
        effective_model_error=error,
    )


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
