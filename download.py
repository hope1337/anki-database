#!/usr/bin/env python3
"""
download.py

Bootstrap data for the Traditional-Chinese Anki project from an empty
data/ directory.

CLEAN-v1 DEFAULT PIPELINE (what `python download.py` fetches):
  1) MOE Taiwan stroke-order source (embed CSV + per-char XML + JS player
     + optional 6063 PNG fallback)
  2) MOE Taiwan single-character pronunciation audio package
  3) Unicode Unihan (VERSION-PINNED, see UNICODE_VERSION below) + CJKRadicals

The clean-v1 pipeline does NOT download by default:
  - MOE dictionary text/definitions (legacy, opt-in via --include-legacy-dict)
  - Han-Viet CSV / CHISE IDS (legacy, opt-in via --include-legacy-external)

Legacy download code is retained in a clearly marked LEGACY section so
existing on-disk data is not orphaned and rollback stays possible.

GRANULARITY NOTE:
  Upstream archives (Unihan.zip, MOE audio ZIP) are distributed ONLY as
  whole archives. There is no partial download: test mode still downloads
  the full archives, it only limits per-character work (stroke XML crawl).
  Download granularity != generation granularity.

Cài dependency:
    pip install requests

Chạy full clean-v1:
    python download.py

Test 10 chữ stroke trước:
    python download.py --stroke-limit 10

Không tải audio 1.5GB:
    python download.py --skip-audio

Không tải PNG fallback:
    python download.py --skip-png

Chỉ download raw source, chưa crawl 6063 XML:
    python download.py --skip-stroke-xml

Legacy opt-in (NOT part of clean-v1):
    python download.py --include-legacy-dict --include-legacy-external
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html as html_lib
import io
import json
import re
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
    dictionary_audio_zip: str


def discover_moe_downloads(
    session: requests.Session,
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
        "dictionary_audio_zip": dict_audio,
    }

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
    record = {
        "path": str(path.relative_to(ROOT)),
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
- `audio_char/`: audio chữ đơn sau khi giải nén (CLEAN-v1: audio IS used).

## raw/hanviet
- `hanviet.csv`: LEGACY in clean-v1, kept on disk, not used.

## raw/chise
- IDS decomposition data. LEGACY in clean-v1, kept on disk, not used.

## raw/unicode
- Unihan (VERSION-PINNED, see UNICODE_VERSION) + CJKRadicals (CLEAN-v1 factual source).

## processed/indexes
- `stroke_catalog.jsonl`: index stroke chính để generate.py dùng.

Lưu ý:
- download.py KHÔNG gọi LLM.
- download.py KHÔNG tạo .apkg.
- audio chỉ được tải/extract ở đây; generate.py mới chọn audio đúng
  rồi đóng vào Anki bằng `[sound:...]`.
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
        "--skip-audio",
        action="store_true",
        help="Không tải/extract package audio chữ đơn (~1.5GB).",
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

    moe = discover_moe_downloads(session)

    print("stroke PNG :", moe.stroke_png_zip)
    print("stroke CSV :", moe.stroke_embed_csv)
    print("dict text  :", moe.dictionary_text_zip)
    print("dict audio :", moe.dictionary_audio_zip)

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
    print("3. MOE audio (clean-v1) + MOE dictionary (LEGACY opt-in)")
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
    if not args.skip_audio:
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

    manifest = {
        "generated_at_utc": utc_now(),
        "pipeline": "clean-v1",
        "unicode_version": UNICODE_VERSION,
        "unicode_base_url": UNICODE_BASE_URL,
        "clean_sources": ["moe_stroke", "moe_audio", "unicode"],
        "legacy_included": {
            "moe_dictionary_text": dict_zip is not None,
            "hanviet_chise": bool(external_records),
        },
        "project_root": str(ROOT),
        "data_root": str(DATA),
        "sources": source_records,
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
            str(MOE_DICT_AUDIO)
            if not args.skip_audio
            else "(skipped)"
        )
    )
    print(f"Han-Viet            : {HANVIET_DIR}")
    print(f"CHISE               : {CHISE_DIR}")
    print(f"Unicode             : {UNICODE_DIR}")
    print(f"Manifest            : {MANIFESTS / 'sources.json'}")
    print()
    print(
        "Tiếp theo generate.py sẽ chỉ đọc data/ này; "
        "không cần download/crawl lại."
    )
    print("==============================================")


if __name__ == "__main__":
    main()