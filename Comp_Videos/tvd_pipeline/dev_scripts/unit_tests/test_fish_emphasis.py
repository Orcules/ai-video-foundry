"""Test: Does ZapCap respect our 'important' field on BYOT transcript entries?

4 tests, 1 word marked important (fish), emoji on 3 words in all tests:
  A: custom style + emphasizeKeywords=true
  B: custom style + emphasizeKeywords=false
  C: random template + emphasizeKeywords=true
  D: random template + emphasizeKeywords=false

Run:
  cd Comp_Videos
  set -a && source .env && set +a
  python -m tvd_pipeline.dev_scripts.unit_tests.test_fish_emphasis
"""

import copy
import os
import sys
import time
import json
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from tvd_pipeline.services.zapcap import ZapCapService
from tvd_pipeline.services.gcs_storage import GCSStorageService
from tvd_pipeline.config import Config
from tvd_pipeline import data_loader

OUT_DIR = os.path.join(os.path.dirname(__file__), "test_output")

WORD_SEGMENTS = [
    {"text": "You", "start_time": 0.0, "end_time": 0.18},
    {"text": "have", "start_time": 0.18, "end_time": 0.32},
    {"text": "to", "start_time": 0.32, "end_time": 0.40},
    {"text": "try", "start_time": 0.40, "end_time": 0.58},
    {"text": "this", "start_time": 0.58, "end_time": 0.72},
    {"text": "amazing", "start_time": 0.72, "end_time": 1.10},
    {"text": "sushi", "start_time": 1.10, "end_time": 1.42},
    {"text": "place", "start_time": 1.42, "end_time": 1.68},
    {"text": "on", "start_time": 1.68, "end_time": 1.80},
    {"text": "Spalena", "start_time": 1.80, "end_time": 2.20},
    {"text": "street.", "start_time": 2.20, "end_time": 2.60},
    {"text": "The", "start_time": 2.90, "end_time": 3.02},
    {"text": "fish", "start_time": 3.02, "end_time": 3.28},
    {"text": "is", "start_time": 3.28, "end_time": 3.38},
    {"text": "incredibly", "start_time": 3.38, "end_time": 3.90},
    {"text": "fresh", "start_time": 3.90, "end_time": 4.20},
    {"text": "and", "start_time": 4.20, "end_time": 4.35},
    {"text": "the", "start_time": 4.35, "end_time": 4.45},
    {"text": "rolls", "start_time": 4.45, "end_time": 4.78},
    {"text": "just", "start_time": 4.78, "end_time": 4.98},
    {"text": "melt", "start_time": 4.98, "end_time": 5.25},
    {"text": "in", "start_time": 5.25, "end_time": 5.35},
    {"text": "your", "start_time": 5.35, "end_time": 5.50},
    {"text": "mouth.", "start_time": 5.50, "end_time": 5.90},
    {"text": "Trust", "start_time": 6.20, "end_time": 6.48},
    {"text": "me,", "start_time": 6.48, "end_time": 6.65},
    {"text": "once", "start_time": 6.65, "end_time": 6.88},
    {"text": "you", "start_time": 6.88, "end_time": 7.00},
    {"text": "visit", "start_time": 7.00, "end_time": 7.32},
    {"text": "you", "start_time": 7.32, "end_time": 7.45},
    {"text": "will", "start_time": 7.45, "end_time": 7.60},
    {"text": "keep", "start_time": 7.60, "end_time": 7.85},
    {"text": "coming", "start_time": 7.85, "end_time": 8.15},
    {"text": "back", "start_time": 8.15, "end_time": 8.40},
    {"text": "for", "start_time": 8.40, "end_time": 8.55},
    {"text": "more.", "start_time": 8.55, "end_time": 8.90},
]

# Only "fish" (index 12) marked important. Emoji on sushi/fish/visit in all tests.
ENRICHMENTS = {
    6:  {"emoji": "\U0001f363"},                        # sushi - emoji only
    12: {"emoji": "\U0001f41f", "important": True},     # fish - emoji + IMPORTANT
    28: {"emoji": "\U0001f4cd"},                        # visit - emoji only
}

TEST_VIDEO = os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..",
    "api_pipeline", "documents", "test_scripts", "oishi_assets",
    "WhatsApp Video 2026-03-04 at 01.12.52 copy.mp4",
))


def download(url, local_path):
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    with open(local_path, "wb") as f:
        f.write(r.content)
    print(f"  Downloaded {len(r.content)/(1024*1024):.1f}MB -> {local_path}")


def run_test(zs, video_url, label, filename, style_override, emphasize):
    print(f"\n--- {label} ---")
    print(f"  style_override={style_override}, emphasizeKeywords={emphasize}")

    orig = data_loader.get_zapcap_config

    # Lock to first template so randomness is not a variable
    fixed_template = orig()["template_ids"][0]

    def patched():
        c = copy.deepcopy(orig())
        c["style_override"] = style_override
        c["render_options"]["subs_options"]["emphasizeKeywords"] = emphasize
        c["template_ids"] = [fixed_template]  # force same template for all tests
        print(f"  Config: style_override={c['style_override']}, "
              f"emphasizeKeywords={c['render_options']['subs_options']['emphasizeKeywords']}, "
              f"template={fixed_template[:8]}...")
        return c

    data_loader.get_zapcap_config = patched
    try:
        t0 = time.time()
        url = zs.add_subtitles(
            video_url=video_url,
            language="en",
            transcript=WORD_SEGMENTS,
            enrichments=ENRICHMENTS,
        )
        print(f"  Done in {time.time()-t0:.0f}s")
        if url:
            path = os.path.join(OUT_DIR, filename)
            download(url, path)
            return path
        else:
            print("  ZapCap returned None")
            return None
    finally:
        data_loader.get_zapcap_config = orig


def main():
    print("=" * 60)
    print("Fish Emphasis Test — 4 combinations")
    print("Only 'fish' marked important. Emoji on sushi/fish/visit.")
    print("=" * 60)

    config = Config()
    zs = ZapCapService(api_key=config.ZAPCAP_API_KEY)

    # Upload once
    print("\nUploading test video to GCS...")
    gcs = GCSStorageService(
        credentials_file=os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "service_account.json",
        ),
        bucket_name="automatiq",
        folder_path="test/end_card_subtitles",
    )
    with open(TEST_VIDEO, "rb") as f:
        vb = f.read()
    video_url = gcs.upload_video_bytes(vb, f"oishi_fish_{int(time.time())}.mp4")
    print(f"  {video_url}")

    # 4 tests
    a = run_test(zs, video_url, "A: custom style + emphasize ON",
                 "fish_A_custom_emphasize_on.mp4", True, True)
    b = run_test(zs, video_url, "B: custom style + emphasize OFF",
                 "fish_B_custom_emphasize_off.mp4", True, False)
    c = run_test(zs, video_url, "C: random template + emphasize ON",
                 "fish_C_random_emphasize_on.mp4", False, True)
    d = run_test(zs, video_url, "D: random template + emphasize OFF",
                 "fish_D_random_emphasize_off.mp4", False, False)

    print("\n" + "=" * 60)
    print("Done! Compare these files:")
    print("=" * 60)
    base = os.path.abspath(OUT_DIR)
    print(f"  A: {os.path.join(base, 'fish_A_custom_emphasize_on.mp4')}")
    print(f"  B: {os.path.join(base, 'fish_B_custom_emphasize_off.mp4')}")
    print(f"  C: {os.path.join(base, 'fish_C_random_emphasize_on.mp4')}")
    print(f"  D: {os.path.join(base, 'fish_D_random_emphasize_off.mp4')}")
    print('\nLook at the word "fish" (~3.0s) — is it emphasized or same as others?')


if __name__ == "__main__":
    main()
