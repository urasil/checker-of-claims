"""ElevenLabs TTS helpers for debate playback."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from functools import lru_cache
from typing import Any

ELEVENLABS_API_URL = os.getenv("ELEVENLABS_API_URL", "https://api.elevenlabs.io/v1")
ELEVENLABS_MODEL_ID = os.getenv("ELEVENLABS_MODEL_ID")
ELEVENLABS_MAX_CHARS = int(os.getenv("ELEVENLABS_MAX_CHARS", "1200"))

# Default persona list for voice assignment
PERSONA_IDS = (
    "skeptic",
    "pedant",
    "pragmatist",
    "devil_advocate",
    "context_expert",
    "moderator",
)

# Prefer these voice names when available, otherwise fall back to any unique voices.
DEFAULT_VOICE_NAME_MAP: dict[str, str] = {
    "skeptic": "adam",
    "pedant": "rachel",
    "pragmatist": "sam",
    "devil_advocate": "josh",
    "context_expert": "bella",
    "moderator": "antoni",
}


def _require_api_key() -> str:
    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        raise RuntimeError("ELEVENLABS_API_KEY is not set.")
    return api_key


def _request_json(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    api_key = _require_api_key()
    url = f"{ELEVENLABS_API_URL.rstrip('/')}/{path.lstrip('/')}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "xi-api-key": api_key,
        "accept": "application/json",
    }
    if data is not None:
        headers["content-type"] = "application/json"

    req = urllib.request.Request(url=url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec - controlled URL
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8") if exc.fp else str(exc)
        raise RuntimeError(f"ElevenLabs API error: {detail}") from exc


def _request_audio(path: str, payload: dict[str, Any]) -> bytes:
    api_key = _require_api_key()
    url = f"{ELEVENLABS_API_URL.rstrip('/')}/{path.lstrip('/')}"
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "xi-api-key": api_key,
        "accept": "audio/mpeg",
        "content-type": "application/json",
    }
    req = urllib.request.Request(url=url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # nosec - controlled URL
            return resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8") if exc.fp else str(exc)
        raise RuntimeError(f"ElevenLabs TTS error: {detail}") from exc


@lru_cache(maxsize=1)
def _fetch_voices() -> list[dict[str, Any]]:
    data = _request_json("GET", "/voices")
    voices = data.get("voices", [])
    if not isinstance(voices, list):
        return []
    return voices


def _parse_voice_map_from_env() -> dict[str, str]:
    raw = os.getenv("ELEVENLABS_VOICE_MAP")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("ELEVENLABS_VOICE_MAP must be valid JSON.") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("ELEVENLABS_VOICE_MAP must be a JSON object.")
    return {str(k): str(v) for k, v in parsed.items()}


def _voice_id_by_name(voices: list[dict[str, Any]], name: str) -> str | None:
    target = name.strip().lower()
    for voice in voices:
        voice_name = str(voice.get("name", "")).strip().lower()
        if voice_name == target:
            return str(voice.get("voice_id"))
    return None


@lru_cache(maxsize=1)
def resolve_voice_assignments() -> dict[str, str]:
    """Resolve a unique voice_id for each persona."""
    voices = _fetch_voices()
    if not voices:
        raise RuntimeError("No ElevenLabs voices returned.")

    assignments: dict[str, str] = {}
    used: set[str] = set()

    # 1) JSON map from env
    env_map = _parse_voice_map_from_env()
    for persona_id in PERSONA_IDS:
        voice_id = env_map.get(persona_id)
        if voice_id and voice_id not in used:
            assignments[persona_id] = voice_id
            used.add(voice_id)

    # 2) Individual env overrides
    for persona_id in PERSONA_IDS:
        if persona_id in assignments:
            continue
        env_key = f"ELEVENLABS_VOICE_{persona_id.upper()}"
        voice_id = os.getenv(env_key)
        if voice_id and voice_id not in used:
            assignments[persona_id] = voice_id
            used.add(voice_id)

    # 3) Preferred names (if present)
    for persona_id in PERSONA_IDS:
        if persona_id in assignments:
            continue
        preferred = DEFAULT_VOICE_NAME_MAP.get(persona_id)
        if preferred:
            voice_id = _voice_id_by_name(voices, preferred)
            if voice_id and voice_id not in used:
                assignments[persona_id] = voice_id
                used.add(voice_id)

    # 4) Fill with remaining voices (stable order by name)
    remaining = sorted(
        (
            {
                "voice_id": str(v.get("voice_id")),
                "name": str(v.get("name", "")),
            }
            for v in voices
            if v.get("voice_id")
        ),
        key=lambda v: (v["name"].lower(), v["voice_id"]),
    )

    for persona_id in PERSONA_IDS:
        if persona_id in assignments:
            continue
        next_voice = next((v for v in remaining if v["voice_id"] not in used), None)
        if not next_voice:
            raise RuntimeError("Not enough unique ElevenLabs voices to assign.")
        assignments[persona_id] = next_voice["voice_id"]
        used.add(next_voice["voice_id"])

    return assignments


def synthesize_speech(persona_id: str, text: str) -> bytes:
    """Synthesize speech for a persona using ElevenLabs."""
    trimmed = text.strip()
    if not trimmed:
        return b""

    if len(trimmed) > ELEVENLABS_MAX_CHARS:
        trimmed = trimmed[:ELEVENLABS_MAX_CHARS]

    assignments = resolve_voice_assignments()
    voice_id = assignments.get(persona_id)
    if not voice_id:
        raise RuntimeError(f"No voice assignment for persona: {persona_id}")

    payload = {
        "text": trimmed,
        "voice_settings": {
            "stability": 0.4,
            "similarity_boost": 0.7,
        },
    }
    if ELEVENLABS_MODEL_ID:
        payload["model_id"] = ELEVENLABS_MODEL_ID
    return _request_audio(f"/text-to-speech/{voice_id}", payload)
