#!/usr/bin/env python3
"""Static/mock tests for CNS11643 download machinery in download.py.

Covers required scenarios A–D (skip-existing, .part resume,
already-extracted, interrupted recovery) plus forced reacquire,
path-traversal rejection, and the verified-resource CNS flow.
All HTTP is mocked — no network, no real downloads.

Run:  python tests/test_download_cns.py
"""

import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import download as dl


class FakeResponse:
    def __init__(self, payload: bytes, status: int = 200,
                 accept_ranges: bool = True):
        self._payload = payload
        self.status_code = status
        self.headers = {"content-length": str(len(payload))}
        if accept_ranges:
            self.headers["Accept-Ranges"] = "bytes"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=1024 * 1024):
        for i in range(0, len(self._payload), chunk_size):
            yield self._payload[i:i + chunk_size]


class FakeSession:
    """Mock requests.Session: serves full or Range-sliced payloads."""

    def __init__(self, payload: bytes):
        self.payload = payload
        self.calls: list[dict] = []

    def get(self, url, headers=None, stream=True, timeout=120):
        headers = headers or {}
        self.calls.append({"url": url, "headers": dict(headers)})
        if "Range" in headers:
            start = int(headers["Range"].split("=")[1].split("-")[0])
            resp = FakeResponse(self.payload[start:], status=206)
            resp.headers["content-length"] = str(
                len(self.payload) - start)
            return resp
        return FakeResponse(self.payload, status=200)


def _make_zip(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)


class SkipExistingTest(unittest.TestCase):
    """Scenario A: complete file present -> no redownload."""

    def test_skipped_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "voice.zip"
            dest.write_bytes(b"complete-bytes")
            session = FakeSession(b"server-bytes")
            report: list = []
            status = dl.download_tracked(
                session, "http://x/voice.zip", dest,
                report=report, key="voice")
            self.assertEqual(status, "skipped-existing")
            self.assertEqual(session.calls, [])
            self.assertEqual(dest.read_bytes(), b"complete-bytes")
            self.assertEqual(report[0]["status"], "skipped-existing")


class ResumeTest(unittest.TestCase):
    """Scenario B: partial .part resumes via HTTP Range."""

    def test_resume_appends(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "voice.zip"
            full = b"0123456789ABCDEF" * 64
            part = dest.with_name(dest.name + ".part")
            part.write_bytes(full[:100])
            session = FakeSession(full)
            report: list = []
            status = dl.download_tracked(
                session, "http://x/voice.zip", dest,
                report=report, key="voice")
            self.assertEqual(status, "resumed")
            self.assertIn("Range", session.calls[0]["headers"])
            self.assertEqual(
                session.calls[0]["headers"]["Range"], "bytes=100-")
            self.assertEqual(dest.read_bytes(), full)
            self.assertFalse(part.exists())

    def test_fresh_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "voice.zip"
            session = FakeSession(b"fresh-bytes")
            status = dl.download_tracked(
                session, "http://x/voice.zip", dest, key="voice")
            self.assertEqual(status, "downloaded")
            self.assertNotIn("Range", session.calls[0]["headers"])
            self.assertEqual(dest.read_bytes(), b"fresh-bytes")


class ExtractSkipTest(unittest.TestCase):
    """Scenario C: completed extraction is not repeated."""

    def test_already_extracted(self):
        with tempfile.TemporaryDirectory() as tmp:
            zipp = Path(tmp) / "voice.zip"
            _make_zip(zipp, {"a/b.wav": b"data"})
            dest = Path(tmp) / "audio"
            first = dl.safe_extract_zip_resumable(zipp, dest)
            self.assertEqual(first["status"], "extracted")
            marker = dest / ".extracted.ok"
            stamp = marker.stat().st_mtime_ns
            target = dest / "a" / "b.wav"
            tstamp = target.stat().st_mtime_ns
            second = dl.safe_extract_zip_resumable(zipp, dest)
            self.assertEqual(second["status"], "already-extracted")
            # nothing rewritten
            self.assertEqual(marker.stat().st_mtime_ns, stamp)
            self.assertEqual(target.stat().st_mtime_ns, tstamp)


class InterruptedRecoveryTest(unittest.TestCase):
    """Scenario D: interrupted extraction recovers without rewriting
    thousands of valid files."""

    def test_recovery_keeps_valid_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            zipp = Path(tmp) / "voice.zip"
            members = {f"clip_{i:04d}.wav": bytes([i % 256]) * 32
                       for i in range(20)}
            _make_zip(zipp, members)
            dest = Path(tmp) / "audio"
            dest.mkdir()
            # simulate interruption: half the files present, no marker
            names = sorted(members)
            for name in names[:10]:
                (dest / name).write_bytes(members[name])
            before = {n: (dest / n).stat().st_mtime_ns for n in names[:10]}
            result = dl.safe_extract_zip_resumable(zipp, dest)
            self.assertEqual(result["status"], "extracted")
            self.assertEqual(result["extracted"], 10)
            self.assertEqual(result["skipped"], 10)
            for name in names[:10]:
                self.assertEqual(
                    (dest / name).stat().st_mtime_ns, before[name])
            for name in names[10:]:
                self.assertEqual((dest / name).read_bytes(),
                                 members[name])
            self.assertTrue((dest / ".extracted.ok").exists())

    def test_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            zipp = Path(tmp) / "evil.zip"
            _make_zip(zipp, {"../escape.txt": b"x"})
            with self.assertRaises(RuntimeError):
                dl.safe_extract_zip_resumable(zipp, Path(tmp) / "out")


class DictSession:
    """URL-keyed mock session for the multi-resource CNS flow."""

    def __init__(self, payloads: dict[str, bytes]):
        self.payloads = payloads
        self.calls: list[dict] = []

    def get(self, url, headers=None, stream=True, timeout=120):
        headers = headers or {}
        self.calls.append({"url": url, "headers": dict(headers)})
        payload = self.payloads[url]
        if "Range" in headers:
            start = int(headers["Range"].split("=")[1].split("-")[0])
            resp = FakeResponse(payload[start:], status=206)
            resp.headers["content-length"] = str(len(payload) - start)
            return resp
        return FakeResponse(payload, status=200)


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _real_layout_payloads() -> dict[str, bytes]:
    """Small archives mirroring the official CNS layout (nested Voice)."""
    male_inner = _zip_bytes({"M_xing.mp3": b"m-xing",
                             "M_hang2.mp3": b"m-hang2"})
    female_inner = _zip_bytes({"F_xing2.mp3": b"f-xing2"})
    voice_outer = _zip_bytes({
        "male.zip": male_inner,
        "female.zip": female_inner,
        "readme.txt": ("bopomofo\thanpin\n"
                       "ㄒㄧㄥ\txing\nㄒㄧㄥˊ\txing2\n"
                       "ㄏㄤˊ\thang2\n").encode("utf-8-sig"),
    })
    # voice readme must be discoverable by name; rename member below in
    # the flow test via Big5 name instead (see nested test).
    props = _zip_bytes({
        "CNS_phonetic.txt": ("1-4401\tㄒㄧㄥ\n1-4401\tㄏㄤˊ\n"
                              ).encode("utf-8-sig"),
    })
    mapping = _zip_bytes({
        "Unicode/CNS2UNICODE_Unicode BMP.txt":
            "1-4401\t884C\n".encode("utf-8-sig"),
    })
    listing = ("名稱,所屬,類別,說明\n"
               "Voice.zip,-,檔案,音檔\n"
               "Properties.zip,-,檔案,屬性\n"
               "MapingTables.zip,-,檔案,對照表\n"
               "male.zip,Voice.zip,檔案,內含男性發音音檔\n"
               "female.zip,Voice.zip,檔案,內含女性發音音檔\n"
               "CNS_phonetic.txt,Properties.zip,檔案,注音\n")
    return {
        "http://x/OpenDataFilesList.csv": listing.encode("big5"),
        "http://x/release.txt":
            "檔案名稱：Voice.zip\n版本：20250113\n".encode("utf-8"),
        "http://x/Properties.zip": props,
        "http://x/MapingTables.zip": mapping,
        "http://x/Voice.zip": voice_outer,
    }


class ListingParseTest(unittest.TestCase):
    """Real-format official listing/release parsing."""

    def test_listing_big5(self):
        rows = dl.parse_cns_listing(
            "名稱,所屬,類別,說明\n"
            "Voice.zip,-,檔案,全字庫音檔 ZIP 壓縮檔\n"
            "male.zip,Voice.zip,檔案,內含男性發音音檔\n")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["parent"], "Voice.zip")
        with self.assertRaises(RuntimeError):
            dl.parse_cns_listing("bad,row\n")

    def test_release_versions(self):
        versions = dl.parse_cns_release_versions(
            "檔案名稱：Voice.zip\n版本：20250113\n"
            "下載路徑：https://www.cns11643.gov.tw/opendata/Voice.zip\n")
        self.assertEqual(versions, {"Voice.zip": "20250113"})

    def test_decode_zip_name_big5(self):
        # Big5 bytes for 全 as stored (cp437 mojibake round-trip)
        raw = "全字庫.txt".encode("big5").decode("cp437")
        info2 = zipfile.ZipInfo(raw)
        info2.flag_bits = 0
        self.assertEqual(dl.decode_zip_name(info2), "全字庫.txt")
        info3 = zipfile.ZipInfo("plain.mp3")
        info3.flag_bits = 0
        self.assertEqual(dl.decode_zip_name(info3), "plain.mp3")


class CnsFlowTest(unittest.TestCase):
    """Real-layout CNS flow: listing discovery, nested voice archives,
    automatic index build, and idempotent reruns (L)."""

    def _patch(self, tmp: str):
        import download as _dl
        keep = (_dl.CNS11643_ARCHIVES, _dl.CNS11643_DIR,
                _dl.CNS11643_INDEX_OUT, _dl.CNS11643_RESOURCES)
        _dl.CNS11643_ARCHIVES = Path(tmp) / "archives"
        _dl.CNS11643_DIR = Path(tmp) / "raw"
        _dl.CNS11643_INDEX_OUT = Path(tmp) / "cns_index.json"
        base = "http://x"
        _dl.CNS11643_RESOURCES = [
            dl.CnsResource(key="listing", url=f"{base}/OpenDataFilesList.csv",
                           filename="OpenDataFilesList.csv",
                           extract_to=None),
            dl.CnsResource(key="release", url=f"{base}/release.txt",
                           filename="release.txt", extract_to=None),
            dl.CnsResource(key="properties",
                           url=f"{base}/Properties.zip",
                           filename="Properties.zip",
                           extract_to="properties"),
            dl.CnsResource(key="mapping", url=f"{base}/MapingTables.zip",
                           filename="MapingTables.zip",
                           extract_to="mapping"),
            dl.CnsResource(key="voice", url=f"{base}/Voice.zip",
                           filename="Voice.zip", extract_to="audio",
                           nested_zips=True),
        ]
        return keep

    def _restore(self, keep):
        import download as _dl
        (_dl.CNS11643_ARCHIVES, _dl.CNS11643_DIR,
         _dl.CNS11643_INDEX_OUT, _dl.CNS11643_RESOURCES) = keep

    def test_full_flow_builds_index(self):
        import download as _dl
        with tempfile.TemporaryDirectory() as tmp:
            keep = self._patch(tmp)
            try:
                # voice readme name must match discovery (*聲音檔說明*);
                # our fixture readme is generic, so place the expected
                # name by renaming inside the nested flow is overkill:
                # instead assert extraction works and index build reports
                # the missing-readme warning path is NOT hit after we
                # rename readme.txt below.
                session = DictSession(_real_layout_payloads())
                out = _dl.download_cns11643(session, force=False)
                # rename generic readme to the discoverable pattern is
                # unnecessary: _find_voice_readme falls back to any txt
                # containing a bopomofo->hanpin table.
                statuses = [e["status"] for e in out["report"]]
                self.assertIn("downloaded", statuses)
                self.assertIn("extracted", statuses)
                nested = [e for e in out["report"]
                          if e["resource"].startswith("nested:")]
                self.assertTrue(
                    any(e["status"] == "extracted" for e in nested),
                    out["report"])
                self.assertGreater(out["index"]["records"], 0)
                self.assertIn("processed", statuses)
                self.assertEqual(
                    out["versions"].get("Voice.zip"), "20250113")
            finally:
                self._restore(keep)

    def test_rerun_skips_everything(self):
        """Scenarios I (no download) + K (no extraction) + L."""
        import download as _dl
        with tempfile.TemporaryDirectory() as tmp:
            keep = self._patch(tmp)
            try:
                session = DictSession(_real_layout_payloads())
                _dl.download_cns11643(session, force=False)
                second = _dl.download_cns11643(session, force=False)
                statuses = [e["status"] for e in second["report"]]
                self.assertNotIn("downloaded", statuses)
                self.assertNotIn("extracted", statuses)
                self.assertIn("skipped-existing", statuses)
                self.assertIn("already-extracted", statuses)
                # L: unchanged inputs -> no index rebuild
                self.assertIn("already-processed", statuses)
                # --force reacquires + rebuilds
                third = _dl.download_cns11643(session, force=True)
                statuses3 = [e["status"] for e in third["report"]]
                self.assertIn("downloaded", statuses3)
                self.assertIn("processed", statuses3)
            finally:
                self._restore(keep)


if __name__ == "__main__":
    unittest.main(verbosity=2)
