"""Music service — Sonos control via node-sonos-http-api."""

import logging
import json
from urllib.parse import quote

import httpx

from app.services.ollama_service import generate
from app.core.config import settings

logger = logging.getLogger("pai.services.music")

# The sonos-http-api runs on Synology NAS (192.168.0.5:5005)
SONOS_API = getattr(settings, "sonos_api_url", None) or "http://192.168.0.5:5005"


async def _parse_music_command(message: str, http_client: httpx.AsyncClient) -> dict:
    """Use LLM to extract music intent from natural language."""
    raw = await generate(
        prompt=(
            f"Extract the music command from this message. Return ONLY valid JSON.\n"
            f"Fields:\n"
            f'  "action": one of "play", "pause", "resume", "stop", "skip", "previous", "volume", "status"\n'
            f'  "query": what to play (playlist name, song, artist) — empty string if not applicable\n'
            f'  "media_type": one of "song", "album", "playlist", "station" if clear, else empty string\n'
            f'  "service": one of "spotify", "apple", "deezer", "elite", "library" if clear, else empty string\n'
            f'  "room": which speaker/room — empty string if not specified\n'
            f'  "volume": integer 0-100 if volume command, else null\n'
            f"\nMessage: {message}"
        ),
        system_prompt="Extract structured data. Return only valid JSON, no explanation.",
        model="qwen3:4b",
        http_client=http_client,
    )
    try:
        text = raw.strip()
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0:
            parsed = json.loads(text[start:end])
            action = str(parsed.get("action", "")).strip().lower()
            if action not in {"play", "pause", "resume", "stop", "skip", "previous", "volume", "status"}:
                action = ""

            # Heuristic fallback when action is missing/invalid.
            if not action:
                lower = message.lower()
                if any(k in lower for k in ["what's playing", "what is playing", "now playing", "status"]):
                    action = "status"
                elif "pause" in lower:
                    action = "pause"
                elif any(k in lower for k in ["resume", "continue"]):
                    action = "resume"
                elif any(k in lower for k in ["skip", "next"]):
                    action = "skip"
                elif any(k in lower for k in ["previous", "back"]):
                    action = "previous"
                elif "stop" in lower:
                    action = "stop"
                elif "volume" in lower:
                    action = "volume"
                else:
                    action = "play"

            parsed["action"] = action
            if str(parsed.get("media_type", "")).strip().lower() not in {"song", "album", "playlist", "station"}:
                parsed["media_type"] = ""
            if str(parsed.get("service", "")).strip().lower() not in {"spotify", "apple", "deezer", "elite", "library"}:
                parsed["service"] = ""
            return parsed
    except Exception:
        pass
    return {"action": "play", "query": message, "media_type": "", "service": "", "room": "", "volume": None}


def _infer_media_type(message: str, query: str, parsed_type: str = "") -> str:
    """Infer song/album/playlist/station for musicsearch endpoint."""
    if parsed_type in {"song", "album", "playlist", "station"}:
        return parsed_type

    lower = f"{message} {query}".lower()
    if "playlist" in lower:
        return "playlist"
    if "album" in lower:
        return "album"
    if "station" in lower or "radio" in lower:
        return "station"
    if "song" in lower or "track" in lower:
        return "song"
    # Sonos musicsearch/song with artist name plays top tracks for that artist.
    return "song"


def _infer_service(message: str, query: str, parsed_service: str = "") -> str:
    """Infer provider for musicsearch endpoint."""
    if parsed_service in {"spotify", "apple", "deezer", "elite", "library"}:
        return parsed_service

    lower = f"{message} {query}".lower()
    if "apple music" in lower:
        return "apple"
    if "deezer elite" in lower:
        return "elite"
    if "deezer" in lower:
        return "deezer"
    if "library" in lower or "local" in lower:
        return "library"
    return "spotify"


async def _try_musicsearch(
    room_encoded: str,
    query: str,
    media_type: str,
    preferred_service: str,
    http_client: httpx.AsyncClient,
) -> tuple[bool, str]:
    """Try Sonos musicsearch endpoint across likely services."""
    services = [preferred_service] + [s for s in ["spotify", "apple", "deezer", "elite", "library"] if s != preferred_service]
    query_encoded = quote(query)

    for service in services:
        url = f"{SONOS_API}/{room_encoded}/musicsearch/{service}/{media_type}/{query_encoded}"
        try:
            resp = await http_client.get(url, timeout=12.0)
            if resp.status_code != 200:
                continue

            ok = True
            try:
                body = resp.json()
                # Some endpoints return {status: "success"}; if status exists and isn't success, treat as failure.
                if isinstance(body, dict) and body.get("status") and str(body.get("status")).lower() != "success":
                    ok = False
            except Exception:
                # Non-JSON 200 payloads are still acceptable for this API.
                pass

            if ok:
                return True, service
        except Exception:
            continue

    return False, ""


async def _get_rooms(http_client: httpx.AsyncClient) -> list[str]:
    """Get available Sonos rooms/zones."""
    try:
        resp = await http_client.get(f"{SONOS_API}/zones", timeout=5.0)
        if resp.status_code == 200:
            zones = resp.json()
            return [z["coordinator"]["roomName"] for z in zones if z.get("coordinator")]
    except Exception as e:
        logger.warning(f"Failed to get Sonos zones: {e}")
    return []


async def _resolve_room(requested: str, http_client: httpx.AsyncClient) -> str:
    """Fuzzy-match a room name from available Sonos devices."""
    rooms = await _get_rooms(http_client)
    if not rooms:
        return requested or "Living Room"

    if not requested:
        return rooms[0]  # Default to first available room

    req_lower = requested.lower().strip()
    for room in rooms:
        if req_lower in room.lower() or room.lower() in req_lower:
            return room

    # No match — return closest or first
    return rooms[0]


async def handle_music_command(message: str, http_client: httpx.AsyncClient) -> str:
    """Parse and execute a music command via Sonos HTTP API."""
    cmd = await _parse_music_command(message, http_client)
    action = cmd.get("action", "play")
    query = cmd.get("query", "")
    media_type = _infer_media_type(message, query, str(cmd.get("media_type", "")).strip().lower())
    preferred_service = _infer_service(message, query, str(cmd.get("service", "")).strip().lower())
    room_req = cmd.get("room", "")
    volume = cmd.get("volume")

    room = await _resolve_room(room_req, http_client)
    room_encoded = quote(room)

    try:
        if action == "pause":
            resp = await http_client.get(f"{SONOS_API}/{room_encoded}/pause", timeout=5.0)
            return f"Paused in {room}."

        elif action == "resume":
            resp = await http_client.get(f"{SONOS_API}/{room_encoded}/play", timeout=5.0)
            return f"Resumed in {room}."

        elif action == "stop":
            resp = await http_client.get(f"{SONOS_API}/{room_encoded}/pause", timeout=5.0)
            return f"Stopped in {room}."

        elif action == "skip":
            resp = await http_client.get(f"{SONOS_API}/{room_encoded}/next", timeout=5.0)
            return f"Skipped to next track in {room}."

        elif action == "previous":
            resp = await http_client.get(f"{SONOS_API}/{room_encoded}/previous", timeout=5.0)
            return f"Playing previous track in {room}."

        elif action == "volume":
            vol = volume if volume is not None else 30
            resp = await http_client.get(f"{SONOS_API}/{room_encoded}/volume/{vol}", timeout=5.0)
            return f"Volume set to {vol} in {room}."

        elif action == "status":
            resp = await http_client.get(f"{SONOS_API}/{room_encoded}/state", timeout=5.0)
            if resp.status_code == 200:
                state = resp.json()
                track = state.get("currentTrack", {})
                title = track.get("title", "Unknown")
                artist = track.get("artist", "Unknown")
                playback = state.get("playbackState", "unknown")
                vol_level = state.get("volume", "?")
                return f"{room}: {playback} — \"{title}\" by {artist} (vol: {vol_level})"
            return f"Could not get status for {room}."

        else:  # play
            if not query:
                # Resume playback
                resp = await http_client.get(f"{SONOS_API}/{room_encoded}/play", timeout=5.0)
                return f"Resumed playback in {room}."

            query_lower = query.lower()

            # If user explicitly asks for artist/album/song/station, prefer musicsearch first.
            explicit_search = any(k in message.lower() for k in ["artist", "album", "song", "track", "station", "radio"])
            if explicit_search or media_type != "playlist":
                found, service = await _try_musicsearch(
                    room_encoded=room_encoded,
                    query=query,
                    media_type=media_type,
                    preferred_service=preferred_service,
                    http_client=http_client,
                )
                if found:
                    return f"Playing {media_type} match for \"{query}\" on {room} via {service}."

            # 1. Fuzzy match against Sonos playlists
            try:
                pl_resp = await http_client.get(f"{SONOS_API}/{room_encoded}/playlists", timeout=5.0)
                if pl_resp.status_code == 200:
                    playlists = pl_resp.json()
                    for pl in playlists:
                        if query_lower in pl.lower() or pl.lower() in query_lower:
                            pl_encoded = quote(pl)
                            resp = await http_client.get(
                                f"{SONOS_API}/{room_encoded}/playlist/{pl_encoded}",
                                timeout=10.0,
                            )
                            if resp.status_code == 200:
                                return f"Playing playlist \"{pl}\" on {room}."
            except Exception:
                pass

            # 2. Fuzzy match against Sonos favourites
            try:
                fav_resp = await http_client.get(f"{SONOS_API}/{room_encoded}/favourites", timeout=5.0)
                if fav_resp.status_code == 200:
                    favs = fav_resp.json()
                    for fav in favs:
                        if query_lower in fav.lower() or fav.lower() in query_lower:
                            fav_encoded = quote(fav)
                            resp = await http_client.get(
                                f"{SONOS_API}/{room_encoded}/favourite/{fav_encoded}",
                                timeout=10.0,
                            )
                            if resp.status_code == 200:
                                return f"Playing \"{fav}\" on {room}."
            except Exception:
                pass

            # 3. General music search fallback (song/album/playlist/station)
            found, service = await _try_musicsearch(
                room_encoded=room_encoded,
                query=query,
                media_type=media_type,
                preferred_service=preferred_service,
                http_client=http_client,
            )
            if found:
                return f"Playing {media_type} match for \"{query}\" on {room} via {service}."

            # 4. Try exact playlist name as last resort
            query_encoded = quote(query)
            resp = await http_client.get(
                f"{SONOS_API}/{room_encoded}/playlist/{query_encoded}",
                timeout=10.0,
            )
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    if data.get("status") == "success":
                        return f"Playing playlist \"{query}\" on {room}."
                except Exception:
                    pass

            return (
                f"Couldn't find \"{query}\" as a playable playlist, favourite, song, album, or station. "
                "Try specifying the service, like 'play album Thriller by Michael Jackson on Spotify'."
            )

    except httpx.ConnectError:
        return "Cannot reach Sonos controller. Make sure the Sonos devices are on the network."
    except Exception as e:
        logger.error("music_command_failed", extra={"error": str(e), "cmd": cmd})
        return f"Music command failed: {e}"


async def get_music_status(http_client: httpx.AsyncClient) -> str:
    """Get playback status from all rooms."""
    rooms = await _get_rooms(http_client)
    if not rooms:
        return "No Sonos devices found on the network."

    lines = []
    for room in rooms:
        try:
            resp = await http_client.get(f"{SONOS_API}/{quote(room)}/state", timeout=5.0)
            if resp.status_code == 200:
                state = resp.json()
                track = state.get("currentTrack", {})
                playback = state.get("playbackState", "STOPPED")
                if playback == "PLAYING":
                    lines.append(f"  {room}: Playing \"{track.get('title', '?')}\" by {track.get('artist', '?')}")
                else:
                    lines.append(f"  {room}: {playback}")
        except Exception:
            lines.append(f"  {room}: unreachable")

    return "Sonos status:\n" + "\n".join(lines) if lines else "No Sonos status available."
