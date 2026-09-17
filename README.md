# Traditional Chinese Anki Database — Taiwan-Oriented Learning-Data Pipeline

A reproducible Traditional Chinese learning-data and Anki-deck generation pipeline
focused on Taiwan-oriented pronunciation, character structure, stroke order,
multilingual learner content, and traceable data provenance.

This repository is not just a static prebuilt deck. It is a pipeline that
downloads official reference data, normalizes it deterministically, enriches it
with configurable AI-generated learner content, validates the result, and
compiles it into an offline Anki package (`.apkg`) plus an immutable
reference snapshot.

The pipeline keeps three kinds of data conceptually separate:

- **Authoritative / reference data** — Unicode Unihan facts, MOE stroke geometry,
  CNS11643 human-recorded pronunciation.
- **Generated / enriched data** — normalized card records, packaged decks,
  published snapshots (CNS audio bytes are *copied*, not generated).
- **User-configurable AI enrichment** — learner-facing meanings, explanations,
  examples, and sayings produced by a configured text backend per
  target-language profile.

## About the Author

**Nguyễn Đặng Đức Mạnh**

Graduated from the University of Information Technology (UIT); currently
pursuing a Master's degree in Computer Science at National Central University
(NCU).

## What This Project Is For

Learning Traditional Chinese — especially in a Taiwan Mandarin context —
requires combining several independent resources: reliable romanization and
character metadata, stroke-order visualization, native pronunciation audio,
pedagogical explanations, and spaced-repetition cards. Assembling these by hand
is slow, hard to reproduce, and mixes facts of very different reliability
(official standards vs. AI-generated study aids).

This project addresses that by providing a single auditable pipeline:

- Download pinned reference sources (strokes, Unihan metadata, pronunciation audio).
- Normalize them into factual character records with explicit provenance.
- Enrich them with learner-facing content in a configurable target language.
- Validate the published dataset offline.
- Compile everything into an Anki deck with a frozen card design.

Intended users:

- Learners of Traditional Chinese, including learners studying Mandarin in a
  Taiwan context.
- Anki users who want an offline deck with stroke animation and native audio.
- Learners who want the same characters explained in their own language via
  profiles (Vietnamese and English shipped).
- Developers and researchers interested in reproducible language-learning
  datasets with explicit source provenance.

## What You Can Build With This Project

- A small configured **test deck** (exactly the 10 characters in
  `test_characters`) for fast iteration.
- A larger **full/official reference deck** covering the supported
  stroke-order character set.
- Cards containing: Traditional Hanzi, Pinyin, Zhuyin, learner-language
  meaning, alternate Unihan readings, radical/stroke/variant facts, component
  explanations, structure notes, example sentences, notable sayings, MOE
  stroke-order animation, and pronunciation audio.
- Target-language decks by selecting a profile (`vi`, `en`), including
  profile-gated features such as Hán-Việt display for Vietnamese.
- Custom enrichment behavior by editing prompt files under `config/prompts/`
  without touching Python, or by switching the text backend (local Hugging Face
  model vs. OpenAI-compatible endpoint).
- Auditable provenance: source manifests with SHA256 hashes, per-field source
  labels, and an offline validator.

Data roles, in short:

| Kind | Content | Example |
|---|---|---|
| Reference facts | Deterministic, official | Pinyin, Zhuyin (derived), radical, stroke count, variants, English gloss |
| Source media | Copied verbatim, never modified | MOE stroke XML (Base64), CNS11643 audio clips |
| AI enrichment | Generated, clearly labeled | Meaning, structure note, components, examples, sayings |

Coverage is bounded by the sources: only characters present in the MOE
stroke-order set can become cards, and pronunciation audio is included **only
when an exact character + Zhuyin match exists in CNS11643**. Missing audio
renders no `[sound:]` tag and is reported — it is never synthesized, because
this project intentionally has no TTS fallback.

## Feature Overview

**Data sources (each with a fixed role)**

- Unicode 17.0.0 Unihan + `CJKRadicals.txt` — all factual text metadata
  (`kMandarin`, `kDefinition`, `kRSUnicode`, `kTotalStrokes`, variants,
  `kVietnamese`).
- MOE Taiwan stroke-order resources — per-character vector XML plus the JS
  player for offline animation; geometry is never modified.
- CNS11643 / 全字庫 — human-recorded Taiwan Mandarin character pronunciation;
  fixed source, exact reading match only.

**AI enrichment**

- Configurable text backend: `local` (Hugging Face runtime model) or
  `endpoint` (OpenAI-compatible API), selected in config.
- File-backed prompts (`config/prompts/`); prompt edits invalidate the cache.
- Per-profile caching under `data/processed/llm/<lang>/`; switching profiles
  never reuses another language's enrichment.
- Validated output: NFC normalization, capped examples/components/sayings,
  `[]` preferred over invented quotations.

**Pronunciation**

- `(character, normalized expected Zhuyin)` lookup; polyphonic readings resolve
  per reading (e.g. 行+ㄒㄧㄥˊ vs. 行+ㄏㄤˊ are different lookups).
- `preferred_voice: auto | male | female` selects among same-reading CNS
  recordings when voice metadata exists.
- No fallback to another reading, no concatenation into sentence speech, no TTS.

**Stroke order**

- Original MOE XML bytes → Base64 `StrokeDataB64` field → vendored MOE JS
  engine with Replay / Previous / Pause / Play / Next / Grid controls.
- Optional 6063 PNG fallback (`--skip-png` to omit).

**Anki generation**

- Frozen note type (`config/anki/model.json`, 15 fields, append-only changes),
  `front.html` / `back.html` / `style.css` card design with profile label
  substitution (`%%LABEL_*%%`) and player injection (`%%MOE_PLAYER_JS%%`).
- Deterministic note identity: `genanki.guid_for("MOE-TRADITIONAL-V1", char)`.
- Test vs. official modes; per-run reports (`build/reports/`).

**Reproducibility**

- Pinned Unicode version (17.0.0, never "latest"); source SHAs in manifests;
  deterministic parsing, Zhuyin derivation, audio lookup, packaging, and hashes.

## Project Structure

```text
config/
  config.example.json      # tracked, documented template (safe values)
  config.local.json        # untracked local runtime config (created by you)
  profiles/vi.json, en.json# target-learner-language profiles
  prompts/text_system.txt  # text-enrichment system prompt template
  prompts/text_user.txt    # text-enrichment user prompt template (SOURCE_FACTS)
  anki/                    # frozen card design: front/back.html, style.css, model.json
data/
  raw/                     # immutable upstream sources (MOE, Unicode, CNS11643, legacy)
  processed/               # derived indexes, per-profile LLM cache, card records, CNS index
  manifests/sources.json   # provenance + SHA256 + licenses
  published/reference-v1/  # immutable generated snapshot (manifest, characters, strokes, audio…)
build/
  taiwan_traditional_chinese.apkg  # generated deck (default output path)
  media/                   # copied CNS clips prepared for packaging
  reports/                 # build_summary.json, audio_missing.json, generation_errors.json
licenses/                  # per-source notices (MOE stroke/audio, CNS11643, Unicode)
download.py                # reference-data acquisition (no LLM, no .apkg)
generate.py                # enrichment + card building + packaging + publishing
cns_audio.py               # CNS11643 index building + (character, Zhuyin) resolver
text_providers.py          # local (Hugging Face) / endpoint (OpenAI-compatible) text backends
prompts.py                 # strict prompt loading, rendering, and hashing
validate_reference.py      # offline validator for the published snapshot
tests/                     # mocked unit tests (no network)
lab.py                     # scratch LLM probe; not part of the pipeline
DATA_SOURCES_AND_PIPELINE.md  # detailed source/provenance/processing contract
data/README.md             # data-layout notes
```

`data/` and `build/` are gitignored local artifacts; only code, config
templates, prompts, profiles, card design, licenses, docs, and tests are
tracked.

## Installation

There is currently **no `requirements.txt`, no `pyproject.toml`, and no
declared Python version** in the repository — dependency packaging is not yet
centralized. The authoritative dependency hints are the imports and docstrings
in the code:

- Always needed: `genanki`, `requests` (per `generate.py` / `download.py`
  docstrings; `download.py` also uses `urllib3` retry support, normally pulled
  in with `requests`).
- Legacy path only: `openpyxl` (MOE-dictionary rollback code; clean generation
  never calls it).
- Local text mode only: `transformers` + `torch` (lazy-loaded; missing packages
  fail with an exact `pip install` instruction instead of auto-installing).

Recommended setup (isolated environment):

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install genanki requests openpyxl
```

Linux / macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install genanki requests openpyxl
```

For local text generation only, additionally:

```bash
pip install transformers torch --index-url https://download.pytorch.org/whl/cu121
```

(Test files use only the standard library plus the project modules.)

## Configuration

Copy the tracked template to create your local runtime config:

```bash
cp config/config.example.json config/config.local.json
```

`config/config.local.json` is untracked and is the main source of truth at
runtime; `config/config.example.json` documents every option with safe example
values. Config files are self-documenting: keys starting with `_comment`,
`_options`, or `_description` are documentation and ignored by the loader —
any *other* unknown key produces a warning, so typos are surfaced rather than
silently ignored.

Key settings:

| Key | Meaning |
|---|---|
| `mode` | `"test"` (exactly `test_characters`) or `"official"` (full supported set) |
| `profile` | Learner profile code, e.g. `"vi"` or `"en"` |
| `test_characters` | Exactly 10 single characters, duplicate-free, all in the stroke dataset |
| `generation.text.mode` | `"endpoint"` (default) or `"local"` |
| `generation.text.local` | `model_id`, `revision`, `device`, `dtype`, `generation` (temperature, max_tokens) |
| `generation.text.endpoint` | `provider`, `base_url`, `model` alias, timeouts, `api_key_env`, generation params |
| `generation.text.prompt` | Paths to `system_file` / `user_file` under `config/prompts/` |
| `audio` | Fixed `{source: "cns11643", preferred_voice: "auto\|male\|female"}` — voice preference only |
| `generation.refresh_ai` | Bypass the text cache and regenerate enrichment |
| `generation.deck_name` / `generation.output` | Anki deck name / `.apkg` path (default `build/taiwan_traditional_chinese.apkg`) |

Legacy top-level `llm.*` values still load and migrate into
`generation.text.endpoint`. Removed TTS/audio-model keys are **rejected with a
migration error**, not silently accepted.

**Text vs. audio configuration** (important): model/backend selection exists
only for *text* enrichment. Audio has no model, provider, endpoint, or runtime
choice — pronunciation comes exclusively from the fixed CNS11643 human-recorded
source, and a missing exact reading yields no audio rather than a substitution.

Authentication for endpoints never lives in config files: `api_key_env` names an
*environment variable* holding the bearer token (empty means no auth header),
and endpoint URLs/keys are never written into manifests or caches. Local
connection details in `config.local.json` likewise never reach published
manifests.

## Prompts

Text-generation prompts live in `config/prompts/` so they can be edited without
modifying Python:

- `config/prompts/text_system.txt` — system prompt; required placeholders:
  `{language_name}`, `{profile_instruction}`, `{max_sayings}`, `{target_char}`.
- `config/prompts/text_user.txt` — user prompt wrapping the authoritative facts;
  required placeholder: `{facts_json}`.

Rendering is strict: missing required placeholders and unknown `{placeholders}`
both fail loudly instead of emitting silently broken prompts. Both text modes
(`local` and `endpoint`) render the same files. The SHA256 of each prompt file
is part of the text cache identity, so editing either file automatically
invalidates cached enrichment. (There is no audio prompt file — nothing
consumes one.)

## Target-Language Profiles

Profiles live in `config/profiles/`; the shipped profiles are exactly:

- `config/profiles/vi.json` — Vietnamese (`show_han_viet: true`)
- `config/profiles/en.json` — English (`show_han_viet: false`)

A profile defines `language_code` / `language_name` / `native_name`, all card
`labels.*`, `features.*` (`show_han_viet`, `notable_sayings_max_items` 0–3),
and `llm.instruction` (merged into the system prompt). Python never hardcodes
profile text; missing keys fall back to built-in English defaults.

Facts (Pinyin, Zhuyin, radicals, strokes, variants, media) are identical for
every language; only learner-facing enrichment (meaning, explanations,
translations, sayings) is generated per profile and cached per profile under
`data/processed/llm/<lang>/`. Select with `"profile"` in config or
`--profile vi|en`. To add a language, copy `vi.json`, translate labels and
`llm.instruction`, set `features`, and run test mode first. The project is not
Vietnamese-only — Vietnamese is one profile among many.

## Downloading Reference Data

`download.py` bootstraps `data/` from an empty checkout. It fetches **reference
data only**: no LLM calls, no `.apkg`, no AI model weights, no audio synthesis.

```bash
python download.py                 # full clean-v1 acquisition
python download.py --stroke-limit 10   # crawl only 10 stroke pages (test)
python download.py --skip-png          # omit the 6063 PNG fallback
python download.py --skip-stroke-xml   # fetch archives only, no per-char crawl
```

What it downloads (clean-v1 default):

1. **MOE Taiwan stroke-order** — embed catalog CSV, per-character XML
   (`dictFrame.jsp` crawl with resume), JS player, optional PNG fallback.
2. **Unicode 17.0.0** — `Unihan.zip` + `CJKRadicals.txt` (version-pinned;
   whole archives only, never partial).
3. **CNS11643 pronunciation** — official listing/release files, `Voice.zip`
   (nested `male.zip`/`female.zip` + voice readme), `Properties.zip`
   (`CNS_phonetic.txt`), `MapingTables.zip` (CNS→Unicode tables); then builds
   `data/processed/cns11643/cns_index.json` automatically (skipped when inputs
   are unchanged).

Behavior is idempotent: complete files are skipped, partial `.part` downloads
resume via HTTP Range where supported, completed extractions are skipped via an
`.extracted.ok` marker (interrupted extractions recover without rewriting valid
files), and unchanged CNS inputs skip index rebuilds. `--force` explicitly
re-acquires and rebuilds.

Additional flags:

```bash
python download.py --force              # re-download + clear stale markers
python download.py --ignore-env-proxy   # ignore HTTP(S)_PROXY env vars
python download.py --insecure           # disable TLS verification (fallback only)
```

`--insecure` exists because the MOE and CNS11643 hosts use a TWCA chain rejected
by modern OpenSSL. It is a workaround, not the preferred posture — use it only
if you trust the host. Legacy opt-ins (`--include-legacy-dict`,
`--include-legacy-external`, deprecated `--include-legacy-audio`) fetch rollback
data that clean generation never uses.

## Generating an Anki Deck

`generate.py` normalizes facts, enriches them with the configured text backend,
resolves CNS audio, builds card records, packages the `.apkg`, and publishes
the reference snapshot.

```bash
python generate.py                    # test mode by default (config test_characters)
python generate.py --mode official     # full supported set
python generate.py --profile en        # target-language override
python generate.py --refresh-ai        # bypass text cache, regenerate enrichment
python generate.py --no-ai             # debug: skip LLM, minimal enrichment fields
python generate.py --chars 思學         # debug override: raw characters, deduped
python generate.py --test-chars 思,八,乾  # debug override: validated list, any length >= 1
python generate.py --limit 50          # debug cap (applies in official mode)
python generate.py --output build/custom.apkg --deck-name "My Deck"
python generate.py --llm-base-url http://127.0.0.1:8080/v1 --llm-model my-alias
```

**Test mode** processes exactly the configured `test_characters` (10 entries,
order-preserving, each validated and required to exist in the stroke dataset);
the manifest records `generation_mode=test, complete=false`. **Official mode**
(`"mode": "official"` or `--mode official`) processes the full supported
stroke set; the manifest records `generation_mode=official` (`complete=false`
if any per-character errors occurred — see `build/reports/generation_errors.json`).

`--refresh-ai` regenerates *AI enrichment only* (it reuses reference downloads;
to re-acquire sources use `download.py --force`). Outputs: the `.apkg`
(default `build/taiwan_traditional_chinese.apkg`), normalized records in
`data/processed/cards/`, the snapshot in `data/published/reference-v1/`, and
reports (`build_summary.json`, `audio_missing.json`, `generation_errors.json`).
Re-running without `--refresh-ai` reuses cached enrichment.

## End-to-End Usage

```bash
git clone <this-repository> && cd anki-database
python -m venv .venv
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# Linux/macOS:
# source .venv/bin/activate
pip install genanki requests openpyxl
cp config/config.example.json config/config.local.json
# Edit config.local.json: mode, profile, test_characters,
# generation.text (local model or endpoint base_url/model)
python download.py --stroke-limit 10   # small first acquisition; drop the flag for full
python generate.py                     # 10-character test deck
python validate_reference.py           # offline check of data/published/reference-v1
# Inspect build/taiwan_traditional_chinese.apkg + build/reports/
# Then scale up:
python download.py                     # full reference data
python generate.py --mode official     # full deck (requires the text backend)
python validate_reference.py --check-cns-inputs
```

Run the mocked test suites anytime (no network, no downloads, no synthesis):

```bash
python tests/test_cns_audio.py
python tests/test_download_cns.py
```

`--check-cns-inputs` additionally validates local CNS raw/processed inputs, and
`--check-audio-sha256` verifies every audio file referenced by the CNS index
(slow on full datasets; existence is always checked).

## Data Pipeline

```text
Official reference sources
  ├── Unicode 17.0.0 (Unihan + CJKRadicals) → factual text
  ├── MOE Taiwan (stroke XML + JS player)   → stroke geometry
  └── CNS11643 / 全字庫 (Voice/Properties/Mapping) → pronunciation index
          │
          ▼
Deterministic normalization (UnihanIndex, pinyin→zhuyin, stroke catalog)
          │
          ▼
Factual character records (+ provenance)
          │
          ├── Configurable LLM enrichment (per-profile, cached, validated)
          │
          ▼
Normalized cards (data/processed/cards/U+*.json)
          │
          ├── Frozen Anki template + profile labels → .apkg (genanki)
          │
          ▼
Published snapshot (data/published/reference-v1) → offline validator
```

Deterministic processing (parsing, romanization conversion, audio lookup,
packaging, hashing) is kept separate from AI-generated fields (meaning,
structure notes, component glosses, examples, sayings), which always carry
their model and prompt-version provenance.

## CNS11643 Audio

Character pronunciation comes from CNS11643 / 全字庫 human-recorded Taiwan
Mandarin clips. The processed index maps `(character, normalized Zhuyin)` to a
recording; `generate.py` never sees archive internals — it performs one dict
lookup per card via `CharacterAudioResolver` (index loaded once per run).

- Match key is the normalized expected Zhuyin (pipeline first-tone `ˉ` maps to
  the CNS unmarked form; neutral-tone leading `˙` is preserved); Pinyin is
  secondary metadata only.
- Polyphonic characters resolve per reading; a missing reading returns
  `unavailable` — no fallback to another reading, no TTS, no LLM choice.
- `preferred_voice` (`auto | male | female`) selects among same-reading male /
  female recordings when voice metadata exists; otherwise the first sorted
  record wins deterministically.
- Original CNS bytes are copied without transcoding
  (`build/media/cns_U<UCS>_<zhuyin8>.<ext>`, one `[sound:]` tag) and published
  under `published/audio/` with SHA256 recorded.
- Unavailable audio (no index, missing reading, absent file) renders no
  `[sound:]` and is listed in `build/reports/audio_missing.json`; one missing
  record never aborts the run.

## Data Sources and Attribution

Summary of roles and terms (details govern — see links):

| Source | Role | Terms note |
|---|---|---|
| MOE Taiwan stroke-order | Geometry + player | CC BY-NC-ND 3.0 TW, attribution 中華民國教育部 — [licenses/MOE_STROKE.md](licenses/MOE_STROKE.md) |
| CNS11643 / 全字庫 | Pronunciation audio | Dataset-level Open Government Data License v1.0; audio-specific terms need verification — [licenses/CNS11643.md](licenses/CNS11643.md) |
| Unicode Unihan | Factual text | Unicode terms need verification — [licenses/UNICODE.md](licenses/UNICODE.md) |
| MOE pronunciation recordings | **Removed / legacy** | Deprecated rollback only — [licenses/MOE_AUDIO.md](licenses/MOE_AUDIO.md) |

For full provenance, acquisition behavior, processing rules, and licensing
notes, see [DATA_SOURCES_AND_PIPELINE.md](DATA_SOURCES_AND_PIPELINE.md),
[licenses/README.md](licenses/README.md), and [data/README.md](data/README.md).

## Disclaimer and Licensing

**A. Project code.** The repository currently contains **no explicit license
file for its own code** (no `LICENSE`, `COPYING`, or license field in package
metadata was found). Until one is added, do not assume MIT/Apache/GPL or any
other terms — all rights remain with the author by default.

**B. Third-party data.** Each source family has its own license and
restrictions (notably MOE's non-commercial, no-derivatives terms). This
repository does not relicense third-party datasets: downloading, embedding
(e.g. stroke XML in cards), redistributing decks or snapshots, and commercial
use must each comply with the original source terms linked above.

**C. AI-generated content.** Meanings, structure notes, component glosses,
examples, translations, and notable sayings may be produced by an LLM. They are
study aids, may contain errors, and must not be treated as authoritative
linguistic, academic, or legal advice.

**D. Pronunciation audio.** Clips are human-recorded CNS11643 originals, copied
without transcoding. Redistribution follows the dataset-level Open Government
Data License v1.0, but audio-specific additional terms are explicitly
unverified — re-check [licenses/CNS11643.md](licenses/CNS11643.md) and the
official dataset page before public redistribution, and preserve source notices
shipped under `data/raw/cns11643/`.

**E. Other sources.** MOE dictionary text/audio, CHISE IDS, and external
Hán-Việt data are legacy/removed and documented only for rollback transparency
(see [DATA_SOURCES_AND_PIPELINE.md](DATA_SOURCES_AND_PIPELINE.md)).

This README is not legal advice.

## Limitations

- Source coverage is incomplete: cards exist only for characters in the MOE
  stroke-order set, and CNS audio is absent for many character–reading pairs
  (by design, silence beats a wrong or synthetic clip).
- AI enrichment quality depends entirely on the configured model/backend; local
  text models may require substantial compute (e.g. GPU VRAM) depending on the
  selected repository.
- Upstream datasets, MOE page markup, and LLM outputs can all drift; pinned
  versions, caches, and immutable snapshots mitigate but do not eliminate this.
- Full legal texts for redistribution are not yet vendored — see the
  `NEEDS VERIFICATION` notes in `licenses/`.

## Reproducibility

- Text, stroke, and audio processing is deterministic (Unihan parsing,
  `pinyin_to_zhuyin`, exact CNS lookup, packaging, hashing).
- Generated enrichment is keyed by a full identity: backend mode/provider,
  model ID or alias, revision, device/dtype, generation params, prompt-file
  content hashes, profile + instruction, and normalized facts.
- Cards carry deterministic GUIDs (`genanki.guid_for("MOE-TRADITIONAL-V1",
  char)`), so regenerating preserves Anki note identity when the field order
  contract is respected.