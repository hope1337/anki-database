#!/usr/bin/env python3
"""
validate_reference.py — lightweight offline validator for the published
clean-v1 reference dataset (data/published/reference-v1/).

Checks WITHOUT network access:
  - manifest exists, schema version supported
  - expected files exist, SHA256 values match
  - no duplicate characters/UCS
  - stroke/audio references resolve
  - normalized records well-formed, required factual fields present
  - test vs official consistency

Usage:
    python validate_reference.py
    python validate_reference.py --dir data/published/reference-v1
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_DIR = ROOT / "data" / "published" / "reference-v1"
ANKI_DIR = ROOT / "config" / "anki"

SUPPORTED_SCHEMA_VERSIONS = {"clean-v1"}
REQUIRED_FACT_KEYS = {
    "pinyin", "zhuyin", "definition_en", "radical",
    "stroke_count", "han_viet",
}
# Canonical language-neutral enrichment keys. Legacy suffixed keys
# (meaning_vi, structure_explanation_vi, component_meanings_vi,
# han_viet_generated, examples[].vi) are rejected: see migration notes in
# DATA_SOURCES_AND_PIPELINE.md.
REQUIRED_ENRICHMENT_KEYS = {
    "meaning", "structure_explanation", "components_generated",
    "component_meanings", "examples",
}
# notable_sayings is optional as a KEY (pre-v2 records lack it): when
# present its items must satisfy the saying contract below; an empty
# list is always valid.
LEGACY_ENRICHMENT_KEYS = {
    "meaning_vi", "structure_explanation_vi", "component_meanings_vi",
    "han_viet_generated",
}
SAYING_TYPES = {
    "quotation", "proverb", "idiom", "maxim", "classical", "other",
}
AUDIO_STATUSES = {"matched", "unavailable"}
# Tolerated pre-CNS records (MOE/TTS era): validated leniently, warn once.
LEGACY_AUDIO_STATUSES = {"resolved", "multiple", "unresolved",
                         "generated"}
REQUIRED_TEMPLATE_FILES = {"front.html", "back.html", "style.css", "model.json"}
REQUIRED_TEMPLATE_PLACEHOLDERS = (
    "{{Hanzi}}", "{{Meaning}}", "{{StrokeDataB64}}", "{{Audio}}",
    "{{ExamplesHTML}}", "{{NotableSayingsHTML}}", "%%MOE_PLAYER_JS%%",
)


def profile_show_han_viet(language_code: str) -> bool:
    """Mirror generate.py: profile features.show_han_viet, default vi-only."""
    try:
        data = json.loads(
            (ROOT / "config" / "profiles" / f"{language_code}.json")
            .read_text(encoding="utf-8"))
        features = data.get("features", {})
        if isinstance(features, dict) and "show_han_viet" in features:
            return bool(features["show_han_viet"])
    except (OSError, ValueError):
        pass
    return language_code == "vi"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate a published reference-v1 dataset (offline).")
    parser.add_argument("--dir", default=str(DEFAULT_DIR))
    parser.add_argument(
        "--check-cns-inputs", action="store_true",
        help="Also validate local CNS11643 raw/processed inputs "
             "(data/raw/cns11643 + processed index). Absent inputs warn; "
             "inconsistencies fail.")
    parser.add_argument(
        "--check-audio-sha256", action="store_true",
        help="Verify SHA256 of every audio file referenced by the CNS "
             "processed index (slow on full datasets; existence is "
             "always checked).")
    args = parser.parse_args()

    root = Path(args.dir)
    errors: list[str] = []
    warnings: list[str] = []

    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        print(f"FAIL: manifest not found: {manifest_path}")
        return 2
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        print(f"FAIL: manifest is not valid JSON: {exc}")
        return 2

    schema = manifest.get("schema_version")
    if schema not in SUPPORTED_SCHEMA_VERSIONS:
        errors.append(
            f"unsupported schema_version {schema!r} "
            f"(supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)})")

    mode = manifest.get("generation_mode")
    complete = manifest.get("complete")
    count = manifest.get("character_count")
    if mode not in ("test", "official"):
        errors.append(f"generation_mode must be test|official, got {mode!r}")
    if mode == "test":
        if complete is not False:
            errors.append("test manifest must have complete=false")
        # TEST = explicitly configured subset (default 10 chars), not one char.
        if not isinstance(count, int) or count < 1:
            errors.append(
                f"test manifest needs a positive integer character_count, "
                f"got {count!r}")
    if mode == "official" and complete is not True:
        warnings.append("official manifest has complete != true")

    # Private endpoint must never leak into published data.
    blob = json.dumps(manifest)
    if "100.123.148.6" in blob or "chat/completions" in blob:
        errors.append("manifest appears to contain a private LLM endpoint")
    if "base_url" in blob:
        errors.append("manifest appears to contain LLM connection config")
    for leak in ("AZURE_SPEECH_KEY", "Ocp-Apim-Subscription-Key",
                 "api_key_env"):
        if leak in blob:
            errors.append(
                f"manifest appears to leak credentials/paths ({leak})")
    # No machine-specific paths (model caches, home dirs, TTS commands)
    # may appear: audio is a fixed CNS source, not a local model.
    for cache_marker in (".cache/huggingface", "HF_HOME",
                         "C:\\\\Users", "/home/", "/root/.cache"):
        if cache_marker in blob:
            errors.append(
                "manifest appears to contain a machine-specific path "
                f"({cache_marker})")
    for stale in ("primetts", "cosyvoice", "edge_tts", "azure_speech",
                  "\"onnx", "execution_provider"):
        if stale in blob:
            errors.append(
                f"manifest appears to reference removed audio-model "
                f"config ({stale})")

    target_language = manifest.get("target_language", "")
    if not target_language or not isinstance(target_language, str):
        errors.append("manifest lacks target_language (e.g. 'vi', 'en')")

    text = manifest.get("text", {})
    if text:
        if not isinstance(text, dict):
            errors.append("manifest 'text' is not an object")
        else:
            if text.get("mode", "") not in ("", "local", "endpoint"):
                errors.append(
                    f"manifest 'text.mode' unknown: {text.get('mode')!r}")
            if text.get("provider", "") not in (
                    "", "huggingface", "openai_compatible"):
                errors.append(
                    f"manifest 'text.provider' unknown: "
                    f"{text.get('provider')!r}")

    audio_meta = manifest.get("audio", {})
    if "tts" in manifest:
        errors.append(
            "manifest has removed 'tts' block (audio model selection "
            "deleted; regenerate with the CNS11643 audio contract)")
    if audio_meta:
        if not isinstance(audio_meta, dict):
            errors.append("manifest 'audio' is not an object")
        else:
            if audio_meta.get("source", "") not in ("", "cns11643"):
                errors.append(
                    f"manifest 'audio.source' unknown: "
                    f"{audio_meta.get('source')!r} (only fixed source "
                    f"'cns11643' is valid; no TTS fallback)")
            if str(audio_meta.get("preferred_voice", "auto")
                   or "auto").lower() not in ("auto", "male", "female"):
                errors.append(
                    "manifest 'audio.preferred_voice' must be one of "
                    "auto|male|female")

    want_han_viet = (profile_show_han_viet(target_language)
                     if target_language else False)

    # External Anki template must exist with generic placeholders.
    for name in sorted(REQUIRED_TEMPLATE_FILES):
        if not (ANKI_DIR / name).exists():
            errors.append(f"missing Anki template file: config/anki/{name}")
    back_path = ANKI_DIR / "back.html"
    front_path = ANKI_DIR / "front.html"
    if back_path.exists():
        try:
            back = back_path.read_text(encoding="utf-8")
        except OSError:
            back = ""
            errors.append("cannot read config/anki/back.html")
        try:
            front = front_path.read_text(encoding="utf-8")
        except OSError:
            front = ""
        for token in REQUIRED_TEMPLATE_PLACEHOLDERS:
            # {{Hanzi}} lives on the card front (via {{FrontSide}} on the
            # back); all other placeholders are back-side fields.
            scope = front if token == "{{Hanzi}}" else back
            if token not in scope:
                errors.append(
                    f"config/anki/back.html lacks placeholder {token}")
        for legacy in ("{{MeaningVN}}", "{{MOEDefinitionZH}}", "{{IDS}}"):
            if legacy in back:
                errors.append(
                    f"config/anki/back.html uses legacy field {legacy}")

    files = manifest.get("files", {})
    for rel, expected_sha in files.items():
        p = root / rel
        if not p.exists():
            errors.append(f"missing published file: {rel}")
            continue
        actual = sha256_file(p)
        if actual != expected_sha:
            errors.append(f"SHA256 mismatch: {rel}")

    char_dir = root / "characters"
    records = sorted(char_dir.glob("U+*.json")) if char_dir.exists() else []
    if mode == "official" and not records:
        errors.append("official dataset has no character records")
    if isinstance(count, int) and len(records) != count:
        warnings.append(
            f"character_count={count} but {len(records)} record files found")

    seen_chars: set[str] = set()
    seen_ucs: set[str] = set()
    for rec_path in records:
        try:
            rec = json.loads(rec_path.read_text(encoding="utf-8"))
        except ValueError:
            errors.append(f"malformed JSON: {rec_path.name}")
            continue
        if rec.get("schema") not in SUPPORTED_SCHEMA_VERSIONS:
            errors.append(f"{rec_path.name}: bad/missing schema marker")
        char = rec.get("char", "")
        ucs = rec.get("ucs", "")
        if char in seen_chars:
            errors.append(f"duplicate char: {char}")
        if ucs in seen_ucs:
            errors.append(f"duplicate ucs: {ucs}")
        seen_chars.add(char)
        seen_ucs.add(ucs)
        if ucs != f"{ord(char):04X}" if len(char) == 1 else True:
            errors.append(f"{rec_path.name}: ucs does not match char")

        facts = rec.get("facts", {})
        missing = REQUIRED_FACT_KEYS - set(facts)
        if missing:
            errors.append(
                f"{rec_path.name}: missing fact keys {sorted(missing)}")
        for key in REQUIRED_FACT_KEYS & set(facts):
            entry = facts[key]
            if not isinstance(entry, dict) or "value" not in entry \
                    or "source" not in entry:
                errors.append(
                    f"{rec_path.name}: fact {key!r} lacks value/source")
            elif "llm" in str(entry.get("source", "")).lower() and key in {
                    "pinyin", "zhuyin", "stroke_count"}:
                errors.append(
                    f"{rec_path.name}: authoritative fact {key!r} "
                    f"sourced from LLM")

        if not rec.get("target_language"):
            errors.append(f"{rec_path.name}: missing target_language")

        enrichment = rec.get("enrichment", {})
        if not isinstance(enrichment, dict):
            errors.append(f"{rec_path.name}: enrichment is not an object")
            enrichment = {}
        missing_en = REQUIRED_ENRICHMENT_KEYS - set(enrichment)
        if missing_en:
            errors.append(
                f"{rec_path.name}: missing enrichment keys "
                f"{sorted(missing_en)}")
        legacy_en = LEGACY_ENRICHMENT_KEYS & set(enrichment)
        if legacy_en:
            errors.append(
                f"{rec_path.name}: legacy language-suffixed enrichment keys "
                f"{sorted(legacy_en)} (migrate to neutral schema)")
        examples = enrichment.get("examples", {})
        ex_value = examples.get("value", []) if isinstance(
            examples, dict) else []
        if isinstance(ex_value, list):
            for item in ex_value:
                if not isinstance(item, dict):
                    continue
                if "vi" in item and "translation" not in item:
                    errors.append(
                        f"{rec_path.name}: example uses legacy 'vi' key "
                        f"(use 'translation')")
                    break

        # Notable sayings: optional enrichment, empty is valid. Items
        # need Traditional text + pinyin + profile translation; Han-Viet
        # only where the profile contract requires it.
        sayings_entry = enrichment.get("notable_sayings", {})
        say_value = sayings_entry.get("value", []) if isinstance(
            sayings_entry, dict) else []
        if not isinstance(say_value, list):
            errors.append(f"{rec_path.name}: notable_sayings value "
                          f"is not a list")
            say_value = []
        for item in say_value:
            if not isinstance(item, dict):
                errors.append(
                    f"{rec_path.name}: notable saying is not an object")
                continue
            if not item.get("traditional"):
                errors.append(
                    f"{rec_path.name}: saying lacks Traditional text")
            if not item.get("pinyin"):
                errors.append(f"{rec_path.name}: saying lacks pinyin")
            if not item.get("translation"):
                errors.append(f"{rec_path.name}: saying lacks translation")
            if item.get("type") not in SAYING_TYPES:
                errors.append(
                    f"{rec_path.name}: saying has bad type "
                    f"{item.get('type')!r}")
            lang = item.get("language_specific", {})
            if not isinstance(lang, dict):
                errors.append(
                    f"{rec_path.name}: saying language_specific not an object")
                lang = {}
            if want_han_viet and not lang.get("han_viet"):
                errors.append(
                    f"{rec_path.name}: saying lacks han_viet "
                    f"(required by profile)")
            if (isinstance(item.get("traditional"), str) and char
                    and char not in item["traditional"]):
                warnings.append(
                    f"{rec_path.name}: saying does not contain {char}")

        # Enrichment sidecar must exist and match the record language.
        if target_language:
            sidecar = (root / "enrichment" / target_language
                       / rec_path.name)
            if not sidecar.exists():
                errors.append(
                    f"{rec_path.name}: missing enrichment sidecar "
                    f"enrichment/{target_language}/{rec_path.name}")
            else:
                try:
                    side = json.loads(
                        sidecar.read_text(encoding="utf-8"))
                except ValueError:
                    errors.append(
                        f"{rec_path.name}: malformed enrichment sidecar")
                    side = {}
                if (side.get("target_language")
                        != rec.get("target_language")):
                    errors.append(
                        f"{rec_path.name}: sidecar target_language mismatch")
                side_blob = json.dumps(side)
                for leak in ("AZURE_SPEECH_KEY", "Ocp-Apim-Subscription-Key"):
                    if leak in side_blob:
                        errors.append(
                            f"{rec_path.name}: sidecar leaks credentials "
                            f"({leak})")

        media = rec.get("media", {})
        stroke = (media.get("stroke") or {})
        audio = (media.get("audio") or {})
        rec_blob = json.dumps(rec)
        for leak in ("AZURE_SPEECH_KEY", "Ocp-Apim-Subscription-Key",
                     "api_key_env"):
            if leak in rec_blob:
                errors.append(
                    f"{rec_path.name}: leaks credentials ({leak})")
        for cache_marker in (".cache/huggingface", "HF_HOME"):
            if cache_marker in rec_blob:
                errors.append(
                    f"{rec_path.name}: leaks machine-specific model "
                    f"cache path ({cache_marker})")
        if stroke.get("transform", "").startswith("none") is False:
            warnings.append(f"{rec_path.name}: stroke transform flag changed")
        sfile = stroke.get("original_xml", "")
        if sfile and not (root / "strokes" / sfile).exists():
            errors.append(f"{rec_path.name}: broken stroke ref {sfile}")
        afile = audio.get("file", "") or audio.get("filename", "")
        status = audio.get("status", "")
        if status in LEGACY_AUDIO_STATUSES:
            warnings.append(
                f"{rec_path.name}: pre-TTS audio schema (status={status})")
            if afile and not (root / "audio" / afile).exists():
                errors.append(f"{rec_path.name}: broken audio ref {afile}")
            for cand in audio.get("candidates", []) or []:
                cfile = (cand or {}).get("file", "")
                if cfile and not (root / "audio" / cfile).exists():
                    errors.append(
                        f"{rec_path.name}: broken audio candidate {cfile}")
        elif status not in AUDIO_STATUSES:
            errors.append(
                f"{rec_path.name}: bad audio status {status!r} "
                f"(expected matched|unavailable)")
        elif status == "matched":
            if audio.get("source", "") not in ("", "cns11643"):
                errors.append(
                    f"{rec_path.name}: matched audio has non-CNS source "
                    f"{audio.get('source')!r} (no TTS fallback allowed)")
            if not audio.get("human_recorded"):
                errors.append(
                    f"{rec_path.name}: matched audio not marked "
                    f"human_recorded")
            if not audio.get("reading_verified"):
                errors.append(
                    f"{rec_path.name}: matched audio not marked "
                    f"reading_verified")
            if not audio.get("matched_zhuyin"):
                errors.append(
                    f"{rec_path.name}: matched audio without "
                    f"matched_zhuyin")
            if not afile:
                errors.append(
                    f"{rec_path.name}: matched audio without file")
            elif not (root / "audio" / afile).exists():
                errors.append(f"{rec_path.name}: broken audio ref {afile}")
            else:
                if (root / "audio" / afile).stat().st_size == 0:
                    errors.append(
                        f"{rec_path.name}: matched audio is empty {afile}")
                declared = audio.get("sha256", "")
                if declared and sha256_file(root / "audio" / afile) != declared:
                    errors.append(
                        f"{rec_path.name}: audio SHA256 mismatch {afile}")
            for provider_key in ("provider", "runtime", "model_id",
                                 "repo_id", "variant", "onnx",
                                 "execution_provider", "reading_control",
                                 "reading_control_mechanism", "cache_key"):
                if audio.get(provider_key):
                    errors.append(
                        f"{rec_path.name}: matched audio carries removed "
                        f"TTS field {provider_key!r}")
        else:  # unavailable
            if afile:
                warnings.append(
                    f"{rec_path.name}: unavailable audio still names "
                    f"{afile}")

    if args.check_cns_inputs:
        check_cns_inputs(root, args.check_audio_sha256, errors, warnings)

    print(f"Checked {len(records)} character record(s) in {root}")
    for w in warnings:
        print(f"WARN: {w}")
    if errors:
        print(f"FAIL: {len(errors)} error(s)")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("OK: reference dataset valid")
    return 0


CNS_OFFICIAL_BASES = ("https://www.cns11643.gov.tw/",
                      "https://data.gov.tw/")


def check_cns_inputs(root: Path, check_sha: bool, errors: list,
                     warnings: list) -> None:
    """Validate local CNS11643 raw/processed inputs (offline, no network).

    Absent inputs warn (published validation must work standalone);
    present-but-inconsistent inputs fail: index schema/version,
    non-empty normalized Zhuyin, no duplicate (character, zhuyin,
    voice) identity, every indexed path exists (SHA256 when requested),
    and download-manifest provenance URLs under official bases.
    """
    raw = ROOT / "data" / "raw" / "cns11643"
    index_path = ROOT / "data" / "processed" / "cns11643" / "cns_index.json"
    for rel in ("OpenDataFilesList.csv", "release.txt",
                "archives/Voice.zip", "archives/Properties.zip",
                "archives/MapingTables.zip",
                "properties/CNS_phonetic.txt",
                "audio/male", "audio/female"):
        if not (raw / rel).exists():
            warnings.append(f"cns input absent: data/raw/cns11643/{rel} "
                            f"(run `python download.py`)")
    if not index_path.exists():
        warnings.append("cns processed index absent: "
                        "data/processed/cns11643/cns_index.json")
        return
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        errors.append(f"cns index is not valid JSON: {exc}")
        return
    if index.get("index_version") != "cns-index-v2":
        errors.append("cns index has unsupported index_version "
                      f"{index.get('index_version')!r} (want 'cns-index-v2')")
        return
    records = index.get("records", [])
    if not isinstance(records, list):
        errors.append("cns index records malformed")
        return
    seen: set[tuple] = set()
    checked = 0
    for row in records:
        if not isinstance(row, dict):
            errors.append("cns index has non-object record")
            continue
        char = row.get("character", "")
        znorm = row.get("zhuyin_norm", "")
        voice = str(row.get("voice", "") or "")
        if len(char) != 1 or not znorm:
            errors.append("cns index record lacks character/zhuyin_norm: "
                          f"{str(row)[:80]}")
            continue
        identity = (char, znorm, voice)
        if identity in seen:
            errors.append(
                f"cns index duplicate identity: {char} {znorm} "
                f"voice={voice!r}")
        seen.add(identity)
        relpath = row.get("relpath", "")
        audio_path = raw / relpath if relpath else None
        if audio_path is None or not audio_path.exists():
            errors.append(f"cns index references missing audio: "
                          f"{char} {znorm} -> {relpath!r}")
            continue
        checked += 1
        if check_sha:
            declared = row.get("sha256", "")
            if declared and sha256_file(audio_path) != declared:
                errors.append(
                    f"cns indexed audio SHA256 mismatch: {relpath}")
    print(f"CNS index: {len(records)} record(s), {checked} audio "
          f"file(s) present")
    manifest_path = ROOT / "data" / "manifests" / "sources.json"
    if manifest_path.exists():
        try:
            sources = json.loads(
                manifest_path.read_text(encoding="utf-8"))
        except ValueError:
            sources = {}
        cns = (sources.get("cns11643", {}) or {})
        for res in (cns.get("resources", []) or []):
            url = str((res or {}).get("url", "") or "")
            if url and not url.startswith(CNS_OFFICIAL_BASES):
                errors.append(
                    f"cns manifest resource has non-official URL: {url}")
    else:
        warnings.append("download manifest absent: "
                        "data/manifests/sources.json")


if __name__ == "__main__":
    sys.exit(main())
