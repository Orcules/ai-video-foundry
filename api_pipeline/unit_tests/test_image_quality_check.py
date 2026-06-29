"""Live test for the image quality gate (_check_image_quality).

Sends a real image to Gemini via the Vertex AI API and verifies that:
1. A matching image+prompt scores high (>= 7)
2. A mismatched image+prompt scores low (<= 3)
3. The score is a valid integer 1-10

Run inside Docker:
    docker exec video-pipeline python -m api_pipeline.unit_tests.test_image_quality_check

Run locally (requires Vertex AI credentials):
    python -m api_pipeline.unit_tests.test_image_quality_check
"""

import base64
import re
import sys

import requests

# ---------------------------------------------------------------------------
# Setup: load config from the monolith to get Vertex AI credentials
# ---------------------------------------------------------------------------
sys.path.insert(0, "/app")
from Comp_Videos.video_scene_processor import Config

cfg = Config()
API_KEY = cfg.VERTEX_AI_API_KEY
MODEL = cfg.VERTEX_AI_MODEL
PROJECT = cfg.VERTEX_AI_PROJECT_ID
LOCATION = cfg.VERTEX_AI_LOCATION
ENDPOINT = (
    f"https://aiplatform.googleapis.com/v1/projects/{PROJECT}"
    f"/locations/{LOCATION}/publishers/google/models/{MODEL}:generateContent"
    f"?key={API_KEY}"
)

# Public test image — beach / tropical ocean
TEST_IMAGE_URL = "https://images.unsplash.com/photo-1507525428034-b723cf961d3e?w=200&q=50"


def _call_gemini(image_b64: str, prompt_text: str) -> int:
    """Send image+prompt to Gemini and return parsed score (1-10), or -1 on failure."""
    check_prompt = (
        f"Rate how well this image matches the following prompt on a scale of 1-10.\n"
        f"Prompt: {prompt_text}\n\n"
        f"Reply with ONLY a single integer between 1 and 10. Nothing else."
    )
    payload = {
        "contents": [{"role": "user", "parts": [
            {"inlineData": {"mimeType": "image/jpeg", "data": image_b64}},
            {"text": check_prompt},
        ]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 128},
    }
    resp = requests.post(ENDPOINT, json=payload, timeout=30)
    if not resp.ok:
        print(f"  FAIL: HTTP {resp.status_code}: {resp.text[:200]}")
        return -1
    data = resp.json()
    candidates = data.get("candidates", [])
    if not candidates:
        print(f"  FAIL: No candidates in response")
        return -1
    parts = candidates[0].get("content", {}).get("parts", [])
    text = parts[0].get("text", "").strip() if parts else ""
    finish = candidates[0].get("finishReason", "?")
    thinking = data.get("usageMetadata", {}).get("thoughtsTokenCount", 0)
    if not text:
        print(f"  FAIL: Empty response (finishReason={finish}, thinkingTokens={thinking})")
        return -1
    match = re.search(r"\b(\d+)\b", text)
    if not match:
        print(f"  FAIL: Could not parse integer from: \"{text}\"")
        return -1
    score = int(match.group(1))
    print(f"  Response: \"{text}\" -> score={score}, thinkingTokens={thinking}")
    return score


def main():
    print(f"Model: {MODEL}")
    print(f"Endpoint: ...{ENDPOINT[-60:]}")
    print()

    # Download test image
    print("Downloading test image...")
    img_resp = requests.get(TEST_IMAGE_URL, timeout=10)
    if not img_resp.ok:
        print(f"FAIL: Could not download test image: HTTP {img_resp.status_code}")
        sys.exit(1)
    b64 = base64.b64encode(img_resp.content).decode()
    print(f"  OK: {len(img_resp.content)} bytes")
    print()

    passed = 0
    failed = 0

    # Test 1: matching prompt (beach image + beach prompt)
    print("TEST 1: Matching image+prompt (beach photo vs 'tropical beach with ocean')")
    score = _call_gemini(b64, "A tropical beach with clear blue ocean water and sandy shore")
    if 1 <= score <= 10 and score >= 7:
        print(f"  PASS: score {score}/10 >= 7")
        passed += 1
    elif 1 <= score <= 10:
        print(f"  FAIL: score {score}/10 < 7 (expected >= 7 for matching image)")
        failed += 1
    else:
        print(f"  FAIL: invalid score {score}")
        failed += 1
    print()

    # Test 2: mismatched prompt (beach image + snowy mountain prompt)
    print("TEST 2: Mismatched image+prompt (beach photo vs 'snowy mountain with ski lodge')")
    score = _call_gemini(b64, "A snowy mountain peak with a ski lodge and pine trees covered in fresh powder")
    if 1 <= score <= 10 and score <= 3:
        print(f"  PASS: score {score}/10 <= 3")
        passed += 1
    elif 1 <= score <= 10:
        print(f"  FAIL: score {score}/10 > 3 (expected <= 3 for mismatched image)")
        failed += 1
    else:
        print(f"  FAIL: invalid score {score}")
        failed += 1
    print()

    # Test 3: partially matching prompt (beach image + outdoor nature prompt)
    print("TEST 3: Partial match (beach photo vs 'outdoor nature scene with water')")
    score = _call_gemini(b64, "An outdoor nature scene with water and natural lighting")
    if 1 <= score <= 10:
        print(f"  PASS: valid score {score}/10 (partial match, any 1-10 is OK)")
        passed += 1
    else:
        print(f"  FAIL: invalid score {score}")
        failed += 1
    print()

    # Summary
    total = passed + failed
    print("=" * 40)
    print(f"Results: {passed}/{total} passed, {failed}/{total} failed")
    if failed:
        sys.exit(1)
    print("All tests passed!")


if __name__ == "__main__":
    main()
