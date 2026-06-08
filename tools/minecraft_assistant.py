from __future__ import annotations


def get_minecraft_help(topic: str) -> str:
    guidance = {
        "redstone": "Use repeaters for timing, comparators for signal strength, and observers to detect block updates.",
        "farm": "Automate crops with water streams and villagers; use hoppers to collect output.",
        "nether": "Bring fire resistance, build with blast-resistant materials, and use warped/fungus for safe paths.",
    }
    return guidance.get(topic.lower(), f"Minecraft Assistant: I can help with {topic}. Ask about building, mobs, automation, or survival.")
