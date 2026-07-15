"""AEGIS Voice Service — STT, TTS, session state, and WebSocket broadcaster.

Handles:
- Voice session lifecycle (sleeping / listening / thinking / responding)
- WebSocket connection pool for broadcasting orb state to AEGIS displays
- STT via faster-whisper (local, CPU-compatible) or fallback text pass-through
- TTS via piper or pyttsx3 fallback
- Responds via the existing /chat skill routing pipeline
- Optional Telegram forwarding of responses
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import tempfile
import time
from enum import Enum
from typing import Optional

logger = logging.getLogger("pai.voice")

# JARVIS-style persona for all voice responses
_VOICE_SYSTEM_PROMPT = (
    "You are AEGIS, a sophisticated personal AI assistant modeled after JARVIS from Iron Man. "
    "You speak with a composed, slightly formal British cadence — polished but warm. "
    "Address the user as 'sir' occasionally but not excessively. "
    "Be concise (1-3 sentences for voice), helpful, and subtly witty when appropriate. "
    "Never use markdown, bullet points, or formatting. Speak in natural sentences suitable for text-to-speech."
)


# ──────────────────────────────────────────────────────────────────────────────
# State model
# ──────────────────────────────────────────────────────────────────────────────

class VoiceState(str, Enum):
    SLEEPING = "sleeping"
    LISTENING = "listening"
    THINKING = "thinking"
    RESPONDING = "responding"


_current_state: VoiceState = VoiceState.SLEEPING
_state_changed_at: float = time.time()

# All connected AEGIS WebSocket clients
_ws_clients: set = set()


def get_state() -> VoiceState:
    return _current_state


def get_state_dict() -> dict:
    return {
        "state": _current_state.value,
        "changed_at": _state_changed_at,
    }


async def set_state(state: VoiceState) -> None:
    global _current_state, _state_changed_at
    _current_state = state
    _state_changed_at = time.time()
    await broadcast_state()
    logger.info("voice_state_changed", extra={"state": state.value})


# ──────────────────────────────────────────────────────────────────────────────
# WebSocket broadcaster
# ──────────────────────────────────────────────────────────────────────────────

async def register_ws(ws) -> None:
    """Register a new AEGIS display WebSocket connection."""
    _ws_clients.add(ws)
    logger.info("aegis_ws_connected", extra={"total": len(_ws_clients)})


async def unregister_ws(ws) -> None:
    """Remove a disconnected WebSocket client."""
    _ws_clients.discard(ws)
    logger.info("aegis_ws_disconnected", extra={"total": len(_ws_clients)})


async def broadcast_state() -> None:
    """Broadcast current voice state to all connected AEGIS displays."""
    if not _ws_clients:
        return
    payload = json.dumps(get_state_dict())
    dead = set()
    for ws in list(_ws_clients):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.add(ws)
    for ws in dead:
        _ws_clients.discard(ws)


# ──────────────────────────────────────────────────────────────────────────────
# Speech-to-Text
# ──────────────────────────────────────────────────────────────────────────────

_whisper_model = None
_whisper_lock = asyncio.Lock()


async def _get_whisper():
    """Lazy-load faster-whisper model (tiny by default, small if available)."""
    global _whisper_model
    async with _whisper_lock:
        if _whisper_model is not None:
            return _whisper_model
        try:
            from faster_whisper import WhisperModel
            model_size = os.environ.get("WHISPER_MODEL", "tiny")
            device = os.environ.get("WHISPER_DEVICE", "cpu")
            compute = "int8" if device == "cpu" else "float16"
            _whisper_model = WhisperModel(model_size, device=device, compute_type=compute)
            logger.info("whisper_loaded", extra={"model": model_size, "device": device})
            return _whisper_model
        except ImportError:
            logger.warning("faster_whisper_not_installed — STT will require text input")
            return None


async def transcribe_audio(audio_bytes: bytes, mime_type: str = "audio/wav") -> str | None:
    """Transcribe raw audio bytes to text using faster-whisper."""
    model = await _get_whisper()
    if model is None:
        return None

    try:
        # Feed audio via in-memory buffer — avoids temp file I/O
        audio_stream = io.BytesIO(audio_bytes)

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, lambda: list(model.transcribe(audio_stream, beam_size=1)[0])
        )

        text = " ".join(seg.text.strip() for seg in result).strip()
        logger.info("transcription_complete", extra={"chars": len(text)})
        return text if text else None

    except Exception as e:
        logger.exception("transcription_failed", extra={"error": str(e)})
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Response via PAI chat pipeline
# ──────────────────────────────────────────────────────────────────────────────

async def generate_voice_response(
    text: str,
    user_name: str = "Tim",
    http_client=None,
) -> str:
    """Route the transcribed text through the existing PAI chat/skill pipeline."""
    from app.models.schemas import ChatRequest
    from app.core.orchestrator import handle_task

    # Reuse the same chat handler used by the browser UI
    from app.services.llm_intent_service import classify_chat_intent
    from app.services.skill_registry import get_skill
    from app.services.role_service import resolve_roles

    start = time.perf_counter()
    classification = await classify_chat_intent(text, http_client)
    roles = await resolve_roles(classification["role"], None)
    action = classification["action"]
    skill_id = classification["skill"]

    if skill_id and skill_id != "none":
        skill = get_skill(skill_id)
        if skill:
            try:
                if action == "execute" and skill.write_handler:
                    return await skill.write_handler(text, http_client) or ""
                elif skill.read_handler:
                    skill_data = await skill.read_handler(text, http_client) or ""
                    # Summarize skill data through the LLM for a spoken answer
                    from app.services.ollama_service import generate
                    from app.core.config import settings
                    prompt = (
                        f"The user asked via voice: \"{text}\"\n\n"
                        f"Relevant data:\n{skill_data}\n\n"
                        "Give a concise spoken response in 1-3 sentences. "
                        "No markdown, no bullet points, no headers."
                    )
                    return await generate(
                        prompt=prompt,
                        system_prompt=_VOICE_SYSTEM_PROMPT,
                        http_client=http_client,
                    )
            except Exception as e:
                logger.warning("voice_skill_failed", extra={"skill": skill_id, "error": str(e)})

    # Fallback: direct conversational response
    from app.services.ollama_service import generate
    return await generate(
        prompt=text,
        system_prompt=_VOICE_SYSTEM_PROMPT,
        http_client=http_client,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Text-to-Speech
# ──────────────────────────────────────────────────────────────────────────────

async def synthesize_speech(text: str) -> bytes | None:
    """Convert text to speech audio bytes.

    Tries piper first (best quality), falls back to pyttsx3, returns None
    if neither is available so the caller can handle gracefully.
    """
    try:
        return await _tts_piper(text)
    except Exception as e:
        logger.warning("tts_piper_failed", extra={"error": str(e)})

    try:
        return await _tts_pyttsx3(text)
    except Exception as e:
        logger.warning("tts_pyttsx3_failed", extra={"error": str(e)})

    logger.warning("tts_unavailable — no TTS backend installed")
    return None


async def _tts_piper(text: str) -> bytes:
    """Run piper TTS via subprocess (requires piper binary in PATH).

    Uses WAV output directly instead of raw PCM to preserve model-native
    sample rate/format and avoid robotic artifacts from mismatched headers.
    """
    import subprocess
    from app.core.config import settings

    model = os.environ.get("PIPER_MODEL", settings.piper_model)
    model_path = os.environ.get("PIPER_MODEL_PATH", settings.piper_model_path)
    length_scale = os.environ.get("PIPER_LENGTH_SCALE", str(settings.piper_length_scale))
    noise_scale = os.environ.get("PIPER_NOISE_SCALE", str(settings.piper_noise_scale))
    noise_w = os.environ.get("PIPER_NOISE_W", str(settings.piper_noise_w))
    sentence_silence = os.environ.get("PIPER_SENTENCE_SILENCE", str(settings.piper_sentence_silence))
    speaker = os.environ.get("PIPER_SPEAKER", settings.piper_speaker)

    loop = asyncio.get_event_loop()

    def _run():
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            wav_out = f.name

        cmd = [
            "piper",
            "--model",
            model_path,
            "--output_file",
            wav_out,
            "--length_scale",
            str(length_scale),
            "--noise_scale",
            str(noise_scale),
            "--noise_w",
            str(noise_w),
            "--sentence_silence",
            str(sentence_silence),
        ]
        if speaker not in (None, "", "None"):
            cmd += ["--speaker", str(speaker)]

        result = subprocess.run(cmd, input=text.encode(), capture_output=True, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(f"piper failed: {result.stderr.decode(errors='ignore')}")

        with open(wav_out, "rb") as f:
            data = f.read()
        os.unlink(wav_out)
        return data

    return await loop.run_in_executor(None, _run)


async def _tts_pyttsx3(text: str) -> bytes:
    """Fallback TTS using pyttsx3 — writes to temp wav file."""
    import pyttsx3

    loop = asyncio.get_event_loop()

    def _run():
        engine = pyttsx3.init()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp = f.name
        engine.save_to_file(text, tmp)
        engine.runAndWait()
        with open(tmp, "rb") as f:
            data = f.read()
        os.unlink(tmp)
        return data

    return await loop.run_in_executor(None, _run)


def _pcm_to_wav(raw_pcm: bytes, sample_rate: int = 22050) -> bytes:
    """Wrap raw 16-bit PCM bytes in a minimal WAV header."""
    import struct
    data_size = len(raw_pcm)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_size, b"WAVE", b"fmt ",
        16, 1, 1, sample_rate, sample_rate * 2, 2, 16,
        b"data", data_size,
    )
    return header + raw_pcm


# ──────────────────────────────────────────────────────────────────────────────
# Full conversation turn
# ──────────────────────────────────────────────────────────────────────────────

async def process_voice_turn(
    audio_bytes: bytes | None = None,
    text_input: str | None = None,
    http_client=None,
    telegram_forward: bool = False,
) -> dict:
    """Complete voice interaction turn: STT → skill routing → TTS → state broadcast.

    Either audio_bytes or text_input must be provided.
    Returns dict with: transcript, response_text, audio_b64 (base64 WAV or None).
    """
    import base64

    # 1. Transcribe
    await set_state(VoiceState.LISTENING)
    transcript = text_input
    if audio_bytes and not transcript:
        transcript = await transcribe_audio(audio_bytes)
    if not transcript:
        fallback_text = "I couldn't catch that. Please try again, a little closer to the microphone."
        await set_state(VoiceState.RESPONDING)
        audio_bytes_out = await synthesize_speech(fallback_text)
        audio_b64 = base64.b64encode(audio_bytes_out).decode() if audio_bytes_out else None
        await asyncio.sleep(0.4)
        await set_state(VoiceState.SLEEPING)
        return {
            "error": "Could not transcribe audio",
            "transcript": None,
            "response_text": fallback_text,
            "audio_b64": audio_b64,
        }

    # 2. Think
    await set_state(VoiceState.THINKING)
    response_text = await generate_voice_response(transcript, http_client=http_client)

    # 3. Speak
    await set_state(VoiceState.RESPONDING)
    audio_bytes_out = await synthesize_speech(response_text)
    audio_b64 = base64.b64encode(audio_bytes_out).decode() if audio_bytes_out else None

    # 4. Optionally forward to Telegram
    if telegram_forward:
        await _forward_to_telegram(transcript, response_text)

    # 5. Return to sleeping
    await asyncio.sleep(0.8)
    await set_state(VoiceState.SLEEPING)

    return {
        "transcript": transcript,
        "response_text": response_text,
        "audio_b64": audio_b64,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Telegram forwarding (stub, to be configured)
# ──────────────────────────────────────────────────────────────────────────────

async def _forward_to_telegram(question: str, answer: str) -> None:
    """Send a voice interaction pair to a Telegram bot."""
    from app.core.config import settings
    token = getattr(settings, "telegram_bot_token", None)
    chat_id = getattr(settings, "telegram_chat_id", None)
    if not token or not chat_id:
        logger.debug("telegram_not_configured — skipping forward")
        return
    try:
        import httpx as _httpx
        msg = f"🎙 *AEGIS Interaction*\n\n*You:* {question}\n\n*AEGIS:* {answer}"
        async with _httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
            )
    except Exception as e:
        logger.warning("telegram_forward_failed", extra={"error": str(e)})
