import logging
import secrets
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field
from typing import Any, List, Literal

logger = logging.getLogger(__name__)

from api.security import authenticate_admin, create_access_token, get_current_user, get_machine_client
from ai.client import OllamaClient
from ai.processor import execute_conversation_turn
from ai.tools import ToolRegistry
from config import settings
from database.repository import (
    count_pending_actions,
    count_user_conversations,
    get_or_create_user,
    get_queue_statistics,
    get_system_setting,
    get_user_by_id,
    get_user_preference,
    list_diagnostic_inbound_messages,
    list_diagnostic_outbound_messages,
    list_registered_users,
    resolve_effective_model,
    resolve_machine_user,
    retry_failed_outbound_message,
    set_system_setting,
    set_user_preference,
)
from memory.store import MemoryStore, get_memory_store

api_router = APIRouter()
v1_router = APIRouter(prefix="/v1")


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


def mask_identifier(identifier: str | None) -> str:
    """Mask sensitive phone numbers, chat IDs, or external IDs for diagnostic display."""
    if not identifier:
        return ""
    if "@" in identifier:
        parts = identifier.split("@", 1)
        user_part = parts[0]
        domain = parts[1]
        if len(user_part) <= 4:
            masked_user = user_part[:1] + "***"
        else:
            masked_user = user_part[:4] + "***" + user_part[-2:]
        return f"{masked_user}@{domain}"
    if identifier.startswith("telegram:"):
        parts = identifier.split(":", 1)
        prefix, uid = parts[0], parts[1]
        if len(uid) <= 4:
            masked_uid = uid[:1] + "***"
        else:
            masked_uid = uid[:3] + "***" + uid[-2:]
        return f"{prefix}:{masked_uid}"
    if len(identifier) > 6:
        return identifier[:3] + "***" + identifier[-2:]
    return identifier[:1] + "***"


class SystemInfoModel(BaseModel):
    version: str = "0.1.0"
    host: str
    port: int
    loopback_only: bool
    remote_api_allowed: bool


class OllamaDependencyModel(BaseModel):
    reachable: bool
    installed_model_count: int
    configured_model: str
    global_default_model: str | None = None
    effective_default_model: str | None = None


class WhatsAppDependencyModel(BaseModel):
    reachable: bool
    connected: bool
    pairing_code_available: bool = False


class TelegramDependencyModel(BaseModel):
    configured: bool


class DependenciesModel(BaseModel):
    ollama: OllamaDependencyModel
    whatsapp: WhatsAppDependencyModel
    telegram: TelegramDependencyModel
    database: bool


class QueueCountModel(BaseModel):
    received: int = 0
    processing: int = 0
    response_ready: int = 0
    completed: int = 0
    failed: int = 0
    total: int = 0


class OutboundQueueCountModel(BaseModel):
    pending: int = 0
    sending: int = 0
    sent: int = 0
    failed: int = 0
    ambiguous: int = 0
    total: int = 0


class QueueStatsModel(BaseModel):
    inbound: QueueCountModel
    outbound: OutboundQueueCountModel


class AdminOverviewResponse(BaseModel):
    status: Literal["healthy", "degraded"]
    timestamp: str
    system: SystemInfoModel
    dependencies: DependenciesModel
    queues: QueueStatsModel
    pending_actions_count: int
    tools_count: int
    warnings: list[str]


class UserSummaryItem(BaseModel):
    id: int
    external_id: str
    display_name: str
    email: str | None = None
    is_admin: bool = False
    created_at: str | None = None
    conversation_count: int = 0
    preferred_model: str | None = None
    preferred_language: str | None = None
    response_style: str | None = None
    effective_model: str | None = None


class AdminUsersListResponse(BaseModel):
    users: list[UserSummaryItem]
    count: int


class ToolItemModel(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]
    permission: str
    requires_confirmation: bool
    aliases: list[str] = Field(default_factory=list)


class ToolPolicyModel(BaseModel):
    enable_tool_execution: bool
    max_rounds: int
    max_calls_per_request: int
    timeout_seconds: float
    max_output_chars: int
    allow_external_network_tools: bool
    workspace_root: str
    default_permission_policy: str


class AdminToolsResponse(BaseModel):
    tools: list[ToolItemModel]
    policy: ToolPolicyModel


class AdminIntegrationsResponse(BaseModel):
    whatsapp: dict[str, Any]
    telegram: dict[str, Any]


class DiagnosticInboundItem(BaseModel):
    id: int
    platform: str
    external_id: str
    chat_id: str
    sender: str
    sender_name: str | None = None
    message_type: str
    status: str
    attempt_count: int
    last_error: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class DiagnosticOutboundItem(BaseModel):
    id: int
    correlation_id: str
    platform: str
    chat_id: str
    status: str
    attempt_count: int
    max_attempts: int
    last_error: str | None = None
    sent_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class AdminDeliveriesResponse(BaseModel):
    inbound: list[DiagnosticInboundItem]
    outbound: list[DiagnosticOutboundItem]


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
    try:
        record = await memory.save_fact(user.id, key, value)
        return {"id": record.id, "key": record.key, "value": record.value}
    except ValueError as ve:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))



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
    target_user_id: int | None = None,
    current_user: dict = Depends(get_current_user),
    memory: MemoryStore = Depends(get_memory_store),
) -> dict:
    if not is_admin_user(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privilege required")

    if target_user_id is not None:
        user = await get_user_by_id(target_user_id)
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target user not found")
        uid = user.id
    else:
        user = await get_or_create_user(current_user["username"], current_user["username"])
        uid = user.id

    if query:
        records = await memory.search_facts(uid, query)
    else:
        records = await memory.list_facts(uid, limit=100)
    return {"memory": [{"id": record.id, "key": record.key, "value": record.value, "tags": record.tags} for record in records]}


@api_router.post("/admin/memory")
async def manage_memory(
    key: str | None = None,
    value: str | None = None,
    tags: str | None = None,
    target_user_id: int | None = None,
    action: Literal["save", "delete", "clear"] = "save",
    current_user: dict = Depends(get_current_user),
    memory: MemoryStore = Depends(get_memory_store),
) -> dict:
    if not is_admin_user(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privilege required")

    if target_user_id is not None:
        user = await get_user_by_id(target_user_id)
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target user not found")
        uid = user.id
    else:
        user = await get_or_create_user(current_user["username"], current_user["username"])
        uid = user.id

    if action == "delete":
        if not key:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Key is required for delete")
        deleted = await memory.delete_fact_by_key(uid, key)
        return {"status": "deleted" if deleted else "not_found", "key": key}
    if action == "clear":
        count = await memory.clear_all_facts(uid)
        return {"status": "cleared", "count": count}
    if action == "save":
        if not key or value is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Key and value are required for save")
        try:
            record = await memory.save_fact(uid, key, value, tags)
            return {"id": record.id, "key": record.key, "value": record.value, "tags": record.tags}
        except ValueError as ve:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))
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
        async with httpx.AsyncClient(timeout=2.0, follow_redirects=True) as client:
            response = await client.get(f"{str(settings.ollama_url).rstrip('/')}/api/tags")
            ollama_healthy = response.status_code == 200
    except Exception:
        ollama_healthy = False
    
    # Check WhatsApp bridge
    try:
        async with httpx.AsyncClient(timeout=2.0, follow_redirects=True) as client:
            response = await client.get(f"{str(settings.whatsapp_bridge_url).rstrip('/')}/health")
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


# ============================================================================
# Phase 5A: Management, Observability & Diagnostic Endpoints
# ============================================================================

@api_router.get("/admin/overview", response_model=AdminOverviewResponse)
async def get_admin_overview(
    request: Request,
    current_user: dict = Depends(get_current_user),
    client: OllamaClient = Depends(get_ollama_client),
) -> AdminOverviewResponse:
    if not is_admin_user(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privilege required")

    # Dependency: Ollama
    ollama_reachable = await client.is_ollama_available()
    installed_models = await client.get_installed_models(refresh=False)
    installed_names = [m.name for m in installed_models]
    global_def = await get_system_setting("global_default_model")
    effective_default = None
    if global_def and await client.is_model_installed(global_def, installed_names):
        effective_default = global_def
    elif await client.is_model_installed(settings.ollama_model, installed_names):
        effective_default = settings.ollama_model

    # Dependency: WhatsApp
    gateway = getattr(request.app.state, "whatsapp", None)
    whatsapp_reachable = False
    whatsapp_connected = False
    pairing_code_avail = False
    if gateway:
        whatsapp_reachable = await gateway._health_check()
        whatsapp_connected = bool(gateway.connected)
        pairing_code_avail = bool(gateway.pairing_code)

    # Dependency: Telegram
    telegram_configured = bool(settings.telegram_bot_token)

    # Queue Stats & Pending Actions
    queue_stats = await get_queue_statistics()
    pending_actions = await count_pending_actions()

    # Tools
    registry = getattr(gateway, "tool_registry", None) or ToolRegistry(settings)
    tools_count = len(registry.get_tool_definitions() if hasattr(registry, "get_tool_definitions") else registry._tools)

    # Actionable Warnings
    warnings = []
    if not ollama_reachable:
        warnings.append(f"Ollama server is unreachable at {settings.ollama_url}")
    elif effective_default is None:
        warnings.append(f"Configured model '{settings.ollama_model}' is not installed in Ollama")
    if not whatsapp_reachable:
        warnings.append(f"WhatsApp bridge is unreachable at {settings.whatsapp_bridge_url}")
    elif not whatsapp_connected:
        warnings.append("WhatsApp bridge is not paired or connected")
    if not telegram_configured:
        warnings.append("Telegram bot token is not configured")
    if not settings.is_loopback_only and not settings.allow_remote_api:
        warnings.append(f"APP_HOST '{settings.app_host}' is non-loopback without ALLOW_REMOTE_API=True")
    if queue_stats["outbound"]["ambiguous"] > 0:
        warnings.append(f"{queue_stats['outbound']['ambiguous']} ambiguous outbound delivery outcome(s) require operator review")
    if queue_stats["outbound"]["failed"] > 0:
        warnings.append(f"{queue_stats['outbound']['failed']} failed outbound message(s) in dead-letter queue")

    overall_status: Literal["healthy", "degraded"] = "healthy"
    if not ollama_reachable or not whatsapp_reachable or effective_default is None:
        overall_status = "degraded"

    now_iso = datetime.now(timezone.utc).isoformat()

    return AdminOverviewResponse(
        status=overall_status,
        timestamp=now_iso,
        system=SystemInfoModel(
            version="0.1.0",
            host=settings.app_host,
            port=settings.app_port,
            loopback_only=settings.is_loopback_only,
            remote_api_allowed=settings.allow_remote_api,
        ),
        dependencies=DependenciesModel(
            ollama=OllamaDependencyModel(
                reachable=ollama_reachable,
                installed_model_count=len(installed_models),
                configured_model=settings.ollama_model,
                global_default_model=global_def,
                effective_default_model=effective_default,
            ),
            whatsapp=WhatsAppDependencyModel(
                reachable=whatsapp_reachable,
                connected=whatsapp_connected,
                pairing_code_available=pairing_code_avail,
            ),
            telegram=TelegramDependencyModel(
                configured=telegram_configured,
            ),
            database=True,
        ),
        queues=QueueStatsModel(
            inbound=QueueCountModel(**queue_stats["inbound"]),
            outbound=OutboundQueueCountModel(**queue_stats["outbound"]),
        ),
        pending_actions_count=pending_actions,
        tools_count=tools_count,
        warnings=warnings,
    )


@api_router.get("/admin/users", response_model=AdminUsersListResponse)
async def list_admin_users(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_user: dict = Depends(get_current_user),
    client: OllamaClient = Depends(get_ollama_client),
) -> AdminUsersListResponse:
    if not is_admin_user(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privilege required")

    users_data = await list_registered_users(limit=limit, offset=offset)
    installed_models = await client.get_installed_models(refresh=False)
    installed_names = [m.name for m in installed_models]
    is_reachable = await client.is_ollama_available()

    user_items = []
    for u in users_data:
        eff_model, _, _ = await resolve_effective_model(
            user_id=u["id"],
            installed_model_names=installed_names,
            default_env_model=settings.ollama_model,
            ollama_reachable=is_reachable,
        )
        user_items.append(
            UserSummaryItem(
                id=u["id"],
                external_id=mask_identifier(u["external_id"]),
                display_name=u["display_name"],
                email=u["email"],
                is_admin=u["is_admin"],
                created_at=u["created_at"],
                conversation_count=u["conversation_count"],
                preferred_model=u["preferred_model"],
                preferred_language=u["preferred_language"],
                response_style=u["response_style"],
                effective_model=eff_model,
            )
        )
    return AdminUsersListResponse(users=user_items, count=len(user_items))


@api_router.get("/admin/users/{user_id}", response_model=UserSummaryItem)
async def get_admin_user_detail(
    user_id: int,
    current_user: dict = Depends(get_current_user),
    client: OllamaClient = Depends(get_ollama_client),
) -> UserSummaryItem:
    if not is_admin_user(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privilege required")

    user = await get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    pref = await get_user_preference(user_id)
    installed_models = await client.get_installed_models(refresh=False)
    installed_names = [m.name for m in installed_models]
    is_reachable = await client.is_ollama_available()
    eff_model, _, _ = await resolve_effective_model(
        user_id=user_id,
        installed_model_names=installed_names,
        default_env_model=settings.ollama_model,
        ollama_reachable=is_reachable,
    )
    conv_count = await count_user_conversations(user.id)
    return UserSummaryItem(
        id=user.id,
        external_id=mask_identifier(user.external_id),
        display_name=user.display_name,
        email=user.email,
        is_admin=user.is_admin,
        created_at=user.created_at.isoformat() if user.created_at else None,
        conversation_count=conv_count,
        preferred_model=pref.preferred_model if pref else None,
        preferred_language=pref.preferred_language if pref else None,
        response_style=pref.response_style if pref else None,
        effective_model=eff_model,
    )


@api_router.get("/admin/tools", response_model=AdminToolsResponse)
async def list_admin_tools(
    request: Request,
    current_user: dict = Depends(get_current_user),
) -> AdminToolsResponse:
    if not is_admin_user(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privilege required")

    gateway = getattr(request.app.state, "whatsapp", None)
    registry = getattr(gateway, "tool_registry", None) or ToolRegistry(settings)
    tools = registry.get_tool_definitions() if hasattr(registry, "get_tool_definitions") else list(registry._tools.values())

    tool_items = [
        ToolItemModel(
            name=t.name,
            description=t.description,
            parameters=t.parameters,
            permission=t.permission.value if hasattr(t.permission, "value") else str(t.permission),
            requires_confirmation=t.requires_confirmation,
            aliases=t.aliases,
        )
        for t in tools
    ]

    policy = ToolPolicyModel(
        enable_tool_execution=settings.enable_tool_execution,
        max_rounds=settings.tool_max_rounds,
        max_calls_per_request=settings.tool_max_calls_per_request,
        timeout_seconds=settings.tool_timeout_seconds,
        max_output_chars=settings.tool_max_output_chars,
        allow_external_network_tools=settings.allow_external_network_tools,
        workspace_root="storage/workspace",
        default_permission_policy=settings.tool_default_permission_policy,
    )

    return AdminToolsResponse(tools=tool_items, policy=policy)


@api_router.get("/admin/integrations", response_model=AdminIntegrationsResponse)
async def get_admin_integrations(
    request: Request,
    current_user: dict = Depends(get_current_user),
) -> AdminIntegrationsResponse:
    if not is_admin_user(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privilege required")

    gateway = getattr(request.app.state, "whatsapp", None)
    wa_reachable = False
    wa_connected = False
    wa_status_dict = {}
    if gateway:
        wa_reachable = await gateway._health_check()
        wa_connected = bool(gateway.connected)
        if wa_reachable:
            try:
                wa_status_dict = await gateway.status()
            except Exception:
                wa_status_dict = {}

    has_secret = bool(settings.whatsapp_bridge_secret and settings.whatsapp_bridge_secret.get_secret_value())

    wa_info = {
        "configured": bool(settings.whatsapp_phone),
        "configured_phone_masked": mask_identifier(settings.whatsapp_phone),
        "bridge_url": str(settings.whatsapp_bridge_url).rstrip("/"),
        "bridge_host": settings.whatsapp_bridge_host,
        "bridge_port": settings.whatsapp_bridge_port,
        "reachable": wa_reachable,
        "connected": wa_connected,
        "has_bridge_secret": has_secret,
        "pairing_code_available": bool(gateway.pairing_code if gateway else False),
        "details": wa_status_dict,
    }

    tg_token = settings.telegram_bot_token
    tg_configured = bool(tg_token)
    tg_masked = ""
    if tg_configured and ":" in tg_token:
        bot_id, secret = tg_token.split(":", 1)
        tg_masked = f"{bot_id}:***{secret[-4:] if len(secret) >= 4 else '***'}"
    elif tg_configured:
        tg_masked = "***"

    tg_info = {
        "configured": tg_configured,
        "token_masked": tg_masked,
        "status": "ready" if tg_configured else "unconfigured",
    }

    return AdminIntegrationsResponse(whatsapp=wa_info, telegram=tg_info)


@api_router.get("/admin/diagnostics/deliveries", response_model=AdminDeliveriesResponse)
async def get_deliveries_diagnostics(
    platform: str | None = Query(None, description="Filter platform: whatsapp or telegram"),
    status_filter: str | None = Query(None, alias="status", description="Filter message status"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_user: dict = Depends(get_current_user),
) -> AdminDeliveriesResponse:
    if not is_admin_user(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privilege required")

    inbound_raw = await list_diagnostic_inbound_messages(
        status=status_filter, platform=platform, limit=limit, offset=offset
    )
    outbound_raw = await list_diagnostic_outbound_messages(
        status=status_filter, platform=platform, limit=limit, offset=offset
    )

    inbound_items = [
        DiagnosticInboundItem(
            id=m["id"],
            platform=m["platform"],
            external_id=m["external_id"],
            chat_id=mask_identifier(m["chat_id"]),
            sender=mask_identifier(m["sender"]),
            sender_name=m["sender_name"],
            message_type=m["message_type"],
            status=m["status"],
            attempt_count=m["attempt_count"],
            last_error=m["last_error"],
            created_at=m["created_at"],
            updated_at=m["updated_at"],
        )
        for m in inbound_raw
    ]

    outbound_items = [
        DiagnosticOutboundItem(
            id=m["id"],
            correlation_id=m["correlation_id"],
            platform=m["platform"],
            chat_id=mask_identifier(m["chat_id"]),
            status=m["status"],
            attempt_count=m["attempt_count"],
            max_attempts=m["max_attempts"],
            last_error=m["last_error"],
            sent_at=m["sent_at"],
            created_at=m["created_at"],
            updated_at=m["updated_at"],
        )
        for m in outbound_raw
    ]

    return AdminDeliveriesResponse(inbound=inbound_items, outbound=outbound_items)


@api_router.post("/admin/diagnostics/outbound/{message_id}/retry")
async def retry_outbound_delivery(
    message_id: int,
    current_user: dict = Depends(get_current_user),
) -> dict:
    if not is_admin_user(current_user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privilege required")

    result = await retry_failed_outbound_message(message_id)
    if not result:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Outbound message {message_id} not found or not in a retryable state (FAILED or AMBIGUOUS)",
        )
    return {"status": "requeued", "message": result}


# ============================================================================
# Machine-Facing Application Chat API (Phase 5B)
# ============================================================================

class MachineChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4096, description="User message content")
    conversation_id: str | None = Field(None, max_length=128, description="Client conversation/thread identifier")
    model: str | None = Field(None, max_length=128, description="Optional installed Ollama model override")


class MachineChatResponse(BaseModel):
    status: str = "success"
    response: str
    conversation_id: str
    model_used: str | None = None
    correlation_id: str
    created_at: str


@api_router.post("/chat", response_model=MachineChatResponse, tags=["Machine Chat"])
@api_router.post("/v1/chat", response_model=MachineChatResponse, tags=["Machine Chat"])
async def machine_chat_endpoint(
    request: Request,
    payload: MachineChatRequest,
    client: dict = Depends(get_machine_client),
):
    """Authenticated machine-facing endpoint to submit a message to EDITH and receive a response.

    Supports authentication via:
    - `X-API-Key: <key>` header (configured in API_KEYS)
    - `Authorization: Bearer <jwt_token>` header

    Reuses the unified conversation pipeline:
    - User/client identity isolation (keyed to `api:<client_id>`)
    - Long-term memory context retrieval with injection guards
    - Autonomous tool calling with sandboxed execution
    - Dynamic Ollama model selection with non-silent failure guarantees
    """
    correlation_id = f"chat-{secrets.token_hex(8)}"
    client_id = client.get("client_id", "default_client")
    legacy_client_id = client.get("legacy_client_id")
    external_id = f"api:{client_id}"
    legacy_external_id = f"api:{legacy_client_id}" if legacy_client_id else None
    chat_id = payload.conversation_id or "default"

    # Resolve or seamlessly migrate legacy client identity without history/memory loss
    await resolve_machine_user(
        external_id=external_id,
        display_name=client_id,
        legacy_external_id=legacy_external_id,
    )

    # Use app.state components if initialized
    ai_client = getattr(request.app.state, "ai_client", None)
    ai_manager = getattr(request.app.state, "ai_manager", None)
    memory_store = getattr(request.app.state, "memory", None)
    tool_registry = getattr(request.app.state, "tool_registry", None)
    tool_orchestrator = getattr(request.app.state, "tool_orchestrator", None)

    try:
        turn = await execute_conversation_turn(
            external_id=external_id,
            sender_name=client_id,
            chat_id=chat_id,
            content=payload.message,
            platform="api",
            explicit_model=payload.model,
            ai_client=ai_client,
            ai_manager=ai_manager,
            memory_store=memory_store,
            tool_registry=tool_registry,
            tool_orchestrator=tool_orchestrator,
            app_settings=settings,
        )
    except Exception as exc:
        logger.exception("Chat processing error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while processing the chat turn.",
        )

    if turn.is_error:
        if "not installed" in (turn.error_detail or ""):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=turn.error_detail,
            )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=turn.error_detail or "Inference service unavailable",
        )

    return MachineChatResponse(
        status="success",
        response=turn.response,
        conversation_id=chat_id,
        model_used=turn.model_used,
        correlation_id=correlation_id,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


@v1_router.post("/chat", response_model=MachineChatResponse, tags=["Machine Chat"])
async def v1_chat_direct_endpoint(
    request: Request,
    payload: MachineChatRequest,
    client: dict = Depends(get_machine_client),
):
    """Direct alias for /v1/chat matching /api/chat."""
    return await machine_chat_endpoint(request, payload, client)
