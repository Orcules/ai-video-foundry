"""Test: Does ZapCap accept partial styleOptions (position only, no font/color)?

2 tests using the same short video:
  A: No styleOptions at all (baseline, random template)
  B: styleOptions with ONLY {"top": 80} (position-only override)

If both succeed and B's subtitles are visibly lower than A's, partial override works.

Run:
  cd Comp_Videos
  set -a && source .env && set +a
  python -m tvd_pipeline.dev_scripts.unit_tests.test_zapcap_position_only
"""

import copy
import os
import sys
import time
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from tvd_pipeline.services.zapcap import ZapCapService
from tvd_pipeline.services.gcs_storage import GCSStorageService
from tvd_pipeline.config import Config
from tvd_pipeline import data_loader

OUT_DIR = os.path.join(os.path.dirname(__file__), "test_output")

# Use a short test video — any short subtitled or non-subtitled video works
TEST_VIDEO_URL = "https://storage.googleapis.com/automatiq/Comp/Final_Video/veo3_videos/veo3_1772981955_5138.mp4"


def run_test(label, zapcap_svc, video_data, style_options=None):
    """Run a single ZapCap test with optional styleOptions override."""
    import tempfile
    print(f"\n{'='*60}")
    print(f"Test {label}")
    print(f"  styleOptions: {style_options}")
    print(f"{'='*60}")

    import requests as req

    headers = {"x-api-key": zapcap_svc.api_key}

    # Step 1: Upload video (multipart file upload)
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp.write(video_data)
        tmp_path = tmp.name

    try:
        with open(tmp_path, "rb") as f:
            files = {"file": (f"test_{int(time.time())}.mp4", f, "video/mp4")}
            resp = req.post(
                f"{zapcap_svc.base_url}/videos",
                headers=headers,
                files=files,
                timeout=120,
            )
    finally:
        os.unlink(tmp_path)

    if resp.status_code not in [200, 201]:
        print(f"  UPLOAD FAILED: {resp.status_code} {resp.text[:300]}")
        return None
    video_id = resp.json().get("id")
    print(f"  Uploaded: video_id={video_id}")

    # Step 2: Pick a random template
    template_id = zapcap_svc._get_random_template_id()
    print(f"  Template: {template_id}")

    # Step 3: Create task with controlled styleOptions
    zc_config = data_loader.get_zapcap_config()
    render_cfg = zc_config.get("render_options", {})
    render_options = {
        "subsOptions": render_cfg.get("subs_options", {
            "emoji": True,
            "emojiAnimation": True,
            "emphasizeKeywords": True,
        })
    }

    if style_options is not None:
        render_options["styleOptions"] = style_options

    task_body = {
        "templateId": template_id,
        "language": "en",
        "autoApprove": True,
        "renderOptions": render_options,
    }

    print(f"  Task body: {json.dumps(task_body, indent=2)}")

    json_headers = {"x-api-key": zapcap_svc.api_key, "Content-Type": "application/json"}
    task_resp = req.post(
        f"{zapcap_svc.base_url}/videos/{video_id}/task",
        headers=json_headers,
        json=task_body,
        timeout=60,
    )
    if task_resp.status_code not in [200, 201]:
        print(f"  TASK FAILED: {task_resp.status_code} {task_resp.text[:500]}")
        return None

    task_data = task_resp.json()
    task_id = task_data.get("taskId") or task_data.get("id")
    print(f"  Task created: {task_id}")

    # Step 4: Poll for completion
    max_wait = 120
    start = time.time()
    while time.time() - start < max_wait:
        time.sleep(3)
        status_resp = req.get(
            f"{zapcap_svc.base_url}/videos/{video_id}/task/{task_id}",
            headers=json_headers,
            timeout=30,
        )
        if status_resp.status_code != 200:
            continue
        status_data = status_resp.json()
        state = status_data.get("status", "").lower()
        if state in ("completed", "done"):
            download_url = status_data.get("downloadUrl") or status_data.get("url")
            if download_url:
                print(f"  COMPLETED: {download_url[:80]}...")
                return download_url
            break
        elif state in ("failed", "error"):
            print(f"  FAILED: {status_data}")
            return None
        elapsed = int(time.time() - start)
        print(f"  Polling... ({elapsed}s)")

    print(f"  TIMED OUT after {max_wait}s")
    return None


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    cfg = Config()

    zapcap_svc = ZapCapService(api_key=cfg.ZAPCAP_API_KEY)
    if not zapcap_svc.api_key:
        print("ERROR: ZAPCAP_API_KEY not set")
        return

    # Download test video once
    import requests as dl_req
    print(f"Downloading test video: {TEST_VIDEO_URL[:80]}...")
    dl_resp = dl_req.get(TEST_VIDEO_URL, timeout=60)
    if dl_resp.status_code != 200:
        print(f"ERROR: Could not download test video ({dl_resp.status_code})")
        return
    video_data = dl_resp.content
    print(f"Downloaded: {len(video_data) / 1024:.0f} KB")

    results = {}

    # Test A: No styleOptions (baseline)
    url_a = run_test("A (no styleOptions)", zapcap_svc, video_data, style_options=None)
    results["A_no_style"] = url_a

    # Test B: Position-only override (top=80 = bottom position)
    url_b = run_test("B (position only: top=80)", zapcap_svc, video_data, style_options={"top": 80})
    results["B_position_only"] = url_b

    # Summary
    print(f"\n{'='*60}")
    print("RESULTS SUMMARY")
    print(f"{'='*60}")
    for k, v in results.items():
        status = "OK" if v else "FAILED"
        print(f"  {k}: {status}")
        if v:
            print(f"    URL: {v}")

    # Save results
    out_path = os.path.join(OUT_DIR, "zapcap_position_only_results.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to: {out_path}")

    if url_a and url_b:
        print("\nBOTH TESTS PASSED - partial styleOptions (position-only) is accepted by ZapCap.")
        print("Compare the two videos to verify position actually changed:")
        print(f"  A (default):   {url_a}")
        print(f"  B (bottom 80): {url_b}")
    elif url_b:
        print("\nTest B passed but A failed — inconclusive. Position-only override at least doesn't error.")
    else:
        print("\nTest B FAILED — ZapCap may reject partial styleOptions. Keep as known limitation.")


if __name__ == "__main__":
    main()
