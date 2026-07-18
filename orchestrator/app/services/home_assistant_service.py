"""Home Assistant integration — query device states and control devices via HA REST API.

Uses the HA long-lived access token (no custom component required).
Set HA_URL and HA_TOKEN in orchestrator .env.

Read: entity states, area summaries, device search.
Write: turn on/off, set thermostat, lock/unlock, cover open/close, light brightness/color.
"""

import json
import logging
import re
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger("pai.services.home_assistant")

# ── Helpers ──────────────────────────────────────────────────────────────────

def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.ha_token}",
        "Content-Type": "application/json",
    }


def _base() -> str:
    return settings.ha_url.rstrip("/")


async def _get(path: str, http_client: httpx.AsyncClient | None = None) -> Any:
    url = f"{_base()}{path}"
    client = http_client or httpx.AsyncClient(timeout=10.0)
    resp = await client.get(url, headers=_headers())
    resp.raise_for_status()
    return resp.json()


async def _post(path: str, payload: dict, http_client: httpx.AsyncClient | None = None) -> Any:
    url = f"{_base()}{path}"
    client = http_client or httpx.AsyncClient(timeout=10.0)
    resp = await client.post(url, headers=_headers(), json=payload)
    resp.raise_for_status()
    return resp.json()


def _available() -> bool:
    return bool(settings.ha_url and settings.ha_token)


# ── State queries ─────────────────────────────────────────────────────────────

async def get_all_states(http_client=None) -> list[dict]:
    """Fetch all entity states from HA."""
    if not _available():
        return []
    return await _get("/api/states", http_client)


async def get_entity_state(entity_id: str, http_client=None) -> dict | None:
    """Fetch a single entity's state."""
    if not _available():
        return None
    try:
        return await _get(f"/api/states/{entity_id}", http_client)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None
        raise


async def get_areas(http_client=None) -> list[dict]:
    """Fetch all areas from HA."""
    if not _available():
        return []
    try:
        result = await _post("/api/template", {"template": "{{ areas() | list }}"}, http_client)
        area_names = json.loads(result) if isinstance(result, str) else result
        return [{"name": a} for a in area_names] if isinstance(area_names, list) else []
    except Exception:
        return []


# ── Natural language query ────────────────────────────────────────────────────

# Domains that are interesting to surface in a summary
_SUMMARY_DOMAINS = {
    "light", "switch", "climate", "cover", "lock", "sensor",
    "binary_sensor", "media_player", "fan", "vacuum", "camera",
    "alarm_control_panel", "input_boolean",
}

_DOMAIN_LABELS = {
    "light": "Lights",
    "switch": "Switches",
    "climate": "Thermostats",
    "cover": "Covers/Blinds",
    "lock": "Locks",
    "sensor": "Sensors",
    "binary_sensor": "Binary Sensors",
    "media_player": "Media Players",
    "fan": "Fans",
    "vacuum": "Vacuums",
    "alarm_control_panel": "Alarm",
}


def _friendly(entity: dict) -> str:
    attrs = entity.get("attributes", {})
    return attrs.get("friendly_name") or entity.get("entity_id", "")


def _fmt_state(entity: dict) -> str:
    state = entity.get("state", "unknown")
    attrs = entity.get("attributes", {})
    domain = entity["entity_id"].split(".")[0]
    name = _friendly(entity)

    extras = []
    if domain == "climate":
        cur = attrs.get("current_temperature")
        target = attrs.get("temperature")
        mode = attrs.get("hvac_action") or attrs.get("hvac_mode") or state
        if cur:
            extras.append(f"current {cur}°")
        if target:
            extras.append(f"set {target}°")
        extras.append(mode)
    elif domain == "sensor":
        unit = attrs.get("unit_of_measurement", "")
        return f"{name}: {state}{unit}"
    elif domain == "binary_sensor":
        device_class = attrs.get("device_class", "")
        label = {"motion": "motion", "door": "door", "window": "window",
                 "moisture": "leak", "smoke": "smoke"}.get(device_class, "sensor")
        return f"{name} ({label}): {state}"
    elif domain == "cover":
        pos = attrs.get("current_position")
        if pos is not None:
            extras.append(f"{pos}% open")
    elif domain == "media_player":
        title = attrs.get("media_title") or attrs.get("app_name", "")
        if title and state not in ("off", "unavailable", "standby"):
            extras.append(title)

    extra_str = f" ({', '.join(extras)})" if extras else ""
    return f"{name}: {state}{extra_str}"


async def query_home_state(query: str = "", http_client=None) -> str:
    """
    Natural language home state summary.
    Supports filtering by room/area keyword or entity type.
    """
    if not _available():
        return (
            "Home Assistant is not configured. "
            "Set HA_URL and HA_TOKEN in orchestrator .env to enable smart home queries."
        )

    try:
        states = await get_all_states(http_client)
    except Exception as e:
        logger.warning("ha_states_fetch_failed: %s", e)
        return f"Could not reach Home Assistant: {e}"

    if not states:
        return "No entities found in Home Assistant."

    lower = query.lower() if query else ""

    # ── Filter by room/area (keyword in entity_id or friendly_name) ──
    room_match = None
    # Common room keywords
    room_keywords = ["living", "bedroom", "kitchen", "bathroom", "garage", "office",
                     "basement", "attic", "backyard", "front", "hallway", "dining",
                     "laundry", "master", "guest", "upstairs", "downstairs", "porch",
                     "mudroom", "foyer", "entryway", "nursery", "playroom", "studio"]
    for kw in room_keywords:
        if kw in lower:
            room_match = kw
            break

    # ── Filter by domain ──
    domain_filter = None
    domain_map = {
        "light": ["light", "lights", "lamp", "lamps"],
        "switch": ["switch", "switches", "plug", "outlet"],
        "climate": ["thermostat", "ac", "heat", "hvac", "temperature", "climate", "cooling", "heating"],
        "lock": ["lock", "locks", "locked", "unlocked"],
        "cover": ["cover", "blind", "blinds", "shade", "shades", "garage door"],
        "sensor": ["sensor", "sensors", "humidity", "air quality", "co2", "lux"],
        "binary_sensor": ["motion", "door", "window", "leak", "smoke"],
        "media_player": ["tv", "speaker", "media", "playing", "music", "sonos", "netflix"],
        "fan": ["fan", "fans"],
        "alarm_control_panel": ["alarm", "armed", "security"],
    }
    for domain, keywords in domain_map.items():
        if any(kw in lower for kw in keywords):
            domain_filter = domain
            break

    # ── Specific entity search by name ──
    name_search = None
    # Strip common command words to get the device name
    clean = re.sub(
        r'\b(what|is|are|the|status|of|show|me|my|home|house|all|devices?|entities?|state|states|'
        r'turn|on|off|set|open|close|lock|unlock|dim|brighten|control|check)\b',
        ' ', lower
    ).strip()
    clean = re.sub(r'\s+', ' ', clean).strip()
    if clean and len(clean) > 2 and not room_match and not domain_filter:
        name_search = clean

    # ── Apply filters ──
    filtered = []
    for e in states:
        eid = e.get("entity_id", "")
        domain = eid.split(".")[0]
        if domain not in _SUMMARY_DOMAINS:
            continue
        if e.get("state") in ("unavailable", "unknown"):
            continue

        fname = _friendly(e).lower()

        if room_match and room_match not in eid.lower() and room_match not in fname:
            continue
        if domain_filter and domain != domain_filter:
            continue
        if name_search and name_search not in fname and name_search not in eid.lower():
            continue

        filtered.append(e)

    if not filtered:
        if room_match:
            return f"No devices found for '{room_match}'. Check that the room name matches your HA entity names or areas."
        if domain_filter:
            return f"No {domain_filter} entities found (or all unavailable)."
        # Fall back to full summary grouped by domain
        filtered = [e for e in states if e.get("entity_id", "").split(".")[0] in _SUMMARY_DOMAINS
                    and e.get("state") not in ("unavailable", "unknown")]

    # ── Build output ──
    if not filtered:
        return "All Home Assistant entities are unavailable or unknown."

    # Group by domain
    by_domain: dict[str, list] = {}
    for e in filtered:
        domain = e["entity_id"].split(".")[0]
        by_domain.setdefault(domain, []).append(e)

    lines = []
    if room_match:
        lines.append(f"HOME ASSISTANT — {room_match.upper()} DEVICES:")
    elif domain_filter:
        lines.append(f"HOME ASSISTANT — {_DOMAIN_LABELS.get(domain_filter, domain_filter.upper())}:")
    elif name_search:
        lines.append(f"HOME ASSISTANT — SEARCH: {name_search}")
    else:
        lines.append("HOME ASSISTANT — DEVICE SUMMARY:")

    for domain, entities in sorted(by_domain.items()):
        label = _DOMAIN_LABELS.get(domain, domain.title())
        lines.append(f"\n{label}:")
        for e in entities[:20]:  # cap per domain for voice friendliness
            lines.append(f"  • {_fmt_state(e)}")
        if len(entities) > 20:
            lines.append(f"  ... and {len(entities) - 20} more")

    return "\n".join(lines)


# ── Device control ────────────────────────────────────────────────────────────

async def control_device(command: str, http_client=None) -> str:
    """
    Parse a natural language control command and call the appropriate HA service.
    Supports: turn on/off, set temperature, lock/unlock, open/close covers,
              set brightness, set color temperature.
    """
    if not _available():
        return "Home Assistant is not configured. Set HA_URL and HA_TOKEN in .env."

    lower = command.lower()

    # ── Resolve entity from command ──
    try:
        states = await get_all_states(http_client)
    except Exception as e:
        return f"Could not reach Home Assistant: {e}"

    # Score entities by how well their name matches the command
    def score(e: dict) -> int:
        name = _friendly(e).lower()
        eid = e.get("entity_id", "").lower()
        s = 0
        for word in name.split():
            if len(word) > 2 and word in lower:
                s += 2
        for word in eid.replace("_", " ").replace(".", " ").split():
            if len(word) > 2 and word in lower:
                s += 1
        return s

    candidates = [(score(e), e) for e in states if e.get("entity_id", "").split(".")[0] in _SUMMARY_DOMAINS]
    candidates = [(s, e) for s, e in candidates if s > 0]
    candidates.sort(key=lambda x: x[0], reverse=True)

    if not candidates:
        return (
            "I couldn't identify which device you mean. "
            "Try using the device name exactly as it appears in Home Assistant, "
            "e.g. 'turn off the living room light'."
        )

    _, target = candidates[0]
    entity_id = target["entity_id"]
    domain = entity_id.split(".")[0]
    fname = _friendly(target)

    # ── Thermostat: set temperature ──
    if domain == "climate":
        temp_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:degrees?|°)?', lower)
        if temp_match:
            temp = float(temp_match.group(1))
            await _post("/api/services/climate/set_temperature",
                        {"entity_id": entity_id, "temperature": temp}, http_client)
            return f"Set {fname} to {temp}°."
        # hvac mode
        if re.search(r'\b(heat)\b', lower):
            await _post("/api/services/climate/set_hvac_mode",
                        {"entity_id": entity_id, "hvac_mode": "heat"}, http_client)
            return f"Set {fname} to heat mode."
        if re.search(r'\b(cool|ac|air\s*condition)\b', lower):
            await _post("/api/services/climate/set_hvac_mode",
                        {"entity_id": entity_id, "hvac_mode": "cool"}, http_client)
            return f"Set {fname} to cool mode."
        if re.search(r'\b(off)\b', lower):
            await _post("/api/services/climate/set_hvac_mode",
                        {"entity_id": entity_id, "hvac_mode": "off"}, http_client)
            return f"Turned {fname} off."
        if re.search(r'\b(fan\s*only)\b', lower):
            await _post("/api/services/climate/set_hvac_mode",
                        {"entity_id": entity_id, "hvac_mode": "fan_only"}, http_client)
            return f"Set {fname} to fan-only mode."

    # ── Cover: open/close ──
    if domain == "cover":
        if re.search(r'\b(open)\b', lower):
            await _post("/api/services/cover/open_cover", {"entity_id": entity_id}, http_client)
            return f"Opening {fname}."
        if re.search(r'\b(close|shut)\b', lower):
            await _post("/api/services/cover/close_cover", {"entity_id": entity_id}, http_client)
            return f"Closing {fname}."
        pos_match = re.search(r'(\d+)\s*(?:percent|%)', lower)
        if pos_match:
            pos = int(pos_match.group(1))
            await _post("/api/services/cover/set_cover_position",
                        {"entity_id": entity_id, "position": pos}, http_client)
            return f"Set {fname} to {pos}%."

    # ── Lock: lock/unlock ──
    if domain == "lock":
        if re.search(r'\bunlock\b', lower):
            await _post("/api/services/lock/unlock", {"entity_id": entity_id}, http_client)
            return f"Unlocked {fname}."
        if re.search(r'\block\b', lower):
            await _post("/api/services/lock/lock", {"entity_id": entity_id}, http_client)
            return f"Locked {fname}."

    # ── Light: brightness / color temp ──
    if domain == "light":
        bright_match = re.search(r'(\d+)\s*(?:percent|%|brightness)', lower)
        if bright_match:
            pct = int(bright_match.group(1))
            brightness = round(pct * 2.55)  # 0-255
            await _post("/api/services/light/turn_on",
                        {"entity_id": entity_id, "brightness": brightness}, http_client)
            return f"Set {fname} brightness to {pct}%."
        if re.search(r'\b(warm|warmer)\b', lower):
            await _post("/api/services/light/turn_on",
                        {"entity_id": entity_id, "color_temp": 400}, http_client)
            return f"Set {fname} to warm light."
        if re.search(r'\b(cool|cooler|daylight)\b', lower):
            await _post("/api/services/light/turn_on",
                        {"entity_id": entity_id, "color_temp": 200}, http_client)
            return f"Set {fname} to cool/daylight light."
        if re.search(r'\bdim\b', lower):
            await _post("/api/services/light/turn_on",
                        {"entity_id": entity_id, "brightness": 50}, http_client)
            return f"Dimmed {fname}."

    # ── Generic on/off (lights, switches, fans, media players) ──
    if re.search(r'\b(turn\s*off|switch\s*off|off)\b', lower):
        service_domain = domain if domain in ("light", "switch", "fan", "media_player", "cover") else "homeassistant"
        service = "turn_off"
        await _post(f"/api/services/{service_domain}/{service}", {"entity_id": entity_id}, http_client)
        return f"Turned off {fname}."
    if re.search(r'\b(turn\s*on|switch\s*on|on)\b', lower):
        service_domain = domain if domain in ("light", "switch", "fan", "media_player") else "homeassistant"
        await _post(f"/api/services/{service_domain}/turn_on", {"entity_id": entity_id}, http_client)
        return f"Turned on {fname}."

    # ── Fan speed ──
    if domain == "fan":
        if re.search(r'\b(off)\b', lower):
            await _post("/api/services/fan/turn_off", {"entity_id": entity_id}, http_client)
            return f"Turned off {fname}."
        speed_match = re.search(r'\b(low|medium|high)\b', lower)
        if speed_match:
            await _post("/api/services/fan/set_percentage",
                        {"entity_id": entity_id,
                         "percentage": {"low": 33, "medium": 66, "high": 100}[speed_match.group(1)]},
                        http_client)
            return f"Set {fname} to {speed_match.group(1)} speed."

    return (
        f"I found '{fname}' but couldn't determine what action to take. "
        "Try being more specific, e.g. 'turn off', 'set to 72 degrees', 'open', 'lock'."
    )


async def build_ha_context(query: str = "", http_client=None) -> str:
    """Entry point for skill read_handler — returns a formatted state string."""
    return await query_home_state(query, http_client)


async def process_ha_command(command: str, http_client=None) -> str:
    """Entry point for skill write_handler — parses and executes a control command."""
    return await control_device(command, http_client)
