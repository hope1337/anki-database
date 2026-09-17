# Data Sources & Pipeline (clean-v1: CNS11643 pronunciation + notable sayings)

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
              (pronunciation recordings REMOVED from clean-v1; see §2.2)

Unicode 17.0.0 ──► Unihan.zip + CJKRadicals.txt ──► data/raw/unicode/
      (version-PINNED, never "latest")

CNS11643 / 全字庫 ──► human-recorded pronunciation audio ──►
      data/raw/cns11643/ ──► data/processed/cns11643/cns_index.json
      (fixed reference source; exact archive URLs NEEDS VERIFICATION)

Target-language profile (config/profiles/<code>.json: vi, en, ...)
    ├── learner language + labels + LLM instruction
    └── optional features (e.g. show_han_viet, notable_sayings_max_items)

Deterministic local code (generate.py)
    ├── UnihanIndex: kMandarin/kDefinition/kRSUnicode/kTotalStrokes/variants/kVietnamese
    ├── pinyin_to_zhuyin(): deterministic Pinyin → Zhuyin (never LLM)
    ├── CleanGrounder: Unihan facts + MOE stroke index
    ├── CharacterAudioResolver: (character, expected Zhuyin) → CNS clip
    ├── external Anki template (config/anki/) + profile labels
    └── manifests / SHA256

LLM enrichment (local llama.cpp server, config only)
    ├── learner-language meaning, structure note, component glosses,
    │   examples, notable sayings
    └── NEVER authoritative facts; NEVER receives MOE dict/CHISE/HanViet data

Outputs
    ├── data/processed/cards/U+*.json   (normalized records w/ provenance)
    ├── build/*.apkg                     (Anki deck, frozen template)
    └── data/published/reference-v1/     (immutable Android-ready snapshot)
```

**Core rule:** MOE = stroke geometry ONLY. Unicode = factual text.
CNS11643 = human-recorded pronunciation (exact character + expected
Zhuyin match; missing reading = no audio, never synthesized).
LLM = learner-facing enrichment (incl. notable sayings).
Target language = CONFIGURABLE. There is NO TTS fallback.

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

### 2.2 CNS11643 human pronunciation audio (CLEAN-v1, fixed source)

- MOE pronunciation recordings were REMOVED from clean-v1: clips regularly
  speak more than the isolated target character, breaking the contract
  (one pronunciation asset = exactly the intended Hanzi/reading). MOE is
  now stroke-order source only (§2.1). All TTS backends (Edge/Azure/command,
  CosyVoice, PrimeTTS) were likewise REMOVED: there is no synthesis
  fallback. The old package audit and duplicate analysis are preserved
  in §11 for history.
- Source: CNS11643 / 全字庫 (`https://www.cns11643.gov.tw`), Taiwan's
  character standard; dataset page `https://data.gov.tw/dataset/5961`
  (Ministry of Digital Affairs). Human-recorded Taiwan Mandarin
  pronunciation for Traditional Chinese character cards.
- Acquisition (`download.py` §4c, verified official contract): the
  official `OpenDataFilesList.csv` listing + `release.txt` versions are
  fetched first (base `.../opendata/`); then `Voice.zip` (58.5 MB:
  `male.zip` + `female.zip` + voice readme), `Properties.zip`
  (`CNS_phonetic.txt`: `CNS-code<TAB>bopomofo`, first tone unmarked,
  neutral leading ˙), and `MapingTables.zip`
  (`Unicode/CNS2UNICODE_Unicode {BMP,2,3,15}.txt`:
  `CNS-code<TAB>HEX-Unicode`). Originals stay immutable under
  `data/raw/cns11643/`; nested male/female.zip are extracted one level
  deeper (`audio/male/`, `audio/female/`); the processed lookup index
  `data/processed/cns11643/cns_index.json` (`cns-index-v2`) is built
  automatically and skipped when source inputs are unchanged
  (inputs-hash comparison). Downloads are idempotent
  (skip-if-complete, `.part` HTTP-Range resume, `.extracted.ok`
  extraction marker incl. interrupted-extraction recovery,
  `--force` reacquires + clears stale markers).
- Member mapping (all official, no guessing): CNS code -> character via
  the CNS2UNICODE tables; bopomofo -> `hanpin` filename via the
  EXTRACTED official voice readme table (`F_<hanpin>.mp3` female,
  `M_<hanpin>.mp3` male — underscore on disk, omitted in the readme
  prose; neutral-tone `<hanpin>5` clips are `.wav`); every indexed
  member is verified present post-extraction (missing members are
  reported, never indexed).
  cns11643.gov.tw needs `--insecure` (TWCA chain, same as the MOE host).
- Lookup (`cns_audio.CharacterAudioResolver`, index loaded ONCE per run):
  match key is (character, NORMALIZED expected Zhuyin); expected Pinyin
  is secondary metadata only. Polyphonic characters resolve per reading
  (e.g. 行+ㄒㄧㄥˊ vs 行+ㄏㄤˊ are different lookups); a missing reading
  returns unavailable — NO fallback to another reading, NO TTS, NO LLM
  choice. Wrong pronunciation is worse than missing pronunciation.
- Multiple same-reading recordings: male.zip/female.zip are separate
  official archives, preserved with voice metadata; `preferred_voice`
  (`auto|male|female`) selects among them deterministically, else the
  first-sorted record wins. This is source-native selection, not model
  selection.
- No sentence TTS: CNS character clips are NEVER concatenated into fake
  sentence speech.
- Normalized `media.audio`: `{source: cns11643|unavailable,
  status: matched|unavailable, human_recorded, file (+ filename alias),
  sha256, pronunciation{character,pinyin,zhuyin}, expected_pinyin,
  expected_zhuyin, matched_zhuyin, record_id, voice, relpath,
  reading_ambiguity, reading_verified (= matched),
  transform: "none: original CNS bytes copied without transcoding",
  detail}`. Unavailable audio renders no `[sound:]` and is listed in
  `build/reports/audio_missing.json` with its reason.
- License/attribution: dataset-level license VERIFIED — 政府資料開放授權條款第1版
  (Open Government Data License v1.0), free (see `licenses/CNS11643.md`).
  No separate audio-only notice was found in the official voice readme;
  audio-specific additional terms remain UNKNOWN / NEEDS VERIFICATION.
  The original source notice is preserved under `data/raw/cns11643/` and
  must be copied into `licenses/` + published `licenses/` before any
  public redistribution; nothing here claims otherwise.

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
- Prompt version: `anki-zh-generic-v2` (was `anki-zh-generic-v1`; legacy
  `anki-zh-tw-v1`, `anki-zh-tw-clean-v2`). v2 adds the optional
  `notable_sayings` enrichment contract, so old cached enrichment (without
  it) is invalidated via the cache key. The prompt is target-language
  agnostic:
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
  examples[{zh, pinyin, translation}],
  notable_sayings[{traditional, pinyin, translation, type, source,
  language_specific{han_viet}}]}` (see §2.5).
  Components are **AI learning aids, NOT authoritative IDS** (never called
  CHISE). Examples use Taiwan Traditional Chinese (A2–B1).
- Explicit statement: AI-generated data is not authoritative source data;
  the prompt prefers "insufficient data" over invented etymology — and `[]`
  over an invented quotation.

### 2.5 Notable sayings (LLM enrichment, never reference facts)

- One optional list per card: genuinely well-known quotations, proverbs,
  idioms, maxims, or classical sayings that CONTAIN the target Hanzi
  (e.g. 思 → 我思故我在). Quality over coverage: 0 items is valid and
  preferred over a fabricated or unrelated quote; default cap is
  `features.notable_sayings_max_items` = 2 (clamped 0..3).
- Schema (language-neutral canonical keys, stored under
  `enrichment.notable_sayings[]` with the usual `{value, source, model,
  prompt_version}` wrapper):
  `{traditional, pinyin (full-saying Pinyin), translation,
  type: quotation|proverb|idiom|maxim|classical|other,
  source ("" = unknown attribution, never invented),
  language_specific: {han_viet}}`.
  The saying must contain the target character (generation drops misses);
  `han_viet` is the Sino-Vietnamese reading of the full saying text —
  kept nested so other profiles never carry it — and is surfaced only
  where `features.show_han_viet=true`.
- Card rendering reuses the example block styling (same width/spacing/
  typography). The section title is emitted inside the generated HTML, so
  an empty list renders NOTHING: no heading, no container. No new JS.
- Like all enrichment, sayings live beside the language-neutral facts and
  in the per-language sidecar — never in the reference layer as
  authoritative data.

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
  (`stroke.js` + jQuery shims) with Replay/Previous/Pause/Play/Next/Grid controls.
- Geometry is never intentionally modified (see §2.1).
- License restriction: non-commercial (BY-NC-ND) — commercial use prohibited;
  attribution required on cards (`SourceNote`) and in published licenses.

## 5. CNS11643 audio pipeline

- Lookup: `{character, expected zhuyin, expected pinyin (metadata)}` via
  `cns_audio.CharacterAudioResolver` — the CNS index loads ONCE per run,
  each card is a dict lookup. Pipeline Zhuyin (explicit trailing ˉ for
  first tone) normalizes to the CNS form (first tone unmarked — verified
  zero ˉ in `CNS_phonetic.txt`; neutral keeps leading ˙). The resolver
  owns index loading, Zhuyin normalization, exact matching, deterministic
  voice choice, and source metadata; `generate.py` never sees archive
  internals.
- Media: original CNS bytes copied (never transcoded) to
  `build/media/cns_U<UCS>_<zhuyin8>.<ext>`, referenced by one `[sound:]`
  tag, published under `published/audio/` with SHA256 in the manifest
  `files` map. First run needs the CNS dataset (`python download.py`);
  no network is ever needed for audio afterwards.
- Unavailable behavior (no CNS index, missing reading, absent file):
  reason recorded in `audio_missing.json` + published manifest `counts`,
  card renders without `[sound:]`. One missing record never kills the run.
- License/attribution: dataset-level license VERIFIED — 政府資料開放授權條款第1版
  (Open Government Data License v1.0), free (see `licenses/CNS11643.md`).
  Audio-specific additional terms remain UNKNOWN / NEEDS VERIFICATION.

## 6. LLM pipeline

See §2.4. Validation (`LLMClient._validate_result` + `_validate_sayings`):
NFC-normalizes strings, keeps ≤3 complete examples, caps components at 8,
keeps ≤ profile `notable_sayings_max_items` sayings (each needs Traditional
text containing the target char + full pinyin + translation; unknown types
become `other`; empty source = unknown attribution), coerces bad shapes to
empty.
Legacy suffixed keys (`meaning_vi`, `..._vi`, `examples[].vi`,
`han_viet_generated`) are accepted and migrated to neutral keys at runtime,
but are rejected by the published-dataset validator (canonical schema only).

## 7. Test vs official mode

- Config keys `"mode"` / `"profile"` (`config/config.local.json`); default
  **test** / `vi`. CLI `--mode` / `--profile` override. Debug overrides
  (highest precedence first): `--chars <string>` (raw, deduped) then
  `--test-chars <a,b,c>` (validated, any length ≥ 1); normal TEST runs
  should use config instead.
- TEST: processes EXACTLY `test_characters` from config — an explicit,
  deterministic, order-preserving list (default 10: 思八乾差心學明好中愛),
  never `limit=N` over dataset order. Entries must be single characters,
  duplicate-free, and present in the supported stroke dataset, else generation
  fails with a clear error. Manifest: `generation_mode=test,
  complete=false, character_count=10` (+ `target_language`). Ten-character
  deck + ten-character `reference-v1`. To change the test set later, edit
  `test_characters` in `config/config.local.json` (keep exactly 10 entries).
  The legacy singular `test_character` key / `--test-char` flag no longer
  exist (stale key raises a migration error; stale flag is rejected).
- OFFICIAL: full stroke-XML-supported set. Manifest: `generation_mode=official,
  complete=true` (false if any generation errors).
- Archive downloads are always whole files (Unihan.zip);
  test mode only limits per-character work, never invents partial downloads.

## 8. Directory layout

```
config/
    config.example.json        # tracked example/safe values
    config.local.json          # untracked local settings (mode, profile, LLM, test characters)
    profiles/{vi,en}.json      # learner-language profiles (labels, features, LLM hint)
    anki/{front.html,back.html,style.css,model.json}  # frozen card design
download.py / generate.py    # clean-v1 pipeline (legacy sections marked LEGACY)
validate_reference.py        # offline validator
lab.py                       # LEGACY scratch LLM probe (not part of pipeline)
data/
  raw/moe_stroke/{archives,meta,player,xml,png,failed_pages}/
  raw/moe_dictionary/{archives,text(LEGACY),audio_char(LEGACY/deprecated)}/
  raw/hanviet/  raw/chise/   # LEGACY, kept on disk, unused by clean-v1
  raw/unicode/{archives,unihan,CJKRadicals.txt}/
  raw/cns11643/{OpenDataFilesList.csv,release.txt,archives/,properties/,mapping/,audio/{male,female}/}/
  processed/{indexes,llm/<lang>/,cards,cns11643/cns_index.json}/
  manifests/sources.json     # provenance + SHA256 + licenses
  published/reference-v1/{manifest.json,characters/,enrichment/<lang>/,strokes/,audio/,indexes/,licenses}/
build/{media,reports,*.apkg}/
licenses/                    # source notices (see §10)
```

## 9. Published reference-v1 schema

- `manifest.json`: `schema_version=clean-v1`, `dataset_version=reference-v1`,
  mode/complete/counts, `target_language`, unicode version, source file list
  + SHAs, source licenses, prompt version, **LLM model alias only (never
  endpoint URL)**, `audio` provenance (`source: cns11643` +
  `preferred_voice` + index size — no models, no endpoints, no
  machine-specific paths), per-file SHA256 map, missing-data reports.
- `characters/U+<UCS>.json`: `{schema, char, ucs, target_language,
  facts{...{value,source}}, enrichment{target_language, meaning, ...,
  notable_sayings}, media{stroke/audio}, generation_mode}`.
  `media.audio` = CNS11643 provenance (`source: cns11643|unavailable`,
  `status: matched|unavailable`, `human_recorded`, `file`, `sha256`,
  `pronunciation{character,pinyin,zhuyin}`, `expected_pinyin`,
  `expected_zhuyin`, `matched_zhuyin`, `record_id`, `voice`, `relpath`,
  `reading_ambiguity`, `reading_verified (= matched)`, `detail`).
- `enrichment/<lang>/U+<UCS>.json`: sidecar with ONLY learner-language
  enrichment (+ `character_ref`); stroke/audio bytes are NOT duplicated per
  language. Facts-only consumers can ignore `enrichment/` entirely.
- `strokes/`: original MOE XML bytes. `audio/`: original CNS clips.
- `indexes/index.json`: stroke file index. `licenses/ATTRIBUTION.txt`.
- Validator: `python validate_reference.py [--dir ...]` (offline, no
  network) — also checks `target_language`, neutral enrichment
  keys, saying item contracts (Traditional + pinyin + translation; Han-Viet
  iff the profile requires it), CNS audio consistency (`source: cns11643`,
  `human_recorded` + `reading_verified` on matched records,
  `matched_zhuyin` present, file exists and non-empty, SHA matches, no
  TTS/model fields, no machine-specific paths in manifest/records/sidecars),
  sidecar consistency, and `config/anki/` template files/placeholders
  (incl. `{{NotableSayingsHTML}}`). Pre-CNS (MOE/TTS-era) records validate
  leniently with a warning.

## 10. Reproducibility

- Deterministic: Unihan parsing, pinyin→zhuyin, CNS lookup, indexes,
  packaging, hashes.
- Externally hosted: MOE pages/ZIPs (live-discovered URLs may change),
  Unihan 17.0.0 archive, CNS11643 opendata files (stable paths under
  `.../opendata/`; versions from `release.txt`), GitHub raw files
  (legacy only).
- Cached: downloads (skip-if-exists + resume), LLM responses (hash-gated
  per profile in `llm/<lang>/`, prompt `anki-zh-generic-v2`), card records.
  CNS audio needs no synthesis cache: source bytes are addressed by
  (character, reading) + file SHA256.
- Immutable published snapshots are still preserved because upstream hosting,
  MOE markup, CNS resources, and LLM outputs can all drift.

## 11. Deprecated / legacy sources (NOT used by clean-v1)

- MOE 《國語辭典簡編本》 text/definitions/synonyms/antonyms/字詞號 —
  classes `MoeDictionary`, `ReadingInfo`, legacy `Grounder` retained unused;
  on-disk copies preserved, never parsed by the clean path.
- MOE pronunciation recordings (`word_wav/*.wav` + `字詞名→檔案名稱`
  manifest; classes `CleanAudioResolver`, legacy `AudioResolver`; reports
  `audio_multi.json`, `audio_ambiguity_analysis.json`) — REMOVED from
  clean-v1 because clips regularly speak more than the isolated target
  character. Retained unused for rollback comparison only; `download.py`
  fetches the package solely via deprecated `--include-legacy-audio`.
  Historical audit (package `dict_concised_music_word_2014_20260626`):
  single sheet `Metadata` A1:C6477 (`字詞號|字詞名|檔案名稱`), no
  pronunciation column anywhere, `檔案名稱 == 字詞號 + ".wav"` throughout,
  500/5915 characters with 2+ entries (one clip per dictionary entry).
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
  (cache is now `llm/<lang>/`, prompt `anki-zh-generic-v2`).
- Pre-v2 LLM cache (prompt `anki-zh-generic-v1`, no `notable_sayings`) and
  pre-CNS card records (`media.audio` with MOE `resolved/multiple/
  unresolved` or TTS `generated` statuses): invalidated by the prompt bump /
  unread by the new audio path; regenerate after deleting stale outputs.
- Removed TTS backends (Edge/Azure/command, CosyVoice, PrimeTTS/ONNX):
  `tts.py`, `primetts_frontend.py`, audio model selection, and the audio
  instruction prompt are deleted, not deprecated. Old TTS config keys fail
  loudly with a migration error. `data/generated/audio/` clips left on
  disk are orphaned (never read); the validator tolerates pre-CNS records
  with a warning.

## 12. TARGET LANGUAGE ARCHITECTURE

Core data is language-neutral; only learner-facing enrichment is
profile-specific.

- A profile is one JSON file: `config/profiles/<code>.json`
  (`vi.json`, `en.json` shipped). It defines `language_code`,
  `language_name`, `labels.*` (all card labels, table headers, empty-state
  strings, `attribution_ai` tail), `features.*` (e.g. `show_han_viet`,
  `notable_sayings_max_items`),
  and `llm.instruction` (target-language writing instruction merged into
  the system prompt). Python never hardcodes profile text; missing keys
  fall back to built-in English defaults.
- Facts (`pinyin`, `zhuyin`, `definition_en`, radicals, strokes, variants,
  media) are identical for every language. Enrichment (`meaning`,
  `structure_explanation`, `component_meanings`, `examples[].translation`,
  `notable_sayings[]` with per-item `translation`)
  is generated per profile and cached per profile.
- Optional language-specific data (Hán-Việt for Vietnamese) is gated by
  `features.show_han_viet`: the value stays in the record with provenance,
  but only opted-in profiles fill the Anki `HanViet` field; otherwise the
  field is empty and `{{#HanViet}}` hides the block — no empty visual
  blocks. Future per-language extras follow the same pattern without
  changing the core schema. Notable sayings follow it too: per-item
  `han_viet` lives under `language_specific` and renders only for
  opted-in profiles; other profiles get translation-only items.
- Switching languages: set `"profile": "en"` in
  `config/config.local.json` (or pass `--profile en`) and run
  `generate.py`. No code change needed.

### How to add a new target language

1. Copy `config/profiles/vi.json` → `config/profiles/<code>.json`.
2. Set `language_code` / `language_name` / `native_name`.
3. Translate every `labels.*` value and rewrite `llm.instruction` for the
   new language.
4. Set `features`: `show_han_viet` should be `false` unless the language
   genuinely uses Sino-Vietnamese readings; `notable_sayings_max_items`
   caps sayings per card (default 2, range 0..3).
5. Run TEST mode with `"profile": "<code>"` and review one card + the
   published sidecar before running OFFICIAL.

## 13. CUSTOMIZING THE ANKI TEMPLATE

The card visual design is FROZEN (same hierarchy, fonts, sizes, spacing,
  front/back behavior, stroke player with Replay/Previous/Pause/Play/Next/Grid,
examples, notable sayings, audio placement). Safe to edit:

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
  `IDS→Variants` (the old names were incorrect attributions); one field
  APPENDED once: `NotableSayingsHTML` (append-only is the safe change —
  existing notes see it empty). The sayings section title is emitted
  inside the generated HTML (not in `back.html`) so empty lists render
  no heading or container at all.

What must stay consistent: every `{{Field}}` in `back.html` must exist in
`model.json` fields; `generate.py` fills note fields positionally in the
same order. `validate_reference.py` checks the required files and
placeholders offline.

## 14. CONFIG-DRIVEN TEXT BACKEND + FIXED CNS AUDIO

Model/backend selection exists ONLY for TEXT (`generation.text`,
`mode: local | endpoint`). Audio has NO selection: pronunciation comes
exclusively from the fixed CNS11643 human-recorded source. Legacy
top-level `llm` still loads and migrates into `generation.text.endpoint`.
(Removed TTS audio configuration fails loudly with a migration error.)

### 14.1 Text backends

- `endpoint` (`text_providers.EndpointTextProvider`, provider
  `openai_compatible`): the previous behavior, unchanged — OpenAI-compatible
  `POST {base_url}/v1/chat/completions` with `response_format:
  json_object` and graceful retry, temperature/max_tokens from
  `text.endpoint.generation`. Secrets come from the environment variable
  named by `api_key_env` (empty = none); endpoint URLs and keys never
  reach manifests/caches.
- `local` (`text_providers.LocalHFTextProvider`, provider `huggingface`):
  `model_id` / `revision` / `device` / `dtype` / `generation` from config.
  The model lazy-loads on first `complete()` via `transformers`
  (`AutoTokenizer` + `AutoModelForCausalLM`); missing runtime package
  fails with an exact `pip install` instruction, never auto-installs.
  Default `Qwen/Qwen2.5-0.5B-Instruct` is a sensible small default for an
  8 GB VRAM laptop — guidance only, any repository stays configurable
  and no memory limit is enforced anywhere.

### 14.2 Audio: fixed CNS11643 source, no backends

- Source selection: none. `audio` config is `{source: cns11643,
  preferred_voice: auto|male|female}` — voice preference only, honored
  when CNS records carry voice metadata, else inert.
- There is no local TTS model, no endpoint TTS, no sentence synthesis,
  and no audio prompt file. `tts.py`, `primetts_frontend.py`, and
  `config/prompts/audio_instruction.txt` were deleted.
- See §2.2 (source contract) and §5 (pipeline).

### 14.3 Prompts live in config

- `config/prompts/text_system.txt` (placeholders `{language_name}`,
  `{profile_instruction}`, `{max_sayings}`, `{target_char}`) and
  `config/prompts/text_user.txt` (`{facts_json}`) hold the exact
  enrichment contract (SOURCE_FACTS authoritative, Taiwan
  Traditional, profile language, meaning/structure/components/examples/
  notable_sayings, Han-Viet behavior, JSON schema). Both text modes
  render the same files; unknown placeholders fail loudly.
  (There is no audio prompt: nothing consumes one.)

### 14.4 Three data kinds (do not mix them)

- REFERENCE data (`download.py` only): MOE strokes, Unihan/CJKRadicals,
  CNS11643 audio + pronunciation index.
- GENERATED data (`generate.py`): LLM enrichment, card records, `.apkg`,
  published snapshot. (CNS audio bytes are COPIED, not generated.)
- RUNTIME models (`generate.py` text-local mode only): Hugging Face
  text weights. `download.py` NEVER fetches AI models. The local text
  provider acquires missing files on FIRST ACTUAL `generate.py` use
  (`transformers.from_pretrained`) into the normal HF cache
  (`HF_HOME` / `~/.cache/huggingface`) and reuses them afterwards.
  Manifests record the text model alias/revision only — never absolute
  cache paths, endpoint URLs, or keys (validator checks).

### 14.5 Cache invalidation

- TEXT identity (`text_providers.text_cache_identity`): mode, provider,
  local `model_id`/revision OR endpoint model alias, device/dtype,
  generation params, SYSTEM+USER prompt FILE CONTENT hashes, target
  profile + instruction, normalized SOURCE_FACTS. Editing
  `config/prompts/text_*.txt` or switching model/revision regenerates;
  changing only `base_url`/timeout reuses cache.
- AUDIO identity: CNS source bytes are addressed by (character, matched
  normalized Zhuyin, selected recording, file SHA256) — no synthesis
  cache exists. Changing the requested reading can never reuse another
  reading's clip (exact match or nothing).

### 14.6 UNKNOWN / NEEDS VERIFICATION

- Inner member listing of male.zip/female.zip beyond the documented
  `M|F_<hanpin>.mp3` rule (outer layout + naming rule verified against
  extracted members: underscore present, neutral-tone `<hanpin>5` clips
  are `.wav`; every indexed member is additionally verified present
  post-extraction, so a deviation fails loudly instead of silently).
- Audio-specific license terms beyond the dataset-level OGDL-1.0 (no
  separate audio notice found in the official voice readme).
