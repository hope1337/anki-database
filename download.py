#!/usr/bin/env python3
"""
download.py

Bootstrap data for the Traditional-Chinese Anki project from an empty
data/ directory.

CLEAN-v1 DEFAULT PIPELINE (what `python download.py` fetches):
  1) MOE Taiwan stroke-order source (embed CSV + per-char XML + JS player
     + optional 6063 PNG fallback)
  2) Unicode Unihan (VERSION-PINNED, see UNICODE_VERSION below) + CJKRadicals
  3) CNS11643 human pronunciation audio + pronunciation index metadata
     (fixed reference source; verified official contract, see
     CNS11643_RESOURCES — listing/release/Properties/Mapping/Voice)

No synthesis happens here: pronunciation audio is human-recorded CNS11643
source media, copied unchanged into cards at generate.py time. The old
MOE single-character recordings were removed from the clean pipeline
because clips regularly contain more than the isolated target character.

The clean-v1 pipeline does NOT download by default:
  - MOE dictionary text/definitions (legacy, opt-in via --include-legacy-dict)
  - MOE pronunciation audio ZIP + manifest (legacy, opt-in via
    --include-legacy-audio; deprecated, kept only for rollback comparison)
  - Han-Viet CSV / CHISE IDS (legacy, opt-in via --include-legacy-external)

Legacy download code is retained in a clearly marked LEGACY section so
existing on-disk data is not orphaned and rollback stays possible.

GRANULARITY NOTE:
  Upstream archives (Unihan.zip) are distributed ONLY as whole archives.
  There is no partial download: test mode still downloads the full
  archive, it only limits per-character work (stroke XML crawl).
  Download granularity != generation granularity.

Cài dependency:
    pip install requests

Chạy full clean-v1:
    python download.py

Test 10 chữ stroke trước:
    python download.py --stroke-limit 10

Không tải PNG fallback:
    python download.py --skip-png

Chỉ download raw source, chưa crawl 6063 XML:
    python download.py --skip-stroke-xml

Legacy opt-in (NOT part of clean-v1):
    python download.py --include-legacy-dict --include-legacy-external
    python download.py --include-legacy-audio  # deprecated MOE recordings
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html as html_lib
import io
import json
import re
import shutil
import time
import zipfile
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# =============================================================================
# Project paths
# =============================================================================

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

RAW = DATA / "raw"
PROCESSED = DATA / "processed"
MANIFESTS = DATA / "manifests"

MOE_STROKE = RAW / "moe_stroke"
MOE_STROKE_ARCHIVES = MOE_STROKE / "archives"
MOE_STROKE_META = MOE_STROKE / "meta"
MOE_STROKE_PLAYER = MOE_STROKE / "player"
MOE_STROKE_XML = MOE_STROKE / "xml"
MOE_STROKE_PNG = MOE_STROKE / "png"
MOE_STROKE_FAILED = MOE_STROKE / "failed_pages"

MOE_DICT = RAW / "moe_dictionary"
MOE_DICT_ARCHIVES = MOE_DICT / "archives"
MOE_DICT_TEXT = MOE_DICT / "text"
MOE_DICT_AUDIO = MOE_DICT / "audio_char"

HANVIET_DIR = RAW / "hanviet"
CHISE_DIR = RAW / "chise"
UNICODE_DIR = RAW / "unicode"
UNICODE_ARCHIVES = UNICODE_DIR / "archives"
UNICODE_UNIHAN = UNICODE_DIR / "unihan"

INDEX_DIR = PROCESSED / "indexes"

ALL_DIRS = [
    DATA,
    RAW,
    PROCESSED,
    MANIFESTS,
    MOE_STROKE,
    MOE_STROKE_ARCHIVES,
    MOE_STROKE_META,
    MOE_STROKE_PLAYER,
    MOE_STROKE_XML,
    MOE_STROKE_PNG,
    MOE_STROKE_FAILED,
    MOE_DICT,
    MOE_DICT_ARCHIVES,
    MOE_DICT_TEXT,
    MOE_DICT_AUDIO,
    HANVIET_DIR,
    CHISE_DIR,
    UNICODE_DIR,
    UNICODE_ARCHIVES,
    UNICODE_UNIHAN,
    INDEX_DIR,
]

# CNS11643 human pronunciation audio (fixed reference source; see the
# CNS11643_RESOURCES block below). Originals stay immutable under raw/.
CNS11643_DIR = RAW / "cns11643"
CNS11643_ARCHIVES = CNS11643_DIR / "archives"
CNS11643_AUDIO = CNS11643_DIR / "audio"
CNS11643_INDEX_OUT = PROCESSED / "cns11643" / "cns_index.json"

ALL_DIRS.extend([
    CNS11643_DIR,
    CNS11643_ARCHIVES,
    CNS11643_AUDIO,
    CNS11643_INDEX_OUT.parent,
])


# =============================================================================
# Source pages / stable sources
# =============================================================================

MOE_STROKE_BASE = "https://stroke-order.learningweb.moe.edu.tw"
MOE_STROKE_RESOURCE_PAGE = (
    "https://stroke-order.learningweb.moe.edu.tw/resource.jsp?ID=1"
)

MOE_DICT_DOWNLOAD_PAGE = (
    "https://language.moe.gov.tw/001/Upload/Files/site_content/"
    "M0001/respub/dict_concised_download.html"
)

HANVIET_URL = (
    "https://raw.githubusercontent.com/ph0ngp/"
    "hanviet-pinyin-wordlist/main/hanviet.csv"
)

CHISE_BASIC_URL = (
    "https://raw.githubusercontent.com/chise/ids/main/IDS-UCS-Basic.txt"
)
CHISE_EXT_A_URL = (
    "https://raw.githubusercontent.com/chise/ids/main/IDS-UCS-Ext-A.txt"
)

# =============================================================================
# Unicode version pin (clean-v1 reproducibility contract).
# -----------------------------------------------------------------------------
# The ORIGINAL version of this script used unversioned "latest" URLs:
#     https://www.unicode.org/Public/UCD/latest/ucd/Unihan.zip
# clean-v1 pins an explicit version instead. NEVER silently switch this to
# "latest": bump deliberately and record the new version + hashes in the
# manifest + DATA_SOURCES_AND_PIPELINE.md.
# =============================================================================

UNICODE_VERSION = "17.0.0"
UNICODE_BASE_URL = f"https://www.unicode.org/Public/{UNICODE_VERSION}/ucd"
UNIHAN_URL = f"{UNICODE_BASE_URL}/Unihan.zip"
CJK_RADICALS_URL = f"{UNICODE_BASE_URL}/CJKRadicals.txt"

# Direct SVG POC hiện tại chỉ cần 3 file này.
MOE_PLAYER_ASSETS = {
    f"{MOE_STROKE_BASE}/js/jquery-3.7.1.min.js":
        "jquery-3.7.1.min.js",
    f"{MOE_STROKE_BASE}/js/jquery.svg.js":
        "jquery.svg.js",
    f"{MOE_STROKE_BASE}/js/stroke.js":
        "stroke.js",
}


# =============================================================================
# CNS11643 human pronunciation audio (fixed reference source)
# -----------------------------------------------------------------------------
# Verified official contract (dataset page https://data.gov.tw/dataset/5961,
# Ministry of Digital Affairs; listing + release notes + archives fetched
# live during verification — see FINAL REPORT):
#   base      https://www.cns11643.gov.tw/opendata/
#   listing   OpenDataFilesList.csv (Big5; columns 名稱,所屬,類別,說明)
#   release   release.txt (per-file 版本 + 下載路徑 + changelog)
#   Voice.zip (58,524,582 B) -> female.zip + male.zip
#             + 全字庫聲音檔說明文件.txt (UTF-8-SIG; documents F/M +
#             漢拼 filenames, mp3 type, and the bopomofo->hanpin table)
#     female.zip -> F_<hanpin>.mp3 (female); male.zip -> M_<hanpin>.mp3
#     (underscore on disk; neutral-tone <hanpin>5 clips are .wav)
#   Properties.zip (3,158,491 B) -> CNS_phonetic.txt (UTF-8-SIG,
#     `<CNS-code>\\tbopomofo`; first tone unmarked; neutral leading ˙)
#   MapingTables.zip (809,868 B) -> Unicode/CNS2UNICODE_Unicode {BMP,2,3,15}
#     .txt (UTF-8-SIG, `<CNS-code>\\tHEX-Unicode`)
# License (dataset-level, VERIFIED): 政府資料開放授權條款第1版
# (Open Government Data License v1.0), free. Audio-specific separate
# terms: none found — see licenses/CNS11643.md.
# NOTE: cns11643.gov.tw uses a TWCA chain rejected by modern OpenSSL
# (same as the MOE host): use --insecure if you trust the host.
# =============================================================================

CNS11643_HOMEPAGE = "https://www.cns11643.gov.tw"
CNS11643_DATASET_PAGE = "https://data.gov.tw/dataset/5961"
CNS11643_BASE_URL = "https://www.cns11643.gov.tw/opendata"
CNS11643_LISTING_URL = f"{CNS11643_BASE_URL}/OpenDataFilesList.csv"
CNS11643_RELEASE_URL = f"{CNS11643_BASE_URL}/release.txt"


@dataclass
class CnsResource:
    """One CNS11643 downloadable (verified fields only)."""

    key: str               # stable id used in reports/manifest
    url: str               # official file URL (stable opendata path)
    filename: str          # archive name (from the official listing)
    extract_to: str | None  # raw-relative dest dir for archives; None = file
    nested_zips: bool = False  # also extract *.zip members found inside
    note: str = ""         # operator guidance


CNS11643_RESOURCES: list[CnsResource] = [
    CnsResource(
        key="listing",
        url=CNS11643_LISTING_URL,
        filename="OpenDataFilesList.csv",
        extract_to=None,
        note="Official file listing (Big5 CSV); parsed for versions.",
    ),
    CnsResource(
        key="release",
        url=CNS11643_RELEASE_URL,
        filename="release.txt",
        extract_to=None,
        note="Per-file versions + changelog (UTF-8).",
    ),
    CnsResource(
        key="properties",
        url=f"{CNS11643_BASE_URL}/Properties.zip",
        filename="Properties.zip",
        extract_to="properties",
        note="Attribute data incl. CNS_phonetic.txt (code->bopomofo).",
    ),
    CnsResource(
        key="mapping",
        url=f"{CNS11643_BASE_URL}/MapingTables.zip",
        filename="MapingTables.zip",
        extract_to="mapping",
        note="CNS->Unicode tables incl. CNS2UNICODE_Unicode {BMP,2,3,15}.",
    ),
    CnsResource(
        key="voice",
        url=f"{CNS11643_BASE_URL}/Voice.zip",
        filename="Voice.zip",
        extract_to="audio",
        nested_zips=True,
        note="male.zip (M_<hanpin>.mp3) + female.zip (F_<hanpin>.mp3) "
             "+ official voice readme (bopomofo->hanpin table).",
    ),
]


def decode_zip_name(info: zipfile.ZipInfo) -> str:
    """Decode a ZIP member name as the official archives encode it.

    CNS archives use Big5-encoded (non-UTF-8-flag) filenames, e.g. the
    voice readme 全字庫聲音檔說明文件.txt. ASCII names (all audio
    members, mapping tables) pass through unchanged.
    """
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("big5")
    except (UnicodeDecodeError, ValueError):
        return info.filename


def parse_cns_listing(text: str) -> list[dict]:
    """Parse OpenDataFilesList.csv (Big5; 名稱,所屬,類別,說明).

    Returns [{name, parent, kind, note}]. Malformed rows raise
    RuntimeError (official listing shape is verified, not guessed).
    """
    rows: list[dict] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 4 or not parts[0]:
            raise RuntimeError(
                f"OpenDataFilesList.csv malformed row {lineno}: "
                f"{line[:80]!r} (want 名稱,所屬,類別,說明)")
        if parts[0] == "名稱":
            continue  # header row
        rows.append({"name": parts[0], "parent": parts[1],
                     "kind": parts[2], "note": parts[3]})
    return rows


def parse_cns_release_versions(text: str) -> dict[str, str]:
    """Parse release.txt 檔案名稱/版本 blocks -> {filename: version}."""
    versions: dict[str, str] = {}
    name, version = "", ""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("檔案名稱："):
            name = s.split("：", 1)[1].strip()
        elif s.startswith("版本：") and name:
            version = s.split("：", 1)[1].strip()
            versions[name] = version
            name = ""
    return versions


def read_cns_text_file(path: Path) -> str:
    """Read a small official CNS text resource (listing: Big5)."""
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "big5", "cp950"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    raise RuntimeError(f"cannot decode official CNS text file: {path}")


def download_tracked(session: requests.Session, url: str, dest: Path, *,
                     force: bool = False, timeout: int = 120,
                     report: list | None = None,
                     key: str = "") -> str:
    """download_file() + machine-readable status for the CNS report.

    Returns one of: downloaded | resumed | skipped-existing | failed.
    Existing complete files are NEVER re-downloaded (unless force);
    partial .part files resume via HTTP Range (see download_file).
    """
    entry: dict = {"resource": key or dest.name, "status": "",
                   "detail": ""}
    try:
        if dest.exists() and not force:
            entry["status"] = "skipped-existing"
            entry["detail"] = (
                f"{human_bytes(dest.stat().st_size)} already complete")
        else:
            part = dest.with_name(dest.name + ".part")
            resumed = part.exists() and part.stat().st_size > 0 and (
                not force)
            download_file(session, url, dest, force=force,
                          timeout=timeout)
            entry["status"] = "resumed" if resumed else "downloaded"
            entry["detail"] = human_bytes(dest.stat().st_size)
    except Exception as exc:
        entry["status"] = "failed"
        entry["detail"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if report is not None:
            report.append(entry)
    return entry["status"]


def safe_extract_zip_resumable(zip_path: Path, dest: Path) -> dict:
    """Extract a ZIP with skip-if-done + interrupted-extraction recovery.

    Completion marker: ``dest/.extracted.ok`` (JSON: source archive
    name + sha256 + per-member sizes). Behavior:
      - marker present and archive sha256 matches -> already-extracted
        (no files rewritten);
      - otherwise extract only members missing or size-mismatched
        (interrupted runs continue WITHOUT rewriting valid files);
      - marker is written ONLY after successful extraction+validation.
    With --force, callers delete the marker first to force re-extraction.
    Path traversal is rejected like safe_extract_zip. Member names are
    decoded with decode_zip_name() (CNS archives use Big5 filenames).
    """
    dest.mkdir(parents=True, exist_ok=True)
    marker = dest / ".extracted.ok"
    digest = sha256_file(zip_path)
    if marker.exists():
        try:
            marked = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            marked = {}
        if (marked.get("sha256") == digest
                and marked.get("source") == zip_path.name):
            print(f"[extract skip] {zip_path.name} -> {dest}")
            return {"status": "already-extracted", "extracted": 0,
                    "skipped": len(marked.get("members", {})),
                    "dest": str(dest)}
        print(f"[extract resume] {zip_path.name} -> {dest} "
              "(marker stale/missing; verifying members)")
    else:
        print(f"[extract] {zip_path.name} -> {dest}")

    with zipfile.ZipFile(zip_path) as zf:
        root = dest.resolve()
        decoded = [(m, decode_zip_name(m)) for m in zf.infolist()
                   if not m.is_dir()]
        for member, name in decoded:
            out = (dest / name).resolve()
            if root not in out.parents and out != root:
                raise RuntimeError(
                    f"Unsafe ZIP member: {member.filename}"
                )
        extracted = 0
        skipped = 0
        sizes: dict[str, int] = {}
        for member, name in decoded:
            out = dest / name
            if out.exists() and out.stat().st_size == member.file_size:
                skipped += 1
            else:
                out.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, out.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                extracted += 1
            sizes[name] = member.file_size

    marker.write_text(
        json.dumps({
            "source": zip_path.name,
            "sha256": digest,
            "members": sizes,
            "extracted_at": utc_now(),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[extracted] {zip_path.name}: {extracted} new, "
          f"{skipped} already valid")
    return {"status": "extracted", "extracted": extracted,
            "skipped": skipped, "dest": str(dest)}


def _clear_extract_marker(target: Path, force: bool) -> None:
    if force:
        stale = target / ".extracted.ok"
        if stale.exists():
            # Forced archive replacement invalidates the old completion
            # marker; re-extraction revalidates member by member.
            stale.unlink()
            print(f"[extract force] cleared stale marker in {target}")


def _extract_cns_resource(session_report: list, key: str, dest: Path,
                          target: Path, force: bool,
                          failures: list) -> None:
    _clear_extract_marker(target, force)
    try:
        result = safe_extract_zip_resumable(dest, target)
        session_report.append({"resource": key + ":extract",
                               "status": result["status"],
                               "detail": f"{result['extracted']} new, "
                               f"{result['skipped']} already valid"})
    except Exception as exc:
        session_report.append({"resource": key + ":extract",
                               "status": "failed",
                               "detail": f"{type(exc).__name__}: {exc}"})
        failures.append(f"{key}:extract: {exc}")
        raise


def _extract_nested_cns_zips(session_report: list, target: Path,
                             force: bool, failures: list) -> None:
    """Extract nested archives discovered inside an extracted CNS tree.

    Voice.zip ships male.zip/female.zip as members: each discovered
    ``*.zip`` is extracted (resumably) into a same-named subdirectory.
    Discovery is by what actually exists — no filename is hardcoded
    beyond the official listing cross-check below.
    """
    for nested in sorted(target.glob("*.zip")):
        subdir = target / nested.stem
        _clear_extract_marker(subdir, force)
        try:
            result = safe_extract_zip_resumable(nested, subdir)
            session_report.append(
                {"resource": f"nested:{nested.name}",
                 "status": result["status"],
                 "detail": f"{result['extracted']} new, "
                 f"{result['skipped']} already valid"})
        except Exception as exc:
            session_report.append(
                {"resource": f"nested:{nested.name}",
                 "status": "failed",
                 "detail": f"{type(exc).__name__}: {exc}"})
            failures.append(f"nested:{nested.name}: {exc}")
            raise


def download_cns11643(session: requests.Session, *,
                      force: bool = False) -> dict:
    """Acquire CNS11643 pronunciation resources (idempotent).

    Flow: fetch official listing + release notes (small) -> confirm the
    expected members -> download archives (skip/resume) -> extract
    (skip/recover, incl. nested male/female.zip) -> build the processed
    index (skipped when source inputs are unchanged).

    Returns {"records": [...build_file_record...], "report": [...],
             "index": {...}, "versions": {...}}. Verified failures are
    recorded as failed and re-raised after the section summary. An
    unbuildable index yields a warning, never a crash: generation
    continues without audio (see cns_audio.py).
    """
    failures: list[str] = []
    index_info: dict | None = None
    report: list[dict] = []
    records: list[dict] = []
    versions: dict[str, str] = {}

    for res in CNS11643_RESOURCES:
        filename = res.filename or filename_from_url(res.url)
        if res.extract_to is None:
            dest = CNS11643_DIR / filename
        else:
            dest = CNS11643_ARCHIVES / filename
        try:
            download_tracked(
                session, res.url, dest, force=force, timeout=300,
                report=report, key=res.key)
        except Exception as exc:
            failures.append(f"{res.key}: {exc}")
            continue
        records.append(build_file_record(dest, res.url))
        if res.extract_to and dest.suffix.lower() == ".zip":
            target = CNS11643_DIR / res.extract_to
            try:
                _extract_cns_resource(
                    report, res.key, dest, target, force, failures)
                if res.nested_zips:
                    _extract_nested_cns_zips(
                        report, target, force, failures)
            except Exception:
                continue

    # Official listing/release notes double as version source + member
    # cross-check (deterministic discovery from an OFFICIAL source).
    try:
        listing_rows = parse_cns_listing(read_cns_text_file(
            CNS11643_DIR / "OpenDataFilesList.csv"))
        member_names = {r["name"] for r in listing_rows}
        for want in ("Voice.zip", "Properties.zip", "MapingTables.zip",
                     "male.zip", "female.zip", "CNS_phonetic.txt"):
            if want not in member_names:
                print(f"[cns listing] WARNING: expected member "
                      f"{want!r} not in official listing")
        try:
            versions = parse_cns_release_versions(read_cns_text_file(
                CNS11643_DIR / "release.txt"))
        except (OSError, RuntimeError) as exc:
            print(f"[cns versions] WARNING: {exc}")
    except (OSError, RuntimeError) as exc:
        print(f"[cns listing] WARNING: {exc}")

    # Build the processed lookup index — skipped when source inputs are
    # unchanged (hash comparison, no rebuild).
    try:
        from cns_audio import (CNS_INDEX_VERSION, build_cns_index,
                               cns_source_fingerprint)
        fingerprint = cns_source_fingerprint(CNS11643_DIR)
        if (fingerprint is not None and CNS11643_INDEX_OUT.exists()
                and not force):
            try:
                existing = json.loads(
                    CNS11643_INDEX_OUT.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                existing = {}
            if (existing.get("inputs_hash")
                    == fingerprint["inputs_hash"]
                    and existing.get("index_version")
                    == CNS_INDEX_VERSION):
                print(f"[cns index] already-processed "
                      f"({existing.get('record_count', 0)} records; "
                      f"inputs unchanged)")
                report.append({"resource": "cns_index",
                               "status": "already-processed",
                               "detail": f"{existing.get('record_count', 0)} "
                               "records"})
                index_info = {
                    "index": str(CNS11643_INDEX_OUT),
                    "records": existing.get("record_count", 0),
                    "inputs_hash": fingerprint["inputs_hash"],
                    "status": "already-processed",
                }
                records.append(
                    build_file_record(CNS11643_INDEX_OUT, None))
        if index_info is None:
            index_info = build_cns_index(CNS11643_DIR, CNS11643_INDEX_OUT)
            print(f"[cns index] {index_info['records']} records -> "
                  f"{index_info['index']}")
            report.append({"resource": "cns_index",
                           "status": "processed",
                           "detail": f"{index_info['records']} records, "
                           f"{len(index_info.get('skipped', []))} skipped"})
            records.append(build_file_record(CNS11643_INDEX_OUT, None))
    except Exception as exc:
        # Missing inputs or unverifiable layout: warn, keep going.
        # generate.py resolves against the index only when present.
        print(f"[cns index] WARNING: {exc}")
        report.append({"resource": "cns_index", "status": "pending",
                       "detail": str(exc)[:300]})
        index_info = {"index": str(CNS11643_INDEX_OUT), "records": 0,
                      "status": "pending", "detail": str(exc)[:300]}

    if failures:
        raise RuntimeError(
            "CNS11643 failures: " + "; ".join(failures))
    return {"records": records, "report": report, "index": index_info,
            "versions": versions}




# =============================================================================
# Utilities
# =============================================================================

class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        for key, value in attrs:
            if key.lower() == "href" and value:
                self.links.append(value)


def ensure_dirs() -> None:
    for d in ALL_DIRS:
        d.mkdir(parents=True, exist_ok=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def human_bytes(n: int | None) -> str:
    if n is None:
        return "?"
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def filename_from_url(url: str) -> str:
    return unquote(Path(urlparse(url).path).name)


def read_text_auto(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp950", "big5"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    raise RuntimeError(f"Không detect được encoding của {path}")


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def safe_extract_zip(zip_path: Path, dest: Path) -> None:
    """
    Extract ZIP nhưng chặn path traversal.
    """
    dest.mkdir(parents=True, exist_ok=True)
    marker = dest / ".extracted.ok"

    if marker.exists():
        print(f"[extract skip] {zip_path.name} -> {dest}")
        return

    print(f"[extract] {zip_path.name} -> {dest}")

    with zipfile.ZipFile(zip_path) as zf:
        root = dest.resolve()
        for member in zf.infolist():
            out = (dest / member.filename).resolve()
            if root not in out.parents and out != root:
                raise RuntimeError(
                    f"Unsafe ZIP member: {member.filename}"
                )
        zf.extractall(dest)

    marker.write_text(
        f"source={zip_path.name}\n"
        f"sha256={sha256_file(zip_path)}\n"
        f"extracted_at={utc_now()}\n",
        encoding="utf-8",
    )


# =============================================================================
# HTTP
# =============================================================================

def make_session(ignore_env_proxy: bool, verify: bool = True) -> requests.Session:
    s = requests.Session()
    s.trust_env = not ignore_env_proxy
    s.verify = verify
    if not verify:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    retries = Retry(
        total=5,
        connect=5,
        read=5,
        status=5,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        respect_retry_after_header=True,
    )

    adapter = HTTPAdapter(
        max_retries=retries,
        pool_connections=4,
        pool_maxsize=4,
    )
    s.mount("http://", adapter)
    s.mount("https://", adapter)

    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 "
            "Chrome/140 Safari/537.36 "
            "AnkiTraditionalChineseDataBuilder/0.1"
        )
    })
    return s


def get_text(session: requests.Session, url: str, timeout: int = 60) -> str:
    print(f"[GET] {url}")
    try:
        r = session.get(url, timeout=timeout)
    except requests.exceptions.SSLError as exc:
        raise RuntimeError(
            f"SSL verification failed for {url}: {exc}. "
            "The MOE stroke-order host uses a TWCA chain that modern "
            "OpenSSL rejects (root lacks Subject Key Identifier). "
            "If you trust this host, re-run with --insecure."
        ) from exc
    r.raise_for_status()

    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding or "utf-8"

    return r.text


def download_file(
    session: requests.Session,
    url: str,
    dest: Path,
    *,
    force: bool = False,
    timeout: int = 120,
) -> Path:
    """
    Streaming download + resume qua .part nếu server support HTTP Range.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists() and not force:
        print(
            f"[download skip] {dest.name} "
            f"({human_bytes(dest.stat().st_size)})"
        )
        return dest

    part = dest.with_name(dest.name + ".part")
    start = part.stat().st_size if part.exists() and not force else 0

    headers = {}
    mode = "wb"

    if start > 0:
        headers["Range"] = f"bytes={start}-"

    print(f"[download] {url}")
    if start:
        print(f"           resume from {human_bytes(start)}")

    try:
        r = session.get(
            url,
            headers=headers,
            stream=True,
            timeout=timeout,
        )
    except requests.exceptions.SSLError as exc:
        raise RuntimeError(
            f"SSL verification failed for {url}: {exc}. "
            "Re-run with --insecure if you trust this host."
        ) from exc
    r.raise_for_status()

    if start > 0 and r.status_code == 206:
        mode = "ab"
    else:
        start = 0
        mode = "wb"

    content_length = r.headers.get("content-length")
    remaining = int(content_length) if content_length else None
    total = start + remaining if remaining is not None else None

    downloaded = start
    last_print = 0.0

    with part.open(mode) as f:
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            if not chunk:
                continue
            f.write(chunk)
            downloaded += len(chunk)

            now = time.time()
            if now - last_print >= 1.0:
                if total:
                    pct = downloaded * 100 / total
                    msg = (
                        f"\r           {human_bytes(downloaded)} / "
                        f"{human_bytes(total)} ({pct:5.1f}%)"
                    )
                else:
                    msg = f"\r           {human_bytes(downloaded)}"
                print(msg, end="", flush=True)
                last_print = now

    print()
    part.replace(dest)

    print(
        f"[saved] {dest} "
        f"({human_bytes(dest.stat().st_size)})"
    )

    return dest


# =============================================================================
# Discover current MOE download URLs dynamically
# =============================================================================

def discover_links(base_url: str, html: str) -> list[str]:
    p = LinkParser()
    p.feed(html)

    urls = []
    for href in p.links:
        href = html_lib.unescape(href.strip())
        if href.startswith(("javascript:", "#", "mailto:")):
            continue
        urls.append(urljoin(base_url, href))
    return urls


@dataclass
class MoeDownloadLinks:
    stroke_png_zip: str
    stroke_embed_csv: str
    dictionary_text_zip: str
    dictionary_audio_zip: str | None = None


def discover_moe_downloads(
    session: requests.Session,
    *,
    discover_audio: bool = False,
) -> MoeDownloadLinks:
    stroke_html = get_text(session, MOE_STROKE_RESOURCE_PAGE)
    dict_html = get_text(session, MOE_DICT_DOWNLOAD_PAGE)

    source_pages = MANIFESTS / "source_pages"
    source_pages.mkdir(parents=True, exist_ok=True)

    (source_pages / "moe_stroke_resources.html").write_text(
        stroke_html, encoding="utf-8"
    )
    (source_pages / "moe_dictionary_downloads.html").write_text(
        dict_html, encoding="utf-8"
    )

    stroke_links = discover_links(MOE_STROKE_RESOURCE_PAGE, stroke_html)
    dict_links = discover_links(MOE_DICT_DOWNLOAD_PAGE, dict_html)

    stroke_png = next(
        (
            u for u in stroke_links
            if filename_from_url(u).lower().endswith(".zip")
            and "6063png" in filename_from_url(u).lower()
        ),
        None,
    )

    stroke_csv = next(
        (
            u for u in stroke_links
            if filename_from_url(u).lower().endswith(".csv")
        ),
        None,
    )

    dict_audio = None
    if discover_audio:
        # LEGACY only: MOE pronunciation recordings are NOT part of
        # clean-v1 (replaced by generated TTS audio at generate.py time).
        dict_audio = next(
            (
                u for u in dict_links
                if filename_from_url(u).startswith("dict_concised_music_word_")
                and filename_from_url(u).lower().endswith(".zip")
            ),
            None,
        )

    dict_text = next(
        (
            u for u in dict_links
            if re.fullmatch(
                r"dict_concised_\d+_\d+\.zip",
                filename_from_url(u),
            )
        ),
        None,
    )

    missing = {
        "stroke_png_zip": stroke_png,
        "stroke_embed_csv": stroke_csv,
        "dictionary_text_zip": dict_text,
    }
    if discover_audio:
        missing["dictionary_audio_zip"] = dict_audio

    bad = [k for k, v in missing.items() if not v]
    if bad:
        raise RuntimeError(
            "Không discover được link MOE: " + ", ".join(bad)
        )

    return MoeDownloadLinks(
        stroke_png_zip=stroke_png,
        stroke_embed_csv=stroke_csv,
        dictionary_text_zip=dict_text,
        dictionary_audio_zip=dict_audio,
    )


# =============================================================================
# Stroke catalog + XML extraction
# =============================================================================

@dataclass
class StrokeEntry:
    order: int
    moe_id: int
    char: str
    ucs: str
    iframe_html: str
    page_url: str
    xml_relpath: str | None = None
    status: str = "pending"
    error: str | None = None


def parse_stroke_catalog(csv_path: Path) -> list[StrokeEntry]:
    text = read_text_auto(csv_path)
    rows = csv.reader(io.StringIO(text))

    entries: list[StrokeEntry] = []

    for row in rows:
        if len(row) < 3:
            continue

        try:
            order = int(row[0].strip())
            moe_id = int(row[1].strip())
        except ValueError:
            continue

        char = row[2].strip()
        iframe_html = ",".join(row[3:]).strip() if len(row) >= 4 else ""

        if not char:
            continue

        if len(char) != 1:
            print(
                f"[warn] Skip unusual character field: "
                f"order={order}, char={char!r}"
            )
            continue

        ucs = f"{ord(char):04X}"
        page_url = (
            f"{MOE_STROKE_BASE}/dictFrame.jsp"
            f"?ID={moe_id}&la=0"
        )

        entries.append(
            StrokeEntry(
                order=order,
                moe_id=moe_id,
                char=char,
                ucs=ucs,
                iframe_html=iframe_html,
                page_url=page_url,
            )
        )

    if not entries:
        raise RuntimeError(
            f"Không parse được entry nào từ {csv_path}"
        )

    return entries


def extract_embedded_xml(
    html: str,
    moe_id: int,
) -> str:
    """
    Trang dictFrame hiện embed:
        var xml={24605:"<?xml ... </Word>"};
    """
    pattern = rf"""
        var\s+xml\s*=\s*
        \{{\s*
            {re.escape(str(moe_id))}\s*:\s*
            ("(?:\\.|[^"\\])*")
        \s*\}}
        \s*;
    """

    m = re.search(
        pattern,
        html,
        flags=re.VERBOSE | re.DOTALL,
    )

    if not m:
        raise RuntimeError("embedded var xml not found")

    xml_text = json.loads(m.group(1))

    if "<Word" not in xml_text or "<Stroke>" not in xml_text:
        raise RuntimeError("embedded XML does not look like stroke data")

    return xml_text


def crawl_stroke_xml(
    session: requests.Session,
    entries: list[StrokeEntry],
    *,
    delay: float,
    limit: int | None,
) -> list[StrokeEntry]:
    entries_to_process = entries[:limit] if limit is not None else entries
    total = len(entries_to_process)
    failures = []

    for i, entry in enumerate(entries_to_process, start=1):
        filename = f"U+{entry.ucs}__ID{entry.moe_id}.xml"
        xml_path = MOE_STROKE_XML / filename

        entry.xml_relpath = str(xml_path.relative_to(DATA))

        if xml_path.exists() and xml_path.stat().st_size > 100:
            entry.status = "ok"
            print(
                f"[stroke {i}/{total}] skip {entry.char} "
                f"U+{entry.ucs}"
            )
            continue

        print(
            f"[stroke {i}/{total}] {entry.char} "
            f"U+{entry.ucs} ID={entry.moe_id}"
        )

        try:
            page = get_text(session, entry.page_url, timeout=60)
            xml_text = extract_embedded_xml(page, entry.moe_id)

            xml_path.write_text(
                xml_text,
                encoding="utf-8",
            )

            entry.status = "ok"
            entry.error = None

        except Exception as exc:
            entry.status = "error"
            entry.error = f"{type(exc).__name__}: {exc}"
            failures.append(asdict(entry))

            err_path = (
                MOE_STROKE_FAILED
                / f"U+{entry.ucs}__ID{entry.moe_id}.txt"
            )
            err_path.write_text(
                f"url={entry.page_url}\n"
                f"error={entry.error}\n",
                encoding="utf-8",
            )
            print(f"    [ERROR] {entry.error}")

        if i % 50 == 0 or i == total:
            write_jsonl(
                INDEX_DIR / "stroke_catalog.jsonl",
                [asdict(x) for x in entries],
            )
            write_json(
                INDEX_DIR / "stroke_failures.json",
                failures,
            )

        if delay > 0 and i < total:
            time.sleep(delay)

    return entries


# =============================================================================
# Manifest / README
# =============================================================================

def build_file_record(path: Path, source_url: str | None = None) -> dict:
    try:
        rel = str(path.relative_to(ROOT))
    except ValueError:
        rel = str(path)
    record = {
        "path": rel,
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }
    if source_url:
        record["source_url"] = source_url
    return record


def write_data_readme() -> None:
    text = """# Data layout

`raw/` giữ nguyên dữ liệu nguồn hoặc bản extract nguyên trạng.
`processed/` chỉ chứa index/metadata để code truy cập thuận tiện.
`manifests/` chứa provenance và checksum.

## raw/moe_stroke
- `archives/`: ZIP gốc tải từ MOE.
- `meta/embed_urls.csv`: mapping ID/chữ/embed URL.
- `player/`: JS tối thiểu dùng để render stroke animation offline.
- `xml/`: XML vector stroke trích từ từng `dictFrame.jsp`.
- `png/`: ảnh full-stroke PNG fallback/reference.
- `failed_pages/`: log lỗi khi crawl XML.

## raw/moe_dictionary
- `archives/`: ZIP gốc của 《國語辭典簡編本》 (LEGACY in clean-v1).
- `text/`: database sau khi giải nén (LEGACY in clean-v1, kept on disk, not used).
- `audio_char/`: MOE pronunciation recordings (LEGACY/DEPRECATED in
  clean-v1, only fetched with --include-legacy-audio, never used by the
  clean generation path).

## raw/hanviet
- `hanviet.csv`: LEGACY in clean-v1, kept on disk, not used.

## raw/chise
- IDS decomposition data. LEGACY in clean-v1, kept on disk, not used.

## raw/unicode
- Unihan (VERSION-PINNED, see UNICODE_VERSION) + CJKRadicals (CLEAN-v1 factual source).

## processed/indexes
- `stroke_catalog.jsonl`: index stroke chính để generate.py dùng.

## raw/cns11643
- `OpenDataFilesList.csv` + `release.txt`: official listing/versions.
- `archives/`: Voice.zip, Properties.zip, MapingTables.zip (verified
  official URLs, see CNS11643_RESOURCES).
- `properties/`: CNS_phonetic.txt (`CNS-code<TAB>bopomofo`) + tables.
- `mapping/`: CNS2UNICODE_Unicode {BMP,2,3,15}.txt (`CNS-code<TAB>HEX`).
- `audio/`: male.zip/female.zip extracts (`M|F_<hanpin>.mp3`,
  neutral-tone `<hanpin>5` clips are `.wav`) + official
  voice readme (bopomofo->hanpin table). Originals immutable.

## processed/cns11643
- `cns_index.json`: lookup index (character, normalized Zhuyin) ->
  recording để generate.py dùng. Build bằng build_cns_index(); format
  upstream chưa xác minh thì báo warning, không crash.

Lưu ý:
- download.py KHÔNG gọi LLM.
- download.py KHÔNG tạo .apkg.
- download.py KHÔNG tổng hợp audio: audio phát âm là bản ghi âm
  CNS11643 gốc, generate.py chỉ copy nguyên trạng rồi đóng vào Anki
  bằng `[sound:...]`.
"""
    (DATA / "README.md").write_text(text, encoding="utf-8")


# =============================================================================
# Main
# =============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Download and organize all raw data for the "
            "Traditional-Chinese Anki project."
        )
    )

    parser.add_argument(
        "--stroke-limit",
        type=int,
        default=0,
        help="Chỉ crawl N chữ để test. 0 = tất cả.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.35,
        help="Delay giữa các request stroke page, giây.",
    )
    parser.add_argument(
        "--include-legacy-audio",
        action="store_true",
        help="LEGACY opt-in (deprecated): also download the MOE "
        "single-character pronunciation package (~1.5GB). NOT part of "
        "clean-v1: pronunciation now comes from generated TTS audio.",
    )
    parser.add_argument(
        "--skip-png",
        action="store_true",
        help="Không tải/extract 6063 PNG fallback.",
    )
    parser.add_argument(
        "--skip-stroke-xml",
        action="store_true",
        help="Không crawl XML stroke của 6063 chữ.",
    )
    parser.add_argument(
        "--skip-external",
        action="store_true",
        help="LEGACY alias: skip Han-Viet / CHISE downloads. Unihan is always fetched for clean-v1.",
    )
    parser.add_argument(
        "--include-legacy-dict",
        action="store_true",
        help="LEGACY opt-in: also download MOE dictionary text. NOT part of clean-v1.",
    )
    parser.add_argument(
        "--include-legacy-external",
        action="store_true",
        help="LEGACY opt-in: also download Han-Viet / CHISE. NOT part of clean-v1.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Tải lại source dù file đã tồn tại.",
    )
    parser.add_argument(
        "--ignore-env-proxy",
        action="store_true",
        help="Bỏ HTTP_PROXY/HTTPS_PROXY từ environment.",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification (needed for the MOE "
        "stroke-order host whose TWCA chain is rejected by modern "
        "OpenSSL: missing Subject Key Identifier). Only use if you "
        "trust the host.",
    )

    args = parser.parse_args()

    ensure_dirs()
    write_data_readme()

    session = make_session(
        ignore_env_proxy=args.ignore_env_proxy,
        verify=not args.insecure,
    )
    if args.insecure:
        print("WARNING: TLS verification disabled (--insecure).")

    print()
    print("==============================================")
    print("1. Discover current MOE download URLs")
    print("==============================================")

    moe = discover_moe_downloads(
        session, discover_audio=args.include_legacy_audio)

    print("stroke PNG :", moe.stroke_png_zip)
    print("stroke CSV :", moe.stroke_embed_csv)
    print("dict text  :", moe.dictionary_text_zip)
    print("dict audio :", moe.dictionary_audio_zip
          if args.include_legacy_audio else "(not part of clean-v1)")

    print()
    print("==============================================")
    print("2. MOE stroke-order source files")
    print("==============================================")

    embed_csv = MOE_STROKE_META / "embed_urls.csv"

    download_file(
        session,
        moe.stroke_embed_csv,
        embed_csv,
        force=args.force,
    )

    png_zip = None
    if not args.skip_png:
        png_zip = (
            MOE_STROKE_ARCHIVES
            / filename_from_url(moe.stroke_png_zip)
        )

        download_file(
            session,
            moe.stroke_png_zip,
            png_zip,
            force=args.force,
        )

        safe_extract_zip(
            png_zip,
            MOE_STROKE_PNG,
        )

    for url, filename in MOE_PLAYER_ASSETS.items():
        download_file(
            session,
            url,
            MOE_STROKE_PLAYER / filename,
            force=args.force,
        )

    print()
    print("==============================================")
    print("3. MOE dictionary text (LEGACY opt-in)")
    print("==============================================")

    dict_zip = None
    if args.include_legacy_dict:
        dict_zip = (
            MOE_DICT_ARCHIVES
            / filename_from_url(moe.dictionary_text_zip)
        )

        download_file(
            session,
            moe.dictionary_text_zip,
            dict_zip,
            force=args.force,
        )

        safe_extract_zip(
            dict_zip,
            MOE_DICT_TEXT,
        )
    else:
        print("(clean-v1: MOE dictionary text skipped; use --include-legacy-dict to fetch)")

    audio_zip = None
    if args.include_legacy_audio:
        # LEGACY/DEPRECATED: MOE recordings are not used by clean-v1
        # (pronunciation now comes from generated TTS audio). Kept only
        # for rollback comparison of old datasets.
        if moe.dictionary_audio_zip is None:
            raise RuntimeError(
                "Legacy audio requested but no MOE audio package "
                "discovered.")
        audio_zip = (
            MOE_DICT_ARCHIVES
            / filename_from_url(moe.dictionary_audio_zip)
        )

        download_file(
            session,
            moe.dictionary_audio_zip,
            audio_zip,
            force=args.force,
            timeout=300,
        )

        safe_extract_zip(
            audio_zip,
            MOE_DICT_AUDIO,
        )
    else:
        print("(clean-v1: character pronunciation audio comes from "
              "CNS11643 (section 4c); MOE recordings stay legacy-only)")

    external_records = []
    unicode_records = []

    # --- CLEAN-v1: Unicode Unihan + radicals are ALWAYS fetched. ---
    print()
    print("==============================================")
    print(f"4. Unicode {UNICODE_VERSION} (clean-v1, always)")
    print("==============================================")

    for url, path in [
        (UNIHAN_URL, UNICODE_ARCHIVES / "Unihan.zip"),
        (CJK_RADICALS_URL, UNICODE_DIR / "CJKRadicals.txt"),
    ]:
        download_file(
            session,
            url,
            path,
            force=args.force,
        )
        unicode_records.append(
            build_file_record(path, url)
        )

    safe_extract_zip(
        UNICODE_ARCHIVES / "Unihan.zip",
        UNICODE_UNIHAN,
    )

    # --- LEGACY: Han-Viet / CHISE only on explicit opt-in. ---
    if args.include_legacy_external and not args.skip_external:
        print()
        print("==============================================")
        print("4b. Han-Viet / CHISE (LEGACY opt-in)")
        print("==============================================")

        external_downloads = [
            (HANVIET_URL, HANVIET_DIR / "hanviet.csv"),
            (CHISE_BASIC_URL, CHISE_DIR / "IDS-UCS-Basic.txt"),
            (CHISE_EXT_A_URL, CHISE_DIR / "IDS-UCS-Ext-A.txt"),
        ]

        for url, path in external_downloads:
            download_file(
                session,
                url,
                path,
                force=args.force,
            )
            external_records.append(
                build_file_record(path, url)
            )
    else:
        print("(clean-v1: Han-Viet / CHISE skipped; use --include-legacy-external to fetch)")

    print()
    print("==============================================")
    print("4c. CNS11643 pronunciation audio (fixed source)")
    print("==============================================")

    cns = download_cns11643(session, force=args.force)
    cns_records = cns["records"]
    cns_report = cns["report"]
    cns_index = cns["index"]
    cns_versions = cns.get("versions", {})

    for entry in cns_report:
        print(f"[cns {entry['status']}] {entry['resource']}"
              + (f" ({entry['detail']})" if entry.get("detail") else ""))
    if cns_versions:
        print("[cns versions] " + ", ".join(
            f"{k}={v}" for k, v in sorted(cns_versions.items())))

    print()
    print("==============================================")
    print("5. Build stroke catalog")
    print("==============================================")

    entries = parse_stroke_catalog(embed_csv)

    print(f"Parsed stroke entries: {len(entries)}")

    write_jsonl(
        INDEX_DIR / "stroke_catalog.jsonl",
        [asdict(x) for x in entries],
    )

    if not args.skip_stroke_xml:
        print()
        print("==============================================")
        print("6. Crawl MOE official stroke XML")
        print("==============================================")

        limit = (
            args.stroke_limit
            if args.stroke_limit > 0
            else None
        )

        entries = crawl_stroke_xml(
            session,
            entries,
            delay=max(args.delay, 0.0),
            limit=limit,
        )

        write_jsonl(
            INDEX_DIR / "stroke_catalog.jsonl",
            [asdict(x) for x in entries],
        )

    print()
    print("==============================================")
    print("7. Write manifest")
    print("==============================================")

    source_records = [
        build_file_record(embed_csv, moe.stroke_embed_csv),
    ]

    if dict_zip is not None:
        source_records.append(
            build_file_record(dict_zip, moe.dictionary_text_zip)
        )

    if png_zip:
        source_records.append(
            build_file_record(png_zip, moe.stroke_png_zip)
        )

    if audio_zip:
        source_records.append(
            build_file_record(audio_zip, moe.dictionary_audio_zip)
        )

    for url, filename in MOE_PLAYER_ASSETS.items():
        source_records.append(
            build_file_record(
                MOE_STROKE_PLAYER / filename,
                url,
            )
        )

    source_records.extend(unicode_records)
    source_records.extend(external_records)
    source_records.extend(cns_records)

    cns_report_counts: dict[str, int] = {}
    for entry in cns_report:
        cns_report_counts[entry["status"]] = (
            cns_report_counts.get(entry["status"], 0) + 1)

    manifest = {
        "generated_at_utc": utc_now(),
        "pipeline": "clean-v1",
        "unicode_version": UNICODE_VERSION,
        "unicode_base_url": UNICODE_BASE_URL,
        "clean_sources": ["moe_stroke", "unicode", "cns11643"],
        "legacy_included": {
            "moe_dictionary_text": dict_zip is not None,
            "moe_audio": audio_zip is not None,
            "hanviet_chise": bool(external_records),
        },
        "project_root": str(ROOT),
        "data_root": str(DATA),
        "sources": source_records,
        "cns11643": {
            "homepage": CNS11643_HOMEPAGE,
            "dataset_page": CNS11643_DATASET_PAGE,
            "agency": "Ministry of Digital Affairs (數位發展部)",
            "resources": [
                {"key": r.key, "url": r.url, "filename": r.filename,
                 "version": cns_versions.get(r.filename, "")}
                for r in CNS11643_RESOURCES
            ],
            "report": cns_report,
            "report_counts": cns_report_counts,
            "index": cns_index,
            "versions": cns_versions,
        },
        "licenses": {
            "moe_stroke": {
                "name": "CC BY-NC-ND 3.0 TW",
                "attribution": "中華民國教育部",
                "source_page": (
                    "https://stroke-order.learningweb.moe.edu.tw/"
                    "page.jsp?ID=52"
                ),
            },
            "moe_dictionary": {
                "name": "CC BY-ND 3.0 TW",
                "attribution": "中華民國教育部",
                "source_page": MOE_DICT_DOWNLOAD_PAGE,
            },
            "hanviet": {
                "name": "MIT",
                "source": (
                    "https://github.com/ph0ngp/"
                    "hanviet-pinyin-wordlist"
                ),
            },
            "chise": {
                "source": "https://github.com/chise/ids",
            },
            "unicode": {
                "version": UNICODE_VERSION,
                "source": UNICODE_BASE_URL,
                "notice": (
                    "See Unicode license/terms at "
                    "https://www.unicode.org/license.html "
                    "NEEDS VERIFICATION: copy exact notice into "
                    "licenses/ + published/licenses/ at publish time."
                ),
            },
            "cns11643": {
                "name": "政府資料開放授權條款第1版 "
                        "(Open Government Data License v1.0)",
                "attribution": "CNS11643 / 全字庫, "
                               "Ministry of Digital Affairs (數位發展部)",
                "source_page": CNS11643_DATASET_PAGE,
                "charge": "free",
                "notice": (
                    "Dataset-level license VERIFIED on the official "
                    "dataset page. No separate audio-only notice was "
                    "found in the voice readme: audio-specific "
                    "additional terms remain UNKNOWN / NEEDS "
                    "VERIFICATION. Preserve any source notice shipped "
                    "under data/raw/cns11643/ and copy licenses/CNS11643.md "
                    "into published/licenses/ before redistribution."
                ),
            },
        },
    }

    write_json(
        MANIFESTS / "sources.json",
        manifest,
    )

    ok_xml = sum(1 for x in entries if x.status == "ok")
    error_xml = sum(1 for x in entries if x.status == "error")

    print()
    print("==============================================")
    print("DONE")
    print("==============================================")
    print(f"Data root           : {DATA}")
    print(f"Stroke catalog      : {len(entries)} entries")
    print(f"Stroke XML OK       : {ok_xml}")
    print(f"Stroke XML errors   : {error_xml}")
    print(f"Dictionary text     : {MOE_DICT_TEXT}")
    print(
        "Dictionary audio    : "
        + (
            str(MOE_DICT_AUDIO) + " (LEGACY opt-in)"
            if args.include_legacy_audio
            else "(not downloaded; pronunciation comes from CNS11643)"
        )
    )
    print(f"Han-Viet            : {HANVIET_DIR}")
    print(f"CHISE               : {CHISE_DIR}")
    print(f"Unicode             : {UNICODE_DIR}")
    print(f"CNS11643            : {CNS11643_DIR} "
          f"({cns_report_counts or 'nothing to do'})")
    print(f"CNS index           : {CNS11643_INDEX_OUT} "
          f"({(cns_index or {}).get('records', 0)} records)")
    print(f"Manifest            : {MANIFESTS / 'sources.json'}")
    print()
    print(
        "Done!"
    )
    print("==============================================")


if __name__ == "__main__":
    main()