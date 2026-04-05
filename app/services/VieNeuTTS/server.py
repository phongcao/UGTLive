"""FastAPI server for VieNeu-TTS service using the vieneu Python SDK."""

import sys
import os
import io
import time
import asyncio
import wave
from pathlib import Path
from contextlib import asynccontextmanager

import tempfile

import uvicorn
import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response, JSONResponse, StreamingResponse
from pydantic import BaseModel
from typing import Optional

# Add shared folder to path
shared_dir = Path(__file__).parent.parent / "shared"
sys.path.insert(0, str(shared_dir))

from config_parser import parse_service_config, get_config_value

# Load service configuration
config_path = Path(__file__).parent / "service_config.txt"
SERVICE_CONFIG = parse_service_config(str(config_path))

SERVICE_NAME = get_config_value(SERVICE_CONFIG, 'service_name', 'VieNeuTTS')
SERVICE_PORT = int(get_config_value(SERVICE_CONFIG, 'port', '8001'))
SERVICE_INSTALL_VERSION = get_config_value(SERVICE_CONFIG, 'service_install_version', '1')

# Global TTS instance
TTS_MODEL = None
AVAILABLE_VOICES = []


def load_model():
    """Load the VieNeu-TTS model."""
    global TTS_MODEL

    from vieneu import Vieneu

    print("Loading VieNeu-TTS model (Turbo mode - CPU)...")
    start_time = time.time()

    TTS_MODEL = Vieneu()  # Defaults to Turbo mode

    elapsed = time.time() - start_time
    print(f"  Model loaded in {elapsed:.1f}s")
    return TTS_MODEL


def discover_voices():
    """Discover available preset voices from the loaded model."""
    global AVAILABLE_VOICES, TTS_MODEL

    if TTS_MODEL is None:
        return

    try:
        voices = TTS_MODEL.list_preset_voices()
        if voices:
            AVAILABLE_VOICES = []
            if isinstance(voices[0], tuple):
                for desc, vid in voices:
                    AVAILABLE_VOICES.append({"id": vid, "name": desc})
            else:
                for vid in voices:
                    AVAILABLE_VOICES.append({"id": vid, "name": vid})
            print(f"  Discovered {len(AVAILABLE_VOICES)} preset voices")
        else:
            print("  No preset voices found")
    except Exception as e:
        print(f"  Error discovering voices: {e}")


def float32_to_pcm16(audio_float):
    """Convert float32 [-1, 1] to int16 bytes."""
    audio_array = np.asarray(audio_float, dtype=np.float32)
    audio_array = np.clip(audio_array, -1.0, 1.0)
    return (audio_array * 32767).clip(-32768, 32767).astype(np.int16).tobytes()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Pre-load VieNeu-TTS model at startup."""
    print("=" * 60)
    print("PRE-LOADING VIENEU-TTS MODEL AT STARTUP")
    print("=" * 60)

    try:
        load_model()
        discover_voices()
        print("[OK] VieNeu-TTS model loaded successfully")
    except Exception as e:
        print(f"[FAIL] Failed to load VieNeu-TTS model: {e}")
        import traceback
        traceback.print_exc()
        raise RuntimeError("VieNeu-TTS startup aborted: model initialization failed") from e

    print("[OK] Service is ready for requests!")
    print("=" * 60)

    yield

    # Cleanup
    global TTS_MODEL
    if TTS_MODEL is not None:
        try:
            TTS_MODEL.close()
        except Exception:
            pass
        TTS_MODEL = None


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


@app.post("/clone")
async def clone_voice(
    text: str = Form(...),
    voice_id: Optional[str] = Form(None),
    reference_audio: UploadFile = File(...),
):
    """Synthesize with a cloned voice from reference audio (3-5s WAV/MP3/FLAC)."""
    global TTS_MODEL

    if TTS_MODEL is None:
        raise HTTPException(status_code=503, detail="Model not loaded yet")

    if not text or not text.strip():
        raise HTTPException(status_code=400, detail="No text provided")

    # Save uploaded audio to a temp file for encode_reference
    suffix = Path(reference_audio.filename).suffix if reference_audio.filename else ".wav"
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(await reference_audio.read())
            tmp_path = tmp.name

        start_time = time.time()
        voice_data = TTS_MODEL.encode_reference(tmp_path)
        audio = TTS_MODEL.infer(text=text, voice=voice_data)
        elapsed = time.time() - start_time

        text_preview = text[:60] + "..." if len(text) > 60 else text
        print(f"Clone TTS generated in {elapsed:.2f}s, text='{text_preview}'")

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
            headers={"Content-Disposition": "attachment; filename=tts_clone_output.wav"}
        )
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error in voice cloning TTS: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


async def _synthesize_audio(text: str, voice_id: Optional[str] = None):
    """Synthesize audio from text and return as WAV."""
    global TTS_MODEL

    if TTS_MODEL is None:
        raise HTTPException(status_code=503, detail="Model not loaded yet")

    if not text or not text.strip():
        raise HTTPException(status_code=400, detail="No text provided")

    voice_data = None
    if voice_id:
        try:
            voice_data = TTS_MODEL.get_preset_voice(voice_id)
        except Exception:
            print(f"Voice '{voice_id}' not found, using default.")

    try:
        start_time = time.time()
        text_preview = text[:60] + "..." if len(text) > 60 else text

        audio = TTS_MODEL.infer(text=text, voice=voice_data)

        elapsed = time.time() - start_time
        print(f"TTS generated in {elapsed:.2f}s for voice={voice_id or 'default'}, text='{text_preview}'")

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

    except Exception as e:
        print(f"Error generating TTS: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/info")
async def get_info():
    """Get service information."""
    info = {
        "service_name": SERVICE_NAME,
        "description": get_config_value(SERVICE_CONFIG, 'description', ''),
        "service_install_version": SERVICE_INSTALL_VERSION,
        "venv_name": get_config_value(SERVICE_CONFIG, 'venv_name', 'ugt_vieneutts'),
        "port": SERVICE_PORT,
        "server_url": get_config_value(SERVICE_CONFIG, 'server_url', 'http://127.0.0.1'),
        "local_only": get_config_value(SERVICE_CONFIG, 'local_only', 'true') == 'true',
        "github_url": get_config_value(SERVICE_CONFIG, 'github_url', ''),
        "service_author": get_config_value(SERVICE_CONFIG, 'service_author', ''),
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
    print(f"Configuration: {SERVICE_CONFIG}")

    uvicorn.run(
        app,
        host=host,
        port=SERVICE_PORT,
        log_level="info",
        access_log=True
    )
