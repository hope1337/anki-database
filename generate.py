#!/usr/bin/env python3
"""
generate.py (clean-v1)

Generate an offline Anki deck for Traditional Chinese using the data layout
created by download.py.

CLEAN-v1 SOURCE MODEL:
  MOE Taiwan  = stroke-order geometry ONLY.
  Unicode Unihan (version-pinned) = ALL factual text (pinyin, radical,
      stroke count, variants, kDefinition English grounding).
  CNS11643 / 全字庫 = human-recorded Taiwan Mandarin pronunciation audio
      ONLY (fixed reference source; exact character+Zhuyin lookup, no
      synthesis, no fallback).
  Deterministic local code = normalization + pinyin->zhuyin + indexes.
  Language profile (config/profiles/<code>.json) = learner language,
      card labels, optional per-language features, LLM instruction.
  LLM = learner-language meaning, structure explanation, component
      glosses, example sentences, notable sayings. NEVER authoritative facts.
  MOE recordings are NOT used: clips regularly speak more than
  the isolated target character.

The MOE concise dictionary, MOE pronunciation recordings, CHISE IDS, and
the old external Han-Viet dataset are LEGACY: their classes remain below
in a marked LEGACY section for rollback/reference, but the clean path
never calls them.

Install:
    pip install genanki requests openpyxl

Local config (config/config.local.json, see config/config.example.json):
    {"mode": "test"|"official", "profile": "vi",
     "test_characters": ["思", "八", ...],
     "llm": {"base_url": ..., "model": ..., "timeout_seconds": 180,
             "trust_env": false}, ...}
Default mode is TEST (exactly the configured test_characters list).

Typical workflow
----------------
Test (default, configured test_characters from config):
    python generate.py

Official (full set):
    # set "mode": "official" in config/config.local.json, then:
    python generate.py

Debug overrides:
    python generate.py --chars 思 --limit 1 --no-ai

Important:
- LLM results are cached per profile in data/processed/llm/<lang>/.
  Switching profile never reuses another language's enrichment.
- Character pronunciation comes from data/processed/cns11643/cns_index.json
  (built by download.py from CNS11643 originals); original CNS bytes are
  copied unchanged into build media.
- Normalized card records are saved in data/processed/cards/.
- Published Android-ready snapshot: data/published/reference-v1/.
- Re-running does not call the LLM again unless --refresh-ai is used.
- Pronunciation audio is looked up from CNS11643 by character + expected
  Zhuyin (see cns_audio.py + config "audio" block). With no exact match,
  cards simply have no audio — never synthesized, never substituted.
  Unavailable audio is reported in build/audio_missing.json.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import html as html_std
import json
import re
import shutil
import sys
import unicodedata
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

import genanki

from cns_audio import (
    CNS_SOURCE,
    PREFERRED_VOICES,
    CharacterAudioResolver,
    CnsError,
    normalize_zhuyin,
    zhuyin_slug,
)

from prompts import (
    DEFAULT_SYSTEM_FILE,
    DEFAULT_USER_FILE,
    PromptError,
    prompt_file_hash,
    render_system,
    render_user,
    validate_text_prompts,
)

from text_providers import (
    DEFAULT_LOCAL_MODEL_ID,
    TextProviderError,
    build_text_provider,
    text_cache_identity,
)

try:
    from openpyxl import load_workbook
except ImportError:  # legacy MOE-dictionary path only; clean-v1 is stdlib-only
    load_workbook = None


# =============================================================================
# Paths
# =============================================================================

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

RAW = DATA / "raw"
PROCESSED = DATA / "processed"

MOE_STROKE = RAW / "moe_stroke"
MOE_STROKE_PLAYER = MOE_STROKE / "player"
MOE_STROKE_XML = MOE_STROKE / "xml"
STROKE_CATALOG = PROCESSED / "indexes" / "stroke_catalog.jsonl"

MOE_DICT_TEXT = RAW / "moe_dictionary" / "text"
# LEGACY/DEPRECATED: MOE pronunciation recordings are not downloaded by the
# clean pipeline and never used by clean generation (CNS11643 human
# recordings are the fixed pronunciation source).
MOE_DICT_AUDIO = RAW / "moe_dictionary" / "audio_char"

HANVIET_CSV = RAW / "hanviet" / "hanviet.csv"
CHISE_DIR = RAW / "chise"
UNICODE_DIR = RAW / "unicode"
UNIHAN_DIR = UNICODE_DIR / "unihan"

LLM_CACHE_DIR = PROCESSED / "llm"
CARD_CACHE_DIR = PROCESSED / "cards"

# CNS11643 fixed pronunciation source: processed lookup index built by
# download.py from extracted CNS originals (data/raw/cns11643/).
CNS11643_RAW = RAW / "cns11643"
CNS_INDEX_PATH = PROCESSED / "cns11643" / "cns_index.json"

# Published Android-ready snapshot (generated immutable artifact).
PUBLISHED_ROOT = DATA / "published" / "reference-v1"
PUBLISHED_CHARS = PUBLISHED_ROOT / "characters"
PUBLISHED_STROKES = PUBLISHED_ROOT / "strokes"
PUBLISHED_AUDIO = PUBLISHED_ROOT / "audio"
PUBLISHED_INDEXES = PUBLISHED_ROOT / "indexes"
PUBLISHED_LICENSES = PUBLISHED_ROOT / "licenses"

BUILD = ROOT / "build"
BUILD_MEDIA = BUILD / "media"
BUILD_REPORTS = BUILD / "reports"


# =============================================================================
# Constants (clean-v1)
# =============================================================================

# Grounding contract changed (Unihan facts, no MOE dict/CHISE/HanViet input),
# so the prompt version MUST differ from the legacy "anki-zh-tw-v1".
# Bumped again for the language-profile refactor: the prompt is now
# target-language agnostic and the output schema uses language-neutral keys
# (meaning/translation/...) instead of meaning_vi/...vi suffixes.
# Bumped again for notable_sayings: the output contract gained an optional
# enrichment list, so old cached enrichment (without it) is invalidated.
# Changing profile, prompt, or factual input invalidates the LLM cache.
PROMPT_VERSION = "anki-zh-generic-v2"

# Pinned Unicode version for the clean-v1 reproducibility contract.
# Must match UNICODE_VERSION in download.py. Never "latest".
UNICODE_VERSION = "17.0.0"

# Normalized-record + published-dataset schema versions.
CLEAN_SCHEMA_VERSION = "clean-v1"
REFERENCE_DATASET_VERSION = "reference-v1"

# Default TEST subset (used when config has no test_characters list).
# Exactly 10 explicitly listed characters covering: unique audio (八),
# multi-clip same-reading audio (思), multi-clip different readings (乾),
# max-clip-count audio (差 4 clips), plus common characters exercising
# radicals, variants, alternates, and enrichment (心學明好中愛).
# Every entry must exist in the supported stroke dataset.
DEFAULT_TEST_CHARACTERS = [
    "思", "八", "乾", "差", "心", "學", "明", "好", "中", "愛",
]

# Canonical user-editable configuration lives under config/.
CONFIG_DIR = ROOT / "config"
CONFIG_PROFILES_DIR = CONFIG_DIR / "profiles"
CONFIG_ANKI_DIR = CONFIG_DIR / "anki"
CONFIG_LOCAL = CONFIG_DIR / "config.local.json"
CONFIG_EXAMPLE = CONFIG_DIR / "config.example.json"

# Local connection defaults live in config/config.local.json (untracked).
# generate.py intentionally contains NO hardcoded IP/port: an empty base URL
# here forces an explicit error telling the user where to configure it.
DEFAULT_LLM_BASE_URL = ""
DEFAULT_LLM_MODEL = "qwen3"
DEFAULT_LLM_TIMEOUT = 180

ANKI_MODEL_ID = 1739018113
ANKI_DECK_ID = 2059418113

# Attribution must NOT claim MOE provided dictionary/definition content:
# clean-v1 uses MOE for stroke geometry only; text facts come from Unicode;
# pronunciation audio is human-recorded CNS11643 (see cns_audio.py).
# The learner-language tail sentence comes from the active profile
# (labels.attribution_ai); see build_attribution().
MOE_ATTRIBUTION_BASE = (
    "筆順：中華民國教育部（MOE Taiwan）。"
    "文字事實：Unicode Unihan。"
)

IDC_ARITY = {
    "⿰": 2, "⿱": 2, "⿲": 3, "⿳": 3,
    "⿴": 2, "⿵": 2, "⿶": 2, "⿷": 2,
    "⿸": 2, "⿹": 2, "⿺": 2, "⿻": 2,
    # Newer IDS operators. Keeping them here makes the parser degrade better.
    "⿼": 2, "⿽": 2, "⿾": 2, "⿿": 2,
}

CJK_RANGES = (
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0x20000, 0x323AF),
)


# =============================================================================
# Generic helpers
# =============================================================================

def ensure_dirs() -> None:
    for d in (
        LLM_CACHE_DIR,
        CARD_CACHE_DIR,
        BUILD,
        BUILD_MEDIA,
        BUILD_REPORTS,
    ):
        d.mkdir(parents=True, exist_ok=True)


def nfc(s: Any) -> str:
    if s is None:
        return ""
    return unicodedata.normalize("NFC", str(s).strip())


def clean_text(s: Any) -> str:
    text = nfc(s)
    text = html_std.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("\u3000", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def escape(s: Any) -> str:
    return html_std.escape(nfc(s), quote=True)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def is_cjk_char(s: str) -> bool:
    if len(s) != 1:
        return False
    cp = ord(s)
    return any(lo <= cp <= hi for lo, hi in CJK_RANGES)


def pinyin_key(s: str) -> str:
    # Hán-Việt CSV is tone-marked, so keep tone marks; only normalize spaces/case.
    s = nfc(s).lower()
    s = re.sub(r"^\([一二三四五六七八九十\d]+\)", "", s).strip()
    return re.sub(r"\s+", "", s)


def parse_sort_number(value: str) -> tuple[int, str]:
    s = nfc(value)
    m = re.search(r"\d+", s)
    if m:
        return int(m.group()), s

    cn = {
        "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
        "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    }
    for k, v in cn.items():
        if k in s:
            return v, s

    return 999, s


def json_from_model_text(text: str) -> dict:
    text = text.strip()

    # Remove Markdown fences if model ignored the JSON-only instruction.
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)

    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # Fallback: first balanced-ish JSON object.
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        result = json.loads(text[start:end + 1])
        if isinstance(result, dict):
            return result

    raise ValueError("LLM response does not contain a JSON object")


# =============================================================================
# Local config (clean-v1 §11)
# =============================================================================

def _read_json_first(paths: list[Path]) -> tuple[Any, Path | None]:
    """Read the first existing path as JSON. Returns (data, path or None)."""
    for path in paths:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8")), path
    return {}, None


def load_local_config() -> dict:
    """Load config/config.local.json; fall back to built-in defaults.

    Never contacts the network. Endpoint URLs / model aliases live here,
    never in the published dataset manifest.

    New config-driven layout (see config/config.example.json):

        generation.text:  {"mode": "local"|"endpoint", "local": {...},
                           "endpoint": {...}, "prompt": {...}}
        audio: {"source": "cns11643", "preferred_voice": "auto"}

    Audio has NO model/backend selection: pronunciation comes from the
    fixed CNS11643 human-recorded source. Backward compatibility: legacy
    top-level ``llm`` migrates into ``generation.text.endpoint``
    (endpoint mode preserved). Removed TTS audio configuration is
    REJECTED with a migration error, never silently accepted.
    """
    defaults = {
        "mode": "test",
        "profile": "vi",
        "test_characters": list(DEFAULT_TEST_CHARACTERS),
        "llm": {
            "base_url": DEFAULT_LLM_BASE_URL,
            "model": DEFAULT_LLM_MODEL,
            "timeout_seconds": DEFAULT_LLM_TIMEOUT,
            "trust_env": False,
        },
        # Character pronunciation audio: fixed CNS11643 human-recorded
        # source. NO model/provider/endpoint selection. preferred_voice
        # selects ONLY among same-reading CNS recordings when the source
        # records carry voice metadata ("auto" = deterministic default).
        "audio": {
            "source": CNS_SOURCE,
            "preferred_voice": "auto",
        },
        "generation": {
            "refresh_ai": False,
            "deck_name": "Taiwan Traditional Chinese - MOE",
            "output": str(BUILD / "taiwan_traditional_chinese.apkg"),
            "text": {
                "mode": "endpoint",
                "local": {
                    "provider": "huggingface",
                    "model_id": DEFAULT_LOCAL_MODEL_ID,
                    "revision": "",
                    "device": "cuda",
                    "dtype": "auto",
                    "generation": {"temperature": 0.2, "max_tokens": 1200},
                },
                "endpoint": {
                    "provider": "openai_compatible",
                    "base_url": DEFAULT_LLM_BASE_URL,
                    "model": DEFAULT_LLM_MODEL,
                    "timeout_seconds": DEFAULT_LLM_TIMEOUT,
                    "trust_env": False,
                    "api_key_env": "",
                    "generation": {"temperature": 0.2, "max_tokens": 1200},
                },
                "prompt": {
                    "system_file": DEFAULT_SYSTEM_FILE,
                    "user_file": DEFAULT_USER_FILE,
                },
            },
        },
    }
    user, used_path = _read_json_first([CONFIG_LOCAL])
    if used_path is None:
        return defaults
    try:
        if not isinstance(user, dict):
            raise ValueError("top-level JSON must be an object")
    except (OSError, ValueError) as exc:
        print(f"[config] WARNING: cannot parse {used_path}: {exc}; using defaults")
        return defaults
    merged = dict(defaults)
    for key, value in user.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            if key == "generation" and isinstance(value, dict):
                gen = dict(merged["generation"])
                for gkey, gval in value.items():
                    if (isinstance(gval, dict)
                            and isinstance(gen.get(gkey), dict)):
                        sub = dict(gen[gkey])
                        for skey, sval in gval.items():
                            if (isinstance(sval, dict)
                                    and isinstance(sub.get(skey), dict)):
                                sub[skey] = {**sub[skey], **sval}
                            else:
                                sub[skey] = sval
                        gen[gkey] = sub
                    else:
                        gen[gkey] = gval
                merged[key] = gen
            else:
                merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    _warn_unknown_config_keys(user)
    _check_deprecated_audio_config(user)
    _migrate_legacy_blocks(merged, user)
    return merged


def _migrate_legacy_blocks(merged: dict, user: dict) -> None:
    """Migrate legacy top-level llm into generation.text.endpoint.

    Legacy ``llm`` (base_url/model/timeout/trust_env) maps onto
    generation.text.endpoint so old configs keep working in endpoint
    mode. Explicit new-block values always win over migrated values.
    (Removed TTS audio configuration is NOT migrated: see
    _check_deprecated_audio_config, which rejects it explicitly.)
    """
    gen = merged.get("generation")
    if not isinstance(gen, dict):
        return
    text = gen.get("text")
    if isinstance(text, dict) and isinstance(user.get("llm"), dict):
        endpoint = dict(text.get("endpoint", {}))
        legacy = user["llm"]
        for key in ("base_url", "model", "timeout_seconds", "trust_env"):
            if key in legacy and f"{key}" not in (
                    (user.get("generation") or {}).get("text", {})
                    .get("endpoint", {})):
                endpoint[key] = legacy[key]
        text["endpoint"] = endpoint


# Config keys removed with the TTS-architecture deletion. Presence of
# any of them is a hard error (not a warning): silently continuing
# through an obsolete audio path would risk wrong pronunciation.
_DEPRECATED_AUDIO_KEYS = frozenset({
    "mode", "provider", "local", "endpoint", "runtime", "model_id",
    "repo_id", "revision", "variant", "speaker_id", "language_id",
    "execution_provider", "device", "dtype", "speed", "inference",
    "pronunciation", "reference_audio", "reference_text", "locale",
    "voice", "format", "rate", "volume", "pitch", "output_format",
    "timeout_seconds", "supported_locales", "command",
    "uses_reading_control", "reading_control_mechanism", "settings",
    "settings_version", "tones", "onnx_file", "phones_file",
    "onnx_inputs", "sample_rate", "prompt", "base_url", "model",
    "trust_env", "api_key_env",
})


def _check_deprecated_audio_config(user: dict) -> None:
    """Reject removed TTS audio configuration with a migration error."""
    if not isinstance(user, dict):
        return
    bad_top = sorted(
        k for k, v in (user.get("audio") or {}).items()
        if isinstance(user.get("audio"), dict)
        and k in _DEPRECATED_AUDIO_KEYS and not _is_doc_key(k))
    bad_gen: list[str] = []
    gen_audio = ((user.get("generation") or {}).get("audio")
                 if isinstance(user.get("generation"), dict) else None)
    if isinstance(gen_audio, dict):
        bad_gen = sorted(k for k in gen_audio if not _is_doc_key(k))
    if bad_top or bad_gen:
        raise ValueError(
            "Audio model/backend selection was removed: pronunciation "
            "now comes ONLY from the fixed CNS11643 human-recorded "
            "source. Replace your audio config with "
            "{\"source\": \"cns11643\", "
            "\"preferred_voice\": \"auto\"} "
            f"(removed keys: audio.{bad_top}, "
            f"generation.audio.{bad_gen}). See "
            "config/config.example.json.")


# Documentation keys (see config/config.example.json): ignored everywhere
# by the loader. Anything else unknown is reported, not silently accepted,
# so misspelled runtime keys surface as warnings instead of dead config.
_DOC_KEY_PREFIXES = ("_comment", "_options", "_description")

_KNOWN_CONFIG_KEYS: dict[str, set[str] | None] = {
    # None = scalar/list value, validated for presence only.
    "mode": None,
    "profile": None,
    "test_characters": None,
    "llm": {"base_url", "model", "timeout_seconds", "trust_env"},
    # Audio has NO model/backend selection: fixed CNS11643 source plus
    # an optional same-reading voice preference only.
    "audio": {"source", "preferred_voice"},
    # Nested generation.text block: validated recursively
    # (see _KNOWN_GENERATION_SUBKEYS). Only documentation prefixes
    # (_comment*, _options*, _description*) are ignored; real typos
    # (e.g. "modle_id") still warn.
    "generation": {"refresh_ai", "deck_name", "output", "text"},
}

_KNOWN_GENERATION_SUBKEYS: dict[str, Any] = {
    "text": {
        "mode": None,
        "local": {"provider", "model_id", "revision", "device",
                  "dtype", "generation"},
        "endpoint": {"provider", "base_url", "model", "timeout_seconds",
                     "trust_env", "api_key_env", "generation"},
        "prompt": {"system_file", "user_file"},
    },
}


def _is_doc_key(key: Any) -> bool:
    return isinstance(key, str) and key.startswith(_DOC_KEY_PREFIXES)


def _warn_unknown_config_keys(user: dict) -> None:
    """Warn about unknown non-documentation keys in user config.

    Documentation keys (`_comment*`, `_options*`, `_description*`) are
    silently ignored. Unknown real keys print a warning (typo safety)
    without failing, so forward-compatible configs keep working.
    Nested generation.text/audio blocks are checked recursively, so a
    typo like ``generation.text.local.modle_id`` warns instead of
    silently doing nothing.
    """
    if not isinstance(user, dict):
        return
    for key, value in user.items():
        if key in _KNOWN_CONFIG_KEYS or _is_doc_key(key):
            if key in _KNOWN_CONFIG_KEYS and isinstance(value, dict):
                known = _KNOWN_CONFIG_KEYS[key]
                if isinstance(known, set):
                    for sub in value:
                        if (key == "generation" and sub in ("text", "audio")
                                and isinstance(value[sub], dict)):
                            _warn_unknown_nested(
                                f"generation.{sub}", value[sub],
                                _KNOWN_GENERATION_SUBKEYS[sub])
                        elif sub not in known and not _is_doc_key(sub):
                            print(
                                f"[config] WARNING: unknown key "
                                f"{key}.{sub}; ignored")
            continue
        print(f"[config] WARNING: unknown top-level key {key!r}; ignored")


def _warn_unknown_nested(path: str, node: Any, known: Any) -> None:
    """Recursively warn about unknown keys under a nested config block."""
    if not isinstance(node, dict) or not isinstance(known, dict):
        return
    for sub, value in node.items():
        if _is_doc_key(sub):
            continue
        if sub not in known:
            print(f"[config] WARNING: unknown key {path}.{sub}; ignored")
            continue
        child_known = known[sub]
        if isinstance(child_known, dict) and isinstance(value, dict):
            _warn_unknown_nested(f"{path}.{sub}", value, child_known)
        elif isinstance(child_known, set) and isinstance(value, dict):
            for leaf in value:
                if leaf not in child_known and not _is_doc_key(leaf):
                    print(f"[config] WARNING: unknown key "
                          f"{path}.{sub}.{leaf}; ignored")


def validate_text_config(text_cfg: dict) -> None:
    """Validate generation.text (raises ValueError on bad config).

    - mode must be local|endpoint.
    - local: provider + model_id required (files need NOT exist yet;
      first actual use downloads them).
    - endpoint: provider + base_url + model required.
    - prompt files must exist with required placeholders.
    """
    from text_providers import TEXT_MODES
    cfg = text_cfg if isinstance(text_cfg, dict) else {}
    mode = str(cfg.get("mode", "endpoint") or "endpoint").strip().lower()
    if mode not in TEXT_MODES:
        raise ValueError(
            f"generation.text.mode must be one of {list(TEXT_MODES)}, "
            f"got {mode!r}")
    if mode == "local":
        local = cfg.get("local", {})
        if not isinstance(local, dict):
            raise ValueError("generation.text.local must be an object")
        if not str(local.get("provider", "") or "").strip():
            raise ValueError(
                "generation.text.local.provider is required")
        if not str(local.get("model_id", "") or "").strip():
            raise ValueError(
                "generation.text.local.model_id is required "
                "(model files do NOT have to exist yet; they download "
                "on first actual generate.py run)")
    else:
        endpoint = cfg.get("endpoint", {})
        if not isinstance(endpoint, dict):
            raise ValueError("generation.text.endpoint must be an object")
        if not str(endpoint.get("provider", "") or "").strip():
            raise ValueError(
                "generation.text.endpoint.provider is required")
        if not str(endpoint.get("base_url", "") or "").strip():
            raise ValueError(
                "generation.text.endpoint.base_url is required in "
                "endpoint mode")
        if not str(endpoint.get("model", "") or "").strip():
            raise ValueError(
                "generation.text.endpoint.model is required in "
                "endpoint mode")
    prompt = cfg.get("prompt", {})
    if not isinstance(prompt, dict):
        raise ValueError("generation.text.prompt must be an object")
    system_file = str(prompt.get("system_file", "") or "")
    user_file = str(prompt.get("user_file", "") or "")
    if not system_file or not user_file:
        raise ValueError(
            "generation.text.prompt.system_file and user_file are required")
    try:
        validate_text_prompts(system_file, user_file)
    except PromptError as exc:
        raise ValueError(f"bad text prompt config: {exc}") from exc


def validate_audio_config(audio_cfg: dict) -> None:
    """Validate the fixed-source audio config (raises ValueError).

    Audio has NO model/backend selection. The only valid shape is::

        {"source": "cns11643", "preferred_voice": "auto"|"male"|"female"}

    ``preferred_voice`` selects ONLY among same-reading CNS recordings
    and only when source records carry voice metadata. Removed TTS
    fields are rejected earlier by _check_deprecated_audio_config.
    Missing/empty config means defaults (CNS source, auto voice).
    """
    cfg = audio_cfg if isinstance(audio_cfg, dict) else {}
    source = str(cfg.get("source", CNS_SOURCE) or CNS_SOURCE).strip()
    if source != CNS_SOURCE:
        raise ValueError(
            f"audio.source must be {CNS_SOURCE!r} (fixed human-recorded "
            f"source; no TTS fallback), got {source!r}")
    voice = str(cfg.get("preferred_voice", "auto") or "auto").strip()
    if voice.lower() not in PREFERRED_VOICES:
        raise ValueError(
            f"audio.preferred_voice must be one of "
            f"{list(PREFERRED_VOICES)}, got {voice!r}")


# =============================================================================
# Language profiles (target-language architecture)
# -----------------------------------------------------------------------------
# The core pipeline is language-neutral. All learner-facing text (card
# labels, LLM target-language instruction, optional per-language features
# such as Han-Viet for Vietnamese) comes from config/profiles/<code>.json.
# To add a language: copy vi.json, translate labels + llm.instruction,
# set features, and select it via "profile" in config/config.local.json.
# =============================================================================

DEFAULT_PROFILE_LABELS = {
    "hanviet": "Han-Viet",
    "unihan": "Unihan",
    "variants": "Variants",
    "structure": "Character structure",
    "stroke_order": "Stroke order",
    "examples": "Examples",
    "notable_sayings": "Notable sayings",
    "other_readings_title": "Other readings (Unihan)",
    "table_pinyin": "Pinyin",
    "table_zhuyin": "Zhuyin",
    "table_hanviet": "Han-Viet",
    "table_note": "Note",
    "table_character": "Character",
    "table_role": "Role",
    "table_meaning": "Meaning",
    "role_radical": "Radical",
    "role_component": "Component",
    "component_role_ai": "Component (AI suggestion)",
    "components_disclaimer": "Components suggested by AI for learning; not authoritative IDS data.",
    "no_components": "No component data yet.",
    "no_examples": "No examples yet.",
    "alternate_reading_note": "Alternate Unihan kMandarin reading (not primary)",
    "attribution_ai": "Explanations and examples generated by AI for learning reference only.",
}


def load_profile(code: str) -> dict:
    """Load config/profiles/<code>.json with safe English-label fallback.

    Never contacts the network. Unknown/missing profiles fall back to
    built-in defaults so generation never crashes on a typo; the effective
    language_code is always echoed in records and manifests.
    """
    code = (code or "vi").strip().lower() or "vi"
    data, _ = _read_json_first([CONFIG_PROFILES_DIR / f"{code}.json"])
    if not isinstance(data, dict):
        data = {}
    labels = dict(DEFAULT_PROFILE_LABELS)
    if isinstance(data.get("labels"), dict):
        for key, value in data["labels"].items():
            if isinstance(value, str) and value.strip():
                labels[key] = value
    features = data.get("features") if isinstance(data.get("features"), dict) else {}
    llm = data.get("llm") if isinstance(data.get("llm"), dict) else {}
    return {
        "language_code": data.get("language_code", code) or code,
        "language_name": data.get("language_name", code) or code,
        "native_name": data.get("native_name", "") or "",
        "labels": labels,
        "features": features,
        "llm": llm,
    }


def profile_labels(profile: dict) -> dict:
    return profile.get("labels", DEFAULT_PROFILE_LABELS)


def build_attribution(profile: dict) -> str:
    """SourceNote field: fixed MOE/Unicode ownership + profile AI tail."""
    tail = profile_labels(profile).get(
        "attribution_ai", DEFAULT_PROFILE_LABELS["attribution_ai"])
    return f"{MOE_ATTRIBUTION_BASE}{tail}"


# =============================================================================
# CLEAN-v1: deterministic Pinyin -> Zhuyin/Bopomofo (§4)
# -----------------------------------------------------------------------------
# Isolated, dependency-free, fully deterministic. Zhuyin is NEVER generated
# by the LLM; it is always derived from the selected Unihan kMandarin reading.
# =============================================================================

_PINYIN_TONE_MARKS: dict[str, tuple[str, int]] = {}
for _base, _marks in {
    "a": ("ā", "á", "ǎ", "à"),
    "e": ("ē", "é", "ě", "è"),
    "i": ("ī", "í", "ǐ", "ì"),
    "o": ("ō", "ó", "ǒ", "ò"),
    "u": ("ū", "ú", "ǔ", "ù"),
    "ü": ("ǖ", "ǘ", "ǚ", "ǜ"),
    "v": ("ǖ", "ǘ", "ǚ", "ǜ"),
}.items():
    for _i, _m in enumerate(_marks, start=1):
        _PINYIN_TONE_MARKS[_m] = (_base, _i)
    _PINYIN_TONE_MARKS[_base] = (_base, 0)
_PINYIN_TONE_MARKS["ê"] = ("e", 0)
_PINYIN_TONE_MARKS["ń"] = ("n", 2)
_PINYIN_TONE_MARKS["ň"] = ("n", 3)
_PINYIN_TONE_MARKS["ǹ"] = ("n", 4)
_PINYIN_TONE_MARKS["m̀"] = ("m", 4)

_ZHUYIN_INITIALS = {
    "b": "ㄅ", "p": "ㄆ", "m": "ㄇ", "f": "ㄈ",
    "d": "ㄉ", "t": "ㄊ", "n": "ㄋ", "l": "ㄌ",
    "g": "ㄍ", "k": "ㄎ", "h": "ㄏ",
    "j": "ㄐ", "q": "ㄑ", "x": "ㄒ",
    "zh": "ㄓ", "ch": "ㄔ", "sh": "ㄕ", "r": "ㄖ",
    "z": "ㄗ", "c": "ㄘ", "s": "ㄙ",
}

_ZHUYIN_FINALS = {
    # core finals (w/o medial) keyed by normalized pinyin final
    "a": "ㄚ", "o": "ㄛ", "e": "ㄜ", "ai": "ㄞ", "ei": "ㄟ",
    "ao": "ㄠ", "ou": "ㄡ", "an": "ㄢ", "en": "ㄣ",
    "ang": "ㄤ", "eng": "ㄥ", "ong": "ㄨㄥ",
    "i": "ㄧ", "ia": "ㄧㄚ", "ie": "ㄧㄝ", "iao": "ㄧㄠ",
    "iu": "ㄧㄡ", "ian": "ㄧㄢ", "in": "ㄧㄣ",
    "iang": "ㄧㄤ", "ing": "ㄧㄥ", "iong": "ㄩㄥ",
    "u": "ㄨ", "ua": "ㄨㄚ", "uo": "ㄨㄛ", "uai": "ㄨㄞ",
    "ui": "ㄨㄟ", "uan": "ㄨㄢ", "un": "ㄨㄣ",
    "uang": "ㄨㄤ", "ueng": "ㄨㄥ",
    "ü": "ㄩ", "üe": "ㄩㄝ", "üan": "ㄩㄢ", "ün": "ㄩㄣ",
    "er": "ㄦ",
    # shorthand spellings
    "iu_": "ㄧㄡ", "ui_": "ㄨㄟ", "un_": "ㄨㄣ",
    "iou": "ㄧㄡ", "uei": "ㄨㄟ", "uen": "ㄨㄣ",
    "iou_v": "ㄧㄡ",
}

_ZHUYIN_TONE_MARKS = {0: "", 1: "", 2: "ˊ", 3: "ˇ", 4: "ˋ", 5: "˙"}


def pinyin_to_zhuyin(pinyin: str) -> str:
    """Deterministically convert one tone-marked pinyin syllable to Zhuyin.

    Returns "" when the input cannot be parsed (caller must leave the
    field empty, never ask the LLM to invent it).
    """
    s = nfc(pinyin).lower().strip().split()[0] if nfc(pinyin).strip() else ""
    if not s:
        return ""
    # Numeric-tone form e.g. "si1" / "hao3".
    m = re.fullmatch(r"([a-züvê]+)([1-5])", s)
    if m:
        base, tone = m.group(1), int(m.group(2))
        if tone == 5:
            tone = 0  # neutral handled below via ˙ placement
            neutral = True
        else:
            neutral = False
    else:
        base_chars: list[str] = []
        tone = 0
        neutral = False
        for ch in s:
            if ch in _PINYIN_TONE_MARKS:
                plain, t = _PINYIN_TONE_MARKS[ch]
                base_chars.append(plain)
                if t:
                    tone = t
            elif ch.isalpha() or ch in ("ü",):
                base_chars.append(ch)
            else:
                return ""
        base = "".join(base_chars)
    if not base:
        return ""
    base = base.replace("v", "ü")
    # Split initial.
    initial = ""
    final = base
    for cand in ("zh", "ch", "sh", "b", "p", "m", "f", "d", "t", "n",
                 "l", "g", "k", "h", "j", "q", "x", "r", "z", "c", "s"):
        if base.startswith(cand):
            rest = base[len(cand):]
            # Single-letter initial only valid with a non-empty final,
            # except the syllabic nasal/retroflex handled below.
            if rest or cand in ("m", "n", "r"):
                initial, final = cand, rest
                break
    if not final:
        # Syllabic m/n; "r" -> "ri".
        if base in ("m", "n"):
            final = base
            initial = ""
        else:
            return ""
    # Special finals: -i after zh/ch/sh/r/z/c/s, standalone -i/-u/-ü.
    if final == "i" and initial in ("zh", "ch", "sh", "r", "z", "c", "s"):
        body = _ZHUYIN_INITIALS[initial]
    elif final == "i" and not initial:
        body = "ㄧ"
    elif final == "u" and not initial:
        body = "ㄨ"
    elif final in ("ü",) and not initial:
        body = "ㄩ"
    else:
        # j/q/x + u -> ü finals.
        if initial in ("j", "q", "x") and final.startswith("u"):
            trial = "ü" + final[1:]
            if trial in _ZHUYIN_FINALS:
                final = trial
        # -uo variants, w-/y- spellings.
        if not initial:
            if final.startswith("w"):
                final = "u" + final[1:]
            elif final.startswith("y"):
                rest = final[1:]
                final = ("i" + rest) if rest else "i"
        zh_final = _ZHUYIN_FINALS.get(final)
        if zh_final is None:
            return ""
        body = (_ZHUYIN_INITIALS.get(initial, "") if initial else "") + zh_final
    if m and tone == 0 and neutral:
        return "˙" + body
    mark = _ZHUYIN_TONE_MARKS.get(tone, "")
    if tone == 5 or (neutral and tone == 0):
        return "˙" + body
    if tone in (0, 1):
        return body + ("ˉ" if tone == 1 else "")
    return body + mark


# =============================================================================
# CLEAN-v1: Unicode Unihan primary factual source (§3)
# -----------------------------------------------------------------------------
# Parses ONLY documented Unihan properties from the version-pinned Unihan.zip
# extract (data/raw/unicode/unihan/). Never invents missing fields.
# =============================================================================

@dataclass
class UnihanFacts:
    char: str
    ucs: str
    pinyin: str = ""            # first kMandarin token (selected reading)
    pinyin_alternates: list = field(default_factory=list)
    definition_en: str = ""     # kDefinition (English grounding, NOT Chinese)
    radical_number: str = ""    # from kRSUnicode, e.g. "61"
    radical_char: str = ""      # resolved via CJKRadicals.txt
    stroke_count: str = ""      # first kTotalStrokes value
    stroke_counts_all: list = field(default_factory=list)
    traditional_variants: list = field(default_factory=list)
    simplified_variants: list = field(default_factory=list)
    vietnamese: str = ""        # kVietnamese (provenance tracked; semantics documented)


class UnihanIndex:
    """Version-pinned Unihan factual index (clean-v1 primary text source)."""

    CONSUMED_PROPS = {
        "kMandarin", "kDefinition", "kRSUnicode", "kTotalStrokes",
        "kTraditionalVariant", "kSimplifiedVariant", "kVietnamese",
    }

    def __init__(self, unihan_dir: Path, radicals_path: Path):
        self.unihan_dir = unihan_dir
        self.facts: dict[str, UnihanFacts] = {}
        self._load(unihan_dir, radicals_path)

    @staticmethod
    def _iter_prop_lines(root: Path):
        files = sorted(root.glob("Unihan_*.txt"))
        for path in files:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if not line.strip() or line.startswith("#"):
                        continue
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) != 3:
                        continue
                    yield parts

    @staticmethod
    def _cp_to_char(cp: str) -> str:
        if not cp.startswith("U+"):
            return ""
        try:
            return chr(int(cp[2:], 16))
        except ValueError:
            return ""

    def _load(self, root: Path, radicals_path: Path) -> None:
        per_char: dict[str, dict[str, str]] = {}
        found_any = False
        for cp, prop, value in self._iter_prop_lines(root):
            if prop not in self.CONSUMED_PROPS:
                continue
            found_any = True
            char = self._cp_to_char(cp)
            if len(char) != 1:
                continue
            slot = per_char.setdefault(char, {})
            # First occurrence wins (matches legacy UnihanFallback behavior).
            if prop not in slot:
                slot[prop] = value
        if not found_any:
            print(f"[unihan] WARNING: no Unihan_*.txt data under {root}")
        radical_map = self._load_radical_map(radicals_path)
        for char, props in per_char.items():
            mandarin = props.get("kMandarin", "")
            tokens = mandarin.split()
            pinyin = tokens[0] if tokens else ""
            rs = props.get("kRSUnicode", "")
            rad_num = rs.split()[0].split(".")[0] if rs else ""
            rad_num = "".join(c for c in rad_num if c.isdigit())
            strokes = props.get("kTotalStrokes", "").split()
            self.facts[char] = UnihanFacts(
                char=char,
                ucs=f"{ord(char):04X}",
                pinyin=nfc(pinyin),
                pinyin_alternates=[nfc(t) for t in tokens[1:]],
                definition_en=nfc(props.get("kDefinition", "")),
                radical_number=rad_num,
                radical_char=radical_map.get(rad_num, ""),
                stroke_count=nfc(strokes[0]) if strokes else "",
                stroke_counts_all=[nfc(x) for x in strokes],
                traditional_variants=[
                    self._cp_to_char(c) or c
                    for c in props.get("kTraditionalVariant", "").split()
                ],
                simplified_variants=[
                    self._cp_to_char(c) or c
                    for c in props.get("kSimplifiedVariant", "").split()
                ],
                vietnamese=nfc(props.get("kVietnamese", "")),
            )
        print(f"[unihan] indexed {len(self.facts):,} characters (Unicode {UNICODE_VERSION})")

    @staticmethod
    def _load_radical_map(path: Path) -> dict[str, str]:
        """Map radical number -> radical character via CJKRadicals.txt.

        Handles the official format variants; unknown numbers are left
        unresolved (caller leaves the display field empty).
        """
        mapping: dict[str, str] = {}
        if not path.exists():
            print(f"[unihan] WARNING: radicals file not found: {path}")
            return mapping
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split(";")]
            if len(parts) < 2:
                continue
            # Format A: "2E80; 1; ..." (codepoint; radical number; ...)
            # Format B: "1; 4E00 ..." (number first) — accept defensively.
            try:
                if re.fullmatch(r"[0-9A-Fa-f]{4,6}", parts[0]):
                    num = "".join(c for c in parts[1] if c.isdigit())
                    mapping[num] = chr(int(parts[0], 16))
                elif parts[0].isdigit():
                    m = re.search(r"[0-9A-Fa-f]{4,6}", parts[1])
                    if m:
                        mapping[parts[0]] = chr(int(m.group(), 16))
            except (ValueError, IndexError):
                continue
        return mapping

    def get(self, char: str) -> UnihanFacts | None:
        return self.facts.get(char)


# =============================================================================
# LEGACY/DEPRECATED: MOE-recording audio resolver — NOT used by clean-v1.
# -----------------------------------------------------------------------------
# clean-v1 uses fixed CNS11643 human recordings (see cns_audio.py) because
# MOE clips regularly speak more than the isolated target character. This
# class (manifest + filename matching over word_wav/*.wav) is retained here
# only for rollback comparison of old datasets. It has NO active-path
# callers.
# =============================================================================

class CleanAudioResolver:
    """DEPRECATED: MOE-recording resolver. Do not use in the clean path."""
    AUDIO_EXTS = {".wav", ".mp3", ".ogg", ".m4a"}

    def __init__(self, root: Path):
        self.root = root
        self.files = [
            p for p in root.rglob("*")
            if p.is_file() and p.suffix.lower() in self.AUDIO_EXTS
        ]
        self.search_text = {
            p: unicodedata.normalize("NFKC", p.relative_to(root).as_posix()).lower()
            for p in self.files
        }
        self.char_index: dict[str, list[Path]] = defaultdict(list)
        for p in self.files:
            stem = unicodedata.normalize("NFKC", p.stem)
            if len(stem) == 1 and is_cjk_char(stem):
                self.char_index[stem].append(p)
        self.metadata_note = self._scan_metadata(root)
        self.manifest_map, self.manifest_note = self._load_manifest(root)
        print(f"[audio] indexed {len(self.files):,} audio files (clean resolver, no dict IDs)")

    @staticmethod
    def _scan_metadata(root: Path) -> str:
        metas = [p for p in root.rglob("*")
                 if p.is_file() and p.suffix.lower()
                 in {".csv", ".json", ".txt", ".xml", ".xlsx"}]
        if not metas:
            return "no metadata files found in audio package"
        return f"{len(metas)} metadata candidate(s): " + ", ".join(
            str(p.relative_to(root)) for p in metas[:10])

    def _load_manifest(self, root: Path) -> tuple[dict[str, list[Path]], str]:
        """Parse the in-package filename manifest (字詞名 -> 檔案名稱).

        The manifest filename is release-stamped, so it is discovered by
        glob, never hardcoded. Joins on the character column only; the
        字詞號 ID column is ignored. Returns ({}, note) when openpyxl is
        missing or no usable manifest exists (caller falls back to
        filename matching).
        """
        empty: dict[str, list[Path]] = {}
        manifests = sorted(root.glob("*.xlsx"))
        if not manifests:
            return empty, "no manifest xlsx in audio package"
        try:
            from openpyxl import load_workbook as _load
        except ImportError:
            return empty, (
                f"manifest present ({manifests[0].name}) but openpyxl "
                "not installed; filename matching only"
            )
        by_name: dict[str, Path] = {}
        for p in self.files:
            by_name.setdefault(p.name, p)
        manifest = manifests[0]
        try:
            wb = _load(manifest, read_only=True, data_only=True)
            ws = wb[wb.sheetnames[0]]
            header = None
            char_col = file_col = -1
            mapping: dict[str, list[Path]] = defaultdict(list)
            rows = 0
            for row in ws.iter_rows(min_row=1, max_row=8, values_only=True):
                cells = [nfc(c) for c in (row or [])]
                if "字詞名" in cells and "檔案名稱" in cells:
                    header = cells
                    char_col = cells.index("字詞名")
                    file_col = cells.index("檔案名稱")
                    break
            if header is None:
                wb.close()
                return empty, f"manifest {manifest.name}: no 字詞名/檔案名稱 header"
            for row in ws.iter_rows(min_row=2, values_only=True):
                if not row:
                    continue
                char = nfc(row[char_col]) if char_col < len(row) else ""
                fn = nfc(row[file_col]) if file_col < len(row) else ""
                if len(char) != 1 or not fn:
                    continue
                target = by_name.get(fn)
                if target is None:
                    continue
                if target not in mapping[char]:
                    mapping[char].append(target)
                rows += 1
            wb.close()
        except Exception as exc:
            return empty, f"manifest {manifest.name}: unreadable ({exc})"
        note = f"{manifest.name}: {rows} rows, {len(mapping)} chars"
        print(f"[audio] manifest {note}")
        return dict(mapping), note

    def resolve_all(self, char: str) -> tuple[list[Path], dict]:
        """Resolve ALL candidate audio files for a character.

        Returns (paths, info) with paths in deterministic (filename-sorted)
        order:
          - manifest hits (any count) -> score 100, all preserved. Multiple
            hits are NOT discarded: the package carries no per-file
            pronunciation metadata, so no single file can be proven to match
            the card's reading and picking one would be guessing.
          - single filename hit (stem==char 95 / char-in-path 80) -> as before.
          - nothing -> ([], no_match info).
        Callers decide presentation: 1 file = resolved, N files = multiple
        (all preserved, unlabeled), 0 files = unresolved.
        """
        if not self.files:
            return [], {"reason": "no_audio_files"}
        hits = sorted(self.manifest_map.get(char, []), key=lambda p: p.name)
        if hits:
            return hits, {
                "reason": "matched" if len(hits) == 1 else "multiple",
                "score": 100,
                "match_reasons": ["manifest:字詞名->檔案名稱"],
                "source_paths": [str(p.relative_to(self.root)) for p in hits],
                "manifest": self.manifest_note,
            }
        scored: list[tuple[int, Path, list[str]]] = []
        for path in self.files:
            rel = self.search_text[path]
            score, reasons = 0, []
            if path.stem == char:
                score, reasons = 95, ["stem==char"]
            elif char and char in rel:
                score, reasons = 80, ["char-in-path"]
            if score:
                scored.append((score, path, reasons))
        if not scored:
            return [], {"reason": "no_match", "char": char,
                        "metadata": self.metadata_note}
        scored.sort(key=lambda x: (-x[0], str(x[1])))
        best_score = scored[0][0]
        best = [x for x in scored if x[0] == best_score]
        if best_score < 80 or len(best) != 1:
            return [], {
                "reason": "ambiguous_or_low_confidence",
                "best_score": best_score,
                "candidates": [
                    {"path": str(x[1].relative_to(self.root)),
                     "score": x[0], "reasons": x[2]} for x in best[:10]
                ],
            }
        return [best[0][1]], {
            "reason": "matched",
            "score": best[0][0],
            "match_reasons": best[0][2],
            "source_path": str(best[0][1].relative_to(self.root)),
        }

    def resolve(self, char: str) -> tuple[Path | None, dict]:
        """Backward-compatible single-file resolution (first candidate)."""
        paths, info = self.resolve_all(char)
        if not paths:
            return None, info
        single = dict(info)
        single["source_path"] = str(paths[0].relative_to(self.root))
        return paths[0], single


# =============================================================================
# CLEAN-v1 grounding: Unihan facts + MOE stroke index (§1, §10)
# =============================================================================

@dataclass
class CleanGrounding:
    char: str
    ucs: str
    pinyin: str
    pinyin_alternates: list
    zhuyin: str
    definition_en: str
    radical_number: str
    radical_char: str
    stroke_count: str
    traditional_variants: list
    simplified_variants: list
    vietnamese_raw: str
    stroke_xml_path: str


class CleanGrounder:
    def __init__(self, unihan: UnihanIndex, strokes: "StrokeIndex"):
        self.unihan = unihan
        self.strokes = strokes

    def build(self, char: str) -> CleanGrounding:
        stroke = self.strokes.get(char)
        if stroke is None:
            raise RuntimeError(f"No offline MOE stroke XML for {char}")
        facts = self.unihan.get(char)
        if facts is None:
            # Clearly labeled fallback: no authoritative text facts.
            # Zhuyin stays empty (never LLM-invented).
            return CleanGrounding(
                char=char, ucs=stroke.ucs, pinyin="",
                pinyin_alternates=[], zhuyin="",
                definition_en="", radical_number="",
                radical_char="", stroke_count="",
                traditional_variants=[], simplified_variants=[],
                vietnamese_raw="",
                stroke_xml_path=str(stroke.xml_path),
            )
        return CleanGrounding(
            char=char, ucs=facts.ucs, pinyin=facts.pinyin,
            pinyin_alternates=facts.pinyin_alternates,
            zhuyin=pinyin_to_zhuyin(facts.pinyin) if facts.pinyin else "",
            definition_en=facts.definition_en,
            radical_number=facts.radical_number,
            radical_char=facts.radical_char,
            stroke_count=facts.stroke_count,
            traditional_variants=facts.traditional_variants,
            simplified_variants=facts.simplified_variants,
            vietnamese_raw=facts.vietnamese,
            stroke_xml_path=str(stroke.xml_path),
        )

    def facts_block(self, g: CleanGrounding) -> dict:
        """CLEAN source facts sent to the LLM (§8). No MOE dict text."""
        return {
            "character": g.char,
            "pinyin": g.pinyin,
            "zhuyin": g.zhuyin,
            "unicode_definition_en": g.definition_en,
            "radical": g.radical_char,
            "radical_number": g.radical_number,
            "stroke_count": g.stroke_count,
            "variants": {
                "traditional": g.traditional_variants,
                "simplified": g.simplified_variants,
            },
        }


# =============================================================================
# LEGACY (pre-clean-v1) SOURCES — NOT used by the clean pipeline.
# -----------------------------------------------------------------------------
# Retained for rollback/reference; existing on-disk downloads are preserved.
# Do NOT call these from the clean path.
# =============================================================================

@dataclass
class DictionaryEntry:
    title: str
    word_id: str = ""
    radical: str = ""
    stroke_count: str = ""
    non_radical_strokes: str = ""
    het_sort: str = ""
    bopomofo: str = ""
    pinyin: str = ""
    definition_zh: str = ""
    synonyms: str = ""
    antonyms: str = ""
    het_ref: str = ""
    source: str = "moe_concised"
    row_number: int = 0

    def compact(self) -> dict:
        return {
            "title": self.title,
            "word_id": self.word_id,
            "radical": self.radical,
            "stroke_count": self.stroke_count,
            "non_radical_strokes": self.non_radical_strokes,
            "het_sort": self.het_sort,
            "bopomofo": self.bopomofo,
            "pinyin": self.pinyin,
            "definition_zh": self.definition_zh,
            "source": self.source,
        }


class MoeDictionary:
    REQUIRED_HEADER = "字詞名"

    HEADER_ALIASES = {
        "title": ("字詞名",),
        "word_id": ("字詞號",),
        "radical": ("部首字", "部首"),
        "stroke_count": ("總筆畫數", "縂筆畫數"),
        "non_radical_strokes": ("部首外筆畫數",),
        "het_sort": ("多音排序",),
        "bopomofo": ("注音一式", "注音"),
        "pinyin": ("漢語拼音",),
        "synonyms": ("相似詞",),
        "antonyms": ("相反詞",),
        "definition_zh": ("釋義",),
        "het_ref": ("多音參見訊息",),
    }

    def __init__(self, root: Path):
        self.root = root
        self.by_title: dict[str, list[DictionaryEntry]] = defaultdict(list)
        self.workbook_path: Path | None = None
        self.sheet_name: str | None = None
        self._load()

    def _find_workbook(self) -> Path:
        files = [
            p for p in self.root.rglob("*.xlsx")
            if "欄位說明" not in p.name
            and not p.name.startswith("~$")
        ]
        if not files:
            raise FileNotFoundError(
                f"No dictionary .xlsx found under {self.root}. "
                "Run download.py first."
            )

        # The main dictionary workbook is normally the largest xlsx.
        return max(files, key=lambda p: p.stat().st_size)

    @staticmethod
    def _find_header_row(ws) -> tuple[int, list[str]]:
        for row_idx, row in enumerate(
            ws.iter_rows(min_row=1, max_row=20, values_only=True),
            start=1,
        ):
            values = [nfc(x) for x in row]
            if MoeDictionary.REQUIRED_HEADER in values:
                return row_idx, values
        raise RuntimeError(
            f"Could not find header row with {MoeDictionary.REQUIRED_HEADER!r} "
            f"in sheet {ws.title!r}"
        )

    @classmethod
    def _col(cls, headers: list[str], logical: str) -> int | None:
        for candidate in cls.HEADER_ALIASES[logical]:
            if candidate in headers:
                return headers.index(candidate)
        return None

    def _load(self) -> None:
        if load_workbook is None:
            raise RuntimeError(
                "openpyxl is required only for the LEGACY MOE dictionary "
                "path (pip install openpyxl). Clean-v1 never calls this."
            )
        path = self._find_workbook()
        self.workbook_path = path
        print(f"[dictionary] {path}")

        wb = load_workbook(path, read_only=True, data_only=True)

        selected_ws = None
        header_row = None
        headers = None

        for ws in wb.worksheets:
            try:
                hr, h = self._find_header_row(ws)
                selected_ws = ws
                header_row = hr
                headers = h
                break
            except RuntimeError:
                continue

        if selected_ws is None or headers is None or header_row is None:
            wb.close()
            raise RuntimeError(
                f"No usable MOE dictionary sheet found in {path}"
            )

        self.sheet_name = selected_ws.title
        print(f"[dictionary] sheet={self.sheet_name!r}, header_row={header_row}")

        idx = {
            key: self._col(headers, key)
            for key in self.HEADER_ALIASES
        }

        if idx["title"] is None:
            wb.close()
            raise RuntimeError("MOE dictionary lacks 字詞名")

        def cell(row: tuple, key: str) -> str:
            i = idx[key]
            return nfc(row[i]) if i is not None and i < len(row) else ""

        for excel_row_num, row in enumerate(
            selected_ws.iter_rows(
                min_row=header_row + 1,
                values_only=True,
            ),
            start=header_row + 1,
        ):
            title = cell(row, "title")
            if not title:
                continue

            entry = DictionaryEntry(
                title=title,
                word_id=cell(row, "word_id"),
                radical=cell(row, "radical"),
                stroke_count=cell(row, "stroke_count"),
                non_radical_strokes=cell(row, "non_radical_strokes"),
                het_sort=cell(row, "het_sort"),
                bopomofo=cell(row, "bopomofo"),
                pinyin=cell(row, "pinyin"),
                definition_zh=cell(row, "definition_zh"),
                synonyms=cell(row, "synonyms"),
                antonyms=cell(row, "antonyms"),
                het_ref=cell(row, "het_ref"),
                row_number=excel_row_num,
            )
            self.by_title[title].append(entry)

        wb.close()

        for title, entries in self.by_title.items():
            entries.sort(
                key=lambda e: (
                    parse_sort_number(e.het_sort),
                    e.row_number,
                )
            )

        print(
            f"[dictionary] indexed {len(self.by_title):,} unique headwords"
        )

    def readings(self, title: str) -> list[DictionaryEntry]:
        return list(self.by_title.get(title, []))

    def primary(self, title: str) -> DictionaryEntry | None:
        entries = self.by_title.get(title)
        return entries[0] if entries else None


# =============================================================================
# LEGACY: Hán-Việt external dataset (NOT used by clean-v1)
# =============================================================================

class HanVietIndex:
    def __init__(self, csv_path: Path):
        if not csv_path.exists():
            raise FileNotFoundError(
                f"Missing Hán-Việt data: {csv_path}. Run download.py first."
            )

        self.exact: dict[tuple[str, str], str] = {}
        self.wildcard: dict[str, str] = {}
        self.by_char: dict[str, set[str]] = defaultdict(set)

        with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            expected = {"char", "hanviet", "pinyin"}
            if not expected.issubset(reader.fieldnames or []):
                raise RuntimeError(
                    f"Unexpected Hán-Việt CSV headers: {reader.fieldnames}"
                )

            for row in reader:
                char = nfc(row.get("char"))
                hv = nfc(row.get("hanviet"))
                py = nfc(row.get("pinyin"))

                if not char or not hv:
                    continue

                self.by_char[char].add(hv)

                if py == "*":
                    self.wildcard[char] = hv
                elif py:
                    self.exact[(char, pinyin_key(py))] = hv

        print(
            f"[hanviet] indexed {len(self.by_char):,} characters"
        )

    def lookup(self, char: str, pinyin: str) -> str:
        key = (char, pinyin_key(pinyin))
        if key in self.exact:
            return self.exact[key]

        if char in self.wildcard:
            return self.wildcard[char]

        values = sorted(self.by_char.get(char, set()))
        # If there is only one possible Hán-Việt reading, safe fallback.
        if len(values) == 1:
            return values[0]

        return ""


# =============================================================================
# LEGACY: CHISE IDS (NOT used by clean-v1; LLM components are AI enrichment)
# =============================================================================

@dataclass
class IDSNode:
    token: str
    children: list["IDSNode"] = field(default_factory=list)

    def render(self) -> str:
        return self.token + "".join(c.render() for c in self.children)


def tokenize_ids(ids: str) -> list[str]:
    tokens = []
    i = 0

    while i < len(ids):
        ch = ids[i]

        if ch.isspace():
            i += 1
            continue

        if ch == "&":
            j = ids.find(";", i)
            if j != -1:
                tokens.append(ids[i:j + 1])
                i = j + 1
                continue

        # Ignore VS selectors but keep base ideograph.
        cp = ord(ch)
        if 0xFE00 <= cp <= 0xFE0F:
            i += 1
            continue

        tokens.append(ch)
        i += 1

    return tokens


def parse_ids_tree(ids: str) -> IDSNode | None:
    tokens = tokenize_ids(ids)
    if not tokens:
        return None

    def parse_at(pos: int) -> tuple[IDSNode, int]:
        if pos >= len(tokens):
            raise ValueError("Unexpected end of IDS")

        token = tokens[pos]
        pos += 1

        arity = IDC_ARITY.get(token, 0)
        children = []

        for _ in range(arity):
            child, pos = parse_at(pos)
            children.append(child)

        return IDSNode(token=token, children=children), pos

    try:
        root, _ = parse_at(0)
        return root
    except Exception:
        return None


class ChiseIndex:
    def __init__(self, root: Path):
        self.ids_by_char: dict[str, str] = {}
        self._load(root)

    def _load(self, root: Path) -> None:
        files = sorted(root.glob("IDS-*.txt"))
        if not files:
            print(f"[chise] WARNING: no IDS files under {root}")
            return

        for path in files:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue

                    parts = line.split("\t")
                    if len(parts) < 3:
                        continue

                    char = nfc(parts[1])
                    if len(char) != 1:
                        continue

                    candidates = []
                    for raw in parts[2:]:
                        raw = raw.strip()
                        # CHISE may prefix source tags like [G], [T], ...
                        raw = re.sub(r"^\[[^\]]+\]", "", raw).strip()
                        if raw:
                            candidates.append(raw)

                    ids = next(
                        (
                            x for x in candidates
                            if x and x != char
                            and any(op in x for op in IDC_ARITY)
                        ),
                        "",
                    )

                    if ids and char not in self.ids_by_char:
                        self.ids_by_char[char] = ids

        print(
            f"[chise] indexed {len(self.ids_by_char):,} decompositions"
        )

    def ids(self, char: str) -> str:
        return self.ids_by_char.get(char, "")

    def direct_components(self, char: str) -> list[str]:
        ids = self.ids(char)
        root = parse_ids_tree(ids)
        if root is None:
            return []

        if root.token not in IDC_ARITY:
            return [root.render()]

        return [child.render() for child in root.children]


# =============================================================================
# LEGACY: UnihanFallback (superseded by UnihanIndex; NOT used by clean-v1)
# =============================================================================

class UnihanFallback:
    def __init__(self, root: Path):
        self.reading: dict[str, str] = {}
        self.definition: dict[str, str] = {}

        path = root / "Unihan_Readings.txt"
        if not path.exists():
            print(f"[unihan] WARNING: {path} not found")
            return

        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line or line.startswith("#"):
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) != 3:
                    continue

                cp, prop, value = parts
                if not cp.startswith("U+"):
                    continue

                try:
                    char = chr(int(cp[2:], 16))
                except ValueError:
                    continue

                if prop == "kMandarin" and char not in self.reading:
                    self.reading[char] = value
                elif prop == "kDefinition" and char not in self.definition:
                    self.definition[char] = value

    def entry(self, char: str) -> DictionaryEntry | None:
        py = self.reading.get(char, "")
        definition = self.definition.get(char, "")
        if not py and not definition:
            return None

        return DictionaryEntry(
            title=char,
            pinyin=py,
            definition_zh=definition,
            source="unihan_fallback",
        )


# =============================================================================
# Stroke data
# =============================================================================

@dataclass
class StrokeRecord:
    char: str
    ucs: str
    moe_id: str
    xml_path: Path


class StrokeIndex:
    def __init__(self):
        self.by_char: dict[str, StrokeRecord] = {}
        self.ordered_chars: list[str] = []
        self._load()

    def _load(self) -> None:
        if STROKE_CATALOG.exists():
            with STROKE_CATALOG.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    row = json.loads(line)
                    char = nfc(row.get("char"))
                    ucs = nfc(row.get("ucs"))
                    moe_id = nfc(row.get("moe_id"))

                    if not char:
                        continue

                    path = None
                    rel = row.get("xml_relpath")
                    if rel:
                        candidate = DATA / rel
                        if candidate.exists():
                            path = candidate

                    if path is None:
                        matches = sorted(
                            MOE_STROKE_XML.glob(
                                f"U+{ucs}__ID*.xml"
                            )
                        )
                        if matches:
                            path = matches[0]

                    if path is None:
                        continue

                    if char not in self.by_char:
                        self.ordered_chars.append(char)

                    self.by_char[char] = StrokeRecord(
                        char=char,
                        ucs=ucs or f"{ord(char):04X}",
                        moe_id=moe_id,
                        xml_path=path,
                    )
        else:
            # Fallback if catalog was lost but XML exists.
            pattern = re.compile(
                r"U\+([0-9A-Fa-f]+)__ID(\d+)\.xml$"
            )
            for path in sorted(MOE_STROKE_XML.glob("*.xml")):
                m = pattern.match(path.name)
                if not m:
                    continue
                char = chr(int(m.group(1), 16))
                self.ordered_chars.append(char)
                self.by_char[char] = StrokeRecord(
                    char=char,
                    ucs=m.group(1).upper(),
                    moe_id=m.group(2),
                    xml_path=path,
                )

        print(
            f"[stroke] indexed {len(self.by_char):,} offline XML characters"
        )

    def get(self, char: str) -> StrokeRecord | None:
        return self.by_char.get(char)


# =============================================================================
# LEGACY: AudioResolver (used MOE dict 字詞號; NOT used by clean-v1 —
# like CleanAudioResolver above, which is also legacy since CNS11643
# human recordings replaced MOE recordings)
# =============================================================================

class AudioResolver:
    """
    Conservative resolver.

    MOE's single-character audio archive layout has changed across releases.
    We therefore do not assume one hard-coded filename layout.

    Scoring rules use the dictionary 字詞號 and, secondarily, the character
    itself. We only accept a unique high-confidence match.
    """

    AUDIO_EXTS = {".wav", ".mp3", ".ogg", ".m4a"}

    def __init__(self, root: Path):
        self.root = root
        self.files = [
            p for p in root.rglob("*")
            if p.is_file() and p.suffix.lower() in self.AUDIO_EXTS
        ]

        self.search_text = {}
        self.by_exact_stem: dict[str, list[Path]] = defaultdict(list)

        for path in self.files:
            rel = path.relative_to(root).as_posix()
            normalized = unicodedata.normalize("NFKC", rel).lower()
            self.search_text[path] = normalized
            self.by_exact_stem[
                unicodedata.normalize("NFKC", path.stem).lower()
            ].append(path)

        print(f"[audio] indexed {len(self.files):,} audio files")

    @staticmethod
    def _id_variants(word_id: str) -> list[str]:
        raw = nfc(word_id)
        if not raw:
            return []

        variants = {raw.lower()}

        digits = re.sub(r"\D", "", raw)
        if digits:
            variants.add(digits)
            stripped = digits.lstrip("0")
            if stripped:
                variants.add(stripped)

        return sorted(variants, key=len, reverse=True)

    def resolve(
        self,
        entry: DictionaryEntry,
    ) -> tuple[Path | None, dict]:
        if not self.files:
            return None, {"reason": "no_audio_files"}

        char = entry.title
        ids = self._id_variants(entry.word_id)

        scores: list[tuple[int, Path, list[str]]] = []

        for path in self.files:
            rel = self.search_text[path]
            stem = unicodedata.normalize("NFKC", path.stem).lower()
            score = 0
            reasons = []

            # Exact stem == official word id.
            for wid in ids:
                if stem == wid:
                    score = max(score, 120)
                    reasons.append(f"stem==id:{wid}")

            # ID as a standalone numeric/alphanumeric token in relative path.
            for wid in ids:
                if len(wid) >= 3:
                    if re.search(
                        rf"(?<![0-9a-z]){re.escape(wid)}(?![0-9a-z])",
                        rel,
                    ):
                        score = max(score, 100)
                        reasons.append(f"path-token-id:{wid}")

            # Exact Chinese character as filename.
            if path.stem == char:
                score = max(score, 95)
                reasons.append("stem==char")

            # Character appears in the path.
            if char and char in rel:
                score = max(score, 80)
                reasons.append("char-in-path")

            if score > 0:
                scores.append((score, path, reasons))

        if not scores:
            return None, {
                "reason": "no_match",
                "word_id": entry.word_id,
                "char": char,
            }

        scores.sort(key=lambda x: (-x[0], str(x[1])))
        best_score = scores[0][0]
        best = [x for x in scores if x[0] == best_score]

        # Never guess between tied candidates.
        if best_score < 80 or len(best) != 1:
            return None, {
                "reason": "ambiguous_or_low_confidence",
                "best_score": best_score,
                "candidates": [
                    {
                        "path": str(x[1].relative_to(self.root)),
                        "score": x[0],
                        "reasons": x[2],
                    }
                    for x in best[:10]
                ],
            }

        return best[0][1], {
            "reason": "matched",
            "score": best[0][0],
            "match_reasons": best[0][2],
            "source_path": str(best[0][1].relative_to(self.root)),
        }


# =============================================================================
# LEGACY: MOE-dictionary grounding (NOT used by clean-v1 — see CleanGrounder)
# =============================================================================

@dataclass
class ReadingInfo:
    pinyin: str
    bopomofo: str
    han_viet: str
    definition_zh: str
    word_id: str
    source: str


@dataclass
class ComponentInfo:
    token: str
    char: str
    role: str
    pinyin: str = ""
    bopomofo: str = ""
    han_viet: str = ""
    definition_zh: str = ""
    meaning_vi: str = ""


@dataclass
class CharacterGrounding:
    char: str
    ucs: str
    ids: str
    primary: ReadingInfo
    other_readings: list[ReadingInfo]
    radical: str
    stroke_count: str
    components: list[ComponentInfo]
    stroke_xml_path: str


class Grounder:
    def __init__(
        self,
        dictionary: MoeDictionary,
        hanviet: HanVietIndex,
        chise: ChiseIndex,
        unihan: UnihanFallback,
        strokes: StrokeIndex,
    ):
        self.dictionary = dictionary
        self.hanviet = hanviet
        self.chise = chise
        self.unihan = unihan
        self.strokes = strokes

    def _entry_for(self, char: str) -> DictionaryEntry | None:
        return (
            self.dictionary.primary(char)
            or self.unihan.entry(char)
        )

    def _reading_info(
        self,
        char: str,
        entry: DictionaryEntry,
    ) -> ReadingInfo:
        return ReadingInfo(
            pinyin=entry.pinyin,
            bopomofo=entry.bopomofo,
            han_viet=self.hanviet.lookup(char, entry.pinyin),
            definition_zh=clean_text(entry.definition_zh),
            word_id=entry.word_id,
            source=entry.source,
        )

    def _component_info(
        self,
        token: str,
        radical: str,
    ) -> ComponentInfo:
        char = token if is_cjk_char(token) else ""
        role = "component"

        if char and char == radical:
            role = "radical"

        if not char:
            return ComponentInfo(
                token=token,
                char="",
                role=role,
            )

        entry = self._entry_for(char)
        if entry is None:
            return ComponentInfo(
                token=token,
                char=char,
                role=role,
                han_viet=self.hanviet.lookup(char, ""),
            )

        return ComponentInfo(
            token=token,
            char=char,
            role=role,
            pinyin=entry.pinyin,
            bopomofo=entry.bopomofo,
            han_viet=self.hanviet.lookup(char, entry.pinyin),
            definition_zh=clean_text(entry.definition_zh),
        )

    def build(self, char: str) -> CharacterGrounding:
        stroke = self.strokes.get(char)
        if stroke is None:
            raise RuntimeError(
                f"No offline MOE stroke XML for {char}"
            )

        readings = self.dictionary.readings(char)

        if readings:
            primary_entry = readings[0]
            other_entries = readings[1:]
        else:
            fallback = self.unihan.entry(char)
            if fallback is None:
                primary_entry = DictionaryEntry(
                    title=char,
                    source="unknown",
                )
            else:
                primary_entry = fallback
            other_entries = []

        radical = primary_entry.radical
        ids = self.chise.ids(char)
        raw_components = self.chise.direct_components(char)

        components = [
            self._component_info(token, radical)
            for token in raw_components
        ]

        # Always include the official radical even when CHISE's top-level
        # decomposition does not expose it directly.
        if radical and not any(c.char == radical for c in components):
            components.insert(
                0,
                self._component_info(radical, radical),
            )

        primary = self._reading_info(char, primary_entry)
        others = [
            self._reading_info(char, e)
            for e in other_entries
        ]

        return CharacterGrounding(
            char=char,
            ucs=stroke.ucs,
            ids=ids,
            primary=primary,
            other_readings=others,
            radical=radical,
            stroke_count=primary_entry.stroke_count,
            components=components,
            stroke_xml_path=str(stroke.xml_path),
        )


# =============================================================================
# LLM client
# =============================================================================

class LLMClient:
    """File-prompt text enricher over a config-driven text backend.

    Renders config/prompts/text_system.txt + text_user.txt (never
    hardcoded prompt strings) and completes them via generation.text
    mode local|endpoint (see text_providers.py). Endpoint mode preserves
    the previous llama.cpp OpenAI-compatible behavior; local mode uses a
    lazily-loaded Hugging Face model (first actual use downloads missing
    files into the HF cache, later runs reuse them).
    """

    def __init__(self, text_cfg: dict):
        cfg = dict(text_cfg or {})
        prompt = cfg.get("prompt", {})
        if not isinstance(prompt, dict):
            prompt = {}
        self.system_file = str(
            prompt.get("system_file", DEFAULT_SYSTEM_FILE)
            or DEFAULT_SYSTEM_FILE)
        self.user_file = str(
            prompt.get("user_file", DEFAULT_USER_FILE)
            or DEFAULT_USER_FILE)
        try:
            validate_text_prompts(self.system_file, self.user_file)
        except PromptError as exc:
            raise ValueError(f"bad text prompt config: {exc}") from exc
        try:
            self.provider = build_text_provider(cfg)
        except TextProviderError as exc:
            raise ValueError(f"bad generation.text config: {exc}") from exc
        self.text_cfg = cfg
        # Back-compat aliases (previous constructor took base_url/model).
        desc = self.provider.describe()
        self.model = desc.get("model", "") or desc.get("model_id", "")
        self.base_url = ""
        if desc.get("mode") == "endpoint":
            endpoint = cfg.get("endpoint", {})
            if isinstance(endpoint, dict):
                self.base_url = str(endpoint.get("base_url", "") or "")

    @property
    def chat_url(self) -> str:
        chat = getattr(self.provider, "chat_url", "")
        if chat:
            return chat
        return ""

    @property
    def provider_desc(self) -> dict:
        return self.provider.describe()

    def enrich(
        self,
        grounding: CleanGrounding,
        facts: dict,
        profile: dict,
        *,
        refresh: bool,
        max_sayings: int = 2,
    ) -> dict:
        profile_code = str(profile.get("language_code", "vi") or "vi")
        language_name = str(profile.get("language_name", profile_code)
                             or profile_code)
        profile_instruction = str(
            (profile.get("llm") or {}).get("instruction", "") or "").strip()
        if not profile_instruction:
            profile_instruction = (
                "Write all learner-facing explanations in the learner's "
                "language."
            )
        cache_path = (
            LLM_CACHE_DIR
            / profile_code
            / f"U+{grounding.ucs}.json"
        )

        facts_canonical = json.dumps(
            facts, ensure_ascii=False, sort_keys=True)
        facts_json = json.dumps(
            facts, ensure_ascii=False, indent=2)
        system_prompt = render_system(
            self.system_file, language_name=language_name,
            profile_instruction=profile_instruction,
            max_sayings=max_sayings, target_char=grounding.char)
        user_prompt = render_user(self.user_file, facts_json=facts_json)
        system_hash = prompt_file_hash(self.system_file)
        user_hash = prompt_file_hash(self.user_file)
        desc = self.provider.describe()

        input_hash = text_cache_identity(
            provider_desc=desc, system_hash=system_hash,
            user_hash=user_hash, profile_code=profile_code,
            profile_instruction=profile_instruction,
            facts_canonical=facts_canonical)

        if cache_path.exists() and not refresh:
            cached = read_json(cache_path)
            if (
                cached.get("prompt_version") == PROMPT_VERSION
                and cached.get("input_hash") == input_hash
            ):
                print(
                    f"[llm cache] {grounding.char} U+{grounding.ucs}"
                )
                return cached["result"]

        print(f"[llm] {grounding.char} -> mode={desc.get('mode')} "
              f"provider={desc.get('provider')}")

        try:
            content = self.provider.complete(
                system_prompt=system_prompt, user_prompt=user_prompt)
        except (TextProviderError, PromptError) as exc:
            raise RuntimeError(f"text generation failed: {exc}") from exc

        result = json_from_model_text(content)
        result = self._validate_result(
            result, char=grounding.char, max_sayings=max_sayings)

        write_json(
            cache_path,
            {
                "prompt_version": PROMPT_VERSION,
                "input_hash": input_hash,
                # Provenance for cache debugging (no secrets, no URLs,
                # no absolute paths — model alias / repo id only).
                "text_mode": desc.get("mode", ""),
                "text_provider": desc.get("provider", ""),
                "text_model": desc.get("model", "")
                or desc.get("model_id", ""),
                "text_model_revision": desc.get("revision", ""),
                "prompt_system_hash": system_hash,
                "prompt_user_hash": user_hash,
                "target_language": profile_code,
                "facts": facts,
                "result": result,
            },
        )

        return result

    @staticmethod
    def _validate_result(result: dict, *, char: str = "",
                         max_sayings: int = 2) -> dict:
        # Canonical language-neutral keys. Legacy suffixed keys
        # (meaning_vi, structure_explanation_vi, component_meanings_vi,
        # examples[].vi, han_viet_generated) are accepted and migrated so
        # old cached/edge-case outputs do not break generation.
        meaning = nfc(result.get("meaning") or result.get("meaning_vi"))
        structure = nfc(result.get("structure_explanation")
                        or result.get("structure_explanation_vi"))
        han_viet_suggestion = nfc(result.get("han_viet_suggestion")
                                  or result.get("han_viet_generated"))

        components_generated = result.get("components_generated", [])
        if not isinstance(components_generated, list):
            components_generated = []
        components_generated = [
            nfc(c) for c in components_generated[:8] if nfc(c)
        ]

        component_meanings = result.get(
            "component_meanings",
            result.get("component_meanings_vi", {}),
        )
        if not isinstance(component_meanings, dict):
            component_meanings = {}

        examples = result.get("examples", [])
        if not isinstance(examples, list):
            examples = []

        valid_examples = []
        for item in examples[:3]:
            if not isinstance(item, dict):
                continue

            zh = nfc(item.get("zh"))
            py = nfc(item.get("pinyin"))
            tr = nfc(item.get("translation") or item.get("vi"))

            if zh and py and tr:
                valid_examples.append({
                    "zh": zh,
                    "pinyin": py,
                    "translation": tr,
                })

        return {
            "meaning": meaning,
            "han_viet_suggestion": han_viet_suggestion,
            "structure_explanation": structure,
            "components_generated": components_generated,
            "component_meanings": {
                nfc(k): nfc(v)
                for k, v in component_meanings.items()
                if nfc(k)
            },
            "examples": valid_examples,
            "notable_sayings": _validate_sayings(
                result.get("notable_sayings", []),
                char=char, max_items=max_sayings),
        }


SAYING_TYPES = {
    "quotation", "proverb", "idiom", "maxim", "classical", "other",
}


def _validate_sayings(raw: Any, *, char: str = "",
                      max_items: int = 2) -> list[dict]:
    """Validate LLM-generated notable sayings (enrichment, never facts).

    Keeps at most max_items complete items. An item survives only with
    non-empty traditional/pinyin/translation; the traditional text must
    contain the target character (quality over coverage: unrelated quotes
    are dropped, never backfilled). Unknown type labels become "other";
    source may be "" (unknown attribution). han_viet stays nested under
    language_specific so non-Vietnamese profiles never carry it.
    """
    if not isinstance(raw, list) or max_items <= 0:
        return []
    valid = []
    for item in raw[:max_items]:
        if not isinstance(item, dict):
            continue
        traditional = nfc(item.get("traditional"))
        py = nfc(item.get("pinyin"))
        tr = nfc(item.get("translation"))
        if not (traditional and py and tr):
            continue
        if char and char not in traditional:
            continue
        kind = nfc(item.get("type")).lower()
        if kind not in SAYING_TYPES:
            kind = "other"
        lang = item.get("language_specific", {})
        han_viet = nfc((lang or {}).get("han_viet")) \
            if isinstance(lang, dict) else ""
        valid.append({
            "traditional": traditional,
            "pinyin": py,
            "translation": tr,
            "type": kind,
            "source": nfc(item.get("source")),
            "language_specific": {"han_viet": han_viet},
        })
    return valid


# =============================================================================
# Offline MOE stroke player
# =============================================================================

PLAYER_JS = r"""
<script src="_moe_jquery-3.7.1.min.js"></script>
<script src="_moe_jquery.svg.js"></script>
<script src="_moe_stroke.js"></script>

<script>
(function () {
    function decodeBase64Utf8(b64) {
        const binary = atob(b64);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i++) {
            bytes[i] = binary.charCodeAt(i);
        }
        return new TextDecoder("utf-8").decode(bytes);
    }

    function buildStrokeDescriptors(xmlDocument) {
        const builder = new StrokeDescriptorBuilder();

        const commandTable = {
            "MoveTo": builder.buildMoveTo,
            "LineTo": builder.buildLineTo,
            "QuadTo": builder.buildQuadBezierTo,
            "CubicTo": builder.buildCubicBezierTo,
        };

        const descriptors = [];

        $(xmlDocument).find("Stroke").each(function () {
            builder.reset();

            const outline = $(this).children("Outline");
            const track = $(this).children("Track");

            outline.children().each(function () {
                const tag = $(this).prop("tagName");
                const fn = commandTable[tag];
                if (fn) {
                    fn.call(builder, $(this));
                }
            });

            track.children().each(function () {
                builder.buildTrack($(this));
            });

            descriptors.push(
                builder.getStrokeDescriptor()
            );
        });

        return descriptors;
    }

    function initMoeStroke() {
        if (
            typeof window.jQuery === "undefined" ||
            typeof window.StrokeDescriptorBuilder === "undefined" ||
            typeof window.ExercisePanel === "undefined" ||
            typeof window.Demo === "undefined"
        ) {
            setTimeout(initMoeStroke, 50);
            return;
        }

        const stage = document.getElementById("moe-svg");
        if (!stage) return;

        stage.innerHTML = "";

        const size = Math.min(
            360,
            Math.max(260, window.innerWidth - 80)
        );

        stage.style.width = size + "px";
        stage.style.height = size + "px";
        stage.setAttribute(
            "viewBox",
            `0 0 ${size} ${size}`
        );

        const b64 = document
            .getElementById("moe-stroke-data")
            .textContent
            .trim();

        const xmlText = decodeBase64Utf8(b64);

        const xmlDoc = new DOMParser().parseFromString(
            xmlText,
            "text/xml"
        );

        if (
            xmlDoc.getElementsByTagName("parsererror").length
        ) {
            console.error("Could not parse MOE XML");
            return;
        }

        const descriptors =
            buildStrokeDescriptors(xmlDoc);

        for (let i = 0; i < descriptors.length; i++) {
            descriptors[i].transformScale(
                size / 2048,
                size / 1792
            );
        }

        window.moePanel =
            new ExercisePanel(
                "moe-svg",
                descriptors
            );

        window.moePanel.strokeWidth =
            size / 20;

        window.moeDemo =
            new Demo(
                window.moePanel,
                descriptors
            );

        window.moeDemo.setSpeed(40);

        setTimeout(function () {
            window.moeDemo.start();
        }, 250);
    }

    window.moeReplay = function () {
        if (window.moeDemo) {
            window.moeDemo.start();
        }
    };

    window.moePause = function () {
        if (window.moeDemo) {
            window.moeDemo.pause();
        }
    };

    window.moeResume = function () {
        if (!window.moeDemo) return;

        if (
            window.moeDemo.status === "STOP" ||
            window.moeDemo.status === "END"
        ) {
            window.moeDemo.start();
        } else {
            window.moeDemo.resume();
        }
    };

    window.moeNext = function () {
        if (!window.moeDemo) return;

        if (
            window.moeDemo.status === "STOP" ||
            window.moeDemo.status === "END"
        ) {
            window.moeDemo.start();
            return;
        }

        if (window.moeDemo.status === "PLAY") {
            window.moeDemo.pauseAfterDraw();
        } else if (!window.moeDemo.isDrawing) {
            window.moeDemo.next();
        }
    };

    window.moeGrid = function () {
        if (!window.moeDemo) return;
        window.moeDemo.setDatumLine(
            !window.moeDemo.isDatumLine()
        );
    };

    // Step back exactly one stroke. The MOE engine has no native
    // step-backward API, so: take manual control (same stop as moePause),
    // read the committed-stroke count (single source of truth, no parallel
    // state), reset, then statically re-commit the first target strokes
    // through the same nextStroke() playback uses. Never animates, so a
    // paused card stays paused; an in-flight animation is cancelled first.
    // At the beginning (order 0) the target clamps to 0: no-op, no error.
    window.moePrev = function () {
        if (!window.moeDemo || !window.moePanel) return;

        window.moeDemo.pause();
        window.moeDemo.isPauseAfterDraw = false;

        var order = window.moePanel.getCurrentStrokeIndex();
        var target = Math.max(0, order - 1);

        window.moePanel.reset();
        for (var i = 0; i < target; i++) {
            window.moePanel.nextStroke();
        }
    };

    setTimeout(initMoeStroke, 0);
})();
</script>
"""


# =============================================================================
# HTML builders
# -----------------------------------------------------------------------------
# CLEAN-v1 renders Unihan facts + explicitly AI-labeled LLM enrichment.
# Legacy builders (MOE-dictionary based) are kept below for reference.
# =============================================================================

def clean_readings_html(pinyin_alternates: list, labels: dict | None = None) -> str:
    """Alternate kMandarin readings (Unihan factual, non-primary).

    Zhuyin is derived with the SAME deterministic pinyin_to_zhuyin()
    converter as the main card (tones preserved; "" on failure renders
    as "—"). Hán-Việt stays "—": kVietnamese is per-character, not
    per-reading, and copying the character's single Hán-Việt value into
    every alternate row would misattribute it.
    """
    labels = labels or DEFAULT_PROFILE_LABELS
    if not pinyin_alternates:
        return ""
    rows = "".join(
        "<tr>"
        f"<td>{escape(p) or '—'}</td>"
        f"<td>{escape(pinyin_to_zhuyin(p)) or '—'}</td><td>—</td>"
        f"<td>{escape(labels.get('alternate_reading_note', ''))}</td>"
        "</tr>"
        for p in pinyin_alternates
    )
    return (
        f'<div class="section-title">{escape(labels.get("other_readings_title", ""))}</div>'
        '<table class="info-table">'
        "<thead><tr>"
        f"<th>{escape(labels.get('table_pinyin', 'Pinyin'))}</th>"
        f"<th>{escape(labels.get('table_zhuyin', 'Zhuyin'))}</th>"
        f"<th>{escape(labels.get('table_hanviet', ''))}</th>"
        f"<th>{escape(labels.get('table_note', ''))}</th>"
        "</tr></thead>"
        "<tbody>"
        + rows
        + "</tbody></table>"
    )


def clean_components_html(enrichment: dict, labels: dict | None = None, *,
                          unihan: "UnihanIndex | None" = None,
                          show_han_viet: bool = False) -> str:
    """LLM-suggested learning components — AI enrichment, NOT IDS.

    Component SELECTION and glosses are AI-generated (see disclaimer).
    Pinyin / Hán-Việt are factual Unihan data resolved with the SAME
    UnihanIndex backing the main card (first kMandarin token /
    kVietnamese), never LLM output. "—" appears only when the token is
    not a single resolvable CJK character, Unihan has no value, or the
    profile opts out of Hán-Việt.
    """
    labels = labels or DEFAULT_PROFILE_LABELS
    components = enrichment.get("components_generated", [])
    meanings = enrichment.get("component_meanings",
                              enrichment.get("component_meanings_vi", {}))
    if not isinstance(meanings, dict):
        meanings = {}
    if not components:
        return f'<div class="muted">{escape(labels.get("no_components", ""))}.</div>'
    rows = []
    for token in components:
        pinyin = ""
        han_viet = ""
        token_n = nfc(token)
        if len(token_n) == 1 and is_cjk_char(token_n) and unihan is not None:
            facts = unihan.get(token_n)
            if facts is not None:
                pinyin = facts.pinyin
                if show_han_viet:
                    han_viet = facts.vietnamese
        rows.append(
            "<tr>"
            f'<td class="component-char">{escape(token)}</td>'
            f"<td>{escape(labels.get('component_role_ai', ''))}</td>"
            f"<td>{escape(pinyin) or '—'}</td>"
            f"<td>{escape(han_viet) or '—'}</td>"
            f"<td>{escape(meanings.get(token, '')) or '—'}</td>"
            "</tr>"
        )
    return (
        '<table class="info-table">'
        "<thead><tr>"
        f"<th>{escape(labels.get('table_character', ''))}</th>"
        f"<th>{escape(labels.get('table_role', ''))}</th><th>Pinyin</th>"
        f"<th>{escape(labels.get('table_hanviet', ''))}</th>"
        f"<th>{escape(labels.get('table_meaning', ''))}</th>"
        "</tr></thead>"
        "<tbody>"
        + "".join(rows)
        + "</tbody></table>"
        f'<div class="muted">{escape(labels.get("components_disclaimer", ""))}</div>'
    )


def clean_variants_text(grounding: CleanGrounding) -> str:
    """Text for the (backward-compat) IDS field: variant info, never CHISE."""
    parts = []
    if grounding.traditional_variants:
        parts.append("Trad: " + " ".join(grounding.traditional_variants))
    if grounding.simplified_variants:
        parts.append("Simp: " + " ".join(grounding.simplified_variants))
    if grounding.radical_char:
        parts.append(
            f"Radical {grounding.radical_number}: {grounding.radical_char}")
    return " · ".join(parts)


def readings_html(  # LEGACY: MOE-dictionary based, kept for reference
    primary: ReadingInfo,
    others: list[ReadingInfo],
) -> str:
    if not others:
        return ""

    rows = []

    for r in others:
        rows.append(
            "<tr>"
            f"<td>{escape(r.pinyin) or '—'}</td>"
            f"<td>{escape(r.bopomofo) or '—'}</td>"
            f"<td>{escape(r.han_viet) or '—'}</td>"
            f"<td>{escape(r.definition_zh) or '—'}</td>"
            "</tr>"
        )

    return (
        '<div class="section-title">Âm đọc khác</div>'
        '<table class="info-table">'
        "<thead><tr>"
        "<th>Pinyin</th><th>Zhuyin</th>"
        "<th>Hán-Việt</th><th>Nghĩa gốc MOE</th>"
        "</tr></thead>"
        "<tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def components_html(  # LEGACY: CHISE/MOE based, kept for reference
    grounding: CharacterGrounding,
    enrichment: dict,
) -> str:
    component_meanings = enrichment.get(
        "component_meanings_vi",
        {},
    )

    rows = []

    for c in grounding.components:
        meaning_vi = (
            component_meanings.get(c.char)
            or component_meanings.get(c.token)
            or ""
        )

        role_label = (
            "Bộ thủ"
            if c.role == "radical"
            else "Thành phần"
        )

        display = c.char or c.token

        rows.append(
            "<tr>"
            f'<td class="component-char">{escape(display)}</td>'
            f"<td>{escape(role_label)}</td>"
            f"<td>{escape(c.pinyin) or '—'}</td>"
            f"<td>{escape(c.han_viet) or '—'}</td>"
            f"<td>{escape(meaning_vi) or '—'}</td>"
            "</tr>"
        )

    if not rows:
        return '<div class="muted">Chưa có dữ liệu component.</div>'

    return (
        '<table class="info-table">'
        "<thead><tr>"
        "<th>Chữ</th><th>Vai trò</th><th>Pinyin</th>"
        "<th>Hán-Việt</th><th>Nghĩa</th>"
        "</tr></thead>"
        "<tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def examples_html(examples: list[dict], labels: dict | None = None) -> str:
    labels = labels or DEFAULT_PROFILE_LABELS
    blocks = []

    for example in examples:
        translation = example.get("translation", example.get("vi", ""))
        blocks.append(
            '<div class="example">'
            f'<div class="example-zh">{escape(example.get("zh"))}</div>'
            f'<div class="example-pinyin">{escape(example.get("pinyin"))}</div>'
            '<div class="example-translation">'
            f'{escape(translation)}</div>'
            "</div>"
        )

    if not blocks:
        return f'<div class="muted">{escape(labels.get("no_examples", ""))}</div>'

    return "".join(blocks)


def notable_sayings_html(items: list[dict], labels: dict | None = None,
                         show_han_viet: bool = False) -> str:
    """Render the optional notable-sayings block (reuses example styling).

    Returns "" when there are no items, so the template shows no heading
    or container at all. The section title lives here (not in the
    template) for exactly that reason. Per-item Hán–Việt renders only for
    opted-in profiles; source/type render as a muted line when known.
    """
    labels = labels or DEFAULT_PROFILE_LABELS
    if not items:
        return ""
    blocks = [
        f'<div class="section-title">'
        f'{escape(labels.get("notable_sayings", ""))}</div>'
    ]
    for item in items:
        if not isinstance(item, dict):
            continue
        traditional = nfc(item.get("traditional"))
        if not traditional:
            continue
        parts = [
            '<div class="example">',
            f'<div class="example-zh">{escape(traditional)}</div>',
            f'<div class="example-pinyin">'
            f'{escape(item.get("pinyin"))}</div>',
        ]
        han_viet = nfc((item.get("language_specific") or {}).get("han_viet")) \
            if isinstance(item.get("language_specific"), dict) else ""
        if show_han_viet and han_viet:
            parts.append(
                f'<div class="example-pinyin">{escape(han_viet)}</div>')
        parts.append(
            '<div class="example-translation">'
            f'{escape(item.get("translation"))}</div>')
        kind = nfc(item.get("type"))
        source = nfc(item.get("source"))
        credit = " · ".join(p for p in (kind, source) if p)
        if credit:
            parts.append(f'<div class="muted">{escape(credit)}</div>')
        parts.append("</div>")
        blocks.append("".join(parts))
    if len(blocks) == 1:
        return ""
    return "".join(blocks)


# =============================================================================
# Build normalized card records (clean-v1, provenance-explicit §10)
# =============================================================================

def _enrichment_entry(value: Any, llm_model: str | None,
                      source: str = "llm") -> dict:
    return {"value": value, "source": source, "model": llm_model,
            "prompt_version": PROMPT_VERSION}


def build_card_record(
    grounding: CleanGrounding,
    enrichment: dict,
    audio: dict,
    stroke_sha256: str,
    llm_model: str | None,
    generation_mode: str,
    target_language: str = "vi",
) -> dict:
    # Optional language-specific datum: kVietnamese is a Unihan fact, but
    # it is only surfaced to learners whose profile opts in (show_han_viet).
    # The LLM suggestion is a learner aid, never an official reading.
    hanviet_value = grounding.vietnamese_raw
    hanviet_source = "unicode_unihan.kVietnamese"
    if not hanviet_value and enrichment.get("han_viet_suggestion"):
        hanviet_value = enrichment["han_viet_suggestion"]
        hanviet_source = "llm_generated"
    # media.audio arrives fully built (see build_cns_audio_block): the
    # fixed CNS11643 contract is one human recording per character+reading,
    # with honest source/checksum provenance. No synthesis concepts remain.
    if not isinstance(audio, dict):
        audio = {}
    return {
        "schema": CLEAN_SCHEMA_VERSION,
        "char": grounding.char,
        "ucs": grounding.ucs,
        "target_language": target_language,
        "facts": {
            "pinyin": {"value": grounding.pinyin,
                       "source": "unicode_unihan.kMandarin"},
            "pinyin_alternates": {
                "value": grounding.pinyin_alternates,
                "source": "unicode_unihan.kMandarin"},
            "zhuyin": {"value": grounding.zhuyin,
                       "source": "deterministic:pinyin_to_zhuyin"},
            "definition_en": {"value": grounding.definition_en,
                              "source": "unicode_unihan.kDefinition"},
            "radical": {"value": grounding.radical_char,
                        "source": "unicode_unihan.kRSUnicode+CJKRadicals"},
            "radical_number": {"value": grounding.radical_number,
                               "source": "unicode_unihan.kRSUnicode"},
            "stroke_count": {"value": grounding.stroke_count,
                             "source": "unicode_unihan.kTotalStrokes"},
            "variants": {
                "value": {
                    "traditional": grounding.traditional_variants,
                    "simplified": grounding.simplified_variants,
                },
                "source": "unicode_unihan.kTraditionalVariant/kSimplifiedVariant",
            },
            "han_viet": {"value": hanviet_value,
                         "source": hanviet_source},
            "unicode_version": {"value": UNICODE_VERSION,
                                "source": "download.py:UNICODE_VERSION"},
        },
        "enrichment": {
            "target_language": target_language,
            "meaning": _enrichment_entry(
                enrichment.get("meaning", ""), llm_model),
            "han_viet_suggestion": _enrichment_entry(
                enrichment.get("han_viet_suggestion", ""), llm_model),
            "structure_explanation": _enrichment_entry(
                enrichment.get("structure_explanation", ""), llm_model),
            "components_generated": _enrichment_entry(
                enrichment.get("components_generated", []), llm_model,
                "llm (NOT authoritative IDS)"),
            "component_meanings": _enrichment_entry(
                enrichment.get("component_meanings", {}), llm_model),
            "examples": _enrichment_entry(
                enrichment.get("examples", []), llm_model),
            "notable_sayings": _enrichment_entry(
                enrichment.get("notable_sayings", []), llm_model,
                "llm (learner-facing enrichment, never reference facts)"),
        },
        "media": {
            "stroke": {
                "source": "moe_taiwan",
                "original_xml": str(Path(
                    grounding.stroke_xml_path).name),
                "sha256": stroke_sha256,
                "transform": "none: original bytes base64-encoded",
            },
            "audio": audio,
        },
        "generation_mode": generation_mode,
    }


def build_cns_audio_block(*, status: str, filename: str = "",
                          sha256: str = "", pronunciation: dict | None = None,
                          expected_pinyin: str = "",
                          expected_zhuyin: str = "",
                          matched_zhuyin: str = "",
                          record_id: str = "", voice: str = "",
                          relpath: str = "",
                          reading_ambiguity: bool = False,
                          detail: dict | None = None,
                          ) -> dict:
    """Build the normalized media.audio block for CNS11643 audio.

    status is "matched" (exact CNS recording for character + expected
    Zhuyin, human-recorded, reading verified by construction) or
    "unavailable" (no CNS index, or no exact reading match — card gets
    no audio, never synthesized, never substituted).
    """
    pronunciation = dict(pronunciation or {})
    return {
        "source": "cns11643" if status == "matched" else "unavailable",
        "status": status,
        "human_recorded": status == "matched",
        "file": filename,
        "filename": filename,
        "sha256": sha256,
        "pronunciation": {
            "character": pronunciation.get("character", ""),
            "pinyin": pronunciation.get("pinyin", ""),
            "zhuyin": pronunciation.get("zhuyin", ""),
        },
        "expected_pinyin": expected_pinyin,
        "expected_zhuyin": expected_zhuyin,
        "matched_zhuyin": matched_zhuyin,
        "record_id": record_id,
        "voice": voice,
        "relpath": relpath,
        "reading_ambiguity": reading_ambiguity,
        "reading_verified": status == "matched",
        "transform": "none: original CNS bytes copied without transcoding",
        "detail": detail or {},
    }


# =============================================================================
# Anki model / package (template externalized under config/anki/)
# -----------------------------------------------------------------------------
# Visual design is FROZEN: config/anki/{front,back}.html + style.css hold the
# exact card layout previously embedded here. Python only substitutes the
# %%LABEL_*%% tokens from the active profile and appends PLAYER_JS.
# Field ORDER in model.json is frozen for Anki backward compatibility
# (notes match by GUID, values map positionally). ANKI_MODEL_ID/ANKI_DECK_ID
# and guid_for("MOE-TRADITIONAL-V1", char) are unchanged. New fields may
# only be APPENDED (existing notes then see them empty); never reorder,
# remove, or insert mid-list.
# =============================================================================

# Fallback copies used only if config/anki/* is missing; the files on disk
# are canonical. Kept identical to the frozen design (HanViet block without
# Anki conditional in fallback; file version uses {{#HanViet}}).
_FALLBACK_ANKI_FIELDS = [
    "Hanzi", "Zhuyin", "Pinyin", "HanViet", "Meaning", "DefinitionEN",
    "Variants", "StructureExplanation", "ComponentsHTML",
    "OtherReadingsHTML", "StrokeDataB64", "Audio", "ExamplesHTML",
    "SourceNote", "NotableSayingsHTML",
]

_ANKI_LABEL_TOKENS = (
    "LABEL_HANVIET", "LABEL_UNIHAN", "LABEL_VARIANTS", "LABEL_STRUCTURE",
    "LABEL_STROKE_ORDER", "LABEL_EXAMPLES",
)

_LABEL_KEY_BY_TOKEN = {
    "LABEL_HANVIET": "hanviet",
    "LABEL_UNIHAN": "unihan",
    "LABEL_VARIANTS": "variants",
    "LABEL_STRUCTURE": "structure",
    "LABEL_STROKE_ORDER": "stroke_order",
    "LABEL_EXAMPLES": "examples",
}


def _read_anki_file(name: str) -> str | None:
    path = CONFIG_ANKI_DIR / name
    try:
        if path.exists():
            return path.read_text(encoding="utf-8")
    except OSError:
        pass
    return None


def load_anki_template(profile: dict) -> dict:
    """Load external Anki template + substitute profile labels.

    Returns {fields, front, back, css}. No templating engine: plain
    %%TOKEN%% replacement so advanced users can edit HTML/CSS directly.
    """
    labels = profile_labels(profile)
    fields = list(_FALLBACK_ANKI_FIELDS)
    raw_model = _read_anki_file("model.json")
    if raw_model:
        try:
            parsed = json.loads(raw_model)
            if isinstance(parsed.get("fields"), list) and parsed["fields"]:
                fields = [str(f) for f in parsed["fields"]]
        except ValueError:
            print("[anki] WARNING: config/anki/model.json invalid; using fallback fields")

    front = _read_anki_file("front.html")
    back = _read_anki_file("back.html")
    css = _read_anki_file("style.css")
    if front is None or back is None or css is None:
        print("[anki] WARNING: config/anki/* missing; using embedded fallback template")
        return {
            "fields": fields,
            "front": '<div class="front-hanzi">{{Hanzi}}</div>\n',
            "back": "{{FrontSide}}\n{{Meaning}}\n",
            "css": "",
        }
    for token in _ANKI_LABEL_TOKENS:
        key = _LABEL_KEY_BY_TOKEN[token]
        back = back.replace(f"%%{token}%%",
                            str(labels.get(key, DEFAULT_PROFILE_LABELS.get(key, ""))))
    back = back.replace("%%MOE_PLAYER_JS%%", PLAYER_JS)
    return {"fields": fields, "front": front, "back": back, "css": css}


def build_anki_model(profile: dict | None = None) -> genanki.Model:
    if profile is None:
        profile = load_profile("vi")
    tpl = load_anki_template(profile)
    raw_model = _read_anki_file("model.json")
    model_name = "Taiwan Traditional Chinese - Offline MOE"
    template_name = "Recognition"
    if raw_model:
        try:
            parsed = json.loads(raw_model)
            model_name = str(parsed.get("name", model_name))
            template_name = str(parsed.get("template_name", template_name))
        except ValueError:
            pass
    return genanki.Model(
        ANKI_MODEL_ID,
        model_name,
        fields=[{"name": name} for name in tpl["fields"]],
        templates=[{"name": template_name,
                    "qfmt": tpl["front"], "afmt": tpl["back"]}],
        css=tpl["css"],
    )


def prepare_support_media() -> list[Path]:
    mapping = {
        "jquery-3.7.1.min.js":
            "_moe_jquery-3.7.1.min.js",
        "jquery.svg.js":
            "_moe_jquery.svg.js",
        "stroke.js":
            "_moe_stroke.js",
    }

    result = []

    for src_name, dst_name in mapping.items():
        src = MOE_STROKE_PLAYER / src_name
        if not src.exists():
            raise FileNotFoundError(
                f"Missing stroke player asset {src}. "
                "Run download.py first."
            )

        dst = BUILD_MEDIA / dst_name
        shutil.copy2(src, dst)
        result.append(dst)

    return result


def copy_cns_audio_for_anki(
    source: Path,
    ucs: str,
    zhuyin_norm: str,
) -> Path:
    """Copy an ORIGINAL CNS recording into build media, unchanged.

    Deterministic name: cns_U<UCS>_<zhuyin8>.<ext> (no transcoding).
    """
    ext = source.suffix.lower()
    dest = (
        BUILD_MEDIA
        / f"cns_U{ucs}_{zhuyin_slug(zhuyin_norm)}{ext}"
    )
    shutil.copy2(source, dest)
    return dest


def load_cns_resolver() -> CharacterAudioResolver | None:
    """Create the CNS resolver once per run (index loads once).

    Returns None with a clear warning when the CNS dataset/index is
    absent or malformed: cards then generate without audio.
    """
    try:
        resolver = CharacterAudioResolver.load(CNS_INDEX_PATH)
    except CnsError as exc:
        print(f"[cns] WARNING: {exc}")
        return None
    print(f"[cns] index: {resolver.record_count} recordings "
          f"({CNS_INDEX_PATH})")
    return resolver


# =============================================================================
# Published reference-v1 dataset (§14)
# -----------------------------------------------------------------------------
# Generated immutable artifact for Android consumption. Android must never
# need to understand raw MOE/Unicode layouts. The LLM endpoint (private
# local config) is NEVER written into the published manifest.
# =============================================================================

def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _read_sources_manifest() -> dict:
    for candidate in (DATA / "manifests" / "sources.json",):
        if candidate.exists():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}
    return {}


def publish_reference(
    chars: list[str],
    generation_mode: str,
    llm_model: str | None,
    audio_missing: list,
    audio_info: dict,
    generation_errors: list,
    publish_dir: Path,
    target_language: str = "vi",
    text_info: dict | None = None,
) -> Path:
    """Copy normalized records + original media into reference-v1 + manifest.

    Layout separates language-neutral facts from profile enrichment:
      characters/U+*.json      full normalized record (facts + media +
                               this run's enrichment, with target_language)
      enrichment/<lang>/U+*.json  sidecar with ONLY the learner-language
                               enrichment (references, never duplicates,
                               stroke/audio bytes)
    Consumers that only need facts can ignore enrichment/ entirely.
    """
    char_dir = publish_dir / "characters"
    stroke_dir = publish_dir / "strokes"
    audio_dir = publish_dir / "audio"
    index_dir = publish_dir / "indexes"
    license_dir = publish_dir / "licenses"
    enrichment_dir = publish_dir / "enrichment" / target_language
    for d in (char_dir, stroke_dir, audio_dir, index_dir, license_dir,
              enrichment_dir):
        d.mkdir(parents=True, exist_ok=True)

    sources_manifest = _read_sources_manifest()
    published_files: dict[str, str] = {}
    stroke_index_entries = []
    missing_strokes = []
    missing_audio = [m.get("char") for m in audio_missing]

    for char in chars:
        ucs = f"{ord(char):04X}"
        src_record = CARD_CACHE_DIR / f"U+{ucs}.json"
        if not src_record.exists():
            missing_strokes.append(char)
            continue
        record = json.loads(src_record.read_text(encoding="utf-8"))
        dst_record = char_dir / f"U+{ucs}.json"
        dst_record.write_text(
            json.dumps(record, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        published_files[f"characters/U+{ucs}.json"] = sha256_file(dst_record)

        # Enrichment sidecar: learner-language data only, no media
        # duplication. Points back at the character record + media files.
        sidecar = {
            "schema": CLEAN_SCHEMA_VERSION,
            "char": record.get("char", char),
            "ucs": record.get("ucs", ucs),
            "target_language": record.get("target_language", target_language),
            "enrichment": record.get("enrichment", {}),
            "character_ref": f"characters/U+{ucs}.json",
        }
        dst_sidecar = enrichment_dir / f"U+{ucs}.json"
        dst_sidecar.write_text(
            json.dumps(sidecar, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        published_files[f"enrichment/{target_language}/U+{ucs}.json"] = (
            sha256_file(dst_sidecar))

        # Original stroke bytes (no geometry rewrite).
        xml_src = MOE_STROKE_XML.glob(f"U+{ucs}__ID*.xml")
        xml_hits = sorted(xml_src)
        if xml_hits:
            dst_xml = stroke_dir / xml_hits[0].name
            if not dst_xml.exists():
                shutil.copy2(xml_hits[0], dst_xml)
            published_files[f"strokes/{dst_xml.name}"] = sha256_file(dst_xml)
            stroke_index_entries.append({
                "char": char, "ucs": ucs,
                "file": f"strokes/{dst_xml.name}",
            })
        else:
            missing_strokes.append(char)

    # Audio: copy the single CNS recording referenced by each card
    # record. Published with its hash so the manifest covers every file.
    for char in chars:
        ucs = f"{ord(char):04X}"
        rec_path = char_dir / f"U+{ucs}.json"
        if not rec_path.exists():
            continue
        rec = json.loads(rec_path.read_text(encoding="utf-8"))
        audio_block = (rec.get("media") or {}).get("audio") or {}
        fnames = []
        for key in ("file", "filename"):
            if audio_block.get(key) and audio_block[key] not in fnames:
                fnames.append(audio_block[key])
        for cand in audio_block.get("candidates", []) or []:
            if isinstance(cand, dict) and cand.get("file"):
                if cand["file"] not in fnames:
                    fnames.append(cand["file"])
        for fname in fnames:
            if not fname:
                continue
            src_media = BUILD_MEDIA / fname
            if not src_media.exists():
                continue
            dst = audio_dir / fname
            if not dst.exists():
                shutil.copy2(src_media, dst)
            published_files[f"audio/{fname}"] = sha256_file(dst)

    complete = generation_mode == "official" and not generation_errors
    text_info = dict(text_info or {})
    manifest = {
        "schema_version": CLEAN_SCHEMA_VERSION,
        "dataset_version": REFERENCE_DATASET_VERSION,
        "generation_mode": generation_mode,
        "complete": complete,
        "character_count": len(chars),
        "target_language": target_language,
        "unicode_version": UNICODE_VERSION,
        "sources": sources_manifest.get("sources", []),
        "source_licenses": sources_manifest.get("licenses", {}),
        "prompt_version": PROMPT_VERSION,
        # Model ALIAS only — never the private endpoint URL.
        "llm_model": llm_model,
        # Text provenance WITHOUT machine-specific config: mode +
        # provider + model alias/repo id (+ revision) only. Endpoint
        # URLs, API keys, and absolute model-cache paths MUST NOT
        # appear here (the validator checks).
        "text": {
            "mode": text_info.get("mode", ""),
            "provider": text_info.get("provider", ""),
            "model": text_info.get("model", ""),
            "model_revision": text_info.get("model_revision", ""),
        },
        # CNS11643 fixed-source provenance: source + voice preference
        # + index size. No models, no endpoints, no machine-specific
        # paths (the validator checks).
        "audio": {
            "source": audio_info.get("source", CNS_SOURCE),
            "preferred_voice": audio_info.get("preferred_voice", "auto"),
            "index_records": audio_info.get("index_records", 0),
        },
        "files": published_files,
        "counts": {
            "requested": len(chars),
            "errors": len(generation_errors),
            "audio_missing": len(audio_missing),
            "audio_matched": len(chars) - len(audio_missing) - len(
                generation_errors),
            "missing_strokes": missing_strokes,
            "missing_audio_chars": missing_audio,
        },
        "notes": (
            "TEST output is incomplete by design; "
            "OFFICIAL output is the full supported set."
            if generation_mode == "test" else
            "Official complete snapshot."
        ),
    }
    manifest_path = publish_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (index_dir / "index.json").write_text(
        json.dumps({"strokes": stroke_index_entries},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (license_dir / "ATTRIBUTION.txt").write_text(
        "Stroke-order geometry: "
        "中華民國教育部 (MOE Taiwan).\n"
        "Factual text (readings/radicals/strokes/variants/English "
        "definitions): Unicode Unihan "
        f"{UNICODE_VERSION} (https://www.unicode.org/license.html).\n"
        "Pronunciation audio: human-recorded CNS11643 / 全字庫 "
        "(see manifest 'audio' section); NOT synthesized, NOT MOE "
        "recordings.\n"
        "Learner-language glosses, structure notes, examples, notable "
        "sayings: AI-generated, for learning reference only.\n",
        encoding="utf-8",
    )
    return manifest_path


# =============================================================================
# Selection
# =============================================================================

def parse_chars_arg(value: str) -> list[str]:
    value = value.replace(",", "").replace(" ", "")
    seen = set()
    result = []

    for ch in value:
        if ch not in seen:
            seen.add(ch)
            result.append(ch)

    return result


def select_characters(
    stroke_index: StrokeIndex,
    args,
    config: dict,
) -> tuple[list[str], str]:
    """Select characters to generate.

    Precedence (highest first):
      1. ``--chars``: raw character string, debug override (deduped,
         order-preserving; unsupported chars fail at grounding time).
      2. ``--test-chars``: comma/space-separated list, debug override
         (entries validated, duplicates rejected, any length >= 1).
      3. TEST mode (default): exactly ``config["test_characters"]`` —
         never ``limit=N`` over dataset order. Entries are validated
         (single character, no duplicates), config order is preserved,
         and every entry must exist in the supported stroke dataset.
      4. OFFICIAL mode: the full supported set (``--limit`` still works
         as a debug cap).

    OFFICIAL mode returns the full supported set. The legacy singular
    ``test_character`` config key and ``--test-char`` flag no longer exist:
    passing ``--test-char`` fails loudly (unrecognized argument), and a
    stale ``test_character`` key in config raises a migration error.
    """
    if args.chars:
        return parse_chars_arg(args.chars), "debug --chars override"
    if args.test_chars:
        return (
            _validate_test_list(
                re.split(r"[,\s]+", args.test_chars.strip()),
                source="--test-chars",
                exact_count=None,
            ),
            "debug --test-chars override",
        )
    mode = (args.mode or config.get("mode", "test")).lower()
    if mode == "official":
        chars = list(stroke_index.ordered_chars)
        if args.limit > 0:
            chars = chars[:args.limit]
        return chars, "official"
    if "test_character" in config:
        raise ValueError(
            "Config key 'test_character' was replaced by 'test_characters' "
            "(a list). Rename it, e.g. \"test_characters\": [\"思\", ...].")
    chars = _validate_test_list(
        config.get("test_characters", DEFAULT_TEST_CHARACTERS),
        source="config test_characters",
        exact_count=len(DEFAULT_TEST_CHARACTERS),
    )
    missing = [c for c in chars if stroke_index.get(c) is None]
    if missing:
        raise ValueError(
            "TEST characters not in the supported stroke dataset: "
            + ", ".join(f"{c} (U+{ord(c):04X})" for c in missing)
            + ". Edit config test_characters.")
    return chars, "test"


def _validate_test_list(raw: Any, *, source: str,
                        exact_count: int | None) -> list[str]:
    """Validate an explicit test-character list, preserving order."""
    if not isinstance(raw, list) or not raw:
        raise ValueError(
            f"{source} must be a non-empty list of characters")
    chars = [nfc(c) for c in raw if nfc(c)]
    if len(chars) != len(raw):
        raise ValueError(f"{source} contains empty entries")
    if any(len(c) != 1 for c in chars):
        bad = [c for c in chars if len(c) != 1]
        raise ValueError(
            f"{source} must hold exactly one character per entry, "
            f"got {bad!r}")
    seen: set[str] = set()
    dupes = sorted({c for c in chars if c in seen or seen.add(c)})
    if dupes:
        raise ValueError(f"{source} contains duplicates: {dupes!r}")
    if exact_count is not None and len(chars) != exact_count:
        raise ValueError(
            f"{source} must hold exactly {exact_count} characters, "
            f"got {len(chars)}")
    return chars


# =============================================================================
# Main
# =============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate offline Traditional-Chinese Anki cards."
    )

    parser.add_argument(
        "--chars",
        default="",
        help="Characters to generate, e.g. 思志學心",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Explicitly generate every downloaded stroke character (this is also the default when --chars is omitted).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Debug only: limit selected characters. 0 = no limit.",
    )
    parser.add_argument(
        "--mode",
        default="",
        choices=["", "test", "official"],
        help="Generation mode. Default: read from config/config.local.json (test).",
    )
    parser.add_argument(
        "--test-chars",
        default="",
        help="Debug override for the test set, e.g. --test-chars 思,八,乾. "
        "Validated like config test_characters but any length >= 1. "
        "Normal TEST runs should use config instead.",
    )

    parser.add_argument(
        "--llm-base-url",
        default="",
        help="llama.cpp OpenAI-compatible base URL. Default: config/config.local.json.",
    )
    parser.add_argument(
        "--llm-model",
        default="",
        help="Model/alias exposed by llama-server. Default: config/config.local.json.",
    )
    parser.add_argument(
        "--refresh-ai",
        action="store_true",
        help="Ignore LLM cache and regenerate enrichment.",
    )
    parser.add_argument(
        "--no-ai",
        action="store_true",
        help="Debug only: do not call LLM; enrichment fields stay minimal.",
    )
    parser.add_argument(
        "--profile",
        default="",
        help="Learner-language profile (config/profiles/<code>.json). "
        "Default: read from config (vi).",
    )

    parser.add_argument(
        "--deck-name",
        default="",
        help="Deck name. Default: config/config.local.json.",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Output .apkg path. Default: config/config.local.json.",
    )

    parser.add_argument(
        "--publish-dir",
        default=str(PUBLISHED_ROOT),
        help="Published reference-v1 root. Default: data/published/reference-v1.",
    )

    args = parser.parse_args()
    config = load_local_config()
    profile = load_profile(args.profile or config.get("profile", "vi"))
    target_language = str(profile.get("language_code", "vi") or "vi")
    labels = profile_labels(profile)
    show_han_viet = bool((profile.get("features") or {}).get(
        "show_han_viet", target_language == "vi"))

    deck_name = args.deck_name or config["generation"]["deck_name"]
    output_default = config["generation"]["output"]

    # ---- Text backend (generation.text: mode local|endpoint) ----
    text_cfg = config["generation"].get("text", {})
    if not isinstance(text_cfg, dict):
        text_cfg = {}
    # Debug overrides keep working: they target the endpoint block and
    # force endpoint mode for this run (previous CLI behavior).
    if args.llm_base_url or args.llm_model:
        text_cfg = {**text_cfg, "mode": "endpoint"}
        endpoint_override = dict(text_cfg.get("endpoint", {}))
        if args.llm_base_url:
            endpoint_override["base_url"] = args.llm_base_url
        if args.llm_model:
            endpoint_override["model"] = args.llm_model
        text_cfg["endpoint"] = endpoint_override
    # Legacy top-level llm.* flags stay readable for old scripts.
    if not args.llm_base_url and config.get("llm", {}).get("base_url"):
        pass  # already migrated into text.endpoint by the loader
    try:
        validate_text_config(text_cfg)
    except ValueError as exc:
        raise SystemExit(f"Bad text config: {exc}")

    # ---- Audio: fixed CNS11643 source (no model/backend selection) ----
    audio_cfg = config.get("audio", {})
    if not isinstance(audio_cfg, dict):
        audio_cfg = {}
    try:
        validate_audio_config(audio_cfg)
    except ValueError as exc:
        raise SystemExit(f"Bad audio config: {exc}")
    preferred_voice = str(
        audio_cfg.get("preferred_voice", "auto") or "auto").strip().lower()

    ensure_dirs()

    # Windows consoles (cp1252) crash printing CJK progress lines.
    # Prefer UTF-8; fall back to replacement instead of tracebacks.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    print("==============================================")
    print("Load clean-v1 sources (Unihan + MOE stroke; CNS11643 audio)")
    print("==============================================")

    unihan = UnihanIndex(UNIHAN_DIR, UNICODE_DIR / "CJKRadicals.txt")
    strokes = StrokeIndex()

    grounder = CleanGrounder(unihan=unihan, strokes=strokes)

    llm = None
    llm_model: str | None = None
    text_info: dict = {"mode": "", "provider": "", "model": ""}
    if not args.no_ai:
        try:
            llm = LLMClient(text_cfg)
        except ValueError as exc:
            raise SystemExit(f"Bad text config: {exc}")
        desc = llm.provider_desc
        llm_model = desc.get("model", "") or desc.get("model_id", "")
        text_info = {
            "mode": desc.get("mode", ""),
            "provider": desc.get("provider", ""),
            # Model alias (endpoint) or repo id (local) only — never
            # URLs, keys, or absolute cache paths.
            "model": desc.get("model", "") or desc.get("model_id", ""),
            "model_revision": desc.get("revision", ""),
        }
        print(f"[llm] mode={desc.get('mode')} "
              f"provider={desc.get('provider')} model={llm_model}")

    # Character pronunciation audio: fixed CNS11643 human-recorded
    # source, resolved by (character, expected Zhuyin). The resolver is
    # created ONCE and its index loads ONCE for the whole run. Missing
    # dataset or missing reading => cards without audio (never
    # synthesized, never substituted).
    cns_resolver = load_cns_resolver()
    audio_info = {
        "source": CNS_SOURCE,
        "preferred_voice": preferred_voice,
        "index_records": (cns_resolver.record_count
                          if cns_resolver else 0),
    }

    # Notable-sayings cap comes from the active profile
    # (features.notable_sayings_max_items, default 2, clamped 0..3).
    try:
        sayings_max = int((profile.get("features") or {}).get(
            "notable_sayings_max_items", 2))
    except (TypeError, ValueError):
        sayings_max = 2
    sayings_max = max(0, min(3, sayings_max))

    chars, generation_mode = select_characters(strokes, args, config)
    refresh_ai = args.refresh_ai or bool(
        config["generation"].get("refresh_ai", False))

    print()
    print("==============================================")
    print(f"Generation mode    : {generation_mode}")
    print(f"Target language    : {target_language} "
          f"({profile.get('language_name', '')})")
    print(f"Prompt version     : {PROMPT_VERSION}")
    print(f"Selected characters: {len(chars)}")
    if generation_mode == "test":
        print("(test output is marked incomplete; see reference manifest)")
    print("==============================================")
    print("".join(chars[:100]))
    if len(chars) > 100:
        print("...")

    model = build_anki_model(profile)
    deck = genanki.Deck(
        ANKI_DECK_ID,
        deck_name,
    )

    media_files: list[Path] = prepare_support_media()

    audio_missing = []
    audio_found = 0
    generation_errors = []
    generated = 0

    for idx, char in enumerate(chars, start=1):
        print()
        print(
            f"[{idx}/{len(chars)}] {char} "
            f"U+{ord(char):04X}"
        )

        try:
            grounding = grounder.build(char)
            facts = grounder.facts_block(grounding)

            if args.no_ai:
                enrichment = {
                    "meaning": "",
                    "han_viet_suggestion": "",
                    "structure_explanation": "",
                    "components_generated": [],
                    "component_meanings": {},
                    "examples": [],
                    "notable_sayings": [],
                }
            else:
                assert llm is not None
                enrichment = llm.enrich(
                    grounding,
                    facts,
                    profile,
                    refresh=refresh_ai,
                    max_sayings=sayings_max,
                )

            # -------------------------------------------------------------
            # Audio: fixed CNS11643 human recording for this card's Hanzi
            # and expected Zhuyin. Exact match or nothing — never
            # synthesized, never another reading. MOE recordings are
            # never used either.
            # -------------------------------------------------------------

            audio_field = ""
            cns_file = ""
            cns_sha = ""
            cns_matched_zhuyin = ""
            cns_record_id = ""
            cns_voice = ""
            cns_relpath = ""
            audio_status = "unavailable"
            audio_detail: dict = {"reason": "cns_index_unavailable"}

            if cns_resolver is None:
                audio_missing.append({
                    "char": char,
                    "ucs": grounding.ucs,
                    "pinyin": grounding.pinyin,
                    "zhuyin": grounding.zhuyin,
                    "match": audio_detail,
                })
                print(f"[cns] no index: no audio for {char}")
            else:
                record, minfo = cns_resolver.resolve(
                    char, grounding.zhuyin, grounding.pinyin,
                    preferred_voice)
                if record is None:
                    audio_detail = {
                        "reason": minfo.get("reason", "unmatched"),
                        "expected_zhuyin": minfo.get("expected_zhuyin", ""),
                    }
                    audio_missing.append({
                        "char": char,
                        "ucs": grounding.ucs,
                        "pinyin": grounding.pinyin,
                        "zhuyin": grounding.zhuyin,
                        "match": audio_detail,
                    })
                    print(
                        f"[cns] WARNING: no exact CNS recording for "
                        f"{char} {grounding.zhuyin or '(no reading)'}: "
                        f"{audio_detail['reason']}"
                    )
                else:
                    src_audio = CNS11643_RAW / record.relpath
                    if not src_audio.exists():
                        audio_detail = {
                            "reason": "cns_file_missing",
                            "relpath": record.relpath,
                        }
                        audio_missing.append({
                            "char": char,
                            "ucs": grounding.ucs,
                            "pinyin": grounding.pinyin,
                            "zhuyin": grounding.zhuyin,
                            "match": audio_detail,
                        })
                        print(
                            f"[cns] WARNING: indexed CNS file absent for "
                            f"{char}: {record.relpath}"
                        )
                    else:
                        anki_audio = copy_cns_audio_for_anki(
                            src_audio,
                            grounding.ucs,
                            record.zhuyin_norm,
                        )
                        media_files.append(anki_audio)
                        audio_found += 1
                        cns_file = anki_audio.name
                        cns_sha = sha256_file(anki_audio)
                        cns_matched_zhuyin = record.zhuyin_norm
                        cns_record_id = record.record_id
                        cns_voice = record.voice
                        cns_relpath = record.relpath
                        audio_field = f"[sound:{anki_audio.name}]"
                        audio_status = "matched"
                        audio_detail = {
                            "reason": "matched",
                            "record_id": record.record_id,
                            "candidates": minfo.get("candidates", 1),
                        }
                        print(
                            f"[cns] {char} {record.zhuyin_norm} "
                            f"-> {anki_audio.name}"
                        )

            audio_block = build_cns_audio_block(
                status=audio_status,
                filename=cns_file,
                sha256=cns_sha,
                pronunciation={
                    "character": char,
                    "pinyin": grounding.pinyin,
                    "zhuyin": grounding.zhuyin,
                },
                expected_pinyin=grounding.pinyin,
                expected_zhuyin=normalize_zhuyin(grounding.zhuyin),
                matched_zhuyin=cns_matched_zhuyin,
                record_id=cns_record_id,
                voice=cns_voice,
                relpath=cns_relpath,
                reading_ambiguity=len(grounding.pinyin_alternates) > 0,
                detail=audio_detail,
            )

            # -------------------------------------------------------------
            # Stroke XML: original MOE bytes, base64-encoded directly.
            # No geometry parsing/regeneration (clean-v1 §6).
            # -------------------------------------------------------------

            xml_path = Path(grounding.stroke_xml_path)
            xml_bytes = xml_path.read_bytes()
            stroke_sha256 = hashlib.sha256(xml_bytes).hexdigest()
            stroke_b64 = base64.b64encode(xml_bytes).decode("ascii")

            # -------------------------------------------------------------
            # HanViet display field: optional profile feature
            # (features.show_han_viet). kVietnamese stays in the record
            # with provenance; non-opted-in profiles render it empty and
            # the template hides the block via {{#HanViet}}.
            # -------------------------------------------------------------

            hanviet_display = ""
            if show_han_viet:
                hanviet_display = (
                    grounding.vietnamese_raw
                    or enrichment.get("han_viet_suggestion", "")
                )

            # -------------------------------------------------------------
            # Render HTML fields (template structure unchanged)
            # -------------------------------------------------------------

            component_html = clean_components_html(
                enrichment, labels, unihan=unihan,
                show_han_viet=show_han_viet)

            other_readings = clean_readings_html(
                grounding.pinyin_alternates, labels)

            ex_html = examples_html(
                enrichment.get("examples", []), labels
            )

            sayings_html = notable_sayings_html(
                enrichment.get("notable_sayings", []), labels,
                show_han_viet=show_han_viet,
            )

            note = genanki.Note(
                model=model,
                guid=genanki.guid_for(
                    "MOE-TRADITIONAL-V1",
                    char,
                ),
                fields=[
                    char,
                    grounding.zhuyin,
                    grounding.pinyin,
                    hanviet_display,
                    enrichment.get("meaning", ""),
                    grounding.definition_en,
                    clean_variants_text(grounding),
                    enrichment.get(
                        "structure_explanation",
                        "",
                    ),
                    component_html,
                    other_readings,
                    stroke_b64,
                    audio_field,
                    ex_html,
                    build_attribution(profile),
                    sayings_html,
                ],
            )

            deck.add_note(note)

            card_record = build_card_record(
                grounding,
                enrichment,
                audio_block,
                stroke_sha256,
                None if args.no_ai else llm_model,
                generation_mode,
                target_language,
            )

            write_json(
                CARD_CACHE_DIR
                / f"U+{grounding.ucs}.json",
                card_record,
            )

            generated += 1

        except Exception as exc:
            print(
                f"[ERROR] {char}: "
                f"{type(exc).__name__}: {exc}"
            )
            generation_errors.append({
                "char": char,
                "ucs": f"{ord(char):04X}",
                "error": (
                    f"{type(exc).__name__}: {exc}"
                ),
            })

    # Remove duplicates while preserving order.
    unique_media = []
    seen_media = set()

    for path in media_files:
        resolved = str(path.resolve())
        if resolved in seen_media:
            continue
        seen_media.add(resolved)
        unique_media.append(path)

    output = Path(args.output or output_default)
    output.parent.mkdir(parents=True, exist_ok=True)

    package = genanki.Package(deck)
    package.media_files = [
        str(p)
        for p in unique_media
    ]

    package.write_to_file(str(output))

    publish_manifest = publish_reference(
        chars,
        generation_mode,
        None if args.no_ai else llm_model,
        audio_missing,
        audio_info,
        generation_errors,
        Path(args.publish_dir),
        target_language,
        None if args.no_ai else text_info,
    )

    write_json(
        BUILD_REPORTS / "audio_missing.json",
        audio_missing,
    )
    write_json(
        BUILD_REPORTS / "generation_errors.json",
        generation_errors,
    )
    write_json(
        BUILD_REPORTS / "build_summary.json",
        {
            "requested": len(chars),
            "generated": generated,
            "characters": chars,
            "errors": len(generation_errors),
            "audio_missing": len(audio_missing),
            "audio_matched": audio_found,
            "audio": audio_info,
            "text": text_info,
            "output": str(output),
            # Text model alias only; endpoint URLs stay in local config
            # and never reach reports. No machine-specific audio paths.
            "llm_model": (
                None if args.no_ai
                else llm_model
            ),
            "prompt_version": PROMPT_VERSION,
            "unicode_version": UNICODE_VERSION,
            "generation_mode": generation_mode,
            "target_language": target_language,
            "publish_manifest": str(publish_manifest),
        },
    )

    print()
    print("==============================================")
    print("DONE")
    print("==============================================")
    print(f"Mode             : {generation_mode}")
    print(f"Target language  : {target_language}")
    print(f"Requested      : {len(chars)}")
    print(f"Generated      : {generated}")
    print(f"Errors         : {len(generation_errors)}")
    print(f"Audio missing  : {len(audio_missing)}")
    print(f"Audio matched  : {audio_found} (CNS11643, no synthesis)")
    print(f"Output         : {output}")
    print(f"Publish manifest : {publish_manifest}")
    print(
        f"Reports        : {BUILD_REPORTS}"
    )
    print(
        f"Normalized JSON: {CARD_CACHE_DIR}"
    )
    print("==============================================")


if __name__ == "__main__":
    main()