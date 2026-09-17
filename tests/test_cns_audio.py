#!/usr/bin/env python3
"""Static/mock tests for the CNS11643 fixed-source audio architecture.

Uses SMALL fixtures matching the REAL official source structure
(verified live — see download.py CNS11643_RESOURCES):
  properties/CNS_phonetic.txt ... `CNS-code\\tbopomofo` (UTF-8-SIG)
  mapping/CNS2UNICODE_Unicode BMP.txt ... `CNS-code\\tHEX`
  audio/{male,female}/{M,F}_<hanpin>.mp3 (.wav for neutral <hanpin>5)
  + voice readme bopomofo->hanpin table

Covers required scenarios A–H (parse, polyphony, voices, join, missing
member, normalization, index generation, resolver lookup) plus E–J
(absent dataset, load-once, deprecated config, text intact).
No network, no downloads, no synthesis.

Run:  python tests/test_cns_audio.py
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cns_audio import (  # noqa: E402
    CNS_INDEX_VERSION,
    CNS_SOURCE,
    CharacterAudioResolver,
    CnsError,
    build_cns_index,
    normalize_zhuyin,
    parse_cns_mapping,
    parse_phonetic_table,
    parse_voice_readme_mapping,
    zhuyin_slug,
)

# Real-format fixture fragments ( structurally identical to official files)
PHONETIC = (
    "1-4401\tㄒㄧㄥ\n"       # 行 xing1 (first tone UNMARKED, as official)
    "1-4401\tㄏㄤˊ\n"        # 行 hang2 (polyphonic code)
    "1-4402\tㄙ\n"           # 思 si1
    "1-4403\t˙ㄇㄚ\n"        # 嗎 neutral, leading ˙ (official form)
)
MAPPING = (
    "1-4401\t884C\n"         # 行 U+884C
    "1-4402\t601D\n"         # 思 U+601D
    "1-4403\t55CE\n"         # 嗎 U+55CE
)
README = (
    "全字庫聲音檔說明文件\n"
    "female.zip 內含女性發音\n"
    "male.zip 內含男性發音\n"
    "檔案類型為 mp3。\n"
    "bopomofo\thanpin\n"
    "ㄒㄧㄥ\txing\n"
    "ㄒㄧㄥˊ\txing2\n"
    "ㄏㄤˊ\thang2\n"
    "ㄙ\tsi\n"
    "ㄇㄚ\tma\n"
    "˙ㄇㄚ\tma5\n"
)


def _fixture_tree(tmp: str) -> Path:
    """Real-layout synthetic CNS tree under tmp/raw."""
    raw = Path(tmp) / "raw"
    (raw / "properties").mkdir(parents=True)
    (raw / "mapping").mkdir(parents=True)
    (raw / "audio" / "male").mkdir(parents=True)
    (raw / "audio" / "female").mkdir(parents=True)
    (raw / "properties" / "CNS_phonetic.txt").write_text(
        PHONETIC, encoding="utf-8-sig")
    (raw / "mapping" / "CNS2UNICODE_Unicode BMP.txt").write_text(
        MAPPING, encoding="utf-8-sig")
    (raw / "audio" / "全字庫聲音檔說明文件.txt").write_text(
        README, encoding="utf-8-sig")
    # Real on-disk member layout: `{M,F}_<hanpin>` with underscore;
    # neutral-tone `<hanpin>5` clips are `.wav` (as extracted upstream).
    (raw / "audio" / "male" / "M_xing.mp3").write_bytes(b"m-xing")
    (raw / "audio" / "female" / "F_xing2.mp3").write_bytes(b"f-xing2")
    (raw / "audio" / "male" / "M_hang2.mp3").write_bytes(b"m-hang2")
    (raw / "audio" / "female" / "F_si.mp3").write_bytes(b"f-si")
    (raw / "audio" / "male" / "M_ma5.wav").write_bytes(b"m-ma5")
    return raw


def _build(tmp: str) -> tuple[Path, dict]:
    raw = _fixture_tree(tmp)
    out = Path(tmp) / "cns_index.json"
    info = build_cns_index(raw, out)
    return out, info


class ParseTest(unittest.TestCase):
    """Scenario A: actual-format metadata parse."""

    def test_phonetic_rows(self):
        rows = parse_phonetic_table("1-4401\tㄒㄧㄥ\n1-4401\tㄏㄤˊ\n")
        self.assertEqual(rows, [("1-4401", "ㄒㄧㄥ"),
                                ("1-4401", "ㄏㄤˊ")])
        with self.assertRaises(CnsError):
            parse_phonetic_table("no-tab-here\n")
        with self.assertRaises(CnsError):
            parse_phonetic_table("1-1\t\n")

    def test_mapping_rows(self):
        mapping = parse_cns_mapping("1-4401\t884C\n1-4402\t601D\n")
        self.assertEqual(mapping, {"1-4401": "行", "1-4402": "思"})
        with self.assertRaises(CnsError):
            parse_cns_mapping("1-1\tZZZZ\n")

    def test_readme_table(self):
        table = parse_voice_readme_mapping(README)
        self.assertEqual(table["ㄒㄧㄥˊ"], "xing2")
        self.assertEqual(table["ㄙ"], "si")
        self.assertEqual(table["˙ㄇㄚ"], "ma5")
        with self.assertRaises(CnsError):
            parse_voice_readme_mapping("no table here\n")
        dup = README + "ㄙ\tsiX\n"
        with self.assertRaises(CnsError):
            parse_voice_readme_mapping(dup)


class NormalizeTest(unittest.TestCase):
    """Scenario F: Zhuyin normalization per real CNS representation."""

    def test_first_tone_explicit_stripped(self):
        # pipeline form (trailing ˉ) -> CNS form (unmarked)
        self.assertEqual(normalize_zhuyin("ㄒㄧㄥˉ"), "ㄒㄧㄥ")
        self.assertEqual(normalize_zhuyin("ㄒㄧㄥ"), "ㄒㄧㄥ")

    def test_neutral_kept_leading(self):
        self.assertEqual(normalize_zhuyin("˙ㄇㄚ"), "˙ㄇㄚ")

    def test_marked_tones_kept(self):
        self.assertEqual(normalize_zhuyin("ㄒㄧㄥˊ"), "ㄒㄧㄥˊ")
        self.assertEqual(normalize_zhuyin("ㄏㄠˇ"), "ㄏㄠˇ")
        self.assertEqual(normalize_zhuyin("ㄙˋ"), "ㄙˋ")

    def test_unusable(self):
        self.assertEqual(normalize_zhuyin(""), "")
        self.assertEqual(normalize_zhuyin("ㄒㄧㄥˊ ㄏㄤˊ"), "")
        self.assertEqual(normalize_zhuyin("si1"), "")
        self.assertEqual(normalize_zhuyin("ˉ"), "")

    def test_slug_stable(self):
        self.assertEqual(zhuyin_slug("ㄒㄧㄥ"), zhuyin_slug("ㄒㄧㄥ"))
        self.assertNotEqual(zhuyin_slug("ㄒㄧㄥ"), zhuyin_slug("ㄏㄤˊ"))


class BuildTest(unittest.TestCase):
    """Scenario G: index generation from real-layout fixtures."""

    def test_full_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, info = _build(tmp)
            # 4 phonetic rows x 2 voices: only the present members index;
            # absent voice members are reported, never indexed.
            self.assertEqual(info["records"], 4)
            missing = sorted(s["file"] for s in info["skipped"])
            self.assertEqual(missing,
                             ["F_hang2.mp3", "F_ma5.mp3", "F_xing.mp3",
                              "M_si.mp3"])
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(payload["index_version"], CNS_INDEX_VERSION)
            self.assertEqual(payload["source"], CNS_SOURCE)
            self.assertIn("inputs_hash", payload)
            by_id = {r["record_id"] + r["voice"]: r
                     for r in payload["records"]}
            rec = by_id["1-4401male"]
            self.assertEqual(rec["character"], "行")
            self.assertEqual(rec["zhuyin_norm"], "ㄒㄧㄥ")
            self.assertEqual(rec["zhuyin_src"], "ㄒㄧㄥ")
            self.assertEqual(rec["relpath"], "audio/male/M_xing.mp3")
            self.assertTrue(rec["sha256"])
            rec = by_id["1-4403male"]
            self.assertEqual(rec["zhuyin_norm"], "˙ㄇㄚ")
            # Neutral-tone clip resolved via the real `.wav` member.
            self.assertEqual(rec["relpath"], "audio/male/M_ma5.wav")

    def test_missing_member_reported_not_indexed(self):
        """Scenario E: referenced but absent archive member."""
        with tempfile.TemporaryDirectory() as tmp:
            raw = _fixture_tree(tmp)
            (raw / "audio" / "female" / "F_si.mp3").unlink()
            out = Path(tmp) / "idx.json"
            info = build_cns_index(raw, out)
            self.assertEqual(info["records"], 3)
            files = sorted(s.get("file", "") for s in info["skipped"])
            self.assertIn("F_si.mp3", files)

    def test_unmapped_code_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = _fixture_tree(tmp)
            with (raw / "properties" / "CNS_phonetic.txt").open(
                    "a", encoding="utf-8") as f:
                f.write("9-9999\tㄚ\n")
            out = Path(tmp) / "idx.json"
            info = build_cns_index(raw, out)
            self.assertIn("9-9999", info["unmapped"])
            self.assertEqual(info["records"], 4)


class ResolveTest(unittest.TestCase):
    """Scenario B (polyphony) + H (lookup from generated index)."""

    def test_polyphonic_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, _ = _build(tmp)
            r = CharacterAudioResolver.load(out)
            # pipeline-style explicit first tone matches unmarked CNS row
            rec_a, info_a = r.resolve("行", "ㄒㄧㄥˉ", "xing1")
            rec_b, _ = r.resolve("行", "ㄏㄤˊ", "hang2")
            self.assertTrue(info_a["matched"])
            self.assertEqual(rec_a.record_id, "1-4401")
            self.assertEqual(rec_b.record_id, "1-4401")
            self.assertNotEqual(rec_a.relpath, rec_b.relpath)
            self.assertEqual(rec_a.voice, "male")
            self.assertEqual(rec_b.voice, "male")

    def test_missing_reading_no_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, _ = _build(tmp)
            r = CharacterAudioResolver.load(out)
            rec, info = r.resolve("行", "ㄒㄧㄥˋ", "xing4")
            self.assertIsNone(rec)
            self.assertEqual(info["reason"], "reading_not_in_cns")
            rec, _ = r.resolve("龘", "ㄉㄚˊ", "da2")
            self.assertIsNone(rec)

    def test_neutral_and_unmarked(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, _ = _build(tmp)
            r = CharacterAudioResolver.load(out)
            rec, info = r.resolve("嗎", "˙ㄇㄚ", "ma5")
            self.assertTrue(info["matched"])
            self.assertEqual(rec.relpath, "audio/male/M_ma5.wav")
            rec, _ = r.resolve("思", "ㄙ", "si1")
            self.assertTrue(rec.relpath.endswith("F_si.mp3"))


class VoiceTest(unittest.TestCase):
    """Scenario C: male/female recordings + preferred_voice."""

    def test_voices_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, info = _build(tmp)
            payload = json.loads(out.read_text(encoding="utf-8"))
            voices = {r["voice"] for r in payload["records"]}
            self.assertEqual(voices, {"male", "female"})
            _ = info

    def test_preferred_voice_with_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = _fixture_tree(tmp)
            # add female xing1 member + row already maps Mx; add Fx file
            (raw / "audio" / "female" / "F_xing.mp3").write_bytes(b"f-xing")
            out = Path(tmp) / "idx.json"
            build_cns_index(raw, out)
            r = CharacterAudioResolver.load(out)
            rec_f, _ = r.resolve("行", "ㄒㄧㄥ", preferred_voice="female")
            rec_m, _ = r.resolve("行", "ㄒㄧㄥ", preferred_voice="male")
            self.assertEqual(rec_f.voice, "female")
            self.assertEqual(rec_m.voice, "male")
            rec_a, _ = r.resolve("行", "ㄒㄧㄥ", preferred_voice="auto")
            self.assertIn(rec_a.voice, ("male", "female"))


class AbsentDatasetTest(unittest.TestCase):
    def test_missing_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(CnsError) as ctx:
                CharacterAudioResolver.load(Path(tmp) / "nope.json")
            self.assertIn("download.py", str(ctx.exception))

    def test_malformed_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "idx.json"
            bad.write_text("{oops", encoding="utf-8")
            with self.assertRaises(CnsError):
                CharacterAudioResolver.load(bad)
            bad.write_text(json.dumps({"index_version": "cns-index-v1"}),
                           encoding="utf-8")
            with self.assertRaises(CnsError):
                CharacterAudioResolver.load(bad)


class LoadOnceTest(unittest.TestCase):
    def test_resolve_after_source_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, _ = _build(tmp)
            resolver = CharacterAudioResolver.load(out)
            out.unlink()
            rec, info = resolver.resolve("行", "ㄏㄤˊ")
            self.assertTrue(info["matched"])
            self.assertEqual(rec.record_id, "1-4401")


class AudioConfigTest(unittest.TestCase):
    def test_new_config_valid(self):
        import generate
        generate.validate_audio_config(
            {"source": "cns11643", "preferred_voice": "auto"})
        generate.validate_audio_config(
            {"source": "cns11643", "preferred_voice": "female"})
        generate.validate_audio_config({})
        generate.validate_audio_config({"source": ""})

    def test_new_config_rejects(self):
        import generate
        for bad in ({"source": "edge_tts"},
                    {"source": "cns11643", "preferred_voice": "robot"}):
            with self.assertRaises(ValueError):
                generate.validate_audio_config(bad)

    def test_old_tts_config_rejected(self):
        import generate
        user = {"generation": {"audio": {
            "mode": "local",
            "local": {"runtime": "primetts_v2"}}}}
        with self.assertRaises(ValueError) as ctx:
            generate._check_deprecated_audio_config(user)
        self.assertIn("CNS11643", str(ctx.exception))
        user2 = {"audio": {"provider": "edge_tts", "voice": "x"}}
        with self.assertRaises(ValueError):
            generate._check_deprecated_audio_config(user2)
        generate._check_deprecated_audio_config(
            {"audio": {"source": "cns11643"}})

    def test_unknown_keys_still_warn(self):
        import generate
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            generate._warn_unknown_config_keys(
                {"audio": {"soruce": "cns11643",
                           "_comment_x": "doc"}})
        out = buf.getvalue()
        self.assertIn("soruce", out)
        self.assertNotIn("_comment_x", out)


class TextIntactTest(unittest.TestCase):
    def test_text_configs_validate(self):
        import generate
        generate.validate_text_config({
            "mode": "endpoint",
            "endpoint": {"provider": "openai_compatible",
                         "base_url": "http://127.0.0.1:8080/v1",
                         "model": "example-model-alias",
                         "timeout_seconds": 180, "trust_env": False,
                         "api_key_env": "", "generation": {}},
            "prompt": {"system_file": str(
                ROOT / "config/prompts/text_system.txt"),
                "user_file": str(
                    ROOT / "config/prompts/text_user.txt")},
        })
        generate.validate_text_config({
            "mode": "local",
            "local": {"provider": "huggingface",
                      "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
                      "revision": "", "device": "cuda", "dtype": "auto",
                      "generation": {}},
            "prompt": {"system_file": str(
                ROOT / "config/prompts/text_system.txt"),
                "user_file": str(
                    ROOT / "config/prompts/text_user.txt")},
        })

    def test_text_providers_importable(self):
        import text_providers
        self.assertTrue(hasattr(text_providers, "LocalHFTextProvider"))
        self.assertTrue(hasattr(text_providers, "EndpointTextProvider"))


# Real CNS codes, readings, and hanpin values (verified against
# data/raw/cns11643/): 好 1-476F ㄏㄠˇ/ㄏㄠˋ, 中 1-4463 ㄓㄨㄥ/ㄓㄨㄥˋ,
# 愛 1-6378 ㄞˋ, 行 1-4867 ㄒㄧㄥˊ/ㄏㄤˊ. Guards the 0/10 lookup
# failure caused by the missing-underscore filename bug.
_REAL_PHONETIC = (
    "1-476F\tㄏㄠˇ\n"
    "1-476F\tㄏㄠˋ\n"
    "1-4463\tㄓㄨㄥ\n"
    "1-4463\tㄓㄨㄥˋ\n"
    "1-6378\tㄞˋ\n"
    "1-4867\tㄒㄧㄥˊ\n"
    "1-4867\tㄏㄤˊ\n"
)
_REAL_MAPPING = (
    "1-476F\t597D\n"
    "1-4463\t4E2D\n"
    "1-6378\t611B\n"
    "1-4867\t884C\n"
)
_REAL_README = (
    "全字庫聲音檔說明文件\n"
    "bopomofo\thanpin\n"
    "ㄏㄠˇ\thao3\n"
    "ㄏㄠˋ\thao4\n"
    "ㄓㄨㄥ\tzhong\n"
    "ㄓㄨㄥˋ\tzhong4\n"
    "ㄞˋ\tai4\n"
    "ㄒㄧㄥˊ\txing2\n"
    "ㄏㄤˊ\thang2\n"
)


def _real_char_tree(tmp: str) -> Path:
    """Synthetic tree with real characters/readings, real member layout."""
    raw = Path(tmp) / "raw"
    (raw / "properties").mkdir(parents=True)
    (raw / "mapping").mkdir(parents=True)
    (raw / "audio" / "male").mkdir(parents=True)
    (raw / "audio" / "female").mkdir(parents=True)
    (raw / "properties" / "CNS_phonetic.txt").write_text(
        _REAL_PHONETIC, encoding="utf-8-sig")
    (raw / "mapping" / "CNS2UNICODE_Unicode BMP.txt").write_text(
        _REAL_MAPPING, encoding="utf-8-sig")
    (raw / "audio" / "全字庫聲音檔說明文件.txt").write_text(
        _REAL_README, encoding="utf-8-sig")
    for voice, prefix in (("male", "M"), ("female", "F")):
        for hanpin in ("hao3", "hao4", "zhong", "zhong4", "ai4",
                       "xing2", "hang2"):
            (raw / "audio" / voice / f"{prefix}_{hanpin}.mp3").write_bytes(
                f"{voice}-{hanpin}".encode("ascii"))
    return raw


class RealCharacterRegressionTest(unittest.TestCase):
    """好/中/愛 + polyphonic 行 resolve exactly; wrong reading fails."""

    def _resolver(self, tmp: str) -> CharacterAudioResolver:
        raw = _real_char_tree(tmp)
        out = Path(tmp) / "cns_index.json"
        info = build_cns_index(raw, out)
        self.assertEqual(info["records"], 14)
        self.assertEqual(info["skipped"], [])
        return CharacterAudioResolver.load(out)

    def test_reported_characters_resolve(self):
        import generate
        with tempfile.TemporaryDirectory() as tmp:
            r = self._resolver(tmp)
            # (char, Unihan kMandarin pinyin, expected CNS record_id)
            for char, pinyin, record_id in (
                    ("好", "hǎo", "1-476F"),
                    ("中", "zhōng", "1-4463"),
                    ("愛", "ài", "1-6378")):
                zhuyin = generate.pinyin_to_zhuyin(pinyin)
                norm = normalize_zhuyin(zhuyin)
                self.assertTrue(norm, char)
                rec, info = r.resolve(char, zhuyin, pinyin)
                self.assertTrue(info["matched"], char)
                self.assertEqual(rec.record_id, record_id)
                self.assertEqual(rec.zhuyin_norm, norm)
                self.assertTrue(
                    rec.relpath.startswith("audio/"),
                    rec.relpath)

    def test_first_tone_pipeline_form_matches_unmarked_cns(self):
        # 中 zhōng -> pipeline ㄓㄨㄥˉ (explicit ˉ) == CNS ㄓㄨㄥ.
        with tempfile.TemporaryDirectory() as tmp:
            r = self._resolver(tmp)
            rec, info = r.resolve("中", "ㄓㄨㄥˉ", "zhōng")
            self.assertTrue(info["matched"])
            self.assertEqual(rec.zhuyin_norm, "ㄓㄨㄥ")
            self.assertEqual(rec.zhuyin_src, "ㄓㄨㄥ")

    def test_polyphonic_readings_are_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._resolver(tmp)
            rec_a, info_a = r.resolve("行", "ㄒㄧㄥˊ", "xíng")
            rec_b, info_b = r.resolve("行", "ㄏㄤˊ", "háng")
            self.assertTrue(info_a["matched"])
            self.assertTrue(info_b["matched"])
            self.assertEqual(rec_a.record_id, "1-4867")
            self.assertEqual(rec_b.record_id, "1-4867")
            self.assertNotEqual(rec_a.relpath, rec_b.relpath)

    def test_wrong_reading_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._resolver(tmp)
            # 好 has only hao3/hao4 in CNS: hao2 must NOT fall back.
            rec, info = r.resolve("好", "ㄏㄠˊ", "háo")
            self.assertIsNone(rec)
            self.assertFalse(info["matched"])
            self.assertEqual(info["reason"], "reading_not_in_cns")


if __name__ == "__main__":
    unittest.main(verbosity=2)
