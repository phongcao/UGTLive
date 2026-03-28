"""FastAPI server for a generic OpenAI-compatible vision OCR backend."""

import asyncio
import base64
import os
import re
import ssl
import sys
import time
import unicodedata
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import certifi

ssl._create_default_https_context = lambda: ssl.create_default_context(cafile=certifi.where())

import requests
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from PIL import Image

shared_dir = Path(__file__).parent.parent / "shared"
sys.path.insert(0, str(shared_dir))

from config_parser import get_config_value, parse_service_config

_COLOR_ANALYSIS_IMPORT_ERROR: Optional[Exception] = None

try:
    from color_analysis import attach_color_info, extract_foreground_background_colors
except Exception as exc:
    _COLOR_ANALYSIS_IMPORT_ERROR = exc

    def attach_color_info(text_obj: Dict, color_data: Dict) -> None:
        return None

    def extract_foreground_background_colors(*args, **kwargs):
        return None


config_path = Path(__file__).parent / "service_config.txt"
SERVICE_CONFIG = parse_service_config(str(config_path))
APP_CONFIG_PATH = Path(__file__).parent.parent.parent / "config.txt"

SERVICE_NAME = get_config_value(SERVICE_CONFIG, "service_name", "Generic LLM OCR")
SERVICE_PORT = int(get_config_value(SERVICE_CONFIG, "port", "5005"))
SERVICE_INSTALL_VERSION = get_config_value(SERVICE_CONFIG, "service_install_version", "1")

DEFAULT_API_BASE = "http://127.0.0.1:1234"
DEFAULT_MODEL = "qwen2.5-vl-7b-instruct"
DEFAULT_MODE = "OCR + Translate"
DEFAULT_TARGET_LANGUAGE = "en"
DEFAULT_MAX_IMAGE_DIMENSION = 1536
DEFAULT_MAX_IMAGE_TOTAL_PIXELS = 1800000
NO_TEXT_SENTINEL = "__UGTLIVE_NO_TEXT__"
DEBUG_IMAGE_DIR = Path(__file__).parent / "debug"
DEBUG_IMAGE_ENV_VAR = "UGTLIVE_VISUAL_STUDIO_DEBUG"

MODE_OCR_ONLY = "OCR Only"
MODE_OCR_TRANSLATE = "OCR + Translate"

PROMPT_OCR_ONLY = (
    "You are an OCR engine. Detect every text region in the image.\n"
    "For EACH text region output EXACTLY one line in this format:\n"
    "TEXT: <the text> | BBOX: [x1, y1, x2, y2]\n"
    "where x1,y1 is the top-left corner and x2,y2 is the bottom-right corner in pixel coordinates.\n"
    "Use the actual image pixel dimensions.\n"
    f"If no readable text is present, output EXACTLY {NO_TEXT_SENTINEL} and nothing else.\n"
    f"Never translate, explain, or wrap {NO_TEXT_SENTINEL} in any extra text.\n"
    "Output ONLY the list, no extra explanation."
)

PROMPT_OCR_TRANSLATE = (
    "You are an OCR and translation engine. Detect every text region in the image.\n"
    "First infer the likely overall context of the image and use it internally to choose accurate terminology and tone.\n"
    "For EACH text region output EXACTLY one line in this format:\n"
    "TEXT: <translated text> | BBOX: [x1, y1, x2, y2]\n"
    "where x1,y1 is the top-left corner and x2,y2 is the bottom-right corner in pixel coordinates.\n"
    "Use the actual image pixel dimensions.\n"
    "The TEXT field must contain ONLY the final translated text in the target language.\n"
    "Do NOT include the original/source text, transliterations, or extra labels.\n"
    "Do NOT output the inferred context.\n"
    f"If no readable text is present, output EXACTLY {NO_TEXT_SENTINEL} and nothing else.\n"
    f"Never translate, explain, or wrap {NO_TEXT_SENTINEL} in any extra text.\n"
    "Output ONLY the list, no extra explanation."
)

NO_TEXT_RESPONSE_PATTERNS = {
    NO_TEXT_SENTINEL.lower(),
    "no text",
    "no text detected",
    "no text found",
    "no readable text",
    "no readable text detected",
    "no visible text",
    "no visible text detected",
    "text not detected",
    "there is no text",
    "there is no readable text",
    "none",
    "n/a",
    "khong co chu",
    "khong co chu nao",
    "khong co van ban",
    "khong co van ban nao",
    "khong co van ban nao duoc phat hien",
    "khong co van ban phat hien duoc",
    "khong phat hien duoc van ban",
    "khong tim thay van ban",
}

NO_TEXT_RESPONSE_PREFIXES = (
    "no text ",
    "no readable text ",
    "no visible text ",
    "there is no text ",
    "there is no readable text ",
    "khong co chu",
    "khong co van ban",
    "khong phat hien duoc van ban",
    "khong tim thay van ban",
)

LANGUAGE_NAME_MAP = {
    "en": "English",
    "ja": "Japanese",
    "japan": "Japanese",
    "ko": "Korean",
    "korean": "Korean",
    "ch_sim": "Simplified Chinese",
    "ch_tra": "Traditional Chinese",
    "zh": "Chinese",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "ru": "Russian",
    "vi": "Vietnamese",
    "th": "Thai",
}

TRANSLATION_PATTERN = re.compile(
    r"TEXT:\s*(?P<text>[^|]+?)\s*\|\s*"
    r"(?:(?:TRANS|TRANSLATED|TARGET|EN):\s*(?P<translated>[^|]+?)\s*\|\s*)?"
    r"BBOX:\s*\[(?P<bbox>[^\]]+)\]",
    re.IGNORECASE,
)

app = FastAPI(title=SERVICE_NAME, version=SERVICE_INSTALL_VERSION)


try:
    RESAMPLE_LANCZOS = Image.Resampling.LANCZOS
except AttributeError:
    RESAMPLE_LANCZOS = Image.LANCZOS


if _COLOR_ANALYSIS_IMPORT_ERROR is not None:
    print(
        "Color analysis disabled for Generic LLM OCR: "
        f"{type(_COLOR_ANALYSIS_IMPORT_ERROR).__name__}: {_COLOR_ANALYSIS_IMPORT_ERROR}"
    )


def load_runtime_settings() -> Dict[str, str]:
    return parse_service_config(str(APP_CONFIG_PATH))


def normalize_mode(mode: str) -> str:
    if mode.strip().lower() == MODE_OCR_ONLY.lower():
        return MODE_OCR_ONLY
    return MODE_OCR_TRANSLATE


def get_language_name(code: str) -> str:
    normalized = (code or "").strip().lower()
    return LANGUAGE_NAME_MAP.get(normalized, code or "English")


def get_target_language(request: Request, runtime_config: Dict[str, str]) -> str:
    requested_target = (request.query_params.get("target_lang", "") or "").strip()
    if requested_target:
        return requested_target

    configured_target = (runtime_config.get("generic_llm_ocr_target_language", "") or "").strip()
    if configured_target:
        return configured_target

    fallback_target = (runtime_config.get("target_language", DEFAULT_TARGET_LANGUAGE) or "").strip()
    return fallback_target or DEFAULT_TARGET_LANGUAGE


def build_endpoint(api_base: str) -> str:
    base = (api_base or DEFAULT_API_BASE).strip().rstrip("/")
    if base.endswith("/v1/chat/completions"):
        return base
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def build_prompt(mode: str, width: int, height: int, source_lang: str, target_lang: str) -> str:
    if mode == MODE_OCR_ONLY:
        return f"The image is {width}x{height} pixels. Source language hint: {get_language_name(source_lang)}. {PROMPT_OCR_ONLY}"
    return (
        f"The image is {width}x{height} pixels. Source language hint: {get_language_name(source_lang)}. "
        f"Translate all detected text to {get_language_name(target_lang)}. {PROMPT_OCR_TRANSLATE}"
    )


def is_visual_studio_debug_enabled() -> bool:
    value = (os.environ.get(DEBUG_IMAGE_ENV_VAR, "") or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def save_debug_request_image(image: Image.Image, source_lang: str, prefix: str = "request") -> Optional[Path]:
    if not is_visual_studio_debug_enabled():
        return None

    try:
        DEBUG_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        milliseconds = int((time.time() % 1) * 1000)
        safe_lang = re.sub(r"[^a-zA-Z0-9_-]+", "_", (source_lang or "unknown").strip()) or "unknown"
        debug_path = DEBUG_IMAGE_DIR / (
            f"{prefix}_{timestamp}-{milliseconds:03d}_{safe_lang}_{image.width}x{image.height}.png"
        )
        image.save(debug_path, format="PNG")
        print(f"Saved Generic LLM OCR debug image to {debug_path}")
        return debug_path
    except Exception as exc:
        print(f"Failed to save Generic LLM OCR debug image: {exc}")
        return None


def get_non_negative_int(runtime_config: Dict[str, str], key: str, default: int) -> int:
    raw_value = (runtime_config.get(key, "") or "").strip()
    if not raw_value:
        return default

    try:
        return max(0, int(raw_value))
    except ValueError:
        print(f"Invalid Generic LLM OCR config for {key}: {raw_value}. Using {default}.")
        return default


def prepare_image_for_llm(image: Image.Image, runtime_config: Dict[str, str]) -> Tuple[Image.Image, bytes]:
    max_dimension = get_non_negative_int(
        runtime_config,
        "generic_llm_ocr_max_dimension",
        DEFAULT_MAX_IMAGE_DIMENSION,
    )
    max_total_pixels = get_non_negative_int(
        runtime_config,
        "generic_llm_ocr_max_total_pixels",
        DEFAULT_MAX_IMAGE_TOTAL_PIXELS,
    )

    original_width, original_height = image.size
    longest_edge = max(original_width, original_height)
    total_pixels = original_width * original_height
    scale_factor = 1.0

    if max_dimension > 0 and longest_edge > max_dimension:
        scale_factor = min(scale_factor, max_dimension / float(longest_edge))

    if max_total_pixels > 0 and total_pixels > max_total_pixels:
        scale_factor = min(scale_factor, (max_total_pixels / float(total_pixels)) ** 0.5)

    if scale_factor < 0.9995:
        resized_width = max(1, int(round(original_width * scale_factor)))
        resized_height = max(1, int(round(original_height * scale_factor)))
        prepared_image = image.resize((resized_width, resized_height), RESAMPLE_LANCZOS)
        print(
            "Resized Generic LLM OCR request image "
            f"from {original_width}x{original_height} to {resized_width}x{resized_height}"
        )
    else:
        prepared_image = image

    output_buffer = BytesIO()
    prepared_image.save(output_buffer, format="PNG")
    return prepared_image, output_buffer.getvalue()


def parse_bbox_values(raw_bbox: str, img_w: int, img_h: int) -> Optional[Tuple[int, int, int, int]]:
    try:
        coords = [float(x.strip()) for x in raw_bbox.split(",")]
    except ValueError:
        return None

    if len(coords) != 4:
        return None

    x1, y1, x2, y2 = coords
    all_leq1 = all(0.0 <= c <= 1.0 for c in coords)
    all_leq999 = all(0.0 <= c <= 999.0 for c in coords)

    if all_leq1:
        x1, x2 = x1 * img_w, x2 * img_w
        y1, y2 = y1 * img_h, y2 * img_h
    elif all_leq999 and max(x1, x2) <= 999 and max(y1, y2) <= 999:
        x1, x2 = (x1 / 999.0) * img_w, (x2 / 999.0) * img_w
        y1, y2 = (y1 / 999.0) * img_h, (y2 / 999.0) * img_h

    x1_i = max(0, min(img_w, int(round(min(x1, x2)))))
    y1_i = max(0, min(img_h, int(round(min(y1, y2)))))
    x2_i = max(0, min(img_w, int(round(max(x1, x2)))))
    y2_i = max(0, min(img_h, int(round(max(y1, y2)))))

    if x2_i <= x1_i or y2_i <= y1_i:
        return None

    return x1_i, y1_i, x2_i, y2_i


def remap_bbox_to_source(
    bbox: Tuple[int, int, int, int],
    bbox_img_w: int,
    bbox_img_h: int,
    source_img_w: int,
    source_img_h: int,
) -> Optional[Tuple[int, int, int, int]]:
    if bbox_img_w <= 0 or bbox_img_h <= 0 or source_img_w <= 0 or source_img_h <= 0:
        return None

    if bbox_img_w == source_img_w and bbox_img_h == source_img_h:
        return bbox

    x_scale = source_img_w / float(bbox_img_w)
    y_scale = source_img_h / float(bbox_img_h)

    x1 = max(0, min(source_img_w, int(round(bbox[0] * x_scale))))
    y1 = max(0, min(source_img_h, int(round(bbox[1] * y_scale))))
    x2 = max(0, min(source_img_w, int(round(bbox[2] * x_scale))))
    y2 = max(0, min(source_img_h, int(round(bbox[3] * y_scale))))

    if x2 <= x1 or y2 <= y1:
        return None

    return x1, y1, x2, y2


def normalize_response_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text or "")
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = normalized.lower().strip()
    normalized = normalized.replace("\u0111", "d")
    normalized = re.sub(r"[^\w\s]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def is_no_text_response(text: str) -> bool:
    normalized = normalize_response_text(text)
    if not normalized:
        return False

    if NO_TEXT_SENTINEL.lower() in normalized:
        return True

    if normalized in NO_TEXT_RESPONSE_PATTERNS:
        return True

    return any(normalized.startswith(prefix) for prefix in NO_TEXT_RESPONSE_PREFIXES)


def query_llm(image_bytes: bytes, width: int, height: int, runtime_config: Dict[str, str], source_lang: str, target_lang: str) -> Tuple[str, str, str]:
    api_base = runtime_config.get("generic_llm_ocr_api_base", DEFAULT_API_BASE)
    api_key = runtime_config.get("generic_llm_ocr_api_key", "")
    model = runtime_config.get("generic_llm_ocr_model", DEFAULT_MODEL)
    mode = normalize_mode(runtime_config.get("generic_llm_ocr_mode", DEFAULT_MODE))

    endpoint = build_endpoint(api_base)
    prompt = build_prompt(mode, width, height, source_lang, target_lang)

    # Convert the raw image to a data URL for OpenAI-compatible chat-completions vision APIs.
    image_data_url = f"data:image/png;base64,{base64.b64encode(image_bytes).decode('utf-8')}"

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_data_url}},
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        "temperature": 0,
        "max_tokens": 4096,
    }

    headers = {"Content-Type": "application/json"}
    if api_key and not api_key.startswith("<your"):
        headers["Authorization"] = f"Bearer {api_key}"

    response = requests.post(endpoint, json=payload, headers=headers, timeout=180)
    response.raise_for_status()
    response_json = response.json()
    content = response_json["choices"][0]["message"]["content"]
    return content, model, mode


def process_llm_results(
    image: Image.Image,
    raw_response: str,
    mode: str,
    bbox_image_width: int,
    bbox_image_height: int,
) -> List[Dict]:
    text_objects: List[Dict] = []

    if not raw_response.strip() or is_no_text_response(raw_response):
        return text_objects

    for match in TRANSLATION_PATTERN.finditer(raw_response):
        llm_text = (match.group("text") or "").replace("</s>", "").strip()
        translated_text = (match.group("translated") or "").replace("</s>", "").strip()
        parsed_bbox = parse_bbox_values(match.group("bbox"), bbox_image_width, bbox_image_height)
        bbox = None
        if parsed_bbox is not None:
            bbox = remap_bbox_to_source(
                parsed_bbox,
                bbox_image_width,
                bbox_image_height,
                image.width,
                image.height,
            )

        if not llm_text or bbox is None:
            continue

        if is_no_text_response(llm_text) or (translated_text and is_no_text_response(translated_text)):
            continue

        text_value = llm_text
        translated_value = ""

        if mode == MODE_OCR_TRANSLATE:
            translated_value = translated_text or llm_text
            text_value = translated_value

        x1, y1, x2, y2 = bbox
        vertices = [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]

        text_obj = {
            "text": text_value,
            "x": x1,
            "y": y1,
            "width": x2 - x1,
            "height": y2 - y1,
            "vertices": vertices,
            "confidence": None,
            "text_orientation": "horizontal",
        }

        if translated_value:
            text_obj["translated_text"] = translated_value

        try:
            color_data = extract_foreground_background_colors(image, vertices)
            if color_data:
                attach_color_info(text_obj, color_data)
        except Exception as exc:
            print(f"Color extraction failed: {exc}")

        text_objects.append(text_obj)

    return text_objects


@app.post("/process")
async def process_image(request: Request):
    try:
        start_time = time.time()
        runtime_config = load_runtime_settings()

        source_lang = request.query_params.get("lang", runtime_config.get("source_language", "ja"))
        target_lang = get_target_language(request, runtime_config)

        image_bytes = await request.body()
        if not image_bytes:
            raise HTTPException(status_code=400, detail="No image data provided")

        image = Image.open(BytesIO(image_bytes)).convert("RGB")
        save_debug_request_image(image, source_lang)
        llm_image, llm_image_bytes = prepare_image_for_llm(image, runtime_config)
        if llm_image.size != image.size:
            save_debug_request_image(llm_image, source_lang, prefix="request_llm")

        raw_response, model, mode = query_llm(
            llm_image_bytes,
            llm_image.width,
            llm_image.height,
            runtime_config,
            source_lang,
            target_lang,
        )
        text_objects = process_llm_results(
            image,
            raw_response,
            mode,
            llm_image.width,
            llm_image.height,
        )

        return JSONResponse(
            content={
                "status": "success",
                "texts": text_objects,
                "processing_time": time.time() - start_time,
                "language": source_lang,
                "char_level": False,
                "backend": "generic_llm",
                "mode": mode,
                "model": model,
                "includes_translations": any("translated_text" in text_obj for text_obj in text_objects),
                "raw_response": raw_response,
            }
        )
    except Exception as exc:
        print(f"Error processing image: {exc}")
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "message": str(exc),
                "error_type": type(exc).__name__,
            },
        )


@app.post("/analyze_color")
async def analyze_color(request: Request):
    try:
        image_bytes = await request.body()
        if not image_bytes:
            raise HTTPException(status_code=400, detail="No image data provided")

        image = Image.open(BytesIO(image_bytes)).convert("RGB")
        width, height = image.size
        bbox = [[0, 0], [width, 0], [width, height], [0, height]]
        color_info = extract_foreground_background_colors(image, bbox)
        return JSONResponse(content={"status": "success", "color_info": color_info})
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": str(exc), "error_type": type(exc).__name__},
        )


@app.get("/info")
async def get_info():
    return JSONResponse(
        content={
            "service_name": SERVICE_NAME,
            "description": get_config_value(SERVICE_CONFIG, "description", ""),
            "service_install_version": SERVICE_INSTALL_VERSION,
            "venv_name": get_config_value(SERVICE_CONFIG, "venv_name", "ugt_generic_llm_ocr"),
            "port": SERVICE_PORT,
            "server_url": get_config_value(SERVICE_CONFIG, "server_url", "http://127.0.0.1"),
            "local_only": get_config_value(SERVICE_CONFIG, "local_only", "true") == "true",
            "github_url": get_config_value(SERVICE_CONFIG, "github_url", ""),
            "service_author": get_config_value(SERVICE_CONFIG, "service_author", ""),
            "color_analysis_available": _COLOR_ANALYSIS_IMPORT_ERROR is None,
            "color_analysis_error": str(_COLOR_ANALYSIS_IMPORT_ERROR) if _COLOR_ANALYSIS_IMPORT_ERROR else "",
        }
    )


@app.post("/shutdown")
async def shutdown():
    print("Shutdown request received...")

    async def shutdown_task():
        await asyncio.sleep(1)
        os._exit(0)

    asyncio.create_task(shutdown_task())

    return JSONResponse(
        content={
            "status": "success",
            "message": "Service shutting down",
        }
    )


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=SERVICE_PORT)