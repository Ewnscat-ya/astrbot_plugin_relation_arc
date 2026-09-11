from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RelationshipType:
    key: str
    label: str
    category: str
    exclusive_group: str | None
    min_values: dict[str, int]
    required_route: str | None
    safety_required: str | None
    cooldown_hours: int


# B1 is deliberately a small fixed registry. Favour allows arbitrary relationship
# strings; Relation Arc rejects that surface so an LLM cannot invent a type.
_TYPES = (
    RelationshipType("friend", "朋友", "normal", None,
        {"trust": 500, "comfort": 450, "respect": 450}, None, "normal", 24),
    RelationshipType("close_friend", "挚友", "close", None,
        {"trust": 650, "comfort": 600, "closeness": 600, "resonance": 500}, None, "normal", 72),
    RelationshipType("partner", "搭档", "close", None,
        {"trust": 650, "respect": 650, "comfort": 550, "resonance": 550}, None, "normal", 72),
    RelationshipType("romantic_partner", "恋人", "romance", "romance",
        {"trust": 750, "respect": 650, "comfort": 750, "closeness": 750, "resonance": 650, "romance_interest": 700}, "shown", "normal", 168),
    RelationshipType("spouse", "此生挚爱", "romance", "romance",
        {"trust": 850, "respect": 750, "comfort": 850, "closeness": 850, "resonance": 750, "romance_interest": 850}, "shown", "normal", 336),
)
BY_KEY = {item.key: item for item in _TYPES}


def get_type(key: str | None) -> RelationshipType | None:
    return BY_KEY.get(key or "")


def public_directory() -> list[dict[str, Any]]:
    return [{"key": item.key, "label": item.label, "category": item.category, "exclusive_group": item.exclusive_group, "min_values": dict(item.min_values), "required_route": item.required_route, "safety_required": item.safety_required, "cooldown_hours": item.cooldown_hours} for item in _TYPES]


def projected_eligibility(item: RelationshipType, values: dict[str, int], state: dict[str, str], romance_enabled: bool) -> tuple[bool, str]:
    if any(int(values.get(key, 0)) < threshold for key, threshold in item.min_values.items()):
        return False, "threshold"
    if item.category == "romance":
        if not romance_enabled: return False, "romance_disabled"
        if item.required_route and state.get("romance_policy") != item.required_route: return False, "route"
        if state.get("romance_state") not in {"eligible", "committed"}: return False, "romance_state"
    if item.safety_required and state.get("interaction_safety") != item.safety_required:
        return False, "safety"
    return True, "eligible"
