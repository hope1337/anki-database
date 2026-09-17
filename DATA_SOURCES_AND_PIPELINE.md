# Data Sources & Pipeline (clean-v1, language-profile refactor)

> Transparency contract for the Traditional-Chinese Anki data project.
> Rule: anything not verifiable from this repository is marked
> **UNKNOWN / NEEDS VERIFICATION** — never guessed.
>
> The pipeline is language-neutral at its core. Vietnamese is one
> learner profile among many (see §12 TARGET LANGUAGE ARCHITECTURE),
> not an architectural assumption.

## 1. Architecture overview

```
MOE Taiwan ──► stroke-order XML + JS player ──► data/raw/moe_stroke/
             └─► single-char pronunciation audio ──► data/raw/moe_dictionary/audio_char/

Unicode 17.0.0 ──► Unihan.zip + CJKRadicals.txt ──► data/raw/unicode/
      (version-PINNED, never "latest")

Target-language profile (config/profiles/<code>.json: vi, en, ...)
    ├── learner language + labels + LLM instruction
    └── optional features (e.g. show_han_viet for Vietnamese)

Deterministic local code (generate.py)
    ├── UnihanIndex: kMandarin/kDefinition/kRSUnicode/kTotalStrokes/variants/kVietnamese
    ├── pinyin_to_zhuyin(): deterministic Pinyin → Zhuyin (never LLM)
    ├── CleanAudioResolver: char-based match, no dictionary IDs
    ├── CleanGrounder: Unihan facts + MOE stroke index
    ├── external Anki template (config/anki/) + profile labels
    └── manifests / SHA256

LLM enrichment (local llama.cpp server, config only)
    ├── learner-language meaning, structure note, component glosses, examples
    └── NEVER authoritative facts; NEVER receives MOE dict/CHISE/HanViet data

Outputs
    ├── data/processed/cards/U+*.json   (normalized records w/ provenance)
    ├── build/*.apkg                     (Anki deck, frozen template)
    └── data/published/reference-v1/     (immutable Android-ready snapshot)
```

**Core rule:** MOE = stroke + audio ONLY. Unicode = factual text.
LLM = learner-facing enrichment. Target language = CONFIGURABLE.

## 2. Sources

### 2.1 MOE Taiwan stroke-order data (CLEAN-v1, used)

- Owner: 中華民國教育部 (MOE Taiwan).
- Discovery page (scraped live by `download.py:discover_moe_downloads`):
  `https://stroke-order.learningweb.moe.edu.tw/resource.jsp?ID=1`
- Per-character pages: `https://stroke-order.learningweb.moe.edu.tw/dictFrame.jsp?ID=<moe_id>&la=0`
  (embedded `var xml={<id>:"<?xml ... </Word>"}` extracted by `extract_embedded_xml`).
- Embed catalog CSV: filename discovered live (any `*.csv` link on the page).
- 6063 PNG fallback ZIP: discovered live (`*6063png*.zip`) — optional (`--skip-png`).
- JS player (verbatim copies for offline Anki rendering):
  `https://stroke-order.learningweb.moe.edu.tw/js/jquery-3.7.1.min.js`,
  `.../js/jquery.svg.js`, `.../js/stroke.js` → renamed `_moe_*` in package media.
- License (recorded in `data/manifests/sources.json`): **CC BY-NC-ND 3.0 TW**,
  attribution 中華民國教育部, source page
  `https://stroke-order.learningweb.moe.edu.tw/page.jsp?ID=52`.
  Full legal text: **NEEDS VERIFICATION** (not vendored; keep `licenses/` + published
  `licenses/ATTRIBUTION.txt` in sync).
- Fields/media used: per-character vector XML (original bytes preserved).
- Redistributed: yes — original XML bytes embedded as Base64 in the `.apkg`
  `StrokeDataB64` field + copied into `published/reference-v1/strokes/`.
- Transforms performed: **none** on geometry. Base64-encode original bytes;
  presentation (replay/pause/resume/next/grid) is player logic only.
- Transforms explicitly NOT performed: smoothing, reshaping, reordering,
  regenerating paths.

### 2.2 MOE Taiwan single-character audio (CLEAN-v1, used)

- Owner: 中華民國教育部 (MOE Taiwan).
- Package discovered live on the dictionary download page
  (`https://language.moe.gov.tw/001/Upload/Files/site_content/M0001/respub/dict_concised_download.html`,
  pattern `dict_concised_music_word_*.zip`, ~1.5GB). Opt-out: `--skip-audio`.
- License/attribution: exact audio-specific terms **UNKNOWN / NEEDS
  VERIFICATION** — the manifest records the dictionary page as
  CC BY-ND 3.0 TW, but whether that covers the audio bundle must be confirmed
  from the download page before public redistribution.
- Mapping (`CleanAudioResolver`, no dictionary IDs): exact filename-stem ==
  character (score 95) → character substring in path (80); ties or score < 80
  → unresolved + reason in `build/reports/audio_missing.json`. Package-level
  metadata files (csv/json/txt/xml) are scanned and noted but no fixed layout
  is assumed.
- Transforms: **none** — original bytes copied (`moe_audio_U<UCS>_1.<ext>`),
  hash-verified (`sha256` in normalized record). No trim/remix/normalize/
  resample/transcode.
- **Known limitation:** if MOE renames audio files to opaque IDs with no
  in-package mapping, clean matching degrades to char-in-path only and more
  clips stay unresolved. This is reported, never guessed.

### 2.3 Unicode Unihan + CJKRadicals (CLEAN-v1, primary text source)

- Owner: Unicode Consortium.
- Pinned version: **17.0.0** (`download.py:UNICODE_VERSION`,
  `generate.py:UNICODE_VERSION` — must match).
- Official URLs:
  `https://www.unicode.org/Public/17.0.0/ucd/Unihan.zip`,
  `https://www.unicode.org/Public/17.0.0/ucd/CJKRadicals.txt`.
- Previous code used unversioned `.../Public/UCD/latest/ucd/Unihan.zip`;
  clean-v1 pins 17.0.0 deliberately. Bump only with manifest + doc update.
- Archive/filename: `Unihan.zip` → extracted to `data/raw/unicode/unihan/`
  (`Unihan_Readings.txt`, `Unihan_RadicalStrokeCounts.txt`,
  `Unihan_Variants.txt`, …); `CJKRadicals.txt` at `data/raw/unicode/`.
- SHA256 strategy: computed at download time into `data/manifests/sources.json`
  (`build_file_record`); no hardcoded hash in the repo (nothing downloaded yet
  in this checkout to hash).
- License: full Unicode license text **NEEDS VERIFICATION** — see
  `https://www.unicode.org/license.html`; record exact notice in
  `licenses/` + published `licenses/` before distribution.
- Properties consumed (`UnihanIndex.CONSUMED_PROPS`):
  `kMandarin` (first token = selected pinyin; rest = alternates),
  `kDefinition` (English semantic grounding — **English, not Chinese**),
  `kRSUnicode` (`<radicalNo>.<strokes>` → radical number; display char via
  `CJKRadicals.txt`), `kTotalStrokes` (first value selected, all recorded),
  `kTraditionalVariant` / `kSimplifiedVariant` (code points → chars),
  `kVietnamese` (see §2.3.1).
- `kDefinition` semantics: a short English gloss for disambiguation, not a
  dictionary definition. The LLM uses it only as semantic grounding for the
  learner-language gloss.

#### 2.3.1 kVietnamese vs Hán-Việt

- `kVietnamese` is a Unihan fact with provenance
  `unicode_unihan.kVietnamese`. It is an **optional language-specific**
  datum: only profiles with `features.show_han_viet=true` (Vietnamese)
  surface it on cards; other profiles render the HanViet block empty and
  the template hides it via `{{#HanViet}}`.
- Whether `kVietnamese` values equal traditional Sino-Vietnamese (Hán-Việt)
  readings in all cases is **NEEDS VERIFICATION** against
  [UAX #38 / Unihan documentation](https://www.unicode.org/reports/tr38/).
- If `kVietnamese` is absent, the record falls back to the LLM-generated
  `han_viet_suggestion` value with provenance `llm_generated` — never labeled
  as an official reading.

### 2.4 LLM enrichment (local only, never distributed)

- Endpoint/model live in `config/config.local.json` (gitignored), e.g.
  base `http://100.123.148.6:8080/v1`, model alias `qwen3`, timeout 180s,
  `trust_env=false`. `config/config.example.json` holds safe example values.
  The private endpoint/model URL MUST NOT appear in published manifests
  (`validate_reference.py` checks for leaks).
- Protocol: OpenAI-compatible `POST {base}/v1/chat/completions`
  (llama.cpp-style; `response_format: json_object` with graceful retry).
- Prompt version: `anki-zh-generic-v1` (was `anki-zh-tw-clean-v2`, legacy
  was `anki-zh-tw-v1`). The prompt is target-language agnostic:
  `"You are creating Traditional Chinese learner content for a learner
  whose destination language is {language_name}."` plus the active
  profile's `llm.instruction`. CLI `--profile` / config `"profile"`
  selects it.
- Cache key = prompt_version + model alias + target language/profile +
  profile instruction + normalized facts hash
  (`data/processed/llm/<lang>/U+*.json`); `--refresh-ai` bypasses.
  Old `data/processed/llm/U+*.json` files (pre-profile) are left on disk
  for rollback but never read.
- LLM input (§8): `{character, pinyin, zhuyin, unicode_definition_en,
  radical, stroke_count, variants}` — no MOE definitions, no CHISE, no
  HanViet dataset content.
- LLM output (language-neutral keys): `{meaning, han_viet_suggestion,
  structure_explanation, components_generated[], component_meanings{},
  examples[{zh, pinyin, translation}]}`.
  Components are **AI learning aids, NOT authoritative IDS** (never called
  CHISE). Examples use Taiwan Traditional Chinese (A2–B1).
- Explicit statement: AI-generated data is not authoritative source data;
  the prompt prefers "insufficient data" over invented etymology.

## 3. Unicode pipeline details

- Files parsed: all `Unihan_*.txt` under `data/raw/unicode/unihan/`, first
  occurrence per (char, property) wins.
- Pinyin selection: first whitespace-separated `kMandarin` token; remainder
  kept as `pinyin_alternates` and shown under the profile's
  `other_readings_title` label ("Âm đọc khác (Unihan)" for vi).
- Radicals: `kRSUnicode` head number → `CJKRadicals.txt` map
  (`_load_radical_map` accepts codepoint-first and number-first layouts);
  unresolvable numbers leave the display field empty.
- Stroke counts: first `kTotalStrokes` token selected; all tokens recorded.
- Variants: `kTraditionalVariant`/`kSimplifiedVariant` code points resolved
  to characters; shown in the `Variants` card field as
  `Trad:/Simp:` text (never CHISE IDS; the old `IDS` field name was renamed
  because the label was incorrect attribution).
- Missing fields: left empty/null with Unihan provenance; Zhuyin stays empty
  when pinyin is missing (never LLM-invented).

## 4. MOE stroke pipeline

- `download.py` discovers the CSV/ZIP/player URLs live, downloads the catalog
  to `raw/moe_stroke/meta/embed_urls.csv`, crawls each `dictFrame.jsp` with
  resume (existing XML skipped), indexes into
  `processed/indexes/stroke_catalog.jsonl`.
- Retained verbatim: original XML files `xml/U+<UCS>__ID<id>.xml`.
- Indexing: `StrokeIndex` (catalog `xml_relpath` → glob fallback).
- Anki/Android rendering: original XML → Base64 → MOE JS engine
  (`stroke.js` + jQuery shims) with Replay/Pause/Play/Next/Grid controls.
- Geometry is never intentionally modified (see §2.1).
- License restriction: non-commercial (BY-NC-ND) — commercial use prohibited;
  attribution required on cards (`SourceNote`) and in published licenses.

## 5. MOE audio pipeline

- Package + mapping + no-modification guarantee: see §2.2.
- Unresolved behavior: character omitted from audio, reason recorded in
  `audio_missing.json` + published manifest `counts`, card renders without
  `[sound:]`.
- License/attribution: MOE Taiwan; exact terms NEEDS VERIFICATION (§2.2).

## 6. LLM pipeline

See §2.4. Validation (`LLMClient._validate_result`): NFC-normalizes strings,
keeps ≤3 complete examples, caps components at 8, coerces bad shapes to empty.
Legacy suffixed keys (`meaning_vi`, `..._vi`, `examples[].vi`,
`han_viet_generated`) are accepted and migrated to neutral keys at runtime,
but are rejected by the published-dataset validator (canonical schema only).

## 7. Test vs official mode

- Config keys `"mode"` / `"profile"` (`config/config.local.json`); default
  **test** / `vi`. CLI `--mode` / `--profile` override; `--test-char`
  overrides the character.
- TEST: processes EXACTLY `[test_character]` (default `思`) — not `limit=1`
  over arbitrary order. Manifest: `generation_mode=test, complete=false,
  character_count=1` (+ `target_language`). One-character deck +
  one-character `reference-v1`.
- OFFICIAL: full stroke-XML-supported set. Manifest: `generation_mode=official,
  complete=true` (false if any generation errors).
- Archive downloads are always whole files (Unihan.zip, MOE audio ZIP);
  test mode only limits per-character work, never invents partial downloads.

## 8. Directory layout

```
config/
    config.example.json        # tracked example/safe values
    config.local.json          # untracked local settings (mode, profile, LLM, test char)
    profiles/{vi,en}.json      # learner-language profiles (labels, features, LLM hint)
    anki/{front.html,back.html,style.css,model.json}  # frozen card design
download.py / generate.py    # clean-v1 pipeline (legacy sections marked LEGACY)
validate_reference.py        # offline validator
lab.py                       # LEGACY scratch LLM probe (not part of pipeline)
data/
  raw/moe_stroke/{archives,meta,player,xml,png,failed_pages}/
  raw/moe_dictionary/{archives,text(LEGACY),audio_char}/
  raw/hanviet/  raw/chise/   # LEGACY, kept on disk, unused by clean-v1
  raw/unicode/{archives,unihan,CJKRadicals.txt}/
  processed/{indexes,llm/<lang>/,cards}/
  manifests/sources.json     # provenance + SHA256 + licenses
  published/reference-v1/{manifest.json,characters/,enrichment/<lang>/,strokes/,audio/,indexes/,licenses}/
build/{media,reports,*.apkg}/
licenses/                    # source notices (see §10)
```

## 9. Published reference-v1 schema

- `manifest.json`: `schema_version=clean-v1`, `dataset_version=reference-v1`,
  mode/complete/counts, `target_language`, unicode version, source file list
  + SHAs, source licenses, prompt version, **LLM model alias only (never
  endpoint URL)**, per-file SHA256 map, missing-data reports.
- `characters/U+<UCS>.json`: `{schema, char, ucs, target_language,
  facts{...{value,source}}, enrichment{target_language, meaning, ...},
  media{stroke/audio {source,sha256,transform:none}}, generation_mode}`.
- `enrichment/<lang>/U+<UCS>.json`: sidecar with ONLY learner-language
  enrichment (+ `character_ref`); stroke/audio bytes are NOT duplicated per
  language. Facts-only consumers can ignore `enrichment/` entirely.
- `strokes/`: original MOE XML bytes. `audio/`: original MOE audio bytes.
- `indexes/index.json`: stroke file index. `licenses/ATTRIBUTION.txt`.
- Validator: `python validate_reference.py [--dir ...]` (offline) — also
  checks `target_language`, neutral enrichment keys, sidecar consistency,
  and `config/anki/` template files/placeholders.

## 10. Reproducibility

- Deterministic: Unihan parsing, pinyin→zhuyin, indexes, packaging, hashes.
- Externally hosted: MOE pages/ZIPs (live-discovered URLs may change),
  Unihan 17.0.0 archive, GitHub raw files (legacy only).
- Cached: downloads (skip-if-exists + resume), LLM responses (hash-gated
  per profile in `llm/<lang>/`), card records.
- Immutable published snapshots are still preserved because upstream hosting,
  MOE markup, and LLM outputs can all drift.

## 11. Deprecated / legacy sources (NOT used by clean-v1)

- MOE 《國語辭典簡編本》 text/definitions/synonyms/antonyms/字詞號 —
  classes `MoeDictionary`, `ReadingInfo`, legacy `Grounder` retained unused;
  on-disk copies preserved, never parsed by the clean path.
- CHISE IDS (`ChiseIndex`, `IDSNode`) — replaced by LLM `components_generated`
  (AI enrichment, explicitly not IDS).
- Old external Hán-Việt CSV (`HanVietIndex`) — replaced by
  `kVietnamese`/LLM fallback with explicit provenance.
- CVDICT / CC-CEDICT: not present anywhere in this repository (no code, no
  data, no references) — nothing to remove.
- `lab.py`: scratch LLM probe, not part of the pipeline.
- Pre-profile LLM cache (`data/processed/llm/U+*.json` without language
  subdirectory) and pre-profile card records (`meaning_vi`, `examples[].vi`
  keys): left on disk for rollback, never read by the new pipeline
  (cache is now `llm/<lang>/`, prompt `anki-zh-generic-v1`).

## 12. TARGET LANGUAGE ARCHITECTURE

Core data is language-neutral; only learner-facing enrichment is
profile-specific.

- A profile is one JSON file: `config/profiles/<code>.json`
  (`vi.json`, `en.json` shipped). It defines `language_code`,
  `language_name`, `labels.*` (all card labels, table headers, empty-state
  strings, `attribution_ai` tail), `features.*` (e.g. `show_han_viet`),
  and `llm.instruction` (target-language writing instruction merged into
  the system prompt). Python never hardcodes profile text; missing keys
  fall back to built-in English defaults.
- Facts (`pinyin`, `zhuyin`, `definition_en`, radicals, strokes, variants,
  media) are identical for every language. Enrichment (`meaning`,
  `structure_explanation`, `component_meanings`, `examples[].translation`)
  is generated per profile and cached per profile.
- Optional language-specific data (Hán-Việt for Vietnamese) is gated by
  `features.show_han_viet`: the value stays in the record with provenance,
  but only opted-in profiles fill the Anki `HanViet` field; otherwise the
  field is empty and `{{#HanViet}}` hides the block — no empty visual
  blocks. Future per-language extras follow the same pattern without
  changing the core schema.
- Switching languages: set `"profile": "en"` in
  `config/config.local.json` (or pass `--profile en`) and run
  `generate.py`. No code change needed.

### How to add a new target language

1. Copy `config/profiles/vi.json` → `config/profiles/<code>.json`.
2. Set `language_code` / `language_name` / `native_name`.
3. Translate every `labels.*` value and rewrite `llm.instruction` for the
   new language.
4. Set `features`: `show_han_viet` should be `false` unless the language
   genuinely uses Sino-Vietnamese readings.
5. Run TEST mode with `"profile": "<code>"` and review one card + the
   published sidecar before running OFFICIAL.

## 13. CUSTOMIZING THE ANKI TEMPLATE

The card visual design is FROZEN (same hierarchy, fonts, sizes, spacing,
front/back behavior, stroke player with Replay/Pause/Play/Next/Grid,
examples, audio placement). Safe to edit:

- `config/anki/front.html` — front layout (default: big Hanzi only).
- `config/anki/back.html` — back layout. `{{Field}}` tokens are Anki note
  fields (see `model.json`); `%%LABEL_*%%` tokens are replaced at build
  time from the active profile; `%%MOE_PLAYER_JS%%` injects the stroke
  player logic (kept in `generate.py:PLAYER_JS`); `{{#HanViet}}…{{/HanViet}}`
  hides the optional block when empty.
- `config/anki/style.css` — all styling. `.example-translation` is the
  canonical class; `.example-vi` is kept as an alias so old cards look
  identical.
- `config/anki/model.json` — note-type name + field list. Field ORDER is
  frozen (Anki matches notes by GUID, values map positionally); renames
  applied once: `MeaningVN→Meaning`, `MOEDefinitionZH→DefinitionEN`,
  `IDS→Variants` (the old names were incorrect attributions).

What must stay consistent: every `{{Field}}` in `back.html` must exist in
`model.json` fields; `generate.py` fills note fields positionally in the
same order. `validate_reference.py` checks the required files and
placeholders offline.
