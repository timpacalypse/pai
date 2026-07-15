"""AEGIS Pi Voice Agent — always-on voice interface service.

Runs on the Raspberry Pi. Responsibilities:
  1. Continuously listen for the wake word "aegis" using openWakeWord
  2. On wake: record audio until silence
  3. POST audio (or text) to PAI orchestrator /voice/turn endpoint
  4. Receive response text + audio_b64 WAV
  5. Play TTS audio through speaker
  6. Return to wake-word listening

The AEGIS orb display (aegis.html) receives state changes automatically via
the orchestrator's WebSocket (/voice/ws) — this agent just drives the voice loop.

Setup:
  1. Copy this directory to the Pi
  2. pip install -r requirements.txt
  3. Download an openWakeWord model: aegis.onnx (or use the built-in hey_jarvis)
  4. Copy .env.example to .env and fill in PAI_HOST
  5. sudo systemctl enable/start aegis-agent (use aegis-agent.service)
  6. python agent.py  (or let systemd manage it)
"""

import asyncio
import base64
import io
import logging
import os
import sys
import tempfile
import time
import wave
from pathlib import Path

import httpx
import numpy as np
import sounddevice as sd
import soundfile as sf
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# ── Configuration ─────────────────────────────────────────────────────────────

PAI_HOST = os.environ.get("PAI_HOST", "http://192.168.1.100:8000")
SAMPLE_RATE = 16000          # Hz — whisper and wake-word both expect 16kHz
CHANNELS = 1
CHUNK_SECONDS = 0.08         # 80ms chunks for wake-word inference
SILENCE_THRESHOLD = 0.006    # RMS below this = silence
SILENCE_AFTER_SPEECH = 1.6   # seconds of silence before stopping recording
MAX_RECORD_SECONDS = 30      # safety cap on recording
WAKE_WORD = os.environ.get("WAKE_WORD", "aegis")
# openWakeWord fallback (if Vosk model dir not present)
WAKE_MODEL = os.environ.get("WAKE_MODEL", "hey_jarvis")
WAKE_THRESHOLD = float(os.environ.get("WAKE_THRESHOLD", "0.5"))


def _parse_audio_device(raw: str | None):
    """Normalize AUDIO_DEVICE env var for sounddevice.

    - Empty/unset => None (system default)
    - Integer strings => int device index
    - Other strings => name query
    """
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    if value.isdigit() or (value.startswith("-") and value[1:].isdigit()):
        return int(value)
    return value


AUDIO_DEVICE = _parse_audio_device(os.environ.get("AUDIO_DEVICE"))  # None = system default
TELEGRAM_FORWARD = os.environ.get("TELEGRAM_FORWARD", "0") == "1"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("aegis.agent")

# ── Wake word detectors ──────────────────────────────────────────────────────
# Priority: Vosk (primary, no signup) → openWakeWord (fallback) → manual

_vosk_recognizer = None
_oww_model = None


def _get_vosk_recognizer():
    """Load Vosk keyword-grammar recognizer (no signup, runs offline on Pi 3)."""
    global _vosk_recognizer
    if _vosk_recognizer is not None:
        return _vosk_recognizer
    try:
        import vosk
        import json as _json
        vosk.SetLogLevel(-1)  # silence verbose output
        model_dir = Path(__file__).parent / "models" / "vosk-model-small-en-us"
        if not model_dir.exists():
            raise FileNotFoundError(
                f"Vosk model not found at {model_dir}.\n"
                "Download from https://alphacephei.com/vosk/models\n"
                "  wget https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip\n"
                "  unzip vosk-model-small-en-us-0.15.zip -d models/\n"
                "  mv models/vosk-model-small-en-us-0.15 models/vosk-model-small-en-us"
            )
        model = vosk.Model(str(model_dir))
        # Grammar mode: only listen for the wake word + unknown tokens — very fast
        grammar = _json.dumps([WAKE_WORD.lower(), "[unk]"])
        _vosk_recognizer = vosk.KaldiRecognizer(model, SAMPLE_RATE, grammar)
        logger.info(f"Vosk wake-word recognizer loaded (grammar: {WAKE_WORD})")
    except Exception as e:
        logger.warning(f"Vosk not available ({e})")
        _vosk_recognizer = None
    return _vosk_recognizer


def _get_oww_model():
    """Try to load openWakeWord as fallback detector."""
    global _oww_model
    if _oww_model is not None:
        return _oww_model
    try:
        import openwakeword
        from openwakeword.model import Model
        is_custom_path = WAKE_MODEL.endswith(".onnx") or "/" in WAKE_MODEL or WAKE_MODEL.startswith(".")
        if is_custom_path:
            wake_model_path = Path(WAKE_MODEL)
            if not wake_model_path.is_absolute():
                wake_model_path = (Path(__file__).parent / WAKE_MODEL).resolve()
            if not wake_model_path.exists():
                raise FileNotFoundError(f"Wake model not found at {wake_model_path}.")
            _oww_model = Model(wakeword_models=[str(wake_model_path)], inference_framework="onnx")
        else:
            openwakeword.utils.download_models()
            _oww_model = Model(wakeword_models=[WAKE_MODEL], inference_framework="onnx")
        logger.info(f"openWakeWord model loaded: {WAKE_MODEL}")
    except Exception as e:
        logger.warning(f"openWakeWord not available ({e})")
        _oww_model = None
    return _oww_model


# ── Audio utilities ────────────────────────────────────────────────────────────

def _rms(chunk: np.ndarray) -> float:
    return float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))


def _normalize_for_stt(audio: np.ndarray) -> np.ndarray:
    """Lightweight gain normalize for quiet mic input.

    Keeps peaks below clipping while nudging RMS into a range Whisper can parse
    more reliably on low-gain USB mics.
    """
    if audio.size == 0:
        return audio

    peak = float(np.max(np.abs(audio)))
    rms = _rms(audio)
    if peak <= 1e-6 or rms <= 1e-6:
        return audio

    target_rms = 0.08
    gain = target_rms / rms
    max_gain_no_clip = 0.95 / peak
    gain = max(1.0, min(gain, max_gain_no_clip, 12.0))
    return np.clip(audio * gain, -1.0, 1.0).astype(np.float32)


def _np_to_wav_bytes(audio: np.ndarray, rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(rate)
        wf.writeframes((audio * 32767).astype(np.int16).tobytes())
    buf.seek(0)
    return buf.read()


def record_until_silence(
    max_seconds: float = MAX_RECORD_SECONDS,
    silence_after: float = SILENCE_AFTER_SPEECH,
) -> np.ndarray:
    """Record from microphone until silence is detected."""
    logger.info("Recording started — speak now")
    frames = []
    silent_chunks = 0
    speaking = False
    chunks_per_second = int(1.0 / CHUNK_SECONDS)
    silence_chunk_limit = int(silence_after * chunks_per_second)
    max_chunks = int(max_seconds * chunks_per_second)

    chunk_size = int(SAMPLE_RATE * CHUNK_SECONDS)

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="float32",
        blocksize=chunk_size,
        device=AUDIO_DEVICE,
    ) as stream:
        for _ in range(max_chunks):
            chunk, _ = stream.read(chunk_size)
            rms = _rms(chunk)
            frames.append(chunk.copy())
            if rms > SILENCE_THRESHOLD:
                speaking = True
                silent_chunks = 0
            elif speaking:
                silent_chunks += 1
                if silent_chunks >= silence_chunk_limit:
                    break

    audio = np.concatenate(frames, axis=0).flatten()
    duration = len(audio) / SAMPLE_RATE
    logger.info(f"Recording complete: {duration:.1f}s (rms={_rms(audio):.4f})")
    return audio


# ── PAI orchestrator calls ────────────────────────────────────────────────────

async def call_voice_turn(audio: np.ndarray) -> dict | None:
    """POST audio to PAI orchestrator /voice/turn, get response."""
    audio = _normalize_for_stt(audio)
    wav_bytes = _np_to_wav_bytes(audio)
    async with httpx.AsyncClient(timeout=180.0) as client:  # long: whisper cold-starts can take >60s
        try:
            resp = await client.post(
                f"{PAI_HOST}/voice/turn",
                files={"audio": ("recording.wav", wav_bytes, "audio/wav")},
                data={"telegram": "1" if TELEGRAM_FORWARD else "0"},
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            logger.error(f"PAI orchestrator call failed: {e}")
            return None


async def notify_wake() -> None:
    """Tell orchestrator we heard the wake word."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(f"{PAI_HOST}/voice/wake")
    except Exception:
        pass


async def notify_sleep() -> None:
    """Tell orchestrator we returned to dormant."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(f"{PAI_HOST}/voice/sleep")
    except Exception:
        pass


# ── TTS playback ──────────────────────────────────────────────────────────────

def play_audio_b64(audio_b64: str) -> None:
    """Decode base64 WAV from orchestrator TTS and play through speaker."""
    try:
        import pygame
        wav_bytes = base64.b64decode(audio_b64)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav_bytes)
            tmp = f.name
        pygame.mixer.init(frequency=22050, size=-16, channels=1, buffer=1024)
        pygame.mixer.music.load(tmp)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            time.sleep(0.05)
        pygame.mixer.quit()
        os.unlink(tmp)
    except Exception as e:
        logger.warning(f"Audio playback failed: {e}")


def speak_fallback(text: str) -> None:
    """Fallback TTS using espeak directly (always available on Pi)."""
    try:
        import subprocess
        subprocess.run(
            ["espeak-ng", "-v", "en-us", "-s", "145", text],
            check=True,
            timeout=15,
        )
    except Exception as e:
        logger.warning(f"espeak fallback failed: {e}")


def play_response(result: dict) -> None:
    """Play TTS audio or fall back to espeak."""
    if result.get("audio_b64"):
        play_audio_b64(result["audio_b64"])
    elif result.get("response_text"):
        speak_fallback(result["response_text"])


# ── Wake-word detection loop ──────────────────────────────────────────────────

async def listen_for_wake_word() -> bool:
    """Block until wake word detected. Priority: Vosk → openWakeWord → manual."""
    import json as _json
    loop = asyncio.get_event_loop()
    chunk_size = int(SAMPLE_RATE * CHUNK_SECONDS)

    # ─ Vosk (primary — no signup, offline, low CPU) ─────────────────────────
    vosk_rec = _get_vosk_recognizer()
    if vosk_rec is not None:
        logger.info(f"Listening for wake word '{WAKE_WORD}' via Vosk ...")
        buf_size = 4000  # ~0.25s at 16kHz
        with sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=buf_size,
            device=AUDIO_DEVICE,
        ) as stream:
            while True:
                data, _ = await loop.run_in_executor(None, stream.read, buf_size)
                if vosk_rec.AcceptWaveform(bytes(data)):
                    text = _json.loads(vosk_rec.Result()).get("text", "")
                    if WAKE_WORD.lower() in text.lower():
                        logger.info(f"Wake word detected via Vosk: '{text}'")
                        return True
                else:
                    partial = _json.loads(vosk_rec.PartialResult()).get("partial", "")
                    if WAKE_WORD.lower() in partial.lower():
                        logger.info(f"Wake word detected via Vosk (partial): '{partial}'")
                        vosk_rec.Reset()
                        return True

    # ─ openWakeWord (fallback) ───────────────────────────────────────────────
    oww = _get_oww_model()
    if oww is not None:
        logger.info(f"Listening for wake word '{WAKE_WORD}' via openWakeWord ...")
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=chunk_size,
            device=AUDIO_DEVICE,
        ) as stream:
            while True:
                chunk, _ = await loop.run_in_executor(None, stream.read, chunk_size)
                scores = oww.predict(chunk.flatten())
                score = max(scores.values()) if scores else 0.0
                if score >= WAKE_THRESHOLD:
                    logger.info(f"Wake word detected (openWakeWord score={score:.2f})")
                    return True

    # ─ Manual fallback ───────────────────────────────────────────────────────
    logger.warning("No wake-word model available — press ENTER to simulate wake")
    await loop.run_in_executor(None, input)
    return True


# ── Main conversation loop ────────────────────────────────────────────────────

async def run():
    logger.info(f"AEGIS voice agent started — PAI host: {PAI_HOST}")
    # Warm up detectors at startup so first wake has no load delay
    if _get_vosk_recognizer():
        logger.info("Wake backend: Vosk")
    elif _get_oww_model():
        logger.info("Wake backend: openWakeWord")
    else:
        logger.warning("Wake backend: manual (press ENTER to wake)")

    while True:
        try:
            # Phase 1: Wait for wake word
            detected = await listen_for_wake_word()
            if not detected:
                continue

            # Phase 2: Signal orchestrator (orb wakes up)
            await notify_wake()

            # Phase 3: Record speech
            loop = asyncio.get_event_loop()
            audio = await loop.run_in_executor(None, record_until_silence)

            # Phase 4: Send to PAI, get response
            result = await call_voice_turn(audio)
            if not result:
                logger.warning("No response from PAI")
                await notify_sleep()
                continue

            if result.get("error"):
                # Even on STT errors, backend can return a usable fallback message.
                logger.warning(f"PAI returned error: {result.get('error')}")
                if result.get("response_text") or result.get("audio_b64"):
                    play_response(result)
                await notify_sleep()
                continue

            logger.info(f"Transcript: {result.get('transcript', '')}")
            logger.info(f"Response: {result.get('response_text', '')[:120]}")

            # Phase 5: Play response audio
            play_response(result)

            # Phase 6: Return to sleep (orchestrator already handled this in /voice/turn)

        except KeyboardInterrupt:
            logger.info("AEGIS agent stopped by user")
            await notify_sleep()
            break
        except Exception as e:
            logger.error(f"Voice turn failed: {e}", exc_info=True)
            await notify_sleep()
            await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(run())
