# MOE pronunciation audio — REMOVED from clean-v1 (legacy only)

- Owner: 中華民國教育部 (MOE Taiwan).
- Former package: single-character audio ZIP from the MOE dictionary
  download page:
  https://language.moe.gov.tw/001/Upload/Files/site_content/M0001/respub/dict_concised_download.html
- Status: REMOVED from the clean pipeline. MOE clips regularly speak more
   than the isolated target character, which breaks the clean-v1 contract
   (one pronunciation asset = exactly the intended Hanzi/reading).
   Pronunciation now comes exclusively from fixed CNS11643 human
   recordings (see cns_audio.py and the `audio` block in
   config/config.example.json); all TTS backends were removed.
- The `download.py --include-legacy-audio` opt-in still fetches the old
  package for rollback comparison only. It is deprecated, never used by
  `generate.py`, and never published.
- Exact audio-specific license/attribution terms were never confirmed:
  UNKNOWN / NEEDS VERIFICATION. Do not redistribute old MOE audio
  publicly until this file is completed.
