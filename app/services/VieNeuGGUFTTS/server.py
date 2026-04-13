"""FastAPI server for VieNeu-GGUF-TTS service using LM Studio for inference."""

import sys
import os
import io
import re
import time
import asyncio
import wave
from pathlib import Path
from contextlib import asynccontextmanager

import uvicorn
import httpx
import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, JSONResponse
from pydantic import BaseModel
from typing import Optional

# Add shared folder to path
shared_dir = Path(__file__).parent.parent / "shared"
sys.path.insert(0, str(shared_dir))

from config_parser import parse_service_config, get_config_value

# Load service configuration
config_path = Path(__file__).parent / "service_config.txt"
SERVICE_CONFIG = parse_service_config(str(config_path))

SERVICE_NAME = get_config_value(SERVICE_CONFIG, 'service_name', 'VieNeuGGUFTTS')
SERVICE_PORT = int(get_config_value(SERVICE_CONFIG, 'port', '5008'))
SERVICE_INSTALL_VERSION = get_config_value(SERVICE_CONFIG, 'service_install_version', '1')
LM_STUDIO_URL = get_config_value(SERVICE_CONFIG, 'lm_studio_url', 'http://127.0.0.1:1234')
LM_STUDIO_MODEL = get_config_value(SERVICE_CONFIG, 'lm_studio_model', '')

# VieNeu-TTS constants
STANDARD_REPO = "nguyen-brat/VieNeu-TTS-Vietnamese-Finetuned"
TURBO_REPO = "pnnbao-ump/VieNeu-TTS-v2-Turbo-GGUF"
CODEC_REPO = "pnnbao-ump/VieNeu-Codec"
SPEECH_MAX    = 65535

# Global references
VIENEU_MODEL = None
AVAILABLE_VOICES = []
TURBO_VOICE_EMBEDDINGS = {}   # {voice_name: np.ndarray shape (1, 128)}
DEFAULT_TURBO_EMBEDDING = None

# Regex to extract speech token numbers from generated text
SPEECH_TOKEN_RE = re.compile(r"<\|speech_(\d+)\|>")


def load_components():
    """Load VieNeu codec (decoder/encoder), phonemizer, and voices.

    The LLM backbone is handled by LM Studio, so we skip backbone loading
    by constructing TurboVieNeuTTS manually and only loading the codec + voices.
    """
    global VIENEU_MODEL

    from vieneu.turbo import TurboVieNeuTTS, BaseVieneuTTS

    print("Loading VieNeu-TTS components (codec + phonemizer + voices, NO backbone)...")
    start_time = time.time()

    # Construct model without loading backbone — call BaseVieneuTTS.__init__
    # then load only decoder, encoder, and voices.
    model = TurboVieNeuTTS.__new__(TurboVieNeuTTS)
    BaseVieneuTTS.__init__(model)
    model.backbone = None
    model.decoder_sess = None
    model.encoder_sess = None
    model._is_onnx_codec = True
    model.max_context = 4096
    model.device = "cpu"

    # Load only the ONNX codec (decoder + encoder)
    model._load_decoder(CODEC_REPO, "vieneu_decoder.onnx", "cpu", None)
    model._load_encoder(CODEC_REPO, "vieneu_encoder.onnx", "cpu", None)

    VIENEU_MODEL = model

    elapsed = time.time() - start_time
    print(f"  VieNeu components loaded in {elapsed:.1f}s")


def load_turbo_voice_embeddings():
    """Load 128-dim voice embeddings from the turbo model repo for the ONNX decoder."""
    global TURBO_VOICE_EMBEDDINGS, DEFAULT_TURBO_EMBEDDING

    import json
    from huggingface_hub import hf_hub_download

    try:
        voices_path = hf_hub_download(repo_id=TURBO_REPO, filename="voices.json")
        with open(voices_path, 'r', encoding='utf-8') as f:
            voices_data = json.load(f)

        default_name = voices_data.get("default_voice", "")
        presets = voices_data.get("presets", {})

        for name, data in presets.items():
            codes = data.get("codes", [])
            if codes and isinstance(codes[0], float):
                emb = np.array(codes, dtype=np.float32)
                if emb.shape[0] == 128:
                    TURBO_VOICE_EMBEDDINGS[name] = emb[np.newaxis, :]

        if default_name and default_name in TURBO_VOICE_EMBEDDINGS:
            DEFAULT_TURBO_EMBEDDING = TURBO_VOICE_EMBEDDINGS[default_name]
        elif TURBO_VOICE_EMBEDDINGS:
            DEFAULT_TURBO_EMBEDDING = next(iter(TURBO_VOICE_EMBEDDINGS.values()))

        print(f"  Loaded {len(TURBO_VOICE_EMBEDDINGS)} turbo voice embeddings")
    except Exception as e:
        print(f"  Warning: Could not load turbo voice embeddings: {e}")


def find_turbo_embedding(voice_id: Optional[str] = None) -> Optional[np.ndarray]:
    """Find the best matching turbo voice embedding for the given voice."""
    if not TURBO_VOICE_EMBEDDINGS:
        return DEFAULT_TURBO_EMBEDDING

    # Exact match
    if voice_id and voice_id in TURBO_VOICE_EMBEDDINGS:
        return TURBO_VOICE_EMBEDDINGS[voice_id]

    # Partial match (standard voice names are shortened, e.g. 'Vinh' vs 'Xuân Vĩnh')
    if voice_id:
        voice_lower = voice_id.lower()
        for name, emb in TURBO_VOICE_EMBEDDINGS.items():
            if voice_lower in name.lower() or name.lower() in voice_lower:
                return emb

    return DEFAULT_TURBO_EMBEDDING


def discover_voices():
    """Populate available voices from the turbo voice embeddings."""
    global AVAILABLE_VOICES, TURBO_VOICE_EMBEDDINGS

    if TURBO_VOICE_EMBEDDINGS:
        AVAILABLE_VOICES = [{"id": name, "name": name} for name in TURBO_VOICE_EMBEDDINGS]
        print(f"  Discovered {len(AVAILABLE_VOICES)} turbo voices")
    else:
        AVAILABLE_VOICES = [{"id": "", "name": "Default"}]
        print("  No turbo voices found, using default")


def float32_to_pcm16(audio_float):
    """Convert float32 [-1, 1] to int16 bytes."""
    audio_array = np.asarray(audio_float, dtype=np.float32)
    audio_array = np.clip(audio_array, -1.0, 1.0)
    return (audio_array * 32767).clip(-32768, 32767).astype(np.int16).tobytes()


def build_prompt(text: str, voice_id: Optional[str] = None) -> tuple[str, Optional[np.ndarray]]:
    """Build the v2 Turbo LLM prompt for speech generation.

    Returns tuple of (prompt_string, voice_embedding).
    The v2 Turbo model uses <|speaker_16|> token + phonemized text,
    with voice identity handled via embedding to the ONNX decoder.
    """
    from vieneu_utils.phonemize_text import phonemize_text

    # Phonemize using the sea_g2p pipeline (includes normalization)
    phonemes = phonemize_text(text)

    # V2 Turbo prompt format: speaker token + phonemes
    prompt = (
        f"<|speaker_16|>"
        f"<|TEXT_PROMPT_START|>{phonemes}<|TEXT_PROMPT_END|>"
        f"<|SPEECH_GENERATION_START|>"
    )

    # Get the turbo voice embedding for the ONNX decoder
    voice_embedding = find_turbo_embedding(voice_id)

    return prompt, voice_embedding


async def generate_speech_tokens_via_lm_studio(prompt: str) -> list[int]:
    """Send prompt to LM Studio and extract speech token IDs from the response."""

    completions_url = f"{LM_STUDIO_URL}/v1/completions"

    payload = {
        "prompt": prompt,
        "max_tokens": 1024,
        "temperature": 0.4,
        "top_k": 50,
        "top_p": 0.95,
        "repeat_penalty": 1.15,
        "stop": ["<|SPEECH_GENERATION_END|>"],
        "stream": False,
    }

    if LM_STUDIO_MODEL:
        payload["model"] = LM_STUDIO_MODEL

    async with httpx.AsyncClient(timeout=300.0) as client:
        response = await client.post(completions_url, json=payload)
        response.raise_for_status()

    result = response.json()
    generated_text = result["choices"][0]["text"]

    # Parse speech tokens from the generated text (raw codec codes, no offset)
    speech_ids = []
    for match in SPEECH_TOKEN_RE.finditer(generated_text):
        code = int(match.group(1))
        if 0 <= code <= SPEECH_MAX:
            speech_ids.append(code)

    return speech_ids


def decode_speech_tokens(speech_ids: list[int], voice_embedding: Optional[np.ndarray] = None) -> np.ndarray:
    """Decode speech token IDs to audio waveform using VieNeu codec."""
    global VIENEU_MODEL

    decode_str = "".join(f"<|speech_{tid}|>" for tid in speech_ids)
    audio = VIENEU_MODEL._decode(decode_str, voice_embedding)
    return audio


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Pre-load VieNeu components and verify LM Studio connectivity at startup."""
    print("=" * 60)
    print("PRE-LOADING VIENEU-GGUF-TTS COMPONENTS AT STARTUP")
    print(f"LM Studio URL: {LM_STUDIO_URL}")
    print("=" * 60)

    try:
        load_components()
        load_turbo_voice_embeddings()
        discover_voices()
        print("[OK] VieNeu components loaded successfully")
    except Exception as e:
        print(f"[FAIL] Failed to load VieNeu components: {e}")
        import traceback
        traceback.print_exc()
        raise RuntimeError("VieNeu-GGUF-TTS startup aborted: component initialization failed") from e

    # Check LM Studio connectivity
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{LM_STUDIO_URL}/v1/models")
            resp.raise_for_status()
            models = resp.json()
            model_ids = [m.get("id", "unknown") for m in models.get("data", [])]
            print(f"[OK] LM Studio is reachable, loaded models: {model_ids}")
    except Exception as e:
        print(f"[WARN] Could not reach LM Studio at {LM_STUDIO_URL}: {e}")
        print("  The service will start, but TTS requests will fail until LM Studio is running.")

    print("[OK] Service is ready for requests!")
    print("=" * 60)

    yield

    # Cleanup
    global VIENEU_MODEL
    if VIENEU_MODEL is not None:
        try:
            VIENEU_MODEL.close()
        except Exception:
            pass
        VIENEU_MODEL = None


app = FastAPI(title=SERVICE_NAME, version=SERVICE_INSTALL_VERSION, lifespan=lifespan)


class TTSRequest(BaseModel):
    text: str
    voice_id: Optional[str] = None


@app.get("/voices")
async def get_voices():
    """Return list of available preset voices."""
    if not AVAILABLE_VOICES:
        return [{"id": "", "name": "Default"}]
    return AVAILABLE_VOICES


@app.get("/stream")
async def stream_audio_get(text: str, voice_id: Optional[str] = None):
    """Streaming TTS endpoint (GET). Returns audio/wav."""
    return await _synthesize_audio(text, voice_id)


@app.post("/stream")
async def stream_audio_post(req: TTSRequest):
    """Streaming TTS endpoint (POST). Returns audio/wav."""
    return await _synthesize_audio(req.text, req.voice_id)


@app.post("/tts")
async def tts_endpoint(req: TTSRequest):
    """Non-streaming TTS endpoint. Returns audio/wav."""
    return await _synthesize_audio(req.text, req.voice_id)


async def _synthesize_audio(text: str, voice_id: Optional[str] = None):
    """Synthesize audio from text via LM Studio and return as WAV."""
    global VIENEU_MODEL

    if VIENEU_MODEL is None:
        raise HTTPException(status_code=503, detail="Model components not loaded yet")

    if not text or not text.strip():
        raise HTTPException(status_code=400, detail="No text provided")

    try:
        start_time = time.time()
        text_preview = text[:60] + "..." if len(text) > 60 else text

        # Step 1: Build prompt
        prompt, voice_embedding = build_prompt(text, voice_id)
        prompt_time = time.time() - start_time

        # Step 2: Generate speech tokens via LM Studio
        gen_start = time.time()
        speech_ids = await generate_speech_tokens_via_lm_studio(prompt)
        gen_time = time.time() - gen_start

        if not speech_ids:
            raise HTTPException(status_code=500, detail="LM Studio returned no speech tokens")

        # Step 3: Decode to audio
        decode_start = time.time()
        audio = decode_speech_tokens(speech_ids, voice_embedding)
        decode_time = time.time() - decode_start

        elapsed = time.time() - start_time
        duration = len(audio) / 24000
        print(
            f"TTS generated in {elapsed:.2f}s "
            f"(prompt={prompt_time:.2f}s, gen={gen_time:.2f}s [{len(speech_ids)} tokens], "
            f"decode={decode_time:.2f}s), "
            f"audio={duration:.1f}s, "
            f"voice={voice_id or 'default'}, text='{text_preview}'"
        )

        # Convert to WAV bytes
        pcm_data = float32_to_pcm16(audio)
        sample_rate = 24000

        buf = io.BytesIO()
        with wave.open(buf, 'wb') as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(pcm_data)
        buf.seek(0)

        return Response(
            content=buf.read(),
            media_type="audio/wav",
            headers={"Content-Disposition": "attachment; filename=tts_output.wav"}
        )

    except HTTPException:
        raise
    except httpx.HTTPStatusError as e:
        print(f"LM Studio API error: {e.response.status_code} - {e.response.text[:200]}")
        raise HTTPException(status_code=502, detail=f"LM Studio API error: {e.response.status_code}")
    except httpx.ConnectError:
        print(f"Cannot connect to LM Studio at {LM_STUDIO_URL}")
        raise HTTPException(
            status_code=502,
            detail=f"Cannot connect to LM Studio at {LM_STUDIO_URL}. Is it running?"
        )
    except Exception as e:
        print(f"Error generating TTS: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/info")
async def get_info():
    """Get service information."""
    info = {
        "service_name": SERVICE_NAME,
        "description": get_config_value(SERVICE_CONFIG, 'description', ''),
        "service_install_version": SERVICE_INSTALL_VERSION,
        "venv_name": get_config_value(SERVICE_CONFIG, 'venv_name', 'ugt_vieneugguf'),
        "port": SERVICE_PORT,
        "server_url": get_config_value(SERVICE_CONFIG, 'server_url', 'http://127.0.0.1'),
        "local_only": get_config_value(SERVICE_CONFIG, 'local_only', 'true') == 'true',
        "github_url": get_config_value(SERVICE_CONFIG, 'github_url', ''),
        "service_author": get_config_value(SERVICE_CONFIG, 'service_author', ''),
        "lm_studio_url": LM_STUDIO_URL,
        "available_voices": [v["id"] for v in AVAILABLE_VOICES] if AVAILABLE_VOICES else ["default"],
    }
    return JSONResponse(content=info)


@app.post("/shutdown")
async def shutdown():
    """Shutdown the service."""
    print("Shutdown request received...")

    async def shutdown_task():
        await asyncio.sleep(1)
        os._exit(0)

    asyncio.create_task(shutdown_task())

    return JSONResponse(content={
        "status": "success",
        "message": "Service shutting down"
    })


if __name__ == "__main__":
    host = "127.0.0.1" if get_config_value(SERVICE_CONFIG, 'local_only', 'true') == 'true' else "0.0.0.0"

    print(f"Starting {SERVICE_NAME} service on {host}:{SERVICE_PORT}")
    print(f"LM Studio backend: {LM_STUDIO_URL}")
    print(f"Configuration: {SERVICE_CONFIG}")

    uvicorn.run(
        app,
        host=host,
        port=SERVICE_PORT,
        log_level="info",
        access_log=True
    )
