"""FastAPI server for VieNeu-GGUF-TTS service using LM Studio for inference."""

import sys
import os
import io
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
from fastapi.responses import Response, JSONResponse, StreamingResponse
from pydantic import BaseModel
from typing import Optional, List

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
# Model type: 'turbo' for VieNeu-TTS-v2-Turbo-GGUF, 'standard' for VieNeu-TTS-0.3B
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

# VieNeu-TTS constants (repos - configurable via service_config.txt)
STANDARD_BACKBONE_REPO = get_config_value(SERVICE_CONFIG, 'standard_voice_repo', 'pnnbao-ump/VieNeu-TTS-0.3B')
STANDARD_CODEC_REPO = get_config_value(SERVICE_CONFIG, 'standard_codec_repo', 'neuphonic/neucodec-onnx-decoder-int8')
TURBO_REPO = get_config_value(SERVICE_CONFIG, 'turbo_repo', 'pnnbao-ump/VieNeu-TTS-v2-Turbo-GGUF')
CODEC_REPO = get_config_value(SERVICE_CONFIG, 'codec_repo', 'pnnbao-ump/VieNeu-Codec')

# Global references
VIENEU_CODEC = None       # NeuCodecOnnx for standard model or turbo model object
TURBO_MODEL = None        # BaseTurboVieNeuTTS instance (codec only, no backbone)
AVAILABLE_VOICES = []
TURBO_VOICE_EMBEDDINGS = {}   # {voice_name: np.ndarray shape (1, 128)}
DEFAULT_TURBO_EMBEDDING = None

# Standard model voice data: {voice_name: {"codes": list[int], "text": str, "description": str}}
STANDARD_VOICE_PRESETS = {}
DEFAULT_STANDARD_VOICE = None

# Whether the standard model uses chat-style prompt format
# (pnnbao-ump/VieNeu-TTS uses chat format; pnnbao-ump/VieNeu-TTS-0.3B does not)
USE_CHAT_FORMAT = False


def load_components():
    """Load VieNeu codec (decoder/encoder) and voices.

    The LLM backbone is handled by LM Studio, so we skip backbone loading.
    Loads different codec depending on MODEL_TYPE.
    """
    global VIENEU_CODEC, TURBO_MODEL

    start_time = time.time()

    if MODEL_TYPE == "standard":
        LOGGER.info("Loading Standard VieNeu-TTS components (NeuCodec ONNX int8 decoder)...")
        from vieneu.utils import NeuCodecOnnx
        VIENEU_CODEC = NeuCodecOnnx.from_pretrained(STANDARD_CODEC_REPO)
        LOGGER.info("NeuCodec ONNX int8 decoder loaded from %s", STANDARD_CODEC_REPO)
    else:
        from vieneu.turbo import TurboVieNeuTTS, BaseTurboVieNeuTTS

        LOGGER.info("Loading VieNeu-TTS Turbo components (codec + encoder, NO backbone)...")

        # Use TurboVieNeuTTS (concrete) with __new__ to bypass __init__ backbone loading,
        # then call BaseTurboVieNeuTTS.__init__ which only sets up codec attributes.
        model = TurboVieNeuTTS.__new__(TurboVieNeuTTS)
        BaseTurboVieNeuTTS.__init__(model)
        model.backbone = None
        model.max_context = 4096
        model.device = "cpu"

        # Load only the ONNX codec (decoder + encoder)
        model._load_decoder(CODEC_REPO, "vieneu_decoder.onnx", "cpu", None)
        model._load_encoder(CODEC_REPO, "vieneu_encoder.onnx", "cpu", None)

        TURBO_MODEL = model

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
        # Use find_turbo_embedding for config override (supports partial/normalized matching)
        if DEFAULT_VOICE_CONFIG:
            config_emb = find_turbo_embedding(DEFAULT_VOICE_CONFIG)
            if config_emb is not None:
                DEFAULT_TURBO_EMBEDDING = config_emb
        if DEFAULT_TURBO_EMBEDDING is None and default_name and default_name in TURBO_VOICE_EMBEDDINGS:
            DEFAULT_TURBO_EMBEDDING = TURBO_VOICE_EMBEDDINGS[default_name]
        if DEFAULT_TURBO_EMBEDDING is None and TURBO_VOICE_EMBEDDINGS:
            DEFAULT_TURBO_EMBEDDING = next(iter(TURBO_VOICE_EMBEDDINGS.values()))

        LOGGER.info("Loaded %d turbo voice embeddings", len(TURBO_VOICE_EMBEDDINGS))
    except Exception as e:
        LOGGER.warning("Could not load turbo voice embeddings: %s", e)


def load_standard_voice_presets():
    """Load voice presets (integer code sequences + reference text) for the standard model."""
    global STANDARD_VOICE_PRESETS, DEFAULT_STANDARD_VOICE, USE_CHAT_FORMAT

    import json
    from huggingface_hub import hf_hub_download

    # Determine chat format: explicit config takes priority, otherwise auto-detect
    chat_format_config = get_config_value(SERVICE_CONFIG, 'use_chat_format', '').lower()
    if chat_format_config in ('true', '1', 'yes'):
        USE_CHAT_FORMAT = True
    elif chat_format_config in ('false', '0', 'no'):
        USE_CHAT_FORMAT = False
    else:
        # Auto-detect: default to chat format (works for most GGUF models on LM Studio)
        USE_CHAT_FORMAT = True

    try:
        voices_path = hf_hub_download(repo_id=STANDARD_BACKBONE_REPO, filename="voices.json")
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

        LOGGER.info("Loaded %d standard voice presets (default=%s, chat_format=%s)",
                    len(STANDARD_VOICE_PRESETS), DEFAULT_STANDARD_VOICE, USE_CHAT_FORMAT)
    except Exception as e:
        LOGGER.warning("Could not load standard voice presets: %s", e)


def _strip_vietnamese_diacritics(text: str) -> str:
    """Strip Vietnamese diacritics for fuzzy matching (đ→d, etc.)."""
    import unicodedata
    # Handle đ/Đ explicitly (not decomposable by NFD)
    text = text.replace("đ", "d").replace("Đ", "D")
    # Decompose and strip combining marks
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def find_turbo_embedding(voice_id: Optional[str] = None) -> Optional[np.ndarray]:
    """Find the best matching turbo voice embedding for the given voice."""
    import unicodedata

    if not TURBO_VOICE_EMBEDDINGS:
        return DEFAULT_TURBO_EMBEDDING

    if not voice_id:
        return DEFAULT_TURBO_EMBEDDING

    # Normalize the input to NFC for consistent comparison
    voice_id_norm = unicodedata.normalize("NFC", voice_id)

    # Exact match (with normalization)
    for name, emb in TURBO_VOICE_EMBEDDINGS.items():
        if unicodedata.normalize("NFC", name) == voice_id_norm:
            LOGGER.debug("Voice exact match: '%s' -> '%s'", voice_id, name)
            return emb

    # Partial/substring match (case-insensitive, normalized)
    voice_lower = voice_id_norm.lower()
    for name, emb in TURBO_VOICE_EMBEDDINGS.items():
        name_lower = unicodedata.normalize("NFC", name).lower()
        if voice_lower in name_lower or name_lower in voice_lower:
            LOGGER.debug("Voice partial match: '%s' -> '%s'", voice_id, name)
            return emb

    # ASCII-folded partial match (strips diacritics: "Doan" matches "Đoan")
    voice_ascii = _strip_vietnamese_diacritics(voice_id_norm).lower()
    for name, emb in TURBO_VOICE_EMBEDDINGS.items():
        name_ascii = _strip_vietnamese_diacritics(name).lower()
        if voice_ascii in name_ascii or name_ascii in voice_ascii:
            LOGGER.debug("Voice diacritics-stripped match: '%s' -> '%s'", voice_id, name)
            return emb

    LOGGER.warning("Voice not found: '%s', using default", voice_id)
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


def time_stretch_audio(audio: np.ndarray, speed: float) -> np.ndarray:
    """Time-stretch audio with WSOLA while preserving pitch.

    Uses the tested audiotsm WSOLA implementation instead of a handwritten
    overlap-add loop. Padding the input tail gives the stretcher enough
    context to keep the final phonemes from being clipped.
    """
    if speed == 1.0:
        return audio

    from audiotsm import wsola
    from audiotsm.io.array import ArrayReader, ArrayWriter

    audio_array = np.asarray(audio, dtype=np.float32).reshape(-1)
    if audio_array.size == 0:
        return audio_array

    target_len = max(1, int(round(audio_array.size / speed)))

    if audio_array.size < 256:
        source_positions = np.arange(audio_array.size, dtype=np.float32)
        target_positions = np.linspace(0, audio_array.size - 1, target_len, dtype=np.float32)
        return np.interp(target_positions, source_positions, audio_array).astype(np.float32)

    frame_length = min(1024, 1 << int(np.floor(np.log2(audio_array.size))))
    tolerance = frame_length // 2

    # Pad with the final sample so WSOLA can synthesize the real tail instead
    # of running out of context near the end of the utterance.
    padded_audio = np.pad(audio_array, (0, frame_length + tolerance), mode="edge")
    reader = ArrayReader(padded_audio[np.newaxis, :])
    writer = ArrayWriter(1)

    stretcher = wsola(1, speed=speed, frame_length=frame_length, tolerance=tolerance)
    stretcher.run(reader, writer)

    stretched_audio = np.asarray(writer.data[0], dtype=np.float32)
    if stretched_audio.size == 0:
        source_positions = np.arange(audio_array.size, dtype=np.float32)
        target_positions = np.linspace(0, audio_array.size - 1, target_len, dtype=np.float32)
        return np.interp(target_positions, source_positions, audio_array).astype(np.float32)

    if stretched_audio.size < target_len:
        stretched_audio = np.pad(stretched_audio, (0, target_len - stretched_audio.size), mode="edge")

    return stretched_audio[:target_len]


def float32_to_pcm16(audio_float):
    """Convert float32 [-1, 1] to int16 bytes."""
    audio_array = np.asarray(audio_float, dtype=np.float32)
    audio_array = np.clip(audio_array, -1.0, 1.0)
    return (audio_array * 32767).clip(-32768, 32767).astype(np.int16).tobytes()


def build_prompt(text: str, voice_id: Optional[str] = None) -> tuple[str, Optional[np.ndarray]]:
    """Build the LLM prompt for speech generation.

    Returns tuple of (prompt_string, voice_embedding_or_None).
    - Turbo: uses <|speaker_16|> token + phonemized text, voice via embedding.
    - Standard: uses reference codes + phonemized ref + target text.
    """
    if MODEL_TYPE == "standard":
        return _build_standard_prompt(text, voice_id)
    else:
        return _build_turbo_prompt(text, voice_id)


def _build_turbo_prompt(text: str, voice_id: Optional[str] = None) -> tuple[str, Optional[np.ndarray]]:
    """Build v2 Turbo prompt using the latest format."""
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
    """Build standard model prompt with reference voice codes and phonemized text.

    Supports both chat format (VieNeu-TTS full) and compact format (VieNeu-TTS-0.3B).
    """
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

    # Prompt format depends on model variant
    if USE_CHAT_FORMAT:
        # Chat format for full VieNeu-TTS model
        prompt = (
            f"user: Convert the text to speech:"
            f"<|TEXT_PROMPT_START|>{ref_phonemes} {target_phonemes}<|TEXT_PROMPT_END|>\n"
            f"assistant:<|SPEECH_GENERATION_START|>{codes_str}"
        )
    else:
        # Compact format for VieNeu-TTS-0.3B
        prompt = (
            f"<|TEXT_PROMPT_START|>{ref_phonemes} {target_phonemes}"
            f"<|TEXT_PROMPT_END|><|SPEECH_GENERATION_START|>{codes_str}"
        )

    return prompt, None


async def generate_speech_tokens_via_lm_studio(prompt: str) -> list[int]:
    """Send prompt to LM Studio and extract speech token IDs from the response."""
    from vieneu.utils import extract_speech_ids

    completions_url = f"{LM_STUDIO_URL}/v1/completions"

    if MODEL_TYPE == "standard":
        # Standard model: temperature=1.0, top_k=50
        payload = {
            "prompt": prompt,
            "max_tokens": 2048,
            "temperature": 1.0,
            "top_k": 50,
            "stop": ["<|SPEECH_GENERATION_END|>"],
            "stream": False,
        }
    else:
        # Turbo model: temperature=0.4, top_k=50, top_p=0.95, repeat_penalty=1.15
        payload = {
            "prompt": prompt,
            "max_tokens": 2048,
            "temperature": 0.4,
            "top_k": 50,
            "top_p": 0.95,
            "min_p": 0.05,
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

    LOGGER.debug("LM Studio raw response (first 500 chars): %s", generated_text[:500])
    LOGGER.info("LM Studio generated %d chars of text", len(generated_text))

    # Parse speech tokens using vieneu.utils.extract_speech_ids
    speech_ids = extract_speech_ids(generated_text)

    LOGGER.info("Extracted %d speech token IDs from response", len(speech_ids))
    if not speech_ids and generated_text:
        LOGGER.warning("No speech IDs extracted! Raw response snippet: %s", generated_text[:200])

    return speech_ids


def decode_speech_tokens(speech_ids: list[int], voice_embedding: Optional[np.ndarray] = None) -> np.ndarray:
    """Decode speech token IDs to audio waveform using the appropriate codec."""
    global VIENEU_CODEC, TURBO_MODEL

    if MODEL_TYPE == "standard":
        # NeuCodecOnnx: input codes shape [B, 1, T], output audio [B, 1, T_audio]
        codes = np.array(speech_ids, dtype=np.int32)[np.newaxis, np.newaxis, :]
        recon = VIENEU_CODEC.decode_code(codes)
        return recon[0, 0, :]
    else:
        # Turbo: VieNeu-Codec ONNX decoder with voice embedding
        decode_str = "".join(f"<|speech_{tid}|>" for tid in speech_ids)
        audio = TURBO_MODEL._decode(decode_str, voice_embedding)
        return audio


# ---------------------------------------------------------------------------
# Streaming audio generation
# ---------------------------------------------------------------------------

# Streaming constants (matching SDK: standard overrides in __init__)
STREAMING_HOP_LENGTH = 480          # samples per speech token frame
STREAMING_FRAMES_PER_CHUNK = 25     # tokens per decode chunk
STREAMING_LOOKFORWARD = 10          # extra tokens to decode ahead
STREAMING_LOOKBACK = 100            # context tokens before current chunk
STREAMING_OVERLAP_FRAMES = 1        # overlap frames for crossfade
STREAMING_STRIDE_SAMPLES = STREAMING_FRAMES_PER_CHUNK * STREAMING_HOP_LENGTH  # 12000 samples per chunk
SAMPLE_RATE = 24000


def _linear_overlap_add(frames: List[np.ndarray], stride: int) -> np.ndarray:
    """Perform linear overlap-add on a list of audio frames."""
    if not frames:
        return np.array([], dtype=np.float32)

    dtype = frames[0].dtype
    total_size = 0
    for i, frame in enumerate(frames):
        frame_end = stride * i + frame.shape[-1]
        total_size = max(total_size, frame_end)

    out = np.zeros(total_size, dtype=dtype)
    sum_weight = np.zeros(total_size, dtype=dtype)

    offset = 0
    for frame in frames:
        frame_length = frame.shape[-1]
        t = np.linspace(0, 1, frame_length + 2, dtype=dtype)[1:-1]
        weight = np.abs(0.5 - (t - 0.5))
        out[offset:offset + frame_length] += weight * frame
        sum_weight[offset:offset + frame_length] += weight
        offset += stride

    safe_sum_weight = np.where(sum_weight > 0, sum_weight, 1.0)
    return out / safe_sum_weight


def _make_wav_header(sample_rate: int = 24000, bits_per_sample: int = 16, num_channels: int = 1, data_size: int = 0xFFFFFFFF) -> bytes:
    """Create a WAV header. Uses max data_size for streaming (updated later or left as-is)."""
    byte_rate = sample_rate * num_channels * bits_per_sample // 8
    block_align = num_channels * bits_per_sample // 8
    # For streaming, cap chunk sizes to max uint32 so client doesn't stop early
    riff_size = min(data_size + 36, 0xFFFFFFFF)
    header = io.BytesIO()
    header.write(b'RIFF')
    header.write(riff_size.to_bytes(4, 'little'))  # file size - 8
    header.write(b'WAVE')
    header.write(b'fmt ')
    header.write((16).to_bytes(4, 'little'))  # fmt chunk size
    header.write((1).to_bytes(2, 'little'))   # PCM format
    header.write(num_channels.to_bytes(2, 'little'))
    header.write(sample_rate.to_bytes(4, 'little'))
    header.write(byte_rate.to_bytes(4, 'little'))
    header.write(block_align.to_bytes(2, 'little'))
    header.write(bits_per_sample.to_bytes(2, 'little'))
    header.write(b'data')
    header.write(data_size.to_bytes(4, 'little'))
    return header.getvalue()


def _decode_chunk(speech_ids: List[int]) -> np.ndarray:
    """Decode a list of speech IDs to audio using the loaded codec."""
    if MODEL_TYPE == "standard":
        codes = np.array(speech_ids, dtype=np.int32)[np.newaxis, np.newaxis, :]
        recon = VIENEU_CODEC.decode_code(codes)
        return recon[0, 0, :]
    else:
        decode_str = "".join(f"<|speech_{tid}|>" for tid in speech_ids)
        return TURBO_MODEL._decode(decode_str, None)


async def _stream_speech_generation(prompt: str, ref_codes: List[int], voice_embedding: Optional[np.ndarray] = None, speed: float = 1.0):
    """Generator that streams WAV audio chunks as speech tokens arrive from LM Studio.

    Yields bytes: first the WAV header, then PCM16 audio chunks.
    """
    import re
    import json as json_mod
    from vieneu.utils import extract_speech_ids

    RE_SPEECH_TOKEN = re.compile(r"<\|speech_(\d+)\|>")

    completions_url = f"{LM_STUDIO_URL}/v1/completions"

    if MODEL_TYPE == "standard":
        payload = {
            "prompt": prompt,
            "max_tokens": 2048,
            "temperature": 1.0,
            "top_k": 50,
            "stop": ["<|SPEECH_GENERATION_END|>"],
            "stream": True,
        }
    else:
        payload = {
            "prompt": prompt,
            "max_tokens": 2048,
            "temperature": 0.4,
            "top_k": 50,
            "top_p": 0.95,
            "min_p": 0.05,
            "repeat_penalty": 1.15,
            "stop": ["<|SPEECH_GENERATION_END|>"],
            "stream": True,
        }

    if LM_STUDIO_MODEL:
        payload["model"] = LM_STUDIO_MODEL

    # Yield WAV header first (with max data_size for streaming)
    yield _make_wav_header(SAMPLE_RATE)

    # Token state for overlap-add streaming
    token_cache: List[int] = list(ref_codes)  # Start with reference codes for context
    n_decoded_tokens: int = len(ref_codes)
    n_decoded_samples: int = 0
    audio_cache: List[np.ndarray] = []
    text_buffer = ""  # Buffer for incomplete tokens

    start_time = time.time()
    total_generated = 0

    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            async with client.stream("POST", completions_url, json=payload) as response:
                response.raise_for_status()

                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        break

                    try:
                        chunk_data = json_mod.loads(data_str)
                        token_text = chunk_data["choices"][0].get("text", "")
                    except (ValueError, KeyError, IndexError):
                        continue

                    if not token_text:
                        continue

                    # Accumulate text and extract complete speech tokens
                    text_buffer += token_text
                    matches = list(RE_SPEECH_TOKEN.finditer(text_buffer))

                    if not matches:
                        continue

                    # Extract all complete token IDs
                    for m in matches:
                        token_id = int(m.group(1))
                        token_cache.append(token_id)
                        total_generated += 1

                    # Keep any incomplete text after the last match
                    last_end = matches[-1].end()
                    text_buffer = text_buffer[last_end:]

                    # Check if we have enough tokens for a chunk
                    pending = len(token_cache) - n_decoded_tokens
                    if pending >= STREAMING_FRAMES_PER_CHUNK + STREAMING_LOOKFORWARD:
                        # Decode a chunk with context
                        tokens_start = max(n_decoded_tokens - STREAMING_LOOKBACK - STREAMING_OVERLAP_FRAMES, 0)
                        tokens_end = n_decoded_tokens + STREAMING_FRAMES_PER_CHUNK + STREAMING_LOOKFORWARD + STREAMING_OVERLAP_FRAMES
                        tokens_end = min(tokens_end, len(token_cache))

                        sample_start = (n_decoded_tokens - tokens_start) * STREAMING_HOP_LENGTH
                        sample_end = sample_start + (STREAMING_FRAMES_PER_CHUNK + 2 * STREAMING_OVERLAP_FRAMES) * STREAMING_HOP_LENGTH

                        curr_ids = token_cache[tokens_start:tokens_end]
                        recon = _decode_chunk(curr_ids)

                        # Clip to expected range
                        recon = recon[sample_start:sample_end]
                        audio_cache.append(recon)

                        # Overlap-add and yield new samples
                        processed = _linear_overlap_add(audio_cache, stride=STREAMING_STRIDE_SAMPLES)
                        new_samples_end = len(audio_cache) * STREAMING_STRIDE_SAMPLES
                        new_audio = processed[n_decoded_samples:new_samples_end]
                        n_decoded_samples = new_samples_end
                        n_decoded_tokens += STREAMING_FRAMES_PER_CHUNK

                        # Apply speed stretch if needed
                        if speed != 1.0:
                            new_audio = time_stretch_audio(new_audio, speed)

                        # Convert to PCM16 and yield
                        pcm_bytes = float32_to_pcm16(new_audio)
                        yield pcm_bytes

        # Flush remaining tokens
        remaining = len(token_cache) - n_decoded_tokens
        if remaining > 0:
            tokens_start = max(len(token_cache) - (STREAMING_LOOKBACK + STREAMING_OVERLAP_FRAMES + remaining), 0)
            sample_start = (len(token_cache) - tokens_start - remaining - STREAMING_OVERLAP_FRAMES) * STREAMING_HOP_LENGTH

            curr_ids = token_cache[tokens_start:]
            recon = _decode_chunk(curr_ids)
            recon = recon[sample_start:]
            audio_cache.append(recon)

            processed = _linear_overlap_add(audio_cache, stride=STREAMING_STRIDE_SAMPLES)
            final_audio = processed[n_decoded_samples:]

            if speed != 1.0:
                final_audio = time_stretch_audio(final_audio, speed)

            pcm_bytes = float32_to_pcm16(final_audio)
            yield pcm_bytes

        elapsed = time.time() - start_time
        LOGGER.info("Streaming TTS: %d tokens generated in %.2fs, yielded %d audio chunks",
                    total_generated, elapsed, len(audio_cache))

    except Exception as e:
        LOGGER.error("Streaming TTS error: %s", e, exc_info=True)
        # If we haven't yielded anything yet beyond the header, we can't send an error
        # The connection will just close, which the client handles as a failure


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
    global VIENEU_CODEC, TURBO_MODEL
    if TURBO_MODEL is not None:
        try:
            TURBO_MODEL.close()
        except Exception:
            pass
        TURBO_MODEL = None
    VIENEU_CODEC = None


app = FastAPI(title=SERVICE_NAME, version=SERVICE_INSTALL_VERSION, lifespan=lifespan)


class TTSRequest(BaseModel):
    text: str
    voice_id: Optional[str] = None
    speed: Optional[float] = 1.0


@app.post("/debug_prompt")
async def debug_prompt(req: TTSRequest):
    """Debug endpoint: show the prompt and raw LLM response without generating audio."""
    from vieneu.utils import extract_speech_ids

    prompt, voice_embedding = build_prompt(req.text, req.voice_id)

    completions_url = f"{LM_STUDIO_URL}/v1/completions"
    payload = {
        "prompt": prompt,
        "max_tokens": 2048,
        "temperature": 1.0,
        "top_k": 50,
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
    speech_ids = extract_speech_ids(generated_text)

    return JSONResponse(content={
        "prompt_length": len(prompt),
        "prompt_preview": prompt[:500],
        "raw_response_length": len(generated_text),
        "raw_response_preview": generated_text[:500],
        "speech_ids_count": len(speech_ids),
        "speech_ids_first_10": speech_ids[:10],
        "use_chat_format": USE_CHAT_FORMAT,
    })

@app.get("/voices")
async def get_voices():
    """Return list of available preset voices."""
    if not AVAILABLE_VOICES:
        return [{"id": "", "name": "Default"}]
    return AVAILABLE_VOICES


@app.get("/stream")
async def stream_audio_get(text: str, voice_id: Optional[str] = None, speed: Optional[float] = 1.0):
    """Streaming TTS endpoint (GET). Returns audio/wav with chunked transfer."""
    return await _synthesize_audio_streaming(text, voice_id, speed)


@app.post("/stream")
async def stream_audio_post(req: TTSRequest):
    """Streaming TTS endpoint (POST). Returns audio/wav with chunked transfer."""
    return await _synthesize_audio_streaming(req.text, req.voice_id, req.speed)


@app.post("/tts")
async def tts_endpoint(req: TTSRequest):
    """Non-streaming TTS endpoint. Returns audio/wav."""
    return await _synthesize_audio(req.text, req.voice_id, req.speed)


async def _synthesize_audio_streaming(text: str, voice_id: Optional[str] = None, speed: Optional[float] = 1.0):
    """Synthesize audio with streaming: yields WAV chunks as tokens arrive from LM Studio."""
    speed = speed if speed is not None else 1.0
    speed = max(0.25, min(4.0, speed))

    if MODEL_TYPE == "standard" and VIENEU_CODEC is None:
        raise HTTPException(status_code=503, detail="Model components not loaded yet")
    if MODEL_TYPE != "standard" and TURBO_MODEL is None:
        raise HTTPException(status_code=503, detail="Model components not loaded yet")

    if not text or not text.strip():
        raise HTTPException(status_code=400, detail="No text provided")

    text_preview = text[:60] + "..." if len(text) > 60 else text
    LOGGER.info("Streaming TTS request: voice_id='%s', text='%s'", voice_id or '(none)', text_preview)

    prompt, voice_embedding = build_prompt(text, voice_id)

    # Get reference codes for streaming context (standard model only)
    ref_codes: List[int] = []
    if MODEL_TYPE == "standard":
        voice_name = voice_id
        if not voice_name or voice_name not in STANDARD_VOICE_PRESETS:
            voice_name = DEFAULT_STANDARD_VOICE
        if voice_name and voice_name in STANDARD_VOICE_PRESETS:
            ref_codes = STANDARD_VOICE_PRESETS[voice_name]["codes"]

    return StreamingResponse(
        _stream_speech_generation(prompt, ref_codes, voice_embedding, speed),
        media_type="audio/wav",
        headers={"Content-Disposition": "attachment; filename=tts_stream.wav"},
    )


async def _synthesize_audio(text: str, voice_id: Optional[str] = None, speed: Optional[float] = 1.0):
    """Synthesize audio from text via LM Studio and return as WAV."""
    global VIENEU_CODEC, TURBO_MODEL

    speed = speed if speed is not None else 1.0
    speed = max(0.25, min(4.0, speed))

    if MODEL_TYPE == "standard" and VIENEU_CODEC is None:
        raise HTTPException(status_code=503, detail="Model components not loaded yet")
    if MODEL_TYPE != "standard" and TURBO_MODEL is None:
        raise HTTPException(status_code=503, detail="Model components not loaded yet")

    if not text or not text.strip():
        raise HTTPException(status_code=400, detail="No text provided")
    
    try:
        start_time = time.time()
        text_preview = text[:60] + "..." if len(text) > 60 else text
        LOGGER.info("TTS request: voice_id='%s', text='%s'", voice_id or '(none)', text_preview)

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

        # Apply speed adjustment after decoding while keeping pitch stable.
        if speed != 1.0:
            audio = time_stretch_audio(audio, speed)

        elapsed = time.time() - start_time
        duration = len(audio) / 24000
        LOGGER.info(
            "TTS generated in %.2fs "
            "(prompt=%.2fs, gen=%.2fs [%d tokens], "
            "decode=%.2fs), "
            "audio=%.1fs, "
            "speed=%.2f, voice=%s, text='%s'",
            elapsed, prompt_time, gen_time, len(speech_ids),
            decode_time, duration, speed, voice_id or 'default', text_preview
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
