#!/usr/bin/env python3
"""Batch-improve translations in translation_cache.json using an LLM.

Reads the cache in configurable batches, sends source+existing translation
pairs to an LLM for review, and writes back only entries that the LLM
actually improved.  A backup of the original cache is created before any
write.

Usage examples:
    # Review the 50 highest-hit entries, 10 at a time (dry-run by default)
    python improve_translations.py

    # Actually write improvements back
    python improve_translations.py --apply

    # Process entries 100-200 sorted by hits, batch size 20
    python improve_translations.py --offset 100 --limit 100 --batch 20 --apply

    # Use a different model / endpoint
    python improve_translations.py --api-base http://localhost:1234 --model my-model --apply

    # Use OpenAI
    python improve_translations.py --api-base https://api.openai.com --model gpt-4o --api-key sk-... --apply

    # Provide extra context to guide the LLM
    python improve_translations.py --context "This is a Chinese RPG game (仙劍奇俠傳四). Character names should use Sino-Vietnamese readings." --apply

    # Only review entries whose source text matches a regex
    python improve_translations.py --filter "柳夢璃|雲天河" --apply
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_PATH = SCRIPT_DIR / "translation_cache.json"
APP_CONFIG_PATH = SCRIPT_DIR.parent.parent / "config.txt"

DEFAULT_API_BASE = "http://127.0.0.1:1234"
DEFAULT_MODEL = "qwen2.5-vl-7b-instruct"
DEFAULT_BATCH_SIZE = 10
DEFAULT_LIMIT = 50
DEFAULT_OFFSET = 0

IMPROVEMENT_PROMPT = """\
You are a translation quality reviewer for a video game translation project.
Below are numbered entries, each with a source text (in {source_lang}) and \
its current translation (in {target_lang}).

{context_block}\
Review each translation for accuracy, naturalness, and consistency.
If a translation is already good, output it UNCHANGED.
If it can be improved, output the improved version.

IMPORTANT RULES:
- Output EXACTLY the same number of lines as input.
- Each line must be prefixed with the SAME number as the input.
- Format: "N. <translation>" (no extra text or explanation).
- Do NOT add, remove, or reorder lines.
- Preserve proper nouns and character names consistently.
- Keep the same register/formality as the original translation.
- If the source is a UI label (short word/phrase), keep the translation concise.
- Output ONLY the numbered translations. /no_think

{numbered_entries}"""


# ---------------------------------------------------------------------------
# Config helpers (reuse the app's config format: key|value|)
# ---------------------------------------------------------------------------
def load_app_config() -> Dict[str, str]:
    config: Dict[str, str] = {}
    try:
        if APP_CONFIG_PATH.exists():
            for line in APP_CONFIG_PATH.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("|")
                if len(parts) >= 2:
                    config[parts[0].strip()] = parts[1].strip()
    except Exception:
        pass
    return config


LANGUAGE_NAME_MAP = {
    "en": "English", "ja": "Japanese", "ko": "Korean",
    "ch_sim": "Simplified Chinese", "ch_tra": "Traditional Chinese",
    "zh": "Chinese", "zh-tw": "Traditional Chinese",
    "es": "Spanish", "fr": "French", "de": "German",
    "it": "Italian", "pt": "Portuguese", "ru": "Russian",
    "vi": "Vietnamese", "th": "Thai",
}


def lang_name(code: str) -> str:
    return LANGUAGE_NAME_MAP.get(code.strip().lower(), code or "English")


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------
def build_endpoint(api_base: str) -> str:
    base = (api_base or DEFAULT_API_BASE).strip().rstrip("/")
    if base.endswith("/v1/chat/completions"):
        return base
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def call_llm(
    endpoint: str,
    model: str,
    api_key: str,
    prompt: str,
    temperature: float = 0,
) -> str:
    import requests

    headers = {"Content-Type": "application/json"}
    if api_key and not api_key.startswith("<your"):
        headers["Authorization"] = f"Bearer {api_key}"

    is_openai = "openai.com" in endpoint
    token_key = "max_completion_tokens" if model == "gpt-5.4" else "max_tokens"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        token_key: 4096,
    }
    if model != "gpt-5.4":
        payload["temperature"] = temperature
    # chat_template_kwargs is only supported by local backends (llama.cpp, vLLM)
    if not is_openai and "anthropic.com" not in endpoint:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    if model == "gpt-5.4":
        payload["reasoning_effort"] = "medium"
    resp = requests.post(endpoint, json=payload, headers=headers, timeout=300)
    if not resp.ok:
        print(f"  LLM response status: {resp.status_code}")
        try:
            print(f"  LLM response body:   {resp.text[:1000]}")
        except Exception:
            pass
        resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------
def load_cache(path: Path) -> Dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_cache(path: Path, data: Dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def sorted_entries(
    cache: Dict,
    sort_by: str = "hits",
    reverse: bool = True,
) -> List[Tuple[str, Dict]]:
    return sorted(cache.items(), key=lambda kv: kv[1].get(sort_by, 0), reverse=reverse)


def parse_numbered_response(response: str, expected: int) -> List[Optional[str]]:
    results: List[Optional[str]] = [None] * expected
    if not response:
        return results
    for line in response.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(\d+)[.)]\s*(.+)$", line)
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < expected:
                results[idx] = m.group(2).strip()
    return results


def process_batch(
    batch: List[Tuple[str, Dict]],
    endpoint: str,
    model: str,
    api_key: str,
    source_lang: str,
    target_lang: str,
    context: str,
) -> List[Tuple[str, str, str]]:
    """Return list of (cache_key, old_translation, new_translation) for changed entries."""
    numbered_lines = []
    for i, (key, entry) in enumerate(batch, 1):
        src = entry["source"]
        trans = entry["translated"]
        numbered_lines.append(f'{i}. SOURCE: "{src}" → CURRENT: "{trans}"')

    context_block = ""
    if context:
        context_block = f"CONTEXT: {context}\n\n"

    prompt = IMPROVEMENT_PROMPT.format(
        source_lang=lang_name(source_lang),
        target_lang=lang_name(target_lang),
        context_block=context_block,
        numbered_entries="\n".join(numbered_lines),
    )

    raw = call_llm(endpoint, model, api_key, prompt)
    parsed = parse_numbered_response(raw, len(batch))

    changes: List[Tuple[str, str, str]] = []
    for i, (key, entry) in enumerate(batch):
        new_trans = parsed[i]
        if new_trans and new_trans != entry["translated"]:
            changes.append((key, entry["translated"], new_trans))
    return changes


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Batch-improve translations in translation_cache.json",
    )
    parser.add_argument("--cache", type=Path, default=CACHE_PATH, help="Path to translation_cache.json")
    parser.add_argument("--api-base", default=None, help="LLM API base URL (reads config.txt if omitted)")
    parser.add_argument("--api-key", default=None, help="LLM API key (reads config.txt if omitted)")
    parser.add_argument("--model", default=None, help="LLM model name (reads config.txt if omitted)")
    parser.add_argument("--source-lang", default=None, help="Source language code (reads config.txt if omitted)")
    parser.add_argument("--target-lang", default=None, help="Target language code (reads config.txt if omitted)")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH_SIZE, help=f"Entries per LLM call (default {DEFAULT_BATCH_SIZE})")
    parser.add_argument("--offset", type=int, default=DEFAULT_OFFSET, help="Skip this many entries from the top")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"Max entries to process (default {DEFAULT_LIMIT})")
    parser.add_argument("--filter", default=None, help="Regex filter on source text (only matching entries are reviewed)")
    parser.add_argument("--context", default=None, help="Extra context to help the LLM (e.g. game name, character names)")
    parser.add_argument("--sort", default="hits", choices=["hits", "source", "translated"], help="Sort entries by this field")
    parser.add_argument("--asc", action="store_true", help="Sort ascending instead of descending")
    parser.add_argument("--apply", action="store_true", help="Write changes back to cache (default is dry-run)")
    parser.add_argument("--temperature", type=float, default=0, help="LLM temperature (default 0)")
    args = parser.parse_args()

    # Load app config for defaults
    app_cfg = load_app_config()
    api_base = args.api_base or app_cfg.get("generic_llm_ocr_api_base", DEFAULT_API_BASE)
    api_key = args.api_key or app_cfg.get("generic_llm_ocr_api_key", "")
    model = args.model or app_cfg.get("generic_llm_ocr_model", DEFAULT_MODEL)
    source_lang = args.source_lang or app_cfg.get("source_language", "zh")
    target_lang = args.target_lang or app_cfg.get("generic_llm_ocr_target_language", "en")

    endpoint = build_endpoint(api_base)

    print(f"Cache:       {args.cache}")
    print(f"Endpoint:    {endpoint}")
    print(f"Model:       {model}")
    print(f"Source lang: {lang_name(source_lang)} ({source_lang})")
    print(f"Target lang: {lang_name(target_lang)} ({target_lang})")
    print(f"Batch size:  {args.batch}")
    print(f"Offset:      {args.offset}")
    print(f"Limit:       {args.limit}")
    if args.filter:
        print(f"Filter:      {args.filter}")
    if args.context:
        print(f"Context:     {args.context}")
    print(f"Mode:        {'APPLY' if args.apply else 'DRY-RUN (use --apply to write changes)'}")
    print()

    # Load cache
    cache = load_cache(args.cache)
    print(f"Loaded {len(cache)} cache entries.")

    # Sort
    reverse = not args.asc
    if args.sort == "source":
        entries = sorted(cache.items(), key=lambda kv: kv[1].get("source", ""), reverse=reverse)
    elif args.sort == "translated":
        entries = sorted(cache.items(), key=lambda kv: kv[1].get("translated", ""), reverse=reverse)
    else:
        entries = sorted_entries(cache, "hits", reverse)

    # Filter
    if args.filter:
        pattern = re.compile(args.filter)
        entries = [(k, v) for k, v in entries if pattern.search(v.get("source", ""))]
        print(f"After filter: {len(entries)} entries match.")

    # Slice
    entries = entries[args.offset : args.offset + args.limit]
    print(f"Processing {len(entries)} entries (offset={args.offset}, limit={args.limit}).")
    print()

    if not entries:
        print("No entries to process.")
        return

    # Process in batches
    all_changes: List[Tuple[str, str, str]] = []
    total_batches = (len(entries) + args.batch - 1) // args.batch

    for batch_idx in range(total_batches):
        start = batch_idx * args.batch
        end = min(start + args.batch, len(entries))
        batch = entries[start:end]

        print(f"--- Batch {batch_idx + 1}/{total_batches} (entries {start + 1}-{end}) ---")
        for i, (key, entry) in enumerate(batch):
            print(f"  {i + 1}. [{entry.get('hits', 0)} hits] \"{entry['source']}\" → \"{entry['translated']}\"")

        try:
            changes = process_batch(
                batch, endpoint, model, api_key,
                source_lang, target_lang, args.context or "",
            )
        except Exception as exc:
            print(f"  ERROR: {exc}")
            print("  Skipping this batch.\n")
            continue

        if changes:
            for key, old, new in changes:
                print(f"  IMPROVED: \"{old}\" → \"{new}\"")
                all_changes.append((key, old, new))
        else:
            print("  No improvements suggested.")
        print()

    # Summary
    print(f"=== Summary: {len(all_changes)} improvement(s) found ===")
    for key, old, new in all_changes:
        src = cache[key]["source"]
        print(f"  \"{src}\": \"{old}\" → \"{new}\"")

    if not all_changes:
        print("Nothing to update.")
        return

    if not args.apply:
        print(f"\nDry-run complete. Re-run with --apply to write {len(all_changes)} change(s).")
        return

    # Backup and write
    backup_name = f"translation_cache.backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    backup_path = args.cache.parent / backup_name
    shutil.copy2(args.cache, backup_path)
    print(f"\nBackup saved to {backup_path}")

    for key, old, new in all_changes:
        cache[key]["translated"] = new

    save_cache(args.cache, cache)
    print(f"Updated {len(all_changes)} translation(s) in {args.cache}")


if __name__ == "__main__":
    main()
