"""FastAPI server for VieNeu-GGUF-TTS service using LM Studio for inference."""

import sys
import os
import io
import re
import time
import asyncio
import atexit
import logging
import logging.handlers
import faulthandler
import traceback
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
# Model type: 'turbo' for VieNeu-TTS-v2-Turbo-GGUF, 'standard' for VieNeu-TTS-q8-gguf
MODEL_TYPE = get_config_value(SERVICE_CONFIG, 'model_type', 'turbo').lower()
DEFAULT_VOICE_CONFIG = get_config_value(SERVICE_CONFIG, 'default_voice', '')

# ---------------------------------------------------------------------------
# Debug-mode file logging (enabled when launched from Visual Studio debug)
# ---------------------------------------------------------------------------
DEBUG_MODE = os.environ.get("UGTLIVE_VISUAL_STUDIO_DEBUG", "0") == "1"
LOG_DIR = Path(__file__).parent / "logs"
RUNTIME_LOG_PATH = LOG_DIR / "runtime.log"
FAULT_LOG_PATH = LOG_DIR / "fault.log"


def _configure_logging() -> logging.Logger:
    logger = logging.getLogger("vieneu_gguf_tts")
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG if DEBUG_MODE else logging.INFO)
    logger.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | pid=%(process)d | %(message)s"
    )

    # Always log to stdout
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    logger.addHandler(stdout_handler)

    # File logging only in debug mode
    if DEBUG_MODE:
        LOG_DIR.mkdir(parents=True, exist_ok=True)

        file_handler = logging.handlers.RotatingFileHandler(
            RUNTIME_LOG_PATH,
            maxBytes=2 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


LOGGER = _configure_logging()
_FAULT_LOG_FILE = None

if DEBUG_MODE:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _FAULT_LOG_FILE = open(FAULT_LOG_PATH, "a", encoding="utf-8")
    faulthandler.enable(_FAULT_LOG_FILE, all_threads=True)


def _append_fault_log(message: str) -> None:
    if _FAULT_LOG_FILE is None:
        return
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    _FAULT_LOG_FILE.write(f"[{timestamp}] {message}\n")
    _FAULT_LOG_FILE.flush()


def _log_uncaught_exception(exc_type, exc_value, exc_traceback) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    LOGGER.critical("Unhandled exception", exc_info=(exc_type, exc_value, exc_traceback))
    _append_fault_log(
        "Unhandled exception:\n"
        + "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
    )
    sys.__excepthook__(exc_type, exc_value, exc_traceback)


sys.excepthook = _log_uncaught_exception


def _log_process_exit() -> None:
    LOGGER.info("Process exiting pid=%s", os.getpid())
    _append_fault_log(f"Process exiting pid={os.getpid()}")
    if _FAULT_LOG_FILE is not None:
        _FAULT_LOG_FILE.close()


atexit.register(_log_process_exit)

# VieNeu-TTS constants
STANDARD_REPO = "pnnbao-ump/VieNeu-TTS-q8-gguf"
TURBO_REPO = "pnnbao-ump/VieNeu-TTS-v2-Turbo-GGUF"
CODEC_REPO = "pnnbao-ump/VieNeu-Codec"
NEUCODEC_ONNX_REPO = "neuphonic/neucodec-onnx-decoder-int8"
SPEECH_MAX    = 65535

# Global references
VIENEU_MODEL = None
NEUCODEC_DECODER = None  # NeuCodec ONNX decoder for standard model
AVAILABLE_VOICES = []
TURBO_VOICE_EMBEDDINGS = {}   # {voice_name: np.ndarray shape (1, 128)}
DEFAULT_TURBO_EMBEDDING = None

# Standard model voice data: {voice_name: {"codes": list[int], "text": str, "description": str}}
STANDARD_VOICE_PRESETS = {}
DEFAULT_STANDARD_VOICE = None

# Regex to extract speech token numbers from generated text
SPEECH_TOKEN_RE = re.compile(r"<\|speech_(\d+)\|>")


def load_components():
    """Load VieNeu codec (decoder/encoder), phonemizer, and voices.

    The LLM backbone is handled by LM Studio, so we skip backbone loading.
    Loads different codec depending on MODEL_TYPE.
    """
    global VIENEU_MODEL, NEUCODEC_DECODER

    start_time = time.time()

    if MODEL_TYPE == "standard":
        LOGGER.info("Loading Standard VieNeu-TTS components (NeuCodec ONNX decoder via onnxruntime)...")
        import onnxruntime as ort
        from huggingface_hub import hf_hub_download
        model_path = hf_hub_download(repo_id=NEUCODEC_ONNX_REPO, filename="model.onnx")
        NEUCODEC_DECODER = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        LOGGER.info("NeuCodec ONNX decoder loaded from %s", model_path)
        VIENEU_MODEL = True  # Sentinel to indicate components are loaded
    else:
        from vieneu.turbo import TurboVieNeuTTS, BaseVieneuTTS

        LOGGER.info("Loading VieNeu-TTS Turbo components (codec + phonemizer + voices, NO backbone)...")

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
    LOGGER.info("VieNeu components loaded in %.1fs (model_type=%s)", elapsed, MODEL_TYPE)


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

        # Config override takes priority, then voices.json default, then first preset
        if DEFAULT_VOICE_CONFIG and DEFAULT_VOICE_CONFIG in TURBO_VOICE_EMBEDDINGS:
            DEFAULT_TURBO_EMBEDDING = TURBO_VOICE_EMBEDDINGS[DEFAULT_VOICE_CONFIG]
        elif default_name and default_name in TURBO_VOICE_EMBEDDINGS:
            DEFAULT_TURBO_EMBEDDING = TURBO_VOICE_EMBEDDINGS[default_name]
        elif TURBO_VOICE_EMBEDDINGS:
            DEFAULT_TURBO_EMBEDDING = next(iter(TURBO_VOICE_EMBEDDINGS.values()))

        LOGGER.info("Loaded %d turbo voice embeddings", len(TURBO_VOICE_EMBEDDINGS))
    except Exception as e:
        LOGGER.warning("Could not load turbo voice embeddings: %s", e)


def load_standard_voice_presets():
    """Load voice presets (integer code sequences + reference text) for the standard model."""
    global STANDARD_VOICE_PRESETS, DEFAULT_STANDARD_VOICE

    import json
    from huggingface_hub import hf_hub_download

    try:
        voices_path = hf_hub_download(repo_id=STANDARD_REPO, filename="voices.json")
        with open(voices_path, 'r', encoding='utf-8') as f:
            voices_data = json.load(f)

        default_name = voices_data.get("default_voice", "")
        presets = voices_data.get("presets", {})

        for name, data in presets.items():
            codes = data.get("codes", [])
            text = data.get("text", "")
            description = data.get("description", name)
            # Standard voices have integer code sequences (not floats)
            if codes and isinstance(codes[0], int):
                STANDARD_VOICE_PRESETS[name] = {
                    "codes": codes,
                    "text": text,
                    "description": description,
                }

        # Config override takes priority, then voices.json default, then first preset
        if DEFAULT_VOICE_CONFIG and DEFAULT_VOICE_CONFIG in STANDARD_VOICE_PRESETS:
            DEFAULT_STANDARD_VOICE = DEFAULT_VOICE_CONFIG
        elif default_name and default_name in STANDARD_VOICE_PRESETS:
            DEFAULT_STANDARD_VOICE = default_name
        elif STANDARD_VOICE_PRESETS:
            DEFAULT_STANDARD_VOICE = next(iter(STANDARD_VOICE_PRESETS))

        LOGGER.info("Loaded %d standard voice presets (default=%s)",
                    len(STANDARD_VOICE_PRESETS), DEFAULT_STANDARD_VOICE)
    except Exception as e:
        LOGGER.warning("Could not load standard voice presets: %s", e)


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
    """Populate available voices from the loaded voice data."""
    global AVAILABLE_VOICES

    if MODEL_TYPE == "standard":
        if STANDARD_VOICE_PRESETS:
            AVAILABLE_VOICES = [
                {"id": name, "name": data.get("description", name)}
                for name, data in STANDARD_VOICE_PRESETS.items()
            ]
            LOGGER.info("Discovered %d standard voices", len(AVAILABLE_VOICES))
        else:
            AVAILABLE_VOICES = [{"id": "", "name": "Default"}]
            LOGGER.info("No standard voices found, using default")
    else:
        if TURBO_VOICE_EMBEDDINGS:
            AVAILABLE_VOICES = [{"id": name, "name": name} for name in TURBO_VOICE_EMBEDDINGS]
            LOGGER.info("Discovered %d turbo voices", len(AVAILABLE_VOICES))
        else:
            AVAILABLE_VOICES = [{"id": "", "name": "Default"}]
            LOGGER.info("No turbo voices found, using default")


def float32_to_pcm16(audio_float):
    """Convert float32 [-1, 1] to int16 bytes."""
    audio_array = np.asarray(audio_float, dtype=np.float32)
    audio_array = np.clip(audio_array, -1.0, 1.0)
    return (audio_array * 32767).clip(-32768, 32767).astype(np.int16).tobytes()


def build_prompt(text: str, voice_id: Optional[str] = None) -> tuple[str, Optional[np.ndarray]]:
    """Build the LLM prompt for speech generation.

    Returns tuple of (prompt_string, voice_embedding_or_None).
    - Turbo: uses <|speaker_16|> token + phonemized text, voice via embedding.
    - Standard: uses chat-style prompt with reference codes + phonemized ref + target text.
    """
    if MODEL_TYPE == "standard":
        return _build_standard_prompt(text, voice_id)
    else:
        return _build_turbo_prompt(text, voice_id)


def _build_turbo_prompt(text: str, voice_id: Optional[str] = None) -> tuple[str, Optional[np.ndarray]]:
    """Build v2 Turbo prompt."""
    from vieneu_utils.phonemize_text import phonemize_text

    phonemes = phonemize_text(text)

    prompt = (
        f"<|speaker_16|>"
        f"<|TEXT_PROMPT_START|>{phonemes}<|TEXT_PROMPT_END|>"
        f"<|SPEECH_GENERATION_START|>"
    )

    voice_embedding = find_turbo_embedding(voice_id)
    return prompt, voice_embedding


def _build_standard_prompt(text: str, voice_id: Optional[str] = None) -> tuple[str, None]:
    """Build standard model prompt with reference voice codes and phonemized text."""
    from vieneu_utils.phonemize_text import phonemize_with_dict

    # Resolve voice preset
    voice_name = voice_id
    if not voice_name or voice_name not in STANDARD_VOICE_PRESETS:
        voice_name = DEFAULT_STANDARD_VOICE

    if not voice_name or voice_name not in STANDARD_VOICE_PRESETS:
        raise ValueError("No voice preset available for standard model")

    voice_data = STANDARD_VOICE_PRESETS[voice_name]
    ref_codes = voice_data["codes"]
    ref_text = voice_data["text"]

    # Phonemize reference text and target text
    ref_phonemes = phonemize_with_dict(ref_text)
    target_phonemes = phonemize_with_dict(text)

    # Build reference codes string
    codes_str = "".join(f"<|speech_{idx}|>" for idx in ref_codes)

    # Standard prompt format (chat-style, matching the official VieNeu-TTS standard engine)
    prompt = (
        f"user: Convert the text to speech:"
        f"<|TEXT_PROMPT_START|>{ref_phonemes} {target_phonemes}<|TEXT_PROMPT_END|>\n"
        f"assistant:<|SPEECH_GENERATION_START|>{codes_str}"
    )

    return prompt, None


async def generate_speech_tokens_via_lm_studio(prompt: str) -> list[int]:
    """Send prompt to LM Studio and extract speech token IDs from the response."""

    completions_url = f"{LM_STUDIO_URL}/v1/completions"

    if MODEL_TYPE == "standard":
        # Standard model: larger context, slightly different generation params
        payload = {
            "prompt": prompt,
            "max_tokens": 2048,
            "temperature": 1.0,
            "top_k": 50,
            "stop": ["<|SPEECH_GENERATION_END|>"],
            "stream": False,
        }
    else:
        # Turbo model
        payload = {
            "prompt": prompt,
            "max_tokens": 2048,
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
    """Decode speech token IDs to audio waveform using the appropriate codec."""
    global VIENEU_MODEL, NEUCODEC_DECODER

    if MODEL_TYPE == "standard":
        # NeuCodec ONNX decoder: input "codes" shape [B, 1, T] int32, output audio [B, 1, T_audio]
        codes = np.array(speech_ids, dtype=np.int32)[np.newaxis, np.newaxis, :]
        input_name = NEUCODEC_DECODER.get_inputs()[0].name
        recon = NEUCODEC_DECODER.run(None, {input_name: codes})[0]
        return recon[0, 0, :]
    else:
        # Turbo: VieNeu-Codec ONNX decoder with voice embedding
        decode_str = "".join(f"<|speech_{tid}|>" for tid in speech_ids)
        audio = VIENEU_MODEL._decode(decode_str, voice_embedding)
        return audio


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Pre-load VieNeu components and verify LM Studio connectivity at startup."""
    LOGGER.info("=" * 60)
    LOGGER.info("PRE-LOADING VIENEU-GGUF-TTS COMPONENTS AT STARTUP")
    LOGGER.info("Model type: %s", MODEL_TYPE)
    LOGGER.info("LM Studio URL: %s", LM_STUDIO_URL)
    LOGGER.info("=" * 60)

    try:
        load_components()
        if MODEL_TYPE == "standard":
            load_standard_voice_presets()
        else:
            load_turbo_voice_embeddings()
        discover_voices()
        LOGGER.info("[OK] VieNeu components loaded successfully (model_type=%s)", MODEL_TYPE)
    except Exception as e:
        LOGGER.error("[FAIL] Failed to load VieNeu components: %s", e, exc_info=True)
        _append_fault_log(f"Startup failure: {e}\n" + traceback.format_exc())
        raise RuntimeError("VieNeu-GGUF-TTS startup aborted: component initialization failed") from e

    # Check LM Studio connectivity
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{LM_STUDIO_URL}/v1/models")
            resp.raise_for_status()
            models = resp.json()
            model_ids = [m.get("id", "unknown") for m in models.get("data", [])]
            LOGGER.info("[OK] LM Studio is reachable, loaded models: %s", model_ids)
    except Exception as e:
        LOGGER.warning("Could not reach LM Studio at %s: %s", LM_STUDIO_URL, e)
        LOGGER.warning("The service will start, but TTS requests will fail until LM Studio is running.")

    LOGGER.info("[OK] Service is ready for requests!")
    LOGGER.info("=" * 60)

    yield

    # Cleanup
    global VIENEU_MODEL, NEUCODEC_DECODER
    if VIENEU_MODEL is not None:
        try:
            if hasattr(VIENEU_MODEL, 'close'):
                VIENEU_MODEL.close()
        except Exception:
            pass
        VIENEU_MODEL = None
    NEUCODEC_DECODER = None


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
    global VIENEU_MODEL, NEUCODEC_DECODER

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
        LOGGER.info(
            "TTS generated in %.2fs "
            "(prompt=%.2fs, gen=%.2fs [%d tokens], "
            "decode=%.2fs), "
            "audio=%.1fs, "
            "voice=%s, text='%s'",
            elapsed, prompt_time, gen_time, len(speech_ids),
            decode_time, duration, voice_id or 'default', text_preview
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
        LOGGER.error("LM Studio API error: %s - %s", e.response.status_code, e.response.text[:200])
        raise HTTPException(status_code=502, detail=f"LM Studio API error: {e.response.status_code}")
    except httpx.ConnectError:
        LOGGER.error("Cannot connect to LM Studio at %s", LM_STUDIO_URL)
        raise HTTPException(
            status_code=502,
            detail=f"Cannot connect to LM Studio at {LM_STUDIO_URL}. Is it running?"
        )
    except Exception as e:
        LOGGER.error("Error generating TTS: %s", e, exc_info=True)
        _append_fault_log(f"TTS error: {e}\n" + traceback.format_exc())
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
        "model_type": MODEL_TYPE,
        "available_voices": [v["id"] for v in AVAILABLE_VOICES] if AVAILABLE_VOICES else ["default"],
    }
    return JSONResponse(content=info)


@app.post("/shutdown")
async def shutdown():
    """Shutdown the service."""
    LOGGER.info("Shutdown request received...")

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

    LOGGER.info("Starting %s service on %s:%s (debug_mode=%s, model_type=%s)",
                SERVICE_NAME, host, SERVICE_PORT, DEBUG_MODE, MODEL_TYPE)
    LOGGER.info("LM Studio backend: %s", LM_STUDIO_URL)
    LOGGER.info("Configuration: %s", SERVICE_CONFIG)

    uvicorn.run(
        app,
        host=host,
        port=SERVICE_PORT,
        log_level="info",
        access_log=True
    )
