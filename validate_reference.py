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
LEGACY_ENRICHMENT_KEYS = {
    "meaning_vi", "structure_explanation_vi", "component_meanings_vi",
    "han_viet_generated",
}
REQUIRED_TEMPLATE_FILES = {"front.html", "back.html", "style.css", "model.json"}
REQUIRED_TEMPLATE_PLACEHOLDERS = (
    "{{Hanzi}}", "{{Meaning}}", "{{StrokeDataB64}}", "{{Audio}}",
    "{{ExamplesHTML}}", "%%MOE_PLAYER_JS%%",
)


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
        if count != 1:
            warnings.append(
                f"test manifest character_count={count}, expected 1")
    if mode == "official" and complete is not True:
        warnings.append("official manifest has complete != true")

    # Private endpoint must never leak into published data.
    blob = json.dumps(manifest)
    if "100.123.148.6" in blob or "chat/completions" in blob:
        errors.append("manifest appears to contain a private LLM endpoint")
    if "base_url" in blob:
        errors.append("manifest appears to contain LLM connection config")

    target_language = manifest.get("target_language", "")
    if not target_language or not isinstance(target_language, str):
        errors.append("manifest lacks target_language (e.g. 'vi', 'en')")

    # External Anki template must exist with generic placeholders.
    for name in sorted(REQUIRED_TEMPLATE_FILES):
        if not (ANKI_DIR / name).exists():
            errors.append(f"missing Anki template file: config/anki/{name}")
    back_path = ANKI_DIR / "back.html"
    if back_path.exists():
        try:
            back = back_path.read_text(encoding="utf-8")
        except OSError:
            back = ""
            errors.append("cannot read config/anki/back.html")
        for token in REQUIRED_TEMPLATE_PLACEHOLDERS:
            if token not in back:
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

        media = rec.get("media", {})
        stroke = (media.get("stroke") or {})
        audio = (media.get("audio") or {})
        if stroke.get("transform", "").startswith("none") is False:
            warnings.append(f"{rec_path.name}: stroke transform flag changed")
        sfile = stroke.get("original_xml", "")
        if sfile and not (root / "strokes" / sfile).exists():
            errors.append(f"{rec_path.name}: broken stroke ref {sfile}")
        afile = audio.get("filename", "")
        if afile and not (root / "audio" / afile).exists():
            errors.append(f"{rec_path.name}: broken audio ref {afile}")

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


if __name__ == "__main__":
    sys.exit(main())
