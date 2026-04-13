#!/usr/bin/env python3
"""
Benchmark harness for OCR, Translation, and TTS pipeline stages.

Calls the LLM API directly (no need for the GenericLLMOCR FastAPI service).
Uses the first debug image found in the debug/ folder, or a specified image.

Usage:
    python benchmark.py              # Run all benchmarks
    python benchmark.py ocr          # OCR only
    python benchmark.py translate    # Translation only (uses OCR results)
    python benchmark.py tts          # TTS only (uses hardcoded sample text)
    python benchmark.py dialog_tts   # Dialog TTS filter only (LLM text cleanup)
    python benchmark.py ocr+translate  # OCR then separate translation pass
    python benchmark.py full         # Full pipeline: OCR -> Translate -> Dialog TTS -> TTS
    python benchmark.py full_combined # Full pipeline using single OCR+Translate call
"""

import argparse
import base64
import io
import json
import re
import sys
import time
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from PIL import Image

# ---------------------------------------------------------------------------
# Paths & defaults
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent
DEBUG_IMAGE_DIR = SCRIPT_DIR / "debug"
APP_DIR = SCRIPT_DIR.parent.parent          # app/
CONFIG_PATH = APP_DIR / "config.txt"

# Add shared dir for config parser and import server functions
sys.path.insert(0, str(SCRIPT_DIR.parent / "shared"))
sys.path.insert(0, str(SCRIPT_DIR))
from config_parser import parse_service_config  # noqa: E402
from server import (  # noqa: E402
    build_endpoint,
    build_prompt,
    normalize_mode,
    prepare_image_for_llm,
    process_llm_results,
    MODE_OCR_ONLY,
    MODE_OCR_THEN_TRANSLATE,
    MODE_OCR_TRANSLATE,
)


def load_config() -> Dict[str, str]:
    return parse_service_config(str(CONFIG_PATH))


def find_debug_image(image_path: Optional[str] = None) -> Path:
    """Return a debug image path, preferring --image flag, then newest debug image."""
    if image_path:
        p = Path(image_path)
        if not p.exists():
            print(f"ERROR: Image not found: {p}")
            sys.exit(1)
        return p

    if DEBUG_IMAGE_DIR.exists():
        # Prefer original-resolution images (request_YYYYMMDD*), not downscaled ones
        originals = sorted(DEBUG_IMAGE_DIR.glob("request_2*.png"), reverse=True)
        if originals:
            return originals[0]
        # Fallback: any PNG
        pngs = sorted(DEBUG_IMAGE_DIR.glob("*.png"), reverse=True)
        if pngs:
            return pngs[0]

    # Fallback to shared test image
    shared_test = SCRIPT_DIR.parent / "shared" / "test_images" / "anime_test.jpg"
    if shared_test.exists():
        return shared_test

    print("ERROR: No debug image found. Run the OCR service with debug enabled first,")
    print("       or specify --image <path>.")
    sys.exit(1)


# ---------------------------------------------------------------------------
# OCR benchmark — calls LLM API directly (no GenericLLMOCR service needed)
# ---------------------------------------------------------------------------
def benchmark_ocr(
    image: Image.Image,
    config: Dict[str, str],
    source_lang: str,
    target_lang: str,
    iterations: int = 1,
    force_ocr_only: bool = False,
    force_mode: Optional[str] = None,
) -> Tuple[List[Dict], float]:
    """
    Call the LLM vision API directly, reusing server.py helpers for prompt
    building, image resizing, and response parsing.
    Returns (text_objects, avg_time_seconds).
    """
    api_base = config.get("generic_llm_ocr_api_base", "http://127.0.0.1:1234")
    api_key = config.get("generic_llm_ocr_api_key", "")
    model = config.get("generic_llm_ocr_model", "qwen2.5-vl-7b-instruct")
    if force_mode:
        mode = force_mode
    elif force_ocr_only:
        mode = MODE_OCR_ONLY
    else:
        mode = normalize_mode(config.get("generic_llm_ocr_mode", "OCR + Translate"))
    ignore_menu = (config.get("generic_llm_ocr_ignore_menu_text", "false") or "").strip().lower() == "true"

    endpoint = build_endpoint(api_base)

    # Resize image the same way the server does
    llm_image, llm_image_bytes = prepare_image_for_llm(image, config)
    prompt = build_prompt(mode, llm_image.width, llm_image.height, source_lang, target_lang, ignore_menu)

    image_data_url = f"data:image/png;base64,{base64.b64encode(llm_image_bytes).decode('utf-8')}"

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
        "chat_template_kwargs": {"enable_thinking": False},
    }

    headers = {"Content-Type": "application/json"}
    if api_key and not api_key.startswith("<your"):
        headers["Authorization"] = f"Bearer {api_key}"

    print(f"  LLM endpoint: {endpoint}")
    print(f"  Model: {model}  Mode: {mode}")
    print(f"  Image sent to LLM: {llm_image.width}x{llm_image.height} ({len(llm_image_bytes)} bytes)")

    times: List[float] = []
    last_text_objects: List[Dict] = []

    for i in range(iterations):
        t0 = time.perf_counter()
        resp = requests.post(endpoint, json=payload, headers=headers, timeout=180)
        elapsed = time.perf_counter() - t0
        times.append(elapsed)

        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})

        # Parse the response using server.py's logic
        text_objects = process_llm_results(
            image, content, mode, llm_image.width, llm_image.height,
        )
        # Some models return bbox coords in the original image space despite
        # being told the resized dimensions.  Fall back to original dims.
        if not text_objects and llm_image.size != image.size:
            text_objects = process_llm_results(
                image, content, mode, image.width, image.height,
            )
            if text_objects:
                print(f"    (bbox coords matched original {image.width}x{image.height}, not LLM {llm_image.width}x{llm_image.height})")
        last_text_objects = text_objects

        print(f"  [{i+1}/{iterations}] OCR  {elapsed*1000:8.1f} ms  "
              f"texts={len(text_objects)}  mode={mode}  "
              f"prompt_tokens={usage.get('prompt_tokens', '?')}  "
              f"completion_tokens={usage.get('completion_tokens', '?')}")
        if content:
            for raw_line in content.strip().splitlines():
                print(f"    LLM> {raw_line}")

    avg = sum(times) / len(times) if times else 0
    return last_text_objects, avg


# ---------------------------------------------------------------------------
# Translation benchmark (text-only LLM call, same as OCR Then Translate 2nd pass)
# ---------------------------------------------------------------------------
def benchmark_translate(
    text_objects: List[Dict],
    config: Dict[str, str],
    source_lang: str,
    target_lang: str,
    iterations: int = 1,
) -> Tuple[List[str], float]:
    """
    Make a text-only translation request to the same LLM endpoint,
    mirroring the OCR Then Translate second pass.
    Returns (translations, avg_time_seconds).
    """
    if not text_objects:
        print("  SKIP: No text objects to translate.")
        return [], 0.0

    api_base = config.get("generic_llm_ocr_api_base", "http://127.0.0.1:1234")
    api_key = config.get("generic_llm_ocr_api_key", "")
    model = config.get("generic_llm_ocr_model", "qwen2.5-vl-7b-instruct")

    endpoint = api_base.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        if endpoint.endswith("/v1"):
            endpoint += "/chat/completions"
        else:
            endpoint += "/v1/chat/completions"

    LANGUAGE_NAMES = {
        "en": "English", "ja": "Japanese", "ko": "Korean",
        "zh": "Chinese", "zh-TW": "Traditional Chinese", "ch_tra": "Traditional Chinese",
        "ch_sim": "Simplified Chinese", "vi": "Vietnamese", "es": "Spanish",
        "fr": "French", "de": "German", "ru": "Russian", "th": "Thai",
    }
    src_name = LANGUAGE_NAMES.get(source_lang, source_lang)
    tgt_name = LANGUAGE_NAMES.get(target_lang, target_lang)

    numbered = "\n".join(f"{i+1}. {obj['text']}" for i, obj in enumerate(text_objects))
    prompt = (
        f"You are a translation engine. Translate each numbered line below from {src_name} to {tgt_name}.\n"
        "Output EXACTLY the same number of lines, each prefixed with the SAME number.\n"
        "Do NOT add, remove, or reorder lines. Do NOT add explanations.\n"
        "Format:\n1. <translated text>\n2. <translated text>\n...\n"
        "Output ONLY the numbered translations. /no_think\n\n" + numbered
    )

    headers = {"Content-Type": "application/json"}
    if api_key and not api_key.startswith("<your"):
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 4096,
        "chat_template_kwargs": {"enable_thinking": False},
    }

    times: List[float] = []
    last_translations: List[str] = []

    for i in range(iterations):
        t0 = time.perf_counter()
        resp = requests.post(endpoint, json=payload, headers=headers, timeout=180)
        elapsed = time.perf_counter() - t0
        times.append(elapsed)

        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})

        # Parse numbered translations
        translations = [""] * len(text_objects)
        import re
        for line in (content or "").strip().splitlines():
            line = line.strip()
            m = re.match(r"^(\d+)[.)]\s*(.+)$", line)
            if m:
                idx = int(m.group(1)) - 1
                if 0 <= idx < len(translations):
                    translations[idx] = m.group(2).strip()
        last_translations = translations

        print(f"  [{i+1}/{iterations}] TRANSLATE  {elapsed*1000:8.1f} ms  "
              f"prompt_tokens={usage.get('prompt_tokens', '?')}  "
              f"completion_tokens={usage.get('completion_tokens', '?')}")

    avg = sum(times) / len(times) if times else 0
    return last_translations, avg


# ---------------------------------------------------------------------------
# Dialog TTS filter benchmark (LLM text-only call to clean text for TTS)
# ---------------------------------------------------------------------------
NO_DIALOG_SENTINEL = "__UGTLIVE_NO_DIALOG__"

DIALOG_FILTER_SYSTEM_PROMPT = (
    "You clean text before video-game text-to-speech playback. Keep only actual spoken dialogue "
    "or narration that should be read aloud. "
    "Remove speaker names, name tags, menu labels, HUD text, button prompts, inventory/status text, "
    "quest headers, control hints, and standalone character names unless they are part of a spoken sentence. "
    "Keep the original language and wording of the remaining spoken text. "
    f"If nothing should be spoken, reply with EXACTLY {NO_DIALOG_SENTINEL}. "
    "Reply with only the cleaned text and nothing else."
)

NO_DIALOG_RESPONSES = {
    NO_DIALOG_SENTINEL.lower(),
    "no dialog",
    "no spoken dialog",
    "no spoken dialogue",
    "nothing to speak",
    "none",
}


def is_no_dialog_response(text: str) -> bool:
    return (text or "").strip().lower() in NO_DIALOG_RESPONSES


def benchmark_dialog_filter(
    texts: List[str],
    config: Dict[str, str],
    language_code: str,
    iterations: int = 1,
) -> Tuple[List[Optional[str]], float]:
    """
    Send each text through the Dialog TTS filter LLM call (mirrors
    DialogTtsFilterService.cs) and measure response time.
    Returns (filtered_texts, avg_time_seconds).  A None entry means
    the LLM decided nothing should be spoken.
    """
    if not texts:
        print("  SKIP: No texts to filter.")
        return [], 0.0

    api_base = config.get("dialog_tts_api_base", "").strip() or config.get("generic_llm_ocr_api_base", "http://127.0.0.1:1234")
    api_key = config.get("dialog_tts_api_key", "").strip() or config.get("generic_llm_ocr_api_key", "")
    model = config.get("dialog_tts_model", "").strip() or config.get("generic_llm_ocr_model", "qwen2.5-vl-7b-instruct")

    endpoint = api_base.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        if endpoint.endswith("/v1"):
            endpoint += "/chat/completions"
        else:
            endpoint += "/v1/chat/completions"

    LANGUAGE_NAMES = {
        "en": "English", "ja": "Japanese", "ko": "Korean",
        "zh": "Chinese", "zh-TW": "Traditional Chinese", "ch_tra": "Traditional Chinese",
        "ch_sim": "Simplified Chinese", "vi": "Vietnamese", "es": "Spanish",
        "fr": "French", "de": "German", "ru": "Russian", "th": "Thai",
    }
    lang_name = LANGUAGE_NAMES.get(language_code, language_code or "unknown")

    headers = {"Content-Type": "application/json"}
    if api_key and not api_key.startswith("<your"):
        headers["Authorization"] = f"Bearer {api_key}"

    print(f"  LLM endpoint: {endpoint}")
    print(f"  Model: {model}")
    print(f"  Language hint: {lang_name}")
    print(f"  Texts to filter: {len(texts)}")

    times: List[float] = []
    last_filtered: List[Optional[str]] = []

    for i in range(iterations):
        filtered: List[Optional[str]] = []
        iter_start = time.perf_counter()

        for j, text in enumerate(texts):
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": DIALOG_FILTER_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": f"Language hint: {lang_name}\n\nText to clean for TTS:\n{text}",
                    },
                ],
                "temperature": 0,
                "max_tokens": 512,
                "chat_template_kwargs": {"enable_thinking": False},
            }

            t0 = time.perf_counter()
            resp = requests.post(endpoint, json=payload, headers=headers, timeout=60)
            elapsed = time.perf_counter() - t0
            resp.raise_for_status()

            data = resp.json()
            content = (data["choices"][0]["message"]["content"] or "").strip()
            usage = data.get("usage", {})

            if is_no_dialog_response(content):
                filtered.append(None)
                label = "(no dialog)"
            else:
                filtered.append(content)
                label = content[:80] + ("..." if len(content) > 80 else "")

            print(
                f"  [{i+1}/{iterations}] DIALOG_FILTER[{j}]  {elapsed*1000:8.1f} ms  "
                f"prompt_tokens={usage.get('prompt_tokens', '?')}  "
                f"completion_tokens={usage.get('completion_tokens', '?')}  "
                f"result={label}"
            )

        iter_total = time.perf_counter() - iter_start
        times.append(iter_total)
        last_filtered = filtered

    avg = sum(times) / len(times) if times else 0
    return last_filtered, avg


# ---------------------------------------------------------------------------
# TTS benchmark
# ---------------------------------------------------------------------------
def benchmark_tts(
    texts: List[str],
    config: Dict[str, str],
    iterations: int = 1,
) -> float:
    """
    Send text to the configured TTS service and measure response time.
    Returns avg_time_seconds for synthesizing all texts.
    """
    tts_service = config.get("tts_service", "").strip()

    if "qwen" in tts_service.lower() or "qwen3" in tts_service.lower():
        port = int(config.get("qwen3_tts_port", "5004"))
        voice = config.get("qwen3_tts_voice", "ono_anna")
        fast_mode = config.get("qwen3_tts_fast_mode", "false").lower() == "true"
        url = f"http://127.0.0.1:{port}/tts"
        service_label = f"Qwen3TTS (port={port}, voice={voice}, fast={fast_mode})"

        def make_request(text: str) -> requests.Response:
            return requests.post(url, json={
                "text": text, "voice": voice, "language": "Auto", "fast_mode": fast_mode,
            }, timeout=120)

    elif "vieneu" in tts_service.lower():
        port = int(config.get("vieneu_tts_port", "5007"))
        voice = config.get("vieneu_tts_voice", "Default")
        url = f"http://127.0.0.1:{port}/tts"
        service_label = f"VieNeuTTS (port={port}, voice={voice})"

        def make_request(text: str) -> requests.Response:
            return requests.post(url, json={"text": text, "voice_id": voice}, timeout=120)

    else:
        print(f"  SKIP: TTS service '{tts_service}' not supported by this harness.")
        print("  Supported: Qwen3-TTS, VieNeu-TTS")
        return 0.0

    print(f"  TTS service: {service_label}")

    if not texts:
        texts = ["This is a test sentence for text to speech benchmarking."]

    times: List[float] = []

    for i in range(iterations):
        iter_start = time.perf_counter()
        for j, text in enumerate(texts):
            t0 = time.perf_counter()
            resp = make_request(text)
            elapsed = time.perf_counter() - t0
            resp.raise_for_status()
            audio_bytes = len(resp.content)
            duration_est = audio_bytes / (24000 * 2)  # PCM16 mono 24kHz
            print(f"  [{i+1}/{iterations}] TTS[{j}]  {elapsed*1000:8.1f} ms  "
                  f"audio_bytes={audio_bytes}  ~{duration_est:.1f}s audio  "
                  f"text={text[:60]}{'...' if len(text) > 60 else ''}")
        iter_total = time.perf_counter() - iter_start
        times.append(iter_total)

    avg = sum(times) / len(times) if times else 0
    return avg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def print_texts(text_objects: List[Dict], translations: Optional[List[str]] = None) -> None:
    if not text_objects:
        print("  (no text objects)")
        return
    for i, obj in enumerate(text_objects):
        bbox = f"({obj.get('x',0)},{obj.get('y',0)} {obj.get('width',0)}x{obj.get('height',0)})"
        line = f"  [{i}] {bbox} {obj['text']}"
        if translations and i < len(translations) and translations[i]:
            line += f"  ->  {translations[i]}"
        elif obj.get("translated_text"):
            line += f"  ->  {obj['translated_text']}"
        print(line)


def print_summary(results: Dict[str, float]) -> None:
    print("\n" + "=" * 60)
    print("BENCHMARK SUMMARY")
    print("=" * 60)
    total = 0.0
    ocr_ms = results.get("OCR", 0.0)
    translate_ms = results.get("Translate", 0.0)
    ocr_translate_ms = results.get("OCR+Translate", 0.0)
    dialog_filter_ms = results.get("Dialog Filter", 0.0)
    tts_ms = results.get("TTS", 0.0)

    # Show combined single-call OCR+Translate if measured
    if ocr_translate_ms > 0:
        print(f"  {'OCR+Translate':<20s}  {ocr_translate_ms:8.1f} ms")

    # Show individual OCR and Translate if both were measured
    if ocr_ms > 0:
        print(f"  {'OCR':<20s}  {ocr_ms:8.1f} ms")
    if translate_ms > 0:
        print(f"  {'Translate':<20s}  {translate_ms:8.1f} ms")
    if ocr_ms > 0 and translate_ms > 0:
        print(f"  {'OCR + Translate':<20s}  {ocr_ms + translate_ms:8.1f} ms")
    if dialog_filter_ms > 0:
        print(f"  {'Dialog Filter':<20s}  {dialog_filter_ms:8.1f} ms")
    if tts_ms > 0:
        print(f"  {'TTS':<20s}  {tts_ms:8.1f} ms")

    # Include any other stages not covered above
    known = {"OCR", "Translate", "TTS", "OCR+Translate", "Dialog Filter"}
    for stage, avg_ms in results.items():
        if stage not in known and avg_ms > 0:
            print(f"  {stage:<20s}  {avg_ms:8.1f} ms")

    total = sum(results.values())
    print(f"  {'TOTAL':<20s}  {total:8.1f} ms")
    print("=" * 60)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Benchmark OCR / Translation / TTS pipeline")
    parser.add_argument(
        "stages",
        nargs="*",
        default=["all"],
        help="Stages to run: ocr, translate, dialog_tts, tts, ocr+translate, ocr_translate, full, full_combined, all (default: all)",
    )
    parser.add_argument("--image", type=str, default=None, help="Path to test image")
    parser.add_argument("--iterations", "-n", type=int, default=1, help="Iterations per stage")
    parser.add_argument("--source-lang", type=str, default=None, help="Source language (default: from config)")
    parser.add_argument("--target-lang", type=str, default=None, help="Target language (default: from config)")
    parser.add_argument("--tts-text", type=str, default=None, help="Override TTS text (instead of using OCR output)")
    args = parser.parse_args()

    # Capture all output to save as a log file
    log_capture = io.StringIO()
    sys.stdout = TeeWriter(sys.__stdout__, log_capture)

    config = load_config()
    source_lang = args.source_lang or config.get("source_language", "ja")
    target_lang = args.target_lang or config.get("generic_llm_ocr_target_language",
                                                   config.get("target_language", "en"))

    stages = set()
    for s in args.stages:
        s = s.lower().strip()
        if s == "all":
            stages = {"ocr", "translate", "dialog_tts", "tts"}
            break
        elif s == "full":
            stages = {"ocr", "translate", "dialog_tts", "tts"}
            break
        elif s in ("full_combined", "fullcombined"):
            stages = {"ocr_translate", "dialog_tts", "tts"}
            break
        elif s == "ocr+translate":
            stages.update({"ocr", "translate"})
        elif s in ("ocr_translate", "ocrtranslate"):
            stages.add("ocr_translate")
        else:
            stages.add(s)

    image_path = find_debug_image(args.image)
    image = Image.open(image_path).convert("RGB")

    print(f"Image:       {image_path}")
    print(f"Resolution:  {image.width}x{image.height}")
    print(f"Source lang: {source_lang}")
    print(f"Target lang: {target_lang}")
    print(f"Iterations:  {args.iterations}")
    print(f"Stages:      {', '.join(sorted(stages))}")
    print(f"LLM model:   {config.get('generic_llm_ocr_model', '?')}")
    print(f"LLM mode:    {config.get('generic_llm_ocr_mode', '?')}")
    print(f"LLM endpoint:{config.get('generic_llm_ocr_api_base', '?')}")
    print(f"TTS service: {config.get('tts_service', '?')}")
    print()

    results: Dict[str, float] = {}
    text_objects: List[Dict] = []
    translations: List[str] = []

    # --- OCR + Translate (single combined LLM call) ---
    if "ocr_translate" in stages:
        print("--- OCR + TRANSLATE (single call) ---")
        text_objects, avg = benchmark_ocr(
            image, config, source_lang, target_lang, args.iterations,
            force_mode=MODE_OCR_TRANSLATE,
        )
        results["OCR+Translate"] = avg * 1000
        print(f"\n  Detected {len(text_objects)} text region(s):")
        print_texts(text_objects)
        # Collect translations for TTS
        translations = [
            obj.get("translated_text", obj["text"])
            for obj in text_objects
        ]
        print()

    # --- OCR ---
    if "ocr" in stages:
        print("--- OCR ---")
        # When translate is a separate stage, force OCR-only so translation
        # is measured independently instead of being bundled into the OCR call.
        force_ocr = "translate" in stages
        text_objects, avg = benchmark_ocr(
            image, config, source_lang, target_lang, args.iterations,
            force_ocr_only=force_ocr,
        )
        results["OCR"] = avg * 1000
        print(f"\n  Detected {len(text_objects)} text region(s):")
        print_texts(text_objects)
        print()

    # --- Translate ---
    if "translate" in stages:
        print("--- TRANSLATE ---")
        # If we didn't run OCR, do an OCR-only call first to get source text
        if not text_objects:
            print("  (Running OCR first to get text objects...)")
            text_objects, _ = benchmark_ocr(
                image, config, source_lang, target_lang, 1,
                force_ocr_only=True,
            )
            print()

        # Always benchmark translation for all text objects (use original text)
        if text_objects:
            translations, avg = benchmark_translate(
                text_objects, config, source_lang, target_lang, args.iterations,
            )
            results["Translate"] = avg * 1000
            print(f"\n  Translations:")
            print_texts(text_objects, translations)
        else:
            print("  SKIP: OCR returned no text objects to translate.")
            results["Translate"] = 0.0
        print()

    # --- Dialog TTS Filter ---
    dialog_filtered: List[Optional[str]] = []
    if "dialog_tts" in stages:
        print("--- DIALOG TTS FILTER ---")
        # Determine the texts to filter: prefer translations, fall back to OCR text
        filter_input: List[str] = []
        if translations:
            filter_input = [t for t in translations if t]
        elif text_objects:
            filter_input = [
                obj.get("translated_text", obj["text"])
                for obj in text_objects
                if obj.get("translated_text") or obj.get("text")
            ]

        if not filter_input:
            filter_input = [
                "Taro: Let's go to the castle!",
                "HP  120/120",
                "Save   Load   Settings",
                "I can't believe the dragon is still alive...",
            ]
            print("  (Using sample texts since no OCR/translation output is available)")

        dialog_filtered, avg = benchmark_dialog_filter(
            filter_input, config, target_lang, args.iterations,
        )
        results["Dialog Filter"] = avg * 1000

        print(f"\n  Filter results:")
        for idx, (src, filt) in enumerate(zip(filter_input, dialog_filtered)):
            status = filt if filt is not None else "(suppressed)"
            print(f"    [{idx}] {src[:60]}  ->  {status}")
        print()

    # --- TTS ---
    if "tts" in stages:
        print("--- TTS ---")
        if args.tts_text:
            tts_texts = [args.tts_text]
        elif dialog_filtered:
            tts_texts = [t for t in dialog_filtered if t]
        elif translations:
            tts_texts = [t for t in translations if t]
        elif text_objects:
            tts_texts = [
                obj.get("translated_text", obj["text"])
                for obj in text_objects
                if obj.get("translated_text") or obj.get("text")
            ]
        else:
            tts_texts = ["This is a test sentence for text to speech benchmarking."]

        if not tts_texts:
            tts_texts = ["This is a test sentence for text to speech benchmarking."]

        avg = benchmark_tts(tts_texts, config, args.iterations)
        results["TTS"] = avg * 1000
        print()

    print_summary(results)

    # Save full output to debug folder
    save_benchmark_log(log_capture, sorted(stages))


class TeeWriter:
    """Write to both the original stream and a StringIO capture buffer."""

    def __init__(self, original: io.TextIOBase, capture: io.StringIO):
        self._original = original
        self._capture = capture

    def write(self, text: str) -> int:
        self._original.write(text)
        self._capture.write(text)
        return len(text)

    def flush(self) -> None:
        self._original.flush()

    # Forward any other attribute lookups to the original stream
    def __getattr__(self, name: str):
        return getattr(self._original, name)


def save_benchmark_log(capture: io.StringIO, stages: list) -> None:
    """Write captured output to a timestamped file in the debug folder."""
    DEBUG_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    stage_tag = "_".join(stages) if stages else "all"
    log_path = DEBUG_IMAGE_DIR / f"benchmark_{timestamp}_{stage_tag}.txt"
    log_path.write_text(capture.getvalue(), encoding="utf-8")
    print(f"\nLog saved to {log_path}")


if __name__ == "__main__":
    main()
