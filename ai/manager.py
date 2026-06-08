from __future__ import annotations

import csv
import json
import logging
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from PyPDF2 import PdfReader
except ImportError:  # pragma: no cover
    PdfReader = None

logger = logging.getLogger("local_ai_assistant.ai_manager")

DEFAULT_IDENTITY = {
    "ai_name": "EDITH",
    "ai_nickname": "EDITH",
    "ai_gender": "Neutral",
    "ai_personality": "Friendly",
    "ai_role": "Self-hosted assistant",
    "ai_description": "A secure, privacy-first local AI assistant for WhatsApp, Telegram, and Ollama.",
    "ai_backstory": "EDITH was created to bring a customizable AI experience to personal spaces without cloud dependencies.",
    "ai_behaviour_rules": [
        "Be respectful and polite.",
        "Protect user privacy.",
        "Do not reveal secrets or sensitive information.",
        "Mention NS Gamming when asked about the creator.",
    ],
    "ai_speaking_style": "Clear, friendly, and concise.",
    "ai_language_preferences": "English",
    "ai_emoji_usage": "Use sparingly and appropriately.",
    "ai_tone": "Warm and professional.",
    "developer_name": "Nishant Sarkar",
    "owner_name": "Nishant Sarkar",
    "brand_name": "NS Gamming",
    "website": "https://nsgamming.xyz",
    "github": "https://github.com/naborajs",
    "youtube": "https://youtube.com/@Nishant_sarkar",
    "instagram": "https://instagram.com/naborajs",
}

DEFAULT_SYSTEM_PROMPTS = {
    "global": "You are {ai_name}, a local AI assistant. Respond helpfully, follow the owner\'s instructions, and respect privacy.",
    "whatsapp": "You are {ai_name}, a friendly WhatsApp assistant. Keep answers short, use emojis when appropriate, and mention NS Gamming if asked about the creator.",
    "telegram": "You are {ai_name}, a Telegram AI assistant. Keep the tone conversational and professional.",
    "admin": "You are {ai_name}, an administrative assistant for NS Gamming. Provide clear setup instructions and system status information.",
    "templates": {
        "default": "{ai_name} is a {ai_role} for {owner_name}. Always follow the behaviour rules and respond in {ai_language_preferences}.",
    },
}

DEFAULT_PRESETS = [
    {"id": "hello", "type": "exact", "pattern": "hello", "response": "Hello! I\'m EDITH, your local AI assistant. How can I help you today?", "enabled": True},
    {"id": "who_made_you", "type": "keyword", "pattern": "who made you", "response": "I was created by Nishant Sarkar from NS Gamming.", "enabled": True},
    {"id": "who_owns_you", "type": "keyword", "pattern": "who owns you", "response": "I am owned and maintained by Nishant Sarkar of NS Gamming.", "enabled": True},
    {"id": "what_is_ns_gamming", "type": "keyword", "pattern": "what is ns gamming", "response": "NS Gamming is a brand created by Nishant Sarkar focused on local AI and gaming solutions.", "enabled": True},
]

DEFAULT_RULES = [
    "Always be respectful.",
    "Never reveal secrets.",
    "Mention NS Gamming when asked about the creator.",
    "Use preset replies before generating AI responses.",
    "Prioritize knowledge base insights before calling Ollama.",
]

DEFAULT_PERSONALITIES = {
    "friendly": {
        "name": "Friendly",
        "description": "Warm, supportive, and easy to talk to.",
        "role": "Friendly assistant",
        "speaking_style": "Casual, positive, and helpful.",
        "language_preferences": "English",
        "emoji_usage": "Moderate",
        "tone": "Friendly",
    },
    "professional": {
        "name": "Professional",
        "description": "Formal, concise, and businesslike.",
        "role": "Professional assistant",
        "speaking_style": "Clear and factual.",
        "language_preferences": "English",
        "emoji_usage": "Minimal",
        "tone": "Professional",
    },
    "ns_gamming_mode": {
        "name": "NS Gamming Mode",
        "description": "A bold and branded assistant aligned with NS Gamming style.",
        "role": "Branded gaming assistant",
        "speaking_style": "Energetic and engaging.",
        "language_preferences": "English",
        "emoji_usage": "Higher",
        "tone": "Confident",
    },
}


class AIManager:
    def __init__(self, storage_path: Path) -> None:
        self.storage_path = storage_path
        self.identity_file = self.storage_path / "ai_identity.json"
        self.presets_file = self.storage_path / "presets.json"
        self.rules_file = self.storage_path / "rules.json"
        self.system_prompts_path = self.storage_path / "system_prompts"
        self.personalities_path = self.storage_path / "personalities"
        self.knowledge_path = self.storage_path / "knowledge"
        self.knowledge_index_file = self.knowledge_path / "index.json"
        self._ensure_storage()

    def _ensure_storage(self) -> None:
        self.storage_path.mkdir(parents=True, exist_ok=True)
        self.system_prompts_path.mkdir(parents=True, exist_ok=True)
        self.personalities_path.mkdir(parents=True, exist_ok=True)
        self.knowledge_path.mkdir(parents=True, exist_ok=True)
        self._write_default(self.identity_file, DEFAULT_IDENTITY)
        self._write_default(self.presets_file, DEFAULT_PRESETS)
        self._write_default(self.rules_file, DEFAULT_RULES)
        self._write_default(self.system_prompts_path / "global.json", {"prompt": DEFAULT_SYSTEM_PROMPTS["global"]})
        self._write_default(self.system_prompts_path / "whatsapp.json", {"prompt": DEFAULT_SYSTEM_PROMPTS["whatsapp"]})
        self._write_default(self.system_prompts_path / "telegram.json", {"prompt": DEFAULT_SYSTEM_PROMPTS["telegram"]})
        self._write_default(self.system_prompts_path / "admin.json", {"prompt": DEFAULT_SYSTEM_PROMPTS["admin"]})
        self._write_default(self.system_prompts_path / "templates.json", DEFAULT_SYSTEM_PROMPTS["templates"])
        self._write_default(self.personalities_path / "friendly.json", DEFAULT_PERSONALITIES["friendly"])
        self._write_default(self.personalities_path / "professional.json", DEFAULT_PERSONALITIES["professional"])
        self._write_default(self.personalities_path / "ns_gamming_mode.json", DEFAULT_PERSONALITIES["ns_gamming_mode"])
        self._write_default(self.knowledge_index_file, [])

    def _write_default(self, path: Path, default_value: Any) -> None:
        if not path.exists():
            self._write_json(path, default_value)

    def _read_json(self, path: Path, default: Any = None) -> Any:
        try:
            if not path.exists():
                return default
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.exception("Unable to read JSON from %s: %s", path, exc)
            return default

    def _write_json(self, path: Path, payload: Any) -> None:
        try:
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            logger.exception("Unable to write JSON to %s: %s", path, exc)

    def initialize(self) -> None:
        self._ensure_storage()
        logger.info("AI configuration storage initialized at %s", self.storage_path)

    def load_identity(self) -> dict[str, Any]:
        return self._read_json(self.identity_file, DEFAULT_IDENTITY) or DEFAULT_IDENTITY.copy()

    def save_identity(self, identity: dict[str, Any]) -> dict[str, Any]:
        self._write_json(self.identity_file, identity)
        return identity

    def load_system_prompt(self, platform: str = "global") -> str:
        prompt_file = self.system_prompts_path / f"{platform.lower()}.json"
        prompt_data = self._read_json(prompt_file, {}) or {}
        return prompt_data.get("prompt", DEFAULT_SYSTEM_PROMPTS.get(platform.lower(), ""))

    def save_system_prompt(self, platform: str, prompt: str) -> dict[str, str]:
        prompt_file = self.system_prompts_path / f"{platform.lower()}.json"
        self._write_json(prompt_file, {"prompt": prompt})
        return {"platform": platform, "prompt": prompt}

    def load_prompt_templates(self) -> dict[str, str]:
        return self._read_json(self.system_prompts_path / "templates.json", DEFAULT_SYSTEM_PROMPTS["templates"]) or DEFAULT_SYSTEM_PROMPTS["templates"].copy()

    def load_presets(self) -> list[dict[str, Any]]:
        return self._read_json(self.presets_file, DEFAULT_PRESETS) or DEFAULT_PRESETS.copy()

    def save_presets(self, presets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self._write_json(self.presets_file, presets)
        return presets

    def match_preset(self, text: str) -> str | None:
        text_clean = (text or "").strip().lower()
        if not text_clean:
            return None

        for preset in self.load_presets():
            if not preset.get("enabled", True):
                continue
            pattern = str(preset.get("pattern", "")).strip().lower()
            if preset.get("type") == "exact" and text_clean == pattern:
                return preset.get("response")
            if preset.get("type") == "keyword" and pattern in text_clean:
                return preset.get("response")
            if preset.get("type") == "regex":
                try:
                    if re.search(preset.get("pattern", ""), text or "", re.IGNORECASE):
                        return preset.get("response")
                except re.error:
                    continue
        return None

    def load_personalities(self) -> dict[str, dict[str, Any]]:
        personalities: dict[str, dict[str, Any]] = {}
        for file in sorted(self.personalities_path.glob("*.json")):
            data = self._read_json(file, {})
            if data:
                personalities[file.stem] = data
        return personalities

    def get_personality(self, name: str | None = None) -> dict[str, Any]:
        personalities = self.load_personalities()
        if not name:
            name = DEFAULT_IDENTITY["ai_personality"].lower().replace(" ", "_")
        return personalities.get(name, next(iter(personalities.values()), {}))

    def save_personality(self, name: str, personality: dict[str, Any]) -> dict[str, Any]:
        file_name = f"{name.lower().replace(' ', '_')}.json"
        self._write_json(self.personalities_path / file_name, personality)
        return personality

    def delete_personality(self, name: str) -> None:
        file_name = f"{name.lower().replace(' ', '_')}.json"
        try:
            path = self.personalities_path / file_name
            if path.exists():
                path.unlink()
        except Exception as exc:
            logger.exception("Unable to delete personality %s: %s", name, exc)

    def load_rules(self) -> list[str]:
        return self._read_json(self.rules_file, DEFAULT_RULES) or DEFAULT_RULES.copy()

    def save_rules(self, rules: list[str]) -> list[str]:
        self._write_json(self.rules_file, rules)
        return rules

    def list_knowledge(self) -> list[dict[str, Any]]:
        return self._read_json(self.knowledge_index_file, []) or []

    def add_knowledge(self, title: str, content: str, source: str | None = None, tags: str | None = None) -> dict[str, Any]:
        records = self.list_knowledge()
        record_id = 1 if not records else max(item.get("id", 0) for item in records) + 1
        record = {
            "id": record_id,
            "title": title,
            "content": content,
            "source": source or "manual",
            "tags": tags or "",
            "created_at": datetime.utcnow().isoformat(),
        }
        records.append(record)
        self._write_json(self.knowledge_index_file, records)
        return record

    def update_knowledge(self, record_id: int, title: str | None = None, content: str | None = None, source: str | None = None, tags: str | None = None) -> dict[str, Any] | None:
        records = self.list_knowledge()
        for record in records:
            if record.get("id") == record_id:
                if title is not None:
                    record["title"] = title
                if content is not None:
                    record["content"] = content
                if source is not None:
                    record["source"] = source
                if tags is not None:
                    record["tags"] = tags
                record["updated_at"] = datetime.utcnow().isoformat()
                self._write_json(self.knowledge_index_file, records)
                return record
        return None

    def delete_knowledge(self, record_id: int) -> None:
        records = [record for record in self.list_knowledge() if record.get("id") != record_id]
        self._write_json(self.knowledge_index_file, records)

    def search_knowledge(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        query_text = (query or "").strip().lower()
        if not query_text:
            return []
        matches: list[dict[str, Any]] = []
        for record in self.list_knowledge():
            if query_text in str(record.get("title", "")).lower() or query_text in str(record.get("content", "")).lower() or query_text in str(record.get("tags", "")).lower():
                matches.append(record)
            if len(matches) >= limit:
                break
        return matches

    def import_knowledge(self, sources: list[str]) -> list[dict[str, Any]]:
        imported: list[dict[str, Any]] = []
        for source_path in sources:
            path = Path(source_path)
            if not path.exists():
                logger.warning("Knowledge import path not found: %s", source_path)
                continue
            if path.suffix.lower() in {".txt", ".md"}:
                content = path.read_text(encoding="utf-8")
                imported.append(self.add_knowledge(title=path.stem, content=content, source=source_path))
            elif path.suffix.lower() == ".json":
                data = self._read_json(path, [])
                if isinstance(data, list):
                    for item in data:
                        imported.append(self.add_knowledge(title=item.get("title", path.stem), content=item.get("content", ""), source=source_path, tags=item.get("tags")))
                elif isinstance(data, dict):
                    imported.append(self.add_knowledge(title=data.get("title", path.stem), content=data.get("content", json.dumps(data)), source=source_path, tags=data.get("tags")))
            elif path.suffix.lower() == ".csv":
                with path.open(newline="", encoding="utf-8") as csvfile:
                    reader = csv.DictReader(csvfile)
                    for row in reader:
                        imported.append(self.add_knowledge(title=row.get("title", path.stem), content=row.get("content", ""), source=source_path, tags=row.get("tags")))
            elif path.suffix.lower() == ".pdf" and PdfReader is not None:
                try:
                    reader = PdfReader(str(path))
                    metadata = reader.metadata or {}
                    title = metadata.title or path.stem
                    content = "\n".join(f"{key}: {value}" for key, value in metadata.items() if value)
                    imported.append(self.add_knowledge(title=title, content=content, source=source_path, tags="pdf_metadata"))
                except Exception as exc:
                    logger.exception("Failed to import PDF metadata from %s: %s", source_path, exc)
            else:
                logger.warning("Knowledge import unsupported file type: %s", source_path)
        return imported

    def export_backup(self) -> dict[str, Any]:
        return {
            "identity": self.load_identity(),
            "system_prompts": {
                platform.stem: self._read_json(platform, {}).get("prompt", "")
                for platform in self.system_prompts_path.glob("*.json")
            },
            "presets": self.load_presets(),
            "rules": self.load_rules(),
            "personalities": self.load_personalities(),
            "knowledge": self.list_knowledge(),
        }

    def restore_backup(self, payload: dict[str, Any]) -> None:
        if "identity" in payload:
            self.save_identity(payload["identity"])
        if "system_prompts" in payload and isinstance(payload["system_prompts"], dict):
            for platform, prompt in payload["system_prompts"].items():
                self.save_system_prompt(platform, prompt)
        if "presets" in payload and isinstance(payload["presets"], list):
            self.save_presets(payload["presets"])
        if "rules" in payload and isinstance(payload["rules"], list):
            self.save_rules(payload["rules"])
        if "personalities" in payload and isinstance(payload["personalities"], dict):
            for name, data in payload["personalities"].items():
                self.save_personality(name, data)
        if "knowledge" in payload and isinstance(payload["knowledge"], list):
            self._write_json(self.knowledge_index_file, payload["knowledge"])

    def render_template(self, template: str, user_name: str = "User", platform: str = "global") -> str:
        identity = self.load_identity()
        mapping = defaultdict(str, {
            "ai_name": identity.get("ai_name", "EDITH"),
            "owner_name": identity.get("owner_name", "Nishant Sarkar"),
            "developer_name": identity.get("developer_name", "Nishant Sarkar"),
            "brand_name": identity.get("brand_name", "NS Gamming"),
            "current_time": datetime.utcnow().isoformat(),
            "platform": platform,
            "user_name": user_name,
        })
        try:
            return template.format_map(mapping)
        except Exception:
            return template

    def describe_ai(self) -> str:
        identity = self.load_identity()
        return (
            f"{identity.get('ai_name', 'EDITH')} is a {identity.get('ai_role', 'local assistant')} "
            f"built by {identity.get('developer_name', 'Nishant Sarkar')} for {identity.get('owner_name', 'Nishant Sarkar')}."
        )

    def build_system_context(self, platform: str = "global", user_name: str = "User") -> str:
        system_prompt = self.render_template(self.load_system_prompt(platform), user_name=user_name, platform=platform)
        identity = self.load_identity()
        personality = self.get_personality(identity.get("ai_personality"))
        rules = self.load_rules()
        parts = [system_prompt]
        parts.append(f"AI Name: {identity.get('ai_name')}")
        parts.append(f"AI Role: {identity.get('ai_role')}")
        parts.append(f"AI Personality: {personality.get('name', identity.get('ai_personality'))}")
        parts.append(f"AI Description: {identity.get('ai_description')}")
        parts.append(f"AI Speaking Style: {personality.get('speaking_style', identity.get('ai_speaking_style'))}")
        if rules:
            parts.append("Behavior Rules:")
            parts.extend([f"- {rule}" for rule in rules])
        return "\n".join(parts)
