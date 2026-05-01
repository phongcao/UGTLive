"""FastAPI server for a generic OpenAI-compatible vision OCR backend."""

import asyncio
import atexit
import base64
import faulthandler
import json
import logging
import os
import re
import ssl
import sys
import threading
import time
import traceback
import unicodedata
from difflib import SequenceMatcher
from io import BytesIO
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import certifi

ssl._create_default_https_context = lambda: ssl.create_default_context(cafile=certifi.where())

if sys.platform.startswith("win") and hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import requests
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
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
LOG_DIR = Path(__file__).parent / "logs"
RUNTIME_LOG_PATH = LOG_DIR / "runtime.log"
FAULT_LOG_PATH = LOG_DIR / "fault.log"

SERVICE_NAME = get_config_value(SERVICE_CONFIG, "service_name", "Generic LLM OCR")
SERVICE_PORT = int(get_config_value(SERVICE_CONFIG, "port", "5005"))
SERVICE_INSTALL_VERSION = get_config_value(SERVICE_CONFIG, "service_install_version", "1")

DEFAULT_API_BASE = "http://127.0.0.1:1234"
DEFAULT_MODEL = "qwen2.5-vl-7b-instruct"
DEFAULT_MODE = "OCR + Translate"
DEFAULT_TARGET_LANGUAGE = "en"
DEFAULT_IGNORE_MENU_TEXT = False
DEFAULT_MENU_ITEMS_FILTER = False
DEFAULT_FUZZY_MATCH = True
DEFAULT_FUZZY_THRESHOLD = 0.85
DEFAULT_MAX_IMAGE_DIMENSION = 768
DEFAULT_MAX_IMAGE_TOTAL_PIXELS = 450000
DEFAULT_MAX_ASPECT_RATIO = 4.0
DEFAULT_PAD_TARGET_RATIO = 3.0
DEFAULT_TRANSLATION_CACHE_MAX_SIZE = 10000
DEFAULT_TRANSLATION_CACHE_SAVE_INTERVAL = 300  # seconds
TRANSLATION_CACHE_PATH = Path(__file__).parent / "translation_cache.json"
NO_TEXT_SENTINEL = "__UGTLIVE_NO_TEXT__"
DEBUG_IMAGE_DIR = Path(__file__).parent / "debug"
DEBUG_IMAGE_ENV_VAR = "UGTLIVE_VISUAL_STUDIO_DEBUG"

MODE_OCR_ONLY = "OCR Only"
MODE_OCR_TRANSLATE = "OCR + Translate"
MODE_OCR_THEN_TRANSLATE = "OCR Then Translate"

PROMPT_IGNORE_COMMON_MENU_TEXT = (
    "Ignore routine in-game menu labels, navigation UI, and HUD text that is generic or repeatedly present across frames. "
    "This includes single-word or short menu entries such as: "
    "Save, Load, Settings, Options, Back, Exit, Quit, Start, Continue, New Game, "
    "Talk, Action, Items, Status, Equipment, Skills, Magic, System, Map, Party, Formation, "
    "Inventory, Shop, Inn, Rest, Cancel, Confirm, Yes, No, OK, Close, Return, Resume, Help, "
    "as well as their equivalents in any language (e.g. 談話, 動作, 物品, 狀態, 系統, セーブ, ロード, アイテム, etc.). "
    "Also ignore standalone numeric indicators such as HP/MP bars, gold counters, level numbers, and stat labels. "
    "Focus on character dialogue, subtitles, narration, quest text, story text, cutscene text, "
    "and choice/decision prompts whose specific wording matters to the player. "
    f"If the image only contains ignorable menu or UI text, output EXACTLY {NO_TEXT_SENTINEL} and nothing else."
)

PROMPT_MENU_ITEMS_FILTER = (
    "Focus ONLY on in-game menu items, item names, item descriptions, battle commands, "
    "shop listings, equipment names, skill names, magic spell names, ability names, "
    "and any text that describes what an item or ability does. "
    "This includes consumables, weapons, armor, accessories, key items, and their stat descriptions. "
    "Also include battle menu commands such as Attack, Defend, Guard, Flee, Use Item, Cast, Summon, Limit Break, etc. "
    "and their equivalents in any language. "
    "Do NOT include character names, player names, enemy names, timers, currency amounts, "
    "HP/MP numbers, level numbers, damage numbers, generic navigation labels "
    "(like Back, Cancel, Confirm, Yes, No), or dialogue/story text. "
    f"If the image contains no menu items, item descriptions, or battle commands, output EXACTLY {NO_TEXT_SENTINEL} and nothing else."
)

PROMPT_OCR_ONLY = (
    "You are an OCR engine. Detect every text region in the image except standalone raw numbers or numeric-only text regions.\n"
    "Ignore numeric-only text such as 123, 12/50, 99%, 03:21, damage values, counters, stat-only readouts, and other HUD-style number displays.\n"
    "Keep numbers only when they are part of a larger phrase or sentence whose wording matters.\n"
    "For EACH text region output EXACTLY one line in this format:\n"
    "BBOX:[x1,y1,x2,y2]|TEXT:<the text>\n"
    "where x1,y1 is the top-left corner and x2,y2 is the bottom-right corner in pixel coordinates.\n"
    "Use the actual image pixel dimensions. No spaces around colons, commas, or pipes.\n"
    f"If no readable text is present, output EXACTLY {NO_TEXT_SENTINEL} and nothing else.\n"
    f"Never translate, explain, or wrap {NO_TEXT_SENTINEL} in any extra text.\n"
    "Output ONLY the list, no extra explanation."
)

PROMPT_OCR_TRANSLATE = (
    "You are an OCR and translation engine. Detect every text region in the image except standalone raw numbers or numeric-only text regions.\n"
    "Ignore numeric-only text such as 123, 12/50, 99%, 03:21, damage values, counters, stat-only readouts, and other HUD-style number displays.\n"
    "Keep numbers only when they are part of a larger phrase or sentence whose wording matters.\n"
    "First infer the likely overall context of the image and use it internally to choose accurate terminology and tone.\n"
    "For Chinese fantasy, wuxia, xianxia, cultivation, sect, deity, or historical dialogue, use natural Vietnamese Sino-Vietnamese localization terms when the target language is Vietnamese.\n"
    "For EACH text region output EXACTLY one line in this format:\n"
    "BBOX:[x1,y1,x2,y2]|TEXT:<translated text>\n"
    "where x1,y1 is the top-left corner and x2,y2 is the bottom-right corner in pixel coordinates.\n"
    "Use the actual image pixel dimensions. No spaces around colons, commas, or pipes.\n"
    "The TEXT field must contain ONLY the final translated text in the target language.\n"
    "Do NOT include the original/source text, transliterations, or extra labels.\n"
    "Do NOT output the inferred context.\n"
    f"If no readable text is present, output EXACTLY {NO_TEXT_SENTINEL} and nothing else.\n"
    f"Never translate, explain, or wrap {NO_TEXT_SENTINEL} in any extra text.\n"    
    "Output ONLY the list, no extra explanation."
)

PROMPT_TRANSLATE_TEXT = (
    "You are a translation engine. Translate each numbered line below from {source_lang} to {target_lang}.\n"
    "Output EXACTLY the same number of lines, each prefixed with the SAME number.\n"
    "Do NOT add, remove, or reorder lines. Do NOT add explanations.\n"
    "Format:\n"
    "1. <translated text>\n"
    "2. <translated text>\n"
    "...\n"
    "Output ONLY the numbered translations."
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
    r"^\s*(?:"
    r"BBOX:\s*\[?(?P<bbox_first>[\d.,\s]+)\]?\s*(?:\|\s*)?"
    r"(?:TEXT:\s*)?(?P<text_after_bbox>.*?)(?:\s*\|\s*(?:TRANS|TRANSLATED|TARGET|EN):\s*(?P<translated_after_bbox>.*?))?"
    r"|"
    r"TEXT:\s*(?P<text_before_bbox>.*?)\s*\|\s*"
    r"(?:(?:TRANS|TRANSLATED|TARGET|EN):\s*(?P<translated_before_bbox>.*?)\s*\|\s*)?"
    r"BBOX:\s*\[?(?P<bbox_after_text>[\d.,\s]+)\]?"
    r")\s*$",
    re.IGNORECASE,
)

STREAMING_BBOX_PATTERN = re.compile(
    r"^\s*BBOX:\s*\[?(?P<bbox>[\d.,\s]+)\]?",
    re.IGNORECASE,
)

_BBOX_IN_TEXT_RE = re.compile(
    r"\s*\|?\s*BBOX\"?:\s*\[?[^\]]*\]?\s*\|?\s*",
    re.IGNORECASE,
)


def _strip_bbox_from_text(text: str) -> str:
    """Remove residual BBOX: [...] markers (and surrounding pipes) from text."""
    return _BBOX_IN_TEXT_RE.sub(" ", text).strip()


def _clean_llm_text(text: str) -> str:
    """Clean common LLM text artifacts from an extracted text field."""
    cleaned = text.replace("</s>", "").strip()
    cleaned = _strip_bbox_from_text(cleaned)
    # Replace literal "\n" escape sequences (two characters) that some LLMs emit
    cleaned = cleaned.replace("\\n", " ")
    # Replace ~ and ～ tone markers (CJK expressive style) with ! in translated text
    cleaned = cleaned.replace("\uff5e", "!").replace("~", "!")
    return re.sub(r"\s+", " ", cleaned).strip()


def extract_translation_match_fields(match: re.Match) -> Tuple[str, str, str]:
    raw_bbox = (match.group("bbox_first") or match.group("bbox_after_text") or "").strip()
    llm_text = (match.group("text_after_bbox") or match.group("text_before_bbox") or "")
    translated_text = (
        match.group("translated_after_bbox")
        or match.group("translated_before_bbox")
        or ""
    )
    return (
        raw_bbox,
        _clean_llm_text(llm_text),
        _clean_llm_text(translated_text),
    )


def split_completed_response_lines(buffer: str) -> Tuple[List[str], str]:
    completed_lines: List[str] = []
    pending_line = ""

    for line in buffer.splitlines(keepends=True):
        if line.endswith(("\n", "\r")):
            completed_lines.append(line.rstrip("\r\n"))
        else:
            pending_line = line

    return completed_lines, pending_line


def configure_runtime_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)

    logger = logging.getLogger("generic_llm_ocr")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    logger.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | pid=%(process)d | %(threadName)s | %(message)s"
    )

    runtime_handler = RotatingFileHandler(
        RUNTIME_LOG_PATH,
        maxBytes=2 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    runtime_handler.setFormatter(formatter)
    logger.addHandler(runtime_handler)

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    logger.addHandler(stdout_handler)

    return logger


LOGGER = configure_runtime_logging()
_FAULT_LOG_FILE = open(FAULT_LOG_PATH, "a", encoding="utf-8")
faulthandler.enable(_FAULT_LOG_FILE, all_threads=True)


def append_fault_log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    _FAULT_LOG_FILE.write(f"[{timestamp}] {message}\n")
    _FAULT_LOG_FILE.flush()


def log_uncaught_exception(exc_type, exc_value, exc_traceback) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return

    LOGGER.critical("Unhandled top-level exception", exc_info=(exc_type, exc_value, exc_traceback))
    append_fault_log("Unhandled top-level exception:\n" + "".join(traceback.format_exception(exc_type, exc_value, exc_traceback)))
    sys.__excepthook__(exc_type, exc_value, exc_traceback)


sys.excepthook = log_uncaught_exception


def log_process_exit() -> None:
    LOGGER.info("Process exiting normally pid=%s", os.getpid())
    append_fault_log(f"Process exiting normally pid={os.getpid()}")
    _FAULT_LOG_FILE.close()


atexit.register(log_process_exit)


class TranslationCache:
    """LFU cache for OCR-to-translation mappings, persisted to JSON."""

    def __init__(self, cache_path: Path, max_size: int, save_interval: float):
        self._cache: Dict[str, Dict] = {}
        self._cache_path = cache_path
        self._max_size = max_size
        self._save_interval = save_interval
        self._dirty = False
        self._last_save_time = time.time()
        self._lock = threading.Lock()
        self._load()

    # Fullwidth → ASCII punctuation map for cache key normalization
    _FULLWIDTH_PUNCTUATION_MAP = str.maketrans({
        "\uFF1A": ":",   # ：
        "\uFF1B": ";",   # ；
        "\uFF0C": ",",   # ，
        "\uFF0E": ".",   # ．
        "\u3002": ".",   # 。
        "\uFF01": "!",   # ！
        "\uFF1F": "?",   # ？
        "\uFF08": "(",   # （
        "\uFF09": ")",   # ）
        "\u3010": "[",   # 【
        "\u3011": "]",   # 】
        "\u201C": '"',   # "
        "\u201D": '"',   # "
        "\u2018": "'",   # '
        "\u2019": "'",   # '
        "\uFF5E": "~",   # ～
        "\uFF06": "&",   # ＆
        "\uFF20": "@",   # ＠
        "\uFF03": "#",   # ＃
        "\uFF04": "$",   # ＄
        "\uFF05": "%",   # ％
        "\uFF3E": "^",   # ＾
        "\uFF0A": "*",   # ＊
        "\uFF0B": "+",   # ＋
        "\uFF1D": "=",   # ＝
        "\uFF0F": "/",   # ／
        "\uFF3C": "\\",  # ＼
        "\uFF5C": "|",   # ｜
        "\uFF1C": "<",   # ＜
        "\uFF1E": ">",   # ＞
    })

    @staticmethod
    def _normalize_key(text: str, target_lang: str) -> str:
        normalized = (text or "").strip()
        normalized = normalized.replace("\\n", " ").replace("\n", " ")
        normalized = normalized.translate(TranslationCache._FULLWIDTH_PUNCTUATION_MAP)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        if not normalized:
            return ""
        return f"{target_lang.strip().lower()}||{normalized}"

    def get(self, source_text: str, target_lang: str) -> Optional[str]:
        key = self._normalize_key(source_text, target_lang)
        if not key:
            return None
        with self._lock:
            entry = self._cache.get(key)
            if entry is not None:
                entry["hits"] += 1
                self._dirty = True
                self._maybe_periodic_save()
                return entry["translated"]
        return None

    def put(self, source_text: str, translated_text: str, target_lang: str) -> None:
        key = self._normalize_key(source_text, target_lang)
        if not key or not translated_text:
            return
        with self._lock:
            existing_hits = self._cache.get(key, {}).get("hits", 0)
            self._cache[key] = {
                "source": source_text.strip(),
                "translated": translated_text,
                "target_lang": target_lang.strip().lower(),
                "hits": existing_hits + 1,
            }
            self._dirty = True
            self._evict_if_needed()
            self._maybe_periodic_save()

    def _evict_if_needed(self) -> None:
        if len(self._cache) <= self._max_size:
            return
        evict_count = max(1, len(self._cache) // 10)
        sorted_keys = sorted(self._cache, key=lambda k: self._cache[k]["hits"])
        for key in sorted_keys[:evict_count]:
            del self._cache[key]
        LOGGER.info(
            "Translation cache evicted %d entries, size now %d",
            evict_count, len(self._cache),
        )

    def _maybe_periodic_save(self) -> None:
        now = time.time()
        if self._dirty and (now - self._last_save_time) >= self._save_interval:
            self._save_unlocked()

    def _load(self) -> None:
        if not self._cache_path.exists():
            LOGGER.info("No translation cache file at %s, starting fresh", self._cache_path)
            return
        try:
            with open(self._cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._cache = data
            LOGGER.info(
                "Loaded translation cache: %d entries from %s",
                len(self._cache), self._cache_path,
            )
        except Exception as exc:
            LOGGER.warning(
                "Failed to load translation cache from %s: %s",
                self._cache_path, exc,
            )

    def save(self) -> None:
        with self._lock:
            self._save_unlocked()

    def _save_unlocked(self) -> None:
        if not self._dirty:
            return
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._cache_path.with_suffix(".tmp")
            sorted_cache = dict(
                sorted(self._cache.items(), key=lambda item: item[1].get("hits", 0), reverse=True)
            )
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(sorted_cache, f, ensure_ascii=False, indent=2)
            tmp_path.replace(self._cache_path)
            self._dirty = False
            self._last_save_time = time.time()
            LOGGER.info(
                "Saved translation cache: %d entries to %s",
                len(self._cache), self._cache_path,
            )
        except Exception as exc:
            LOGGER.warning(
                "Failed to save translation cache to %s: %s",
                self._cache_path, exc,
            )

    def __len__(self) -> int:
        with self._lock:
            return len(self._cache)


_TRANSLATION_CACHE = TranslationCache(
    TRANSLATION_CACHE_PATH,
    DEFAULT_TRANSLATION_CACHE_MAX_SIZE,
    DEFAULT_TRANSLATION_CACHE_SAVE_INTERVAL,
)
atexit.register(_TRANSLATION_CACHE.save)

app = FastAPI(title=SERVICE_NAME, version=SERVICE_INSTALL_VERSION)
OCR_PROCESS_SEMAPHORE = asyncio.Semaphore(1)

# Reuse a single Session for all LLM requests (HTTP keep-alive / connection pooling)
_LLM_SESSION = requests.Session()
_LLM_SESSION.headers.update({"Content-Type": "application/json"})

# Cache for fuzzy match deduplication (sync + streaming share the same cache)
_last_combined_text: str = ""
_last_text_objects: List[Dict] = []


try:
    RESAMPLE_LANCZOS = Image.Resampling.LANCZOS
except AttributeError:
    RESAMPLE_LANCZOS = Image.LANCZOS


if _COLOR_ANALYSIS_IMPORT_ERROR is not None:
    LOGGER.warning(
        "Color analysis disabled for Generic LLM OCR: "
        f"{type(_COLOR_ANALYSIS_IMPORT_ERROR).__name__}: {_COLOR_ANALYSIS_IMPORT_ERROR}"
    )


def log_asyncio_exception(loop: asyncio.AbstractEventLoop, context: Dict) -> None:
    exception = context.get("exception")
    message = context.get("message", "Unhandled asyncio exception")
    if exception is not None:
        LOGGER.error("%s", message, exc_info=(type(exception), exception, exception.__traceback__))
        append_fault_log(message + "\n" + "".join(traceback.format_exception(type(exception), exception, exception.__traceback__)))
        return

    LOGGER.error("%s", message)
    append_fault_log(message)


def load_runtime_settings() -> Dict[str, str]:
    return parse_service_config(str(APP_CONFIG_PATH))


def normalize_mode(mode: str) -> str:
    stripped = mode.strip().lower()
    if stripped == MODE_OCR_ONLY.lower():
        return MODE_OCR_ONLY
    if stripped == MODE_OCR_THEN_TRANSLATE.lower():
        return MODE_OCR_THEN_TRANSLATE
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


def is_ignore_menu_text_enabled(runtime_config: Dict[str, str]) -> bool:
    value = runtime_config.get(
        "generic_llm_ocr_ignore_menu_text",
        str(DEFAULT_IGNORE_MENU_TEXT).lower(),
    )
    return (value or "").strip().lower() == "true"


def is_menu_items_filter_enabled(runtime_config: Dict[str, str]) -> bool:
    value = runtime_config.get(
        "generic_llm_ocr_menu_items_filter",
        str(DEFAULT_MENU_ITEMS_FILTER).lower(),
    )
    return (value or "").strip().lower() == "true"


def is_fuzzy_match_enabled(runtime_config: Dict[str, str]) -> bool:
    value = runtime_config.get(
        "generic_llm_ocr_fuzzy_match",
        str(DEFAULT_FUZZY_MATCH).lower(),
    )
    return (value or "").strip().lower() == "true"


def get_fuzzy_threshold(runtime_config: Dict[str, str]) -> float:
    raw = (runtime_config.get("generic_llm_ocr_fuzzy_threshold", "") or "").strip()
    if not raw:
        return DEFAULT_FUZZY_THRESHOLD
    try:
        return max(0.0, min(1.0, float(raw)))
    except ValueError:
        LOGGER.warning("Invalid generic_llm_ocr_fuzzy_threshold: %s. Using %.2f.", raw, DEFAULT_FUZZY_THRESHOLD)
        return DEFAULT_FUZZY_THRESHOLD


def combine_text_for_comparison(text_objects: List[Dict]) -> str:
    """Build a single string from all text objects for fuzzy comparison."""
    return "\n".join(obj.get("translated_text") or obj.get("text", "") for obj in text_objects)


def check_fuzzy_unchanged(
    text_objects: List[Dict],
    runtime_config: Dict[str, str],
) -> bool:
    """Return True if *text_objects* are fuzzy-similar to the cached previous result.

    When True the caller should treat the content as unchanged.
    The cache is updated only when the content is considered *different*.
    """
    global _last_combined_text, _last_text_objects

    if not is_fuzzy_match_enabled(runtime_config):
        return False

    combined = combine_text_for_comparison(text_objects)

    # Nothing cached yet – store and report "changed"
    if not _last_combined_text:
        _last_combined_text = combined
        _last_text_objects = text_objects
        return False

    threshold = get_fuzzy_threshold(runtime_config)
    ratio = SequenceMatcher(None, _last_combined_text, combined).ratio()

    LOGGER.info(
        "Fuzzy match ratio=%.4f threshold=%.2f cached_len=%d new_len=%d",
        ratio,
        threshold,
        len(_last_combined_text),
        len(combined),
    )

    if ratio >= threshold:
        # Similar enough – keep the cached version, don't update
        return True

    # Different – update cache
    _last_combined_text = combined
    _last_text_objects = text_objects
    return False


def build_endpoint(api_base: str) -> str:
    base = (api_base or DEFAULT_API_BASE).strip().rstrip("/")
    if base.endswith("/v1/chat/completions"):
        return base
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def build_prompt(
    mode: str,
    width: int,
    height: int,
    source_lang: str,
    target_lang: str,
    ignore_menu_text: bool,
    menu_items_filter: bool = False,
) -> str:
    filter_prompt = ""
    if menu_items_filter:
        filter_prompt = f"{PROMPT_MENU_ITEMS_FILTER} "
    elif ignore_menu_text:
        filter_prompt = f"{PROMPT_IGNORE_COMMON_MENU_TEXT} "

    if mode == MODE_OCR_ONLY or mode == MODE_OCR_THEN_TRANSLATE:
        return (
            f"The image is {width}x{height} pixels. Source language hint: {get_language_name(source_lang)}. "
            f"{filter_prompt}{PROMPT_OCR_ONLY} /no_think"
        )
    return (
        f"The image is {width}x{height} pixels. Source language hint: {get_language_name(source_lang)}. "
        f"Translate all detected text to {get_language_name(target_lang)}. "
        f"{filter_prompt}{PROMPT_OCR_TRANSLATE} /no_think"
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
        LOGGER.info("Saved Generic LLM OCR debug image to %s", debug_path)
        return debug_path
    except Exception as exc:
        LOGGER.warning("Failed to save Generic LLM OCR debug image: %s", exc)
        return None


def get_non_negative_int(runtime_config: Dict[str, str], key: str, default: int) -> int:
    raw_value = (runtime_config.get(key, "") or "").strip()
    if not raw_value:
        return default

    try:
        return max(0, int(raw_value))
    except ValueError:
        LOGGER.warning("Invalid Generic LLM OCR config for %s: %s. Using %s.", key, raw_value, default)
        return default


def get_optional_positive_int_query_param(request: Request, key: str) -> Optional[int]:
    raw_value = (request.query_params.get(key, "") or "").strip()
    if not raw_value:
        return None

    try:
        parsed_value = int(raw_value)
    except ValueError:
        LOGGER.warning("Invalid Generic LLM OCR query param %s=%s. Ignoring.", key, raw_value)
        return None

    return parsed_value if parsed_value > 0 else None


def prepare_image_for_llm(
    image: Image.Image, runtime_config: Dict[str, str],
) -> Tuple[Image.Image, bytes, int, int, int, int]:
    """Resize and optionally pad image for LLM.

    Returns (prepared_image, png_bytes, pad_left, pad_top, content_width, content_height).
    ``pad_left``/``pad_top`` are the pixel offsets of the original content within
    the (possibly padded) image.  ``content_width``/``content_height`` are the
    dimensions of the actual content region (before padding).
    """
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
        LOGGER.info(
            "Resized Generic LLM OCR request image "
            f"from {original_width}x{original_height} to {resized_width}x{resized_height}"
        )
    else:
        prepared_image = image

    # Pad extreme aspect ratios so the LLM can produce accurate bounding boxes.
    content_width, content_height = prepared_image.size
    pad_left, pad_top = 0, 0
    short_edge = max(1, min(content_width, content_height))
    long_edge = max(content_width, content_height)
    ratio = long_edge / float(short_edge)

    if ratio > DEFAULT_MAX_ASPECT_RATIO:
        target_short = max(1, int(round(long_edge / DEFAULT_PAD_TARGET_RATIO)))
        if content_width > content_height:
            # Wide image: pad height
            pad_top = (target_short - content_height) // 2
            padded = Image.new("RGB", (content_width, target_short), (0, 0, 0))
            padded.paste(prepared_image, (0, pad_top))
        else:
            # Tall image: pad width
            pad_left = (target_short - content_width) // 2
            padded = Image.new("RGB", (target_short, content_height), (0, 0, 0))
            padded.paste(prepared_image, (pad_left, 0))
        prepared_image = padded
        LOGGER.info(
            "Padded extreme aspect ratio image from %sx%s to %sx%s "
            "(pad_left=%s, pad_top=%s, ratio=%.1f->%.1f)",
            content_width, content_height,
            prepared_image.width, prepared_image.height,
            pad_left, pad_top,
            ratio,
            max(prepared_image.width, prepared_image.height)
            / float(max(1, min(prepared_image.width, prepared_image.height))),
        )

    output_buffer = BytesIO()
    prepared_image.save(output_buffer, format="PNG")
    return prepared_image, output_buffer.getvalue(), pad_left, pad_top, content_width, content_height


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

    # Some vision LLMs (e.g. Qwen VL) use [0, 999] normalized coordinates but
    # occasionally output values slightly above 999 for text near image edges.
    # Detect this: coords exceed the image dimensions but stay within a
    # reasonable overshoot range, so they clearly aren't raw pixel values.
    likely_999_overshoot = (
        not all_leq999
        and not all_leq1
        and all(0.0 <= c <= 1100.0 for c in coords)
        and max(coords) > max(img_w, img_h)
    )

    if all_leq1:
        x1, x2 = x1 * img_w, x2 * img_w
        y1, y2 = y1 * img_h, y2 * img_h
    elif all_leq999 or likely_999_overshoot:
        if likely_999_overshoot:
            LOGGER.warning(
                "Clamping likely [0,999] bbox coords that exceeded 999: [%s] on %sx%s image",
                raw_bbox, img_w, img_h,
            )
        x1 = min(999.0, max(0.0, x1))
        y1 = min(999.0, max(0.0, y1))
        x2 = min(999.0, max(0.0, x2))
        y2 = min(999.0, max(0.0, y2))
        x1, x2 = (x1 / 999.0) * img_w, (x2 / 999.0) * img_w
        y1, y2 = (y1 / 999.0) * img_h, (y2 / 999.0) * img_h

    x1_i = max(0, min(img_w, int(round(min(x1, x2)))))
    y1_i = max(0, min(img_h, int(round(min(y1, y2)))))
    x2_i = max(0, min(img_w, int(round(max(x1, x2)))))
    y2_i = max(0, min(img_h, int(round(max(y1, y2)))))

    if x2_i <= x1_i or y2_i <= y1_i:
        return None

    return x1_i, y1_i, x2_i, y2_i


def unpad_bbox(
    bbox: Tuple[int, int, int, int],
    pad_left: int,
    pad_top: int,
    content_width: int,
    content_height: int,
) -> Optional[Tuple[int, int, int, int]]:
    """Shift a BBOX from padded image space into content (unpadded) space."""
    x1 = max(0, min(content_width, bbox[0] - pad_left))
    y1 = max(0, min(content_height, bbox[1] - pad_top))
    x2 = max(0, min(content_width, bbox[2] - pad_left))
    y2 = max(0, min(content_height, bbox[3] - pad_top))
    if x2 <= x1 or y2 <= y1:
        return None
    return (x1, y1, x2, y2)


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
    ignore_menu_text = is_ignore_menu_text_enabled(runtime_config)
    menu_items_filter = is_menu_items_filter_enabled(runtime_config)

    endpoint = build_endpoint(api_base)
    prompt = build_prompt(mode, width, height, source_lang, target_lang, ignore_menu_text, menu_items_filter)

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

    # Disable thinking/reasoning on models that support it (e.g. Qwen3/3.5)
    # to avoid hidden chain-of-thought overhead that dramatically increases latency.
    payload["chat_template_kwargs"] = {"enable_thinking": False}

    headers = {"Content-Type": "application/json"}
    if api_key and not api_key.startswith("<your"):
        headers["Authorization"] = f"Bearer {api_key}"

    request_started = time.time()
    LOGGER.info(
        "LLM request start model=%s mode=%s ignore_menu_text=%s endpoint=%s image=%sx%s source_lang=%s target_lang=%s payload_bytes=%s",
        model,
        mode,
        ignore_menu_text,
        endpoint,
        width,
        height,
        source_lang,
        target_lang,
        len(image_bytes),
    )

    try:
        response = _LLM_SESSION.post(endpoint, json=payload, headers=headers, timeout=180)
        response.raise_for_status()
    except requests.RequestException:
        LOGGER.exception(
            "LLM request failed model=%s mode=%s endpoint=%s after %.1f ms",
            model,
            mode,
            endpoint,
            (time.time() - request_started) * 1000.0,
        )
        raise

    response_json = response.json()
    content = response_json["choices"][0]["message"]["content"]
    usage = response_json.get("usage", {})
    LOGGER.info(
        "LLM request complete model=%s mode=%s status=%s duration_ms=%.1f response_chars=%s prompt_tokens=%s completion_tokens=%s total_tokens=%s",
        model,
        mode,
        response.status_code,
        (time.time() - request_started) * 1000.0,
        len(content or ""),
        usage.get("prompt_tokens"),
        usage.get("completion_tokens"),
        usage.get("total_tokens"),
    )
    LOGGER.info("LLM raw response: %s", content)
    return content, model, mode


# Regex matching text that needs no translation (numbers, timestamps, ranges, punctuation)
_NO_TRANSLATE_RE = re.compile(r"^[\d\s\-:;.,~!?/\\|<>()[\]{}+=%^*#@&$'\"]+$")


def _is_no_translate_text(text: str) -> bool:
    """Return True if the text is purely numeric/punctuation and needs no translation."""
    normalized = (text or "").strip().translate(TranslationCache._FULLWIDTH_PUNCTUATION_MAP)
    return bool(_NO_TRANSLATE_RE.match(normalized))


def query_llm_translate_text(
    text_objects: List[Dict],
    runtime_config: Dict[str, str],
    source_lang: str,
    target_lang: str,
) -> List[str]:
    """Make a text-only LLM call to translate OCR results in a second pass.

    Cached translations are reused; only uncached texts are sent to the LLM.
    """
    if not text_objects:
        return []

    translations: List[str] = [""] * len(text_objects)
    uncached_indices: List[int] = []

    # Check cache for each text object; skip translation for numeric/punctuation-only text
    for i, obj in enumerate(text_objects):
        source = obj["text"]
        if _is_no_translate_text(source):
            translations[i] = source
            continue
        cached = _TRANSLATION_CACHE.get(source, target_lang)
        if cached is not None:
            translations[i] = cached
        else:
            uncached_indices.append(i)

    cached_count = len(text_objects) - len(uncached_indices)
    LOGGER.info(
        "Translation cache: %d/%d cached, %d to translate",
        cached_count, len(text_objects), len(uncached_indices),
    )

    if not uncached_indices:
        return translations

    # Build numbered list of only uncached source texts
    uncached_objects = [text_objects[i] for i in uncached_indices]

    api_base = runtime_config.get("generic_llm_ocr_api_base", DEFAULT_API_BASE)
    api_key = runtime_config.get("generic_llm_ocr_api_key", "")
    model = runtime_config.get("generic_llm_ocr_model", DEFAULT_MODEL)
    endpoint = build_endpoint(api_base)

    numbered_lines = []
    for i, obj in enumerate(uncached_objects, 1):
        numbered_lines.append(f"{i}. {obj['text']}")
    numbered_text = "\n".join(numbered_lines)

    prompt = PROMPT_TRANSLATE_TEXT.format(
        source_lang=get_language_name(source_lang),
        target_lang=get_language_name(target_lang),
    ) + "\n\n" + numbered_text

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": prompt,
            }
        ],
        "temperature": 0,
        "max_tokens": 4096,
    }
    payload["chat_template_kwargs"] = {"enable_thinking": False}

    headers = {"Content-Type": "application/json"}
    if api_key and not api_key.startswith("<your"):
        headers["Authorization"] = f"Bearer {api_key}"

    request_started = time.time()
    LOGGER.info(
        "LLM translation request start model=%s endpoint=%s source_lang=%s target_lang=%s text_count=%s (uncached)",
        model, endpoint, source_lang, target_lang, len(uncached_objects),
    )

    try:
        response = _LLM_SESSION.post(endpoint, json=payload, headers=headers, timeout=180)
        response.raise_for_status()
    except requests.RequestException:
        LOGGER.exception(
            "LLM translation request failed model=%s endpoint=%s after %.1f ms",
            model, endpoint, (time.time() - request_started) * 1000.0,
        )
        raise

    response_json = response.json()
    content = response_json["choices"][0]["message"]["content"]
    usage = response_json.get("usage", {})
    LOGGER.info(
        "LLM translation request complete model=%s duration_ms=%.1f response_chars=%s prompt_tokens=%s completion_tokens=%s",
        model,
        (time.time() - request_started) * 1000.0,
        len(content or ""),
        usage.get("prompt_tokens"),
        usage.get("completion_tokens"),
    )
    LOGGER.info("LLM translation raw response: %s", content)

    # Parse numbered translations from response
    new_translations: List[str] = [""] * len(uncached_objects)
    if content:
        for line in content.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            # Match lines like "1. translated text" or "1) translated text"
            m = re.match(r"^(\d+)[.)]\s*(.+)$", line)
            if m:
                idx = int(m.group(1)) - 1
                if 0 <= idx < len(new_translations):
                    new_translations[idx] = m.group(2).strip()

    # Merge new translations into results and update cache
    for list_idx, orig_idx in enumerate(uncached_indices):
        trans = new_translations[list_idx]
        if trans:
            translations[orig_idx] = trans
            _TRANSLATION_CACHE.put(text_objects[orig_idx]["text"], trans, target_lang)

    return translations


def _rejoin_continuation_lines(raw_response: str) -> List[str]:
    """Rejoin lines where the LLM inserted a newline inside a TEXT field.

    Any line that does NOT start with ``BBOX:`` is treated as a continuation
    of the previous BBOX line and appended to it (separated by a space).
    """
    merged: List[str] = []
    for line in raw_response.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if STREAMING_BBOX_PATTERN.match(stripped):
            merged.append(stripped)
        elif merged:
            # Continuation of the previous BBOX line's text
            merged[-1] = merged[-1] + " " + stripped
        # else: stray text before any BBOX – ignore
    return merged


def process_llm_results(
    image: Image.Image,
    raw_response: str,
    mode: str,
    bbox_image_width: int,
    bbox_image_height: int,
    source_image_width: Optional[int] = None,
    source_image_height: Optional[int] = None,
    pad_left: int = 0,
    pad_top: int = 0,
    content_width: Optional[int] = None,
    content_height: Optional[int] = None,
) -> List[Dict]:
    text_objects: List[Dict] = []

    if not raw_response.strip() or is_no_text_response(raw_response):
        return text_objects

    for raw_line in _rejoin_continuation_lines(raw_response):
        text_obj = process_single_text_object(
            raw_line,
            image,
            mode,
            bbox_image_width,
            bbox_image_height,
            source_image_width,
            source_image_height,
            pad_left,
            pad_top,
            content_width,
            content_height,
        )
        if text_obj is not None:
            text_objects.append(text_obj)

    return text_objects


def process_image_sync(
    image_bytes: bytes,
    source_lang: str,
    target_lang: str,
    source_image_width: Optional[int] = None,
    source_image_height: Optional[int] = None,
) -> Dict:
    start_time = time.time()
    runtime_config = load_runtime_settings()

    t0 = time.time()
    image = Image.open(BytesIO(image_bytes)).convert("RGB")
    source_image_width = source_image_width or image.width
    source_image_height = source_image_height or image.height
    save_debug_request_image(image, source_lang)
    llm_image, llm_image_bytes, pad_left, pad_top, content_w, content_h = prepare_image_for_llm(image, runtime_config)
    if llm_image.size != image.size:
        save_debug_request_image(llm_image, source_lang, prefix="request_llm")
    t_prep = time.time()
    LOGGER.info(
        "Timing: image_prep=%.1fms input=%sx%s source=%sx%s llm=%sx%s payload_bytes=%s",
        (t_prep - t0) * 1000.0,
        image.width, image.height,
        source_image_width, source_image_height,
        llm_image.width, llm_image.height,
        len(llm_image_bytes),
    )

    raw_response, model, mode = query_llm(
        llm_image_bytes,
        llm_image.width,
        llm_image.height,
        runtime_config,
        source_lang,
        target_lang,
    )
    t_llm = time.time()

    text_objects = process_llm_results(
        image,
        raw_response,
        mode,
        llm_image.width,
        llm_image.height,
        source_image_width,
        source_image_height,
        pad_left,
        pad_top,
        content_w,
        content_h,
    )
    t_post = time.time()

    # Second pass: translate OCR results via a text-only LLM call
    if mode == MODE_OCR_THEN_TRANSLATE and text_objects:
        translations = query_llm_translate_text(
            text_objects, runtime_config, source_lang, target_lang,
        )
        for obj, trans in zip(text_objects, translations):
            if trans:
                obj["translated_text"] = trans
        t_post = time.time()

    # Fuzzy match: if the new text is similar enough to the last result, return cached objects
    text_unchanged = False
    if text_objects and check_fuzzy_unchanged(text_objects, runtime_config):
        LOGGER.info("Fuzzy match: text considered unchanged, returning cached text objects")
        text_objects = _last_text_objects
        text_unchanged = True

    LOGGER.info(
        "Timing: image_prep=%.1fms llm_call=%.1fms post_process=%.1fms total=%.1fms response_chars=%s",
        (t_prep - t0) * 1000.0,
        (t_llm - t_prep) * 1000.0,
        (t_post - t_llm) * 1000.0,
        (t_post - start_time) * 1000.0,
        len(raw_response),
    )

    return {
        "status": "success",
        "texts": text_objects,
        "processing_time": time.time() - start_time,
        "language": source_lang,
        "char_level": False,
        "backend": "generic_llm",
        "mode": mode,
        "model": model,
        "includes_translations": any("translated_text" in text_obj for text_obj in text_objects),
        "text_unchanged": text_unchanged,
        "raw_response": raw_response,
    }


def analyze_color_sync(image_bytes: bytes) -> Dict:
    image = Image.open(BytesIO(image_bytes)).convert("RGB")
    width, height = image.size
    bbox = [[0, 0], [width, 0], [width, height], [0, height]]
    color_info = extract_foreground_background_colors(image, bbox)
    return {"status": "success", "color_info": color_info}


def process_single_text_object(
    raw_line: str,
    image: Image.Image,
    mode: str,
    bbox_image_width: int,
    bbox_image_height: int,
    source_image_width: Optional[int] = None,
    source_image_height: Optional[int] = None,
    pad_left: int = 0,
    pad_top: int = 0,
    content_width: Optional[int] = None,
    content_height: Optional[int] = None,
) -> Optional[Dict]:
    """Parse a single OCR response line into a text object dict (or None)."""
    content_w = content_width or bbox_image_width
    content_h = content_height or bbox_image_height

    match = TRANSLATION_PATTERN.match((raw_line or "").strip())
    if match is None:
        return None

    raw_bbox, llm_text, translated_text = extract_translation_match_fields(match)
    parsed_bbox = parse_bbox_values(raw_bbox, bbox_image_width, bbox_image_height)
    bbox = None
    color_bbox = None
    if parsed_bbox is not None:
        if pad_left or pad_top:
            parsed_bbox = unpad_bbox(parsed_bbox, pad_left, pad_top, content_w, content_h)
            if parsed_bbox is None:
                return None
        color_bbox = remap_bbox_to_source(
            parsed_bbox,
            content_w,
            content_h,
            image.width,
            image.height,
        )
        bbox = remap_bbox_to_source(
            parsed_bbox,
            content_w,
            content_h,
            source_image_width or image.width,
            source_image_height or image.height,
        )

    if not llm_text or bbox is None:
        return None

    if is_no_text_response(llm_text) or (translated_text and is_no_text_response(translated_text)):
        return None

    text_value = llm_text
    translated_value = ""

    if mode == MODE_OCR_TRANSLATE:
        translated_value = translated_text or llm_text
        text_value = translated_value

    x1, y1, x2, y2 = bbox
    vertices = [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]

    text_obj: Dict = {
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
        color_vertices = vertices
        if color_bbox is not None:
            cx1, cy1, cx2, cy2 = color_bbox
            color_vertices = [[cx1, cy1], [cx2, cy1], [cx2, cy2], [cx1, cy2]]
        color_data = extract_foreground_background_colors(image, color_vertices)
        if color_data:
            attach_color_info(text_obj, color_data)
    except Exception as exc:
        LOGGER.warning("Color extraction failed: %s", exc)

    return text_obj


def process_partial_streaming_text_object(
    raw_line: str,
    image: Image.Image,
    bbox_image_width: int,
    bbox_image_height: int,
    source_image_width: Optional[int] = None,
    source_image_height: Optional[int] = None,
    pad_left: int = 0,
    pad_top: int = 0,
    content_width: Optional[int] = None,
    content_height: Optional[int] = None,
) -> Optional[Dict]:
    content_w = content_width or bbox_image_width
    content_h = content_height or bbox_image_height

    stripped_line = (raw_line or "").strip()
    if not stripped_line:
        return None

    bbox_match = STREAMING_BBOX_PATTERN.match(stripped_line)
    if bbox_match is None:
        return None

    parsed_bbox = parse_bbox_values(bbox_match.group("bbox"), bbox_image_width, bbox_image_height)
    if parsed_bbox is None:
        return None

    if pad_left or pad_top:
        parsed_bbox = unpad_bbox(parsed_bbox, pad_left, pad_top, content_w, content_h)
        if parsed_bbox is None:
            return None

    bbox = remap_bbox_to_source(
        parsed_bbox,
        content_w,
        content_h,
        source_image_width or image.width,
        source_image_height or image.height,
    )
    if bbox is None:
        return None

    text_value = ""
    text_after_bbox = stripped_line[bbox_match.end():]
    # Try explicit |TEXT: first, then fall back to whatever follows the bbox
    explicit_marker = re.search(r"\|\s*TEXT:\s*", text_after_bbox, re.IGNORECASE)
    if explicit_marker is not None:
        text_value = text_after_bbox[explicit_marker.end():].replace("</s>", "").strip()
    elif text_after_bbox.strip():
        # No |TEXT: marker — text directly follows the bbox
        raw = text_after_bbox.lstrip("|").strip()
        if raw.upper().startswith("TEXT:"):
            raw = raw[5:].strip()
        text_value = raw.replace("</s>", "").strip()
    text_value = _strip_bbox_from_text(text_value)
    if text_value and is_no_text_response(text_value):
        return None

    x1, y1, x2, y2 = bbox
    return {
        "text": text_value,
        "x": x1,
        "y": y1,
        "width": x2 - x1,
        "height": y2 - y1,
        "text_orientation": "horizontal",
    }


def query_llm_streaming(
    image_bytes: bytes,
    width: int,
    height: int,
    runtime_config: Dict[str, str],
    source_lang: str,
    target_lang: str,
):
    """Generator that yields (accumulated_text, model, mode) after each SSE chunk."""
    api_base = runtime_config.get("generic_llm_ocr_api_base", DEFAULT_API_BASE)
    api_key = runtime_config.get("generic_llm_ocr_api_key", "")
    model = runtime_config.get("generic_llm_ocr_model", DEFAULT_MODEL)
    mode = normalize_mode(runtime_config.get("generic_llm_ocr_mode", DEFAULT_MODE))
    ignore_menu_text = is_ignore_menu_text_enabled(runtime_config)
    menu_items_filter = is_menu_items_filter_enabled(runtime_config)

    endpoint = build_endpoint(api_base)
    prompt = build_prompt(mode, width, height, source_lang, target_lang, ignore_menu_text, menu_items_filter)

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
        "stream": True,
    }

    # Disable thinking/reasoning on models that support it (e.g. Qwen3/3.5)
    payload["chat_template_kwargs"] = {"enable_thinking": False}

    headers = {"Content-Type": "application/json"}
    if api_key and not api_key.startswith("<your"):
        headers["Authorization"] = f"Bearer {api_key}"

    request_started = time.time()
    LOGGER.info(
        "LLM streaming request start model=%s mode=%s ignore_menu_text=%s endpoint=%s image=%sx%s",
        model, mode, ignore_menu_text, endpoint, width, height,
    )

    response = _LLM_SESSION.post(endpoint, json=payload, headers=headers, timeout=180, stream=True)
    response.raise_for_status()
    response.encoding = "utf-8"

    accumulated = ""

    for raw_line in response.iter_lines(decode_unicode=True):
        if not raw_line or not raw_line.startswith("data: "):
            continue

        json_part = raw_line[6:]
        if json_part.strip() == "[DONE]":
            break

        try:
            chunk = __import__("json").loads(json_part)
            choices = chunk.get("choices", [])
            if not choices:
                continue
            delta = choices[0].get("delta", {})
            content = delta.get("content", "")
            if content:
                accumulated += content
                yield accumulated, content, model, mode
        except Exception:
            continue

    LOGGER.info(
        "LLM streaming request complete model=%s mode=%s duration_ms=%.1f response_chars=%s",
        model, mode, (time.time() - request_started) * 1000.0, len(accumulated),
    )


def generate_sse_events(
    image_bytes: bytes,
    source_lang: str,
    target_lang: str,
    source_image_width: Optional[int] = None,
    source_image_height: Optional[int] = None,
):
    """Generator that yields SSE event strings for each new text object detected."""
    import json as _json

    runtime_config = load_runtime_settings()

    image = Image.open(BytesIO(image_bytes)).convert("RGB")
    source_image_width = source_image_width or image.width
    source_image_height = source_image_height or image.height
    save_debug_request_image(image, source_lang)
    llm_image, llm_image_bytes, pad_left, pad_top, content_w, content_h = prepare_image_for_llm(image, runtime_config)
    if llm_image.size != image.size:
        save_debug_request_image(llm_image, source_lang, prefix="request_llm")

    start_time = time.time()
    mode = normalize_mode(runtime_config.get("generic_llm_ocr_mode", DEFAULT_MODE))
    model = runtime_config.get("generic_llm_ocr_model", DEFAULT_MODEL)

    emitted_count = 0
    emitted_text_objects: List[Dict] = []
    pending_line = ""
    last_preview_signature: Optional[Tuple[int, int, int, int, str]] = None

    # Yield a header event so the client knows streaming has started
    header = {
        "event": "stream_start",
        "mode": mode,
        "model": model,
        "language": source_lang,
    }
    yield f"data: {_json.dumps(header)}\n\n"

    try:
        for _accumulated_text, delta_content, model, mode in query_llm_streaming(
            llm_image_bytes,
            llm_image.width,
            llm_image.height,
            runtime_config,
            source_lang,
            target_lang,
        ):
            # Forward the raw LLM delta so the client can log it in real-time
            delta_event = {"event": "llm_delta", "content": delta_content}
            yield f"data: {_json.dumps(delta_event)}\n\n"

            # Wait for a full line terminator before parsing so bbox-first lines
            # are not emitted while the TEXT field is still streaming in.
            pending_line += delta_content
            completed_lines, pending_line = split_completed_response_lines(pending_line)
            for raw_line in completed_lines:
                stream_id = f"stream_{emitted_count}"
                text_obj = process_single_text_object(
                    raw_line,
                    image,
                    mode,
                    llm_image.width,
                    llm_image.height,
                    source_image_width,
                    source_image_height,
                    pad_left,
                    pad_top,
                    content_w,
                    content_h,
                )
                if text_obj is not None:
                    event_data = {"event": "text_object", "stream_id": stream_id, "data": text_obj}
                    yield f"data: {_json.dumps(event_data)}\n\n"
                    emitted_text_objects.append(text_obj)
                    emitted_count += 1
                last_preview_signature = None

            preview_obj = process_partial_streaming_text_object(
                pending_line,
                image,
                llm_image.width,
                llm_image.height,
                source_image_width,
                source_image_height,
                pad_left,
                pad_top,
                content_w,
                content_h,
            )
            if preview_obj is not None:
                preview_signature = (
                    int(preview_obj["x"]),
                    int(preview_obj["y"]),
                    int(preview_obj["width"]),
                    int(preview_obj["height"]),
                    preview_obj["text"],
                )
                if preview_signature != last_preview_signature:
                    preview_event = {
                        "event": "stream_preview",
                        "stream_id": f"stream_{emitted_count}",
                        "data": preview_obj,
                    }
                    yield f"data: {_json.dumps(preview_event)}\n\n"
                    last_preview_signature = preview_signature
            elif not pending_line.strip():
                last_preview_signature = None
    except Exception as exc:
        LOGGER.exception("Streaming LLM request failed")
        error_data = {"event": "error", "message": str(exc)}
        yield f"data: {_json.dumps(error_data)}\n\n"
        return

    if pending_line.strip():
        stream_id = f"stream_{emitted_count}"
        text_obj = process_single_text_object(
            pending_line,
            image,
            mode,
            llm_image.width,
            llm_image.height,
            source_image_width,
            source_image_height,
            pad_left,
            pad_top,
            content_w,
            content_h,
        )
        if text_obj is not None:
            event_data = {"event": "text_object", "stream_id": stream_id, "data": text_obj}
            yield f"data: {_json.dumps(event_data)}\n\n"
            emitted_text_objects.append(text_obj)
            emitted_count += 1

    # Fuzzy match: check if the streamed text is essentially the same as the last result
    text_unchanged = False
    if emitted_text_objects and check_fuzzy_unchanged(emitted_text_objects, runtime_config):
        LOGGER.info("Streaming fuzzy match: text considered unchanged")
        text_unchanged = True

    # Final event
    done_data = {
        "event": "stream_end",
        "processing_time": time.time() - start_time,
        "total_text_objects": emitted_count,
        "includes_translations": mode == MODE_OCR_TRANSLATE,
        "text_unchanged": text_unchanged,
    }
    yield f"data: {_json.dumps(done_data)}\n\n"


@app.on_event("startup")
async def on_startup() -> None:
    asyncio.get_running_loop().set_exception_handler(log_asyncio_exception)
    LOGGER.info(
        "Service startup service=%s version=%s port=%s pid=%s python=%s",
        SERVICE_NAME,
        SERVICE_INSTALL_VERSION,
        SERVICE_PORT,
        os.getpid(),
        sys.version.split()[0],
    )
    append_fault_log(
        f"Service startup service={SERVICE_NAME} version={SERVICE_INSTALL_VERSION} port={SERVICE_PORT} pid={os.getpid()}"
    )


@app.on_event("shutdown")
async def on_shutdown() -> None:
    _TRANSLATION_CACHE.save()
    LOGGER.info("Service shutdown requested pid=%s", os.getpid())
    append_fault_log(f"Service shutdown requested pid={os.getpid()}")


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()
    should_log = request.url.path != "/info"
    content_length = request.headers.get("content-length", "unknown")
    client = request.client.host if request.client else "unknown"

    if should_log:
        LOGGER.info(
            "HTTP request start method=%s path=%s query=%s client=%s content_length=%s",
            request.method,
            request.url.path,
            request.url.query,
            client,
            content_length,
        )

    try:
        response = await call_next(request)
    except Exception:
        LOGGER.exception(
            "HTTP request failed method=%s path=%s duration_ms=%.1f",
            request.method,
            request.url.path,
            (time.time() - start_time) * 1000.0,
        )
        raise

    if should_log:
        LOGGER.info(
            "HTTP request end method=%s path=%s status=%s duration_ms=%.1f",
            request.method,
            request.url.path,
            response.status_code,
            (time.time() - start_time) * 1000.0,
        )

    return response


@app.post("/process")
async def process_image(request: Request):
    try:
        runtime_config = load_runtime_settings()
        source_lang = request.query_params.get("lang", runtime_config.get("source_language", "ja"))
        target_lang = get_target_language(request, runtime_config)
        source_image_width = get_optional_positive_int_query_param(request, "source_width")
        source_image_height = get_optional_positive_int_query_param(request, "source_height")

        image_bytes = await request.body()
        if not image_bytes:
            raise HTTPException(status_code=400, detail="No image data provided")

        async with OCR_PROCESS_SEMAPHORE:
            response_content = await asyncio.to_thread(
                process_image_sync,
                image_bytes,
                source_lang,
                target_lang,
                source_image_width,
                source_image_height,
            )
        return JSONResponse(content=response_content)
    except Exception as exc:
        LOGGER.exception("Error processing image")
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

        response_content = await asyncio.to_thread(analyze_color_sync, image_bytes)
        return JSONResponse(content=response_content)
    except Exception as exc:
        LOGGER.exception("Error analyzing color")
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": str(exc), "error_type": type(exc).__name__},
        )


@app.post("/process_stream")
async def process_image_stream(request: Request):
    try:
        runtime_config = load_runtime_settings()
        source_lang = request.query_params.get("lang", runtime_config.get("source_language", "ja"))
        target_lang = get_target_language(request, runtime_config)
        source_image_width = get_optional_positive_int_query_param(request, "source_width")
        source_image_height = get_optional_positive_int_query_param(request, "source_height")

        image_bytes = await request.body()
        if not image_bytes:
            raise HTTPException(status_code=400, detail="No image data provided")

        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _run_generator():
            try:
                for chunk in generate_sse_events(
                    image_bytes,
                    source_lang,
                    target_lang,
                    source_image_width,
                    source_image_height,
                ):
                    loop.call_soon_threadsafe(queue.put_nowait, chunk)
            except Exception as exc:
                import json as _json
                error_chunk = f"data: {_json.dumps({'event': 'error', 'message': str(exc)})}\n\n"
                loop.call_soon_threadsafe(queue.put_nowait, error_chunk)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        async def stream_wrapper():
            async with OCR_PROCESS_SEMAPHORE:
                loop.run_in_executor(None, _run_generator)
                while True:
                    chunk = await queue.get()
                    if chunk is None:
                        break
                    yield chunk

        return StreamingResponse(stream_wrapper(), media_type="text/event-stream")
    except Exception as exc:
        LOGGER.exception("Error processing image (streaming)")
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "message": str(exc),
                "error_type": type(exc).__name__,
            },
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


@app.get("/translation_cache_stats")
async def translation_cache_stats():
    min_hits = 5
    with _TRANSLATION_CACHE._lock:
        total = len(_TRANSLATION_CACHE._cache)
        would_remove = sum(
            1 for entry in _TRANSLATION_CACHE._cache.values()
            if entry.get("hits", 0) < min_hits
        )
    return JSONResponse(
        content={
            "status": "success",
            "total": total,
            "would_remove": would_remove,
            "would_remain": total - would_remove,
            "min_hits_threshold": min_hits,
        }
    )


@app.post("/translation_cache_purge")
async def translation_cache_purge():
    min_hits = 5
    with _TRANSLATION_CACHE._lock:
        total_before = len(_TRANSLATION_CACHE._cache)
        keys_to_remove = [
            key for key, entry in _TRANSLATION_CACHE._cache.items()
            if entry.get("hits", 0) < min_hits
        ]
        for key in keys_to_remove:
            del _TRANSLATION_CACHE._cache[key]
        _TRANSLATION_CACHE._dirty = True
        total_after = len(_TRANSLATION_CACHE._cache)
    _TRANSLATION_CACHE.save()
    LOGGER.info(
        "Translation cache purged: removed %d entries (hits < %d), %d → %d",
        len(keys_to_remove), min_hits, total_before, total_after,
    )
    return JSONResponse(
        content={
            "status": "success",
            "removed": len(keys_to_remove),
            "total_before": total_before,
            "total_after": total_after,
            "min_hits_threshold": min_hits,
        }
    )


@app.post("/shutdown")
async def shutdown():
    LOGGER.info("Shutdown request received")
    _TRANSLATION_CACHE.save()

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
    LOGGER.info("Launching uvicorn host=127.0.0.1 port=%s", SERVICE_PORT)
    uvicorn.run(app, host="127.0.0.1", port=SERVICE_PORT)