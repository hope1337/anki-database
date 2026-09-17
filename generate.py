#!/usr/bin/env python3
"""
generate.py

Generate an offline Anki deck for Traditional Chinese using the data layout
created by download.py.

What this script uses
---------------------
Deterministic / source data:
- MOE Taiwan 《國語辭典簡編本》
    * Traditional headword
    * Zhuyin
    * Hanyu Pinyin
    * radical
    * stroke count
    * Chinese definition
- MOE Taiwan stroke-order data
    * official per-character vector XML
    * official JS drawing engine downloaded by download.py
- MOE Taiwan single-character audio
- CHISE IDS
    * modern glyph decomposition / components
- hanviet-pinyin-wordlist
    * Sino-Vietnamese reading keyed by Traditional char + Pinyin
- Unicode Unihan
    * fallback reading / definition only when MOE has no entry

LLM responsibilities:
- translate / condense meaning into Vietnamese
- explain the modern character structure in Vietnamese
- translate component meanings into Vietnamese
- create example sentences in Traditional Chinese + Pinyin + Vietnamese

The LLM is NOT allowed to change source-grounded Pinyin, Zhuyin, Hán-Việt,
radical, IDS, or stroke-order data.

Install:
    pip install genanki requests openpyxl

Typical workflow
----------------
Default production run (all downloaded stroke characters):
    python generate.py

1) Test one character:
    python generate.py \
        --chars 思 \
        --llm-base-url http://100.123.148.6:8080/v1 \
        --llm-model qwen3

2) Generate a few characters:
    python generate.py \
        --chars 思志學心 \
        --llm-base-url http://100.123.148.6:8080/v1

3) Generate the first 100 available stroke characters:
    python generate.py \
        --limit 100 \
        --llm-base-url http://100.123.148.6:8080/v1

4) Generate all available stroke characters:
    python generate.py \
        --all \
        --llm-base-url http://100.123.148.6:8080/v1

Important:
- LLM results are cached in data/processed/llm/.
- Normalized card records are saved in data/processed/cards/.
- Re-running does not call the LLM again unless --refresh-ai is used.
- Audio matching is conservative. A wrong audio clip is worse than no clip.
  Unresolved files are reported in build/audio_missing.json.
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
import requests
from openpyxl import load_workbook


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
MOE_DICT_AUDIO = RAW / "moe_dictionary" / "audio_char"

HANVIET_CSV = RAW / "hanviet" / "hanviet.csv"
CHISE_DIR = RAW / "chise"
UNIHAN_DIR = RAW / "unicode" / "unihan"

LLM_CACHE_DIR = PROCESSED / "llm"
CARD_CACHE_DIR = PROCESSED / "cards"

BUILD = ROOT / "build"
BUILD_MEDIA = BUILD / "media"
BUILD_REPORTS = BUILD / "reports"


# =============================================================================
# Constants
# =============================================================================

PROMPT_VERSION = "anki-zh-tw-v1"

ANKI_MODEL_ID = 1739018113
ANKI_DECK_ID = 2059418113

MOE_ATTRIBUTION = (
    "字形、筆順、字音與辭典資料來源："
    "中華民國教育部（MOE Taiwan）。"
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
# MOE dictionary
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
# Hán-Việt
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
# CHISE IDS
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
# Unihan fallback
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
# Audio matching
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
# Source-grounded character/component information
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
    def __init__(
        self,
        base_url: str,
        model: str,
        timeout: int = 180,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

        # User previously needed trust_env=False because local requests were
        # accidentally routed through an environment proxy.
        self.session = requests.Session()
        self.session.trust_env = False

    @property
    def chat_url(self) -> str:
        if self.base_url.endswith("/v1"):
            return self.base_url + "/chat/completions"
        return self.base_url + "/v1/chat/completions"

    def enrich(
        self,
        grounding: CharacterGrounding,
        *,
        refresh: bool,
    ) -> dict:
        cache_path = (
            LLM_CACHE_DIR
            / f"U+{grounding.ucs}.json"
        )

        facts = {
            "character": grounding.char,
            "primary_reading": asdict(grounding.primary),
            "other_readings": [
                asdict(x) for x in grounding.other_readings
            ],
            "radical": grounding.radical,
            "stroke_count": grounding.stroke_count,
            "ids": grounding.ids,
            "components": [
                asdict(x) for x in grounding.components
            ],
        }

        input_hash = sha256_text(
            PROMPT_VERSION
            + "\n"
            + json.dumps(
                facts,
                ensure_ascii=False,
                sort_keys=True,
            )
        )

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

        system_prompt = """
Bạn là biên tập viên flashcard tiếng Hoa phồn thể dành cho người Việt.

Quy tắc bắt buộc:
1. Luôn dùng chữ PHỒN THỂ ĐÀI LOAN trong câu ví dụ.
2. Dữ liệu trong SOURCE_FACTS là ground truth. Không được sửa Pinyin,
   Zhuyin, Hán-Việt, bộ thủ, IDS hay stroke count.
3. Không bịa nguồn gốc lịch sử của chữ. "structure_explanation_vi" chỉ
   được giải thích CẤU TẠO HÌNH THỂ HIỆN ĐẠI theo IDS và các component
   được cung cấp. Nếu không đủ dữ liệu thì nói ngắn gọn rằng chưa đủ dữ
   liệu để kết luận.
4. meaning_vi phải là nghĩa tiếng Việt tự nhiên, ngắn, hữu dụng cho
   người học; dựa trên definition_zh được cung cấp.
5. component_meanings_vi chỉ dịch/diễn giải nghĩa của component đã được
   cung cấp. Không thay đổi âm đọc.
6. Tạo 2 câu ví dụ tự nhiên ở mức A2-B1, ưu tiên cách dùng thông dụng ở
   Đài Loan. Mỗi ví dụ phải có:
   - zh: tiếng Hoa phồn thể
   - pinyin: Hanyu Pinyin có dấu thanh
   - vi: bản dịch tiếng Việt tự nhiên
7. Chỉ trả JSON hợp lệ. Không Markdown, không giải thích ngoài JSON.

Schema:
{
  "meaning_vi": "...",
  "structure_explanation_vi": "...",
  "component_meanings_vi": {
    "心": "...",
    "田": "..."
  },
  "examples": [
    {"zh": "...", "pinyin": "...", "vi": "..."},
    {"zh": "...", "pinyin": "...", "vi": "..."}
  ]
}
""".strip()

        user_prompt = (
            "SOURCE_FACTS:\n"
            + json.dumps(
                facts,
                ensure_ascii=False,
                indent=2,
            )
        )

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": user_prompt,
                },
            ],
            "temperature": 0.2,
            "max_tokens": 900,
            "response_format": {
                "type": "json_object",
            },
        }

        print(
            f"[llm] {grounding.char} -> {self.chat_url}"
        )

        response = self.session.post(
            self.chat_url,
            json=payload,
            timeout=self.timeout,
        )

        # Some llama.cpp builds may reject response_format.
        if response.status_code >= 400:
            payload.pop("response_format", None)
            response = self.session.post(
                self.chat_url,
                json=payload,
                timeout=self.timeout,
            )

        response.raise_for_status()

        raw = response.json()
        content = (
            raw["choices"][0]["message"]["content"]
        )

        result = json_from_model_text(content)
        result = self._validate_result(result)

        write_json(
            cache_path,
            {
                "prompt_version": PROMPT_VERSION,
                "input_hash": input_hash,
                "facts": facts,
                "result": result,
            },
        )

        return result

    @staticmethod
    def _validate_result(result: dict) -> dict:
        meaning_vi = nfc(result.get("meaning_vi"))
        structure = nfc(result.get("structure_explanation_vi"))

        component_meanings = result.get(
            "component_meanings_vi",
            {},
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
            vi = nfc(item.get("vi"))

            if zh and py and vi:
                valid_examples.append({
                    "zh": zh,
                    "pinyin": py,
                    "vi": vi,
                })

        return {
            "meaning_vi": meaning_vi,
            "structure_explanation_vi": structure,
            "component_meanings_vi": {
                nfc(k): nfc(v)
                for k, v in component_meanings.items()
                if nfc(k)
            },
            "examples": valid_examples,
        }


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

    setTimeout(initMoeStroke, 0);
})();
</script>
"""


# =============================================================================
# HTML builders
# =============================================================================

def readings_html(
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


def components_html(
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


def examples_html(examples: list[dict]) -> str:
    blocks = []

    for example in examples:
        blocks.append(
            '<div class="example">'
            f'<div class="example-zh">{escape(example.get("zh"))}</div>'
            f'<div class="example-pinyin">{escape(example.get("pinyin"))}</div>'
            f'<div class="example-vi">{escape(example.get("vi"))}</div>'
            "</div>"
        )

    if not blocks:
        return '<div class="muted">Chưa có ví dụ.</div>'

    return "".join(blocks)


# =============================================================================
# Build normalized card records
# =============================================================================

def build_card_record(
    grounding: CharacterGrounding,
    enrichment: dict,
    audio_info: dict,
    audio_filename: str,
) -> dict:
    components = []

    component_meanings = enrichment.get(
        "component_meanings_vi",
        {},
    )

    for c in grounding.components:
        components.append({
            **asdict(c),
            "meaning_vi": (
                component_meanings.get(c.char)
                or component_meanings.get(c.token)
                or ""
            ),
        })

    return {
        "char": grounding.char,
        "ucs": grounding.ucs,
        "ids": grounding.ids,
        "radical": grounding.radical,
        "stroke_count": grounding.stroke_count,
        "primary": asdict(grounding.primary),
        "other_readings": [
            asdict(x)
            for x in grounding.other_readings
        ],
        "meaning_vi": enrichment.get(
            "meaning_vi",
            "",
        ),
        "structure_explanation_vi": enrichment.get(
            "structure_explanation_vi",
            "",
        ),
        "components": components,
        "examples": enrichment.get("examples", []),
        "stroke_xml_path": grounding.stroke_xml_path,
        "audio_filename": audio_filename,
        "audio_match": audio_info,
    }


# =============================================================================
# Anki model / package
# =============================================================================

def build_anki_model() -> genanki.Model:
    return genanki.Model(
        ANKI_MODEL_ID,
        "Taiwan Traditional Chinese - Offline MOE",

        fields=[
            {"name": "Hanzi"},
            {"name": "Zhuyin"},
            {"name": "Pinyin"},
            {"name": "HanViet"},
            {"name": "MeaningVN"},
            {"name": "MOEDefinitionZH"},
            {"name": "IDS"},
            {"name": "StructureExplanation"},
            {"name": "ComponentsHTML"},
            {"name": "OtherReadingsHTML"},
            {"name": "StrokeDataB64"},
            {"name": "Audio"},
            {"name": "ExamplesHTML"},
            {"name": "SourceNote"},
        ],

        templates=[
            {
                "name": "Recognition",
                "qfmt": """
<div class="front-hanzi">{{Hanzi}}</div>
""",
                "afmt": f"""
{{{{FrontSide}}}}

<hr>

<div class="reading-line">
    <span class="pinyin">{{{{Pinyin}}}}</span>
    <span class="dot">·</span>
    <span class="zhuyin">{{{{Zhuyin}}}}</span>
</div>

<div class="hanviet">
    Hán-Việt: {{{{HanViet}}}}
</div>

<div class="audio-box">
    {{{{Audio}}}}
</div>

<div class="meaning">
    {{{{MeaningVN}}}}
</div>

<div class="moe-definition">
    <span class="label">MOE:</span>
    {{{{MOEDefinitionZH}}}}
</div>

{{{{OtherReadingsHTML}}}}

<hr>

<div class="section-title">Cấu tạo chữ</div>

<div class="ids">
    <span class="label">IDS:</span>
    {{{{IDS}}}}
</div>

<div class="structure-explanation">
    {{{{StructureExplanation}}}}
</div>

{{{{ComponentsHTML}}}}

<div class="section-title">Thứ tự nét</div>

<div class="moe-player">
    <svg
        id="moe-svg"
        xmlns="http://www.w3.org/2000/svg">
    </svg>

    <div class="moe-controls">
        <button onclick="moeReplay()">↻ Replay</button>
        <button onclick="moePause()">⏸ Pause</button>
        <button onclick="moeResume()">▶ Play</button>
        <button onclick="moeNext()">→ Next stroke</button>
        <button onclick="moeGrid()"># Grid</button>
    </div>
</div>

<div id="moe-stroke-data" style="display:none">
    {{{{StrokeDataB64}}}}
</div>

{PLAYER_JS}

<hr>

<div class="section-title">Ví dụ</div>
{{{{ExamplesHTML}}}}

<hr>

<div class="source-note">
    {{{{SourceNote}}}}
</div>
""",
            },
        ],

        css="""
.card {
    font-family:
        Arial,
        "Noto Sans CJK TC",
        "PingFang TC",
        sans-serif;
    text-align: center;
    font-size: 19px;
    line-height: 1.5;
}

.front-hanzi {
    font-size: 104px;
    margin: 18px;
}

.reading-line {
    font-size: 29px;
    margin: 10px;
}

.dot {
    color: #888;
    margin: 0 8px;
}

.hanviet {
    font-size: 24px;
    font-weight: 700;
    margin: 8px;
}

.audio-box {
    margin: 8px auto;
}

.meaning {
    font-size: 25px;
    font-weight: 600;
    margin: 14px auto;
}

.moe-definition {
    max-width: 720px;
    margin: 10px auto;
    font-size: 17px;
}

.section-title {
    font-size: 24px;
    font-weight: 700;
    margin: 24px 0 10px;
}

.label {
    font-weight: 700;
}

.ids {
    font-size: 22px;
    margin: 6px;
}

.structure-explanation {
    max-width: 720px;
    margin: 10px auto 16px;
}

.info-table {
    border-collapse: collapse;
    margin: 12px auto 20px;
    max-width: 820px;
    width: 100%;
}

.info-table th,
.info-table td {
    border: 1px solid #aaa;
    padding: 7px 9px;
    vertical-align: middle;
}

.component-char {
    font-size: 30px;
}

.moe-player {
    max-width: 440px;
    margin: 0 auto;
}

#moe-svg {
    display: block;
    margin: 15px auto;
    border: 1px solid #aaa;
    border-radius: 8px;
    background: white;
    overflow: hidden;
}

#moe-svg > svg {
    width: 100% !important;
    height: 100% !important;
}

.moe-controls {
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    gap: 7px;
    margin-top: 10px;
}

.moe-controls button {
    padding: 7px 10px;
    font-size: 14px;
}

.example {
    max-width: 720px;
    margin: 18px auto;
}

.example-zh {
    font-size: 29px;
}

.example-pinyin {
    color: #666;
    margin-top: 4px;
}

.example-vi {
    margin-top: 4px;
}

.source-note,
.muted {
    color: #777;
    font-size: 13px;
}

hr {
    margin: 24px 0;
}
"""
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


def copy_audio_for_anki(
    source: Path,
    ucs: str,
    index: int = 1,
) -> Path:
    ext = source.suffix.lower()
    dest = (
        BUILD_MEDIA
        / f"moe_audio_U{ucs}_{index}{ext}"
    )
    shutil.copy2(source, dest)
    return dest


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
) -> list[str]:
    # Production default: generate ALL characters for which download.py
    # successfully prepared offline MOE stroke XML.
    #
    # Use --chars for an explicit subset, or --limit N for a short test run.
    if args.chars:
        chars = parse_chars_arg(args.chars)
    else:
        chars = list(stroke_index.ordered_chars)

    if args.limit > 0:
        chars = chars[:args.limit]

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
        help="Limit selected characters. 0 = no limit.",
    )

    parser.add_argument(
        "--llm-base-url",
        default="http://100.123.148.6:8080/v1",
        help="llama.cpp OpenAI-compatible base URL.",
    )
    parser.add_argument(
        "--llm-model",
        default="qwen3",
        help="Model/alias exposed by llama-server.",
    )
    parser.add_argument(
        "--refresh-ai",
        action="store_true",
        help="Ignore LLM cache and regenerate enrichment.",
    )
    parser.add_argument(
        "--no-ai",
        action="store_true",
        help="Debug only: do not call LLM; Vietnamese fields stay minimal.",
    )

    parser.add_argument(
        "--deck-name",
        default="Taiwan Traditional Chinese - MOE",
    )
    parser.add_argument(
        "--output",
        default=str(BUILD / "taiwan_traditional_chinese.apkg"),
    )

    args = parser.parse_args()

    ensure_dirs()

    print("==============================================")
    print("Load deterministic sources")
    print("==============================================")

    dictionary = MoeDictionary(MOE_DICT_TEXT)
    hanviet = HanVietIndex(HANVIET_CSV)
    chise = ChiseIndex(CHISE_DIR)
    unihan = UnihanFallback(UNIHAN_DIR)
    strokes = StrokeIndex()
    audio = AudioResolver(MOE_DICT_AUDIO)

    grounder = Grounder(
        dictionary=dictionary,
        hanviet=hanviet,
        chise=chise,
        unihan=unihan,
        strokes=strokes,
    )

    llm = None
    if not args.no_ai:
        llm = LLMClient(
            base_url=args.llm_base_url,
            model=args.llm_model,
        )

    chars = select_characters(strokes, args)

    selection_mode = (
        f"explicit --chars ({len(chars)})"
        if args.chars
        else "all downloaded stroke characters"
    )

    print()
    print("==============================================")
    print(f"Selection mode     : {selection_mode}")
    print(f"Selected characters: {len(chars)}")
    print("==============================================")
    print("".join(chars[:100]))
    if len(chars) > 100:
        print("...")

    model = build_anki_model()
    deck = genanki.Deck(
        ANKI_DECK_ID,
        args.deck_name,
    )

    media_files: list[Path] = prepare_support_media()

    audio_missing = []
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

            if args.no_ai:
                enrichment = {
                    "meaning_vi": "",
                    "structure_explanation_vi": "",
                    "component_meanings_vi": {},
                    "examples": [],
                }
            else:
                assert llm is not None
                enrichment = llm.enrich(
                    grounding,
                    refresh=args.refresh_ai,
                )

            # -------------------------------------------------------------
            # Audio: primary MOE dictionary reading only.
            # -------------------------------------------------------------

            source_audio = None
            audio_info = {
                "reason": "no_moe_dictionary_entry"
            }

            primary_entry = dictionary.primary(char)
            if primary_entry is not None:
                source_audio, audio_info = audio.resolve(
                    primary_entry
                )

            audio_field = ""
            audio_filename = ""

            if source_audio is not None:
                anki_audio = copy_audio_for_anki(
                    source_audio,
                    grounding.ucs,
                )
                media_files.append(anki_audio)
                audio_filename = anki_audio.name
                audio_field = (
                    f"[sound:{anki_audio.name}]"
                )
                print(
                    f"[audio] {source_audio.name} "
                    f"-> {anki_audio.name}"
                )
            else:
                audio_missing.append({
                    "char": char,
                    "ucs": grounding.ucs,
                    "pinyin": grounding.primary.pinyin,
                    "word_id": grounding.primary.word_id,
                    "match": audio_info,
                })
                print(
                    f"[audio] WARNING: unresolved for {char}: "
                    f"{audio_info.get('reason')}"
                )

            # -------------------------------------------------------------
            # Stroke XML is embedded directly into the note as Base64.
            # -------------------------------------------------------------

            xml_path = Path(grounding.stroke_xml_path)
            xml_text = xml_path.read_text(
                encoding="utf-8"
            )
            stroke_b64 = base64.b64encode(
                xml_text.encode("utf-8")
            ).decode("ascii")

            # -------------------------------------------------------------
            # Render HTML fields
            # -------------------------------------------------------------

            component_html = components_html(
                grounding,
                enrichment,
            )

            other_readings = readings_html(
                grounding.primary,
                grounding.other_readings,
            )

            ex_html = examples_html(
                enrichment.get("examples", [])
            )

            note = genanki.Note(
                model=model,
                guid=genanki.guid_for(
                    "MOE-TRADITIONAL-V1",
                    char,
                ),
                fields=[
                    char,
                    grounding.primary.bopomofo,
                    grounding.primary.pinyin,
                    grounding.primary.han_viet,
                    enrichment.get("meaning_vi", ""),
                    grounding.primary.definition_zh,
                    grounding.ids,
                    enrichment.get(
                        "structure_explanation_vi",
                        "",
                    ),
                    component_html,
                    other_readings,
                    stroke_b64,
                    audio_field,
                    ex_html,
                    MOE_ATTRIBUTION,
                ],
            )

            deck.add_note(note)

            card_record = build_card_record(
                grounding,
                enrichment,
                audio_info,
                audio_filename,
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

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    package = genanki.Package(deck)
    package.media_files = [
        str(p)
        for p in unique_media
    ]

    package.write_to_file(str(output))

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
            "errors": len(generation_errors),
            "audio_missing": len(audio_missing),
            "output": str(output),
            "llm_base_url": (
                None if args.no_ai
                else args.llm_base_url
            ),
            "llm_model": (
                None if args.no_ai
                else args.llm_model
            ),
            "prompt_version": PROMPT_VERSION,
        },
    )

    print()
    print("==============================================")
    print("DONE")
    print("==============================================")
    print(f"Requested      : {len(chars)}")
    print(f"Generated      : {generated}")
    print(f"Errors         : {len(generation_errors)}")
    print(f"Audio missing  : {len(audio_missing)}")
    print(f"Output         : {output}")
    print(
        f"Reports        : {BUILD_REPORTS}"
    )
    print(
        f"Normalized JSON: {CARD_CACHE_DIR}"
    )
    print("==============================================")


if __name__ == "__main__":
    main()