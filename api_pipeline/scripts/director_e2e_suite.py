"""Director end-to-end test suite.

Hits a locally running server (default http://localhost:8000) and verifies that
the Director (Gemini 3 Pro) picks correct tools and clip categories for five
representative brief types. Pure stdlib — no pip deps.

Run:
    python api_pipeline/scripts/director_e2e_suite.py

Optional env overrides:
    DIRECTOR_E2E_BASE_URL   default http://localhost:8000
    DIRECTOR_E2E_API_KEY    default sk-tvd-studio-bootstrap-orcules
    DIRECTOR_E2E_TIMEOUT    per-call HTTP timeout in seconds (default 240)

The container is expected to be already running. The script does NOT start it.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

BASE_URL = os.environ.get("DIRECTOR_E2E_BASE_URL", "http://localhost:8000").rstrip("/")
API_KEY = os.environ.get("DIRECTOR_E2E_API_KEY", "sk-tvd-studio-bootstrap-orcules")
HTTP_TIMEOUT = float(os.environ.get("DIRECTOR_E2E_TIMEOUT", "240"))

# ---------------------------------------------------------------------------
# Tiny HTTP helper (stdlib only)
# ---------------------------------------------------------------------------


def _request(
    method: str,
    path: str,
    body: Optional[Dict[str, Any]] = None,
    *,
    timeout: Optional[float] = None,
) -> Tuple[int, Dict[str, Any]]:
    """Issue an HTTP request and return (status_code, parsed_json_or_error_dict)."""
    url = f"{BASE_URL}{path}"
    data = None
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout or HTTP_TIMEOUT) as resp:
            status = resp.getcode()
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return status, json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return status, {"_raw": raw}
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"_raw": raw}
        payload["_http_error"] = True
        return e.code, payload
    except urllib.error.URLError as e:
        return 0, {"_network_error": True, "error": str(e.reason)}
    except Exception as e:  # pragma: no cover — last-ditch safety net
        return 0, {"_network_error": True, "error": str(e)}


# ---------------------------------------------------------------------------
# Pipeline glue: start session → send brief → switch mode → direct-storyboard
# ---------------------------------------------------------------------------


def start_session(initial_message: str) -> Tuple[Optional[str], Dict[str, Any]]:
    code, payload = _request(
        "POST",
        "/api/studio-chat/start",
        {"initial_message": initial_message},
    )
    if code != 200 or not payload.get("session_id"):
        return None, payload
    return payload["session_id"], payload


def send_message(session_id: str, message: str) -> Tuple[int, Dict[str, Any]]:
    return _request(
        "POST",
        "/api/studio-chat/message",
        {"session_id": session_id, "message": message},
    )


def set_mode(session_id: str, mode: str) -> Tuple[int, Dict[str, Any]]:
    return _request(
        "POST",
        "/api/studio-chat/mode",
        {"session_id": session_id, "mode": mode},
    )


def direct_storyboard(session_id: str) -> Tuple[int, Dict[str, Any]]:
    # Director can take a while (Gemini 3 Pro structured output) — give it room.
    return _request(
        "POST",
        "/api/studio-chat/direct-storyboard",
        {"session_id": session_id},
        timeout=max(HTTP_TIMEOUT, 300.0),
    )


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------


def _iter_clips(storyboard: Dict[str, Any]):
    for scene in storyboard.get("scenes") or []:
        for clip in scene.get("clips") or []:
            yield scene, clip


def has_framework_render(storyboard: Dict[str, Any], allowed_frameworks: Optional[List[str]] = None) -> Tuple[bool, List[str]]:
    """Return (found, list_of_frameworks_seen). If allowed_frameworks is given,
    require at least one match in that set."""
    seen: List[str] = []
    for _, clip in _iter_clips(storyboard):
        if clip.get("type") == "framework_render":
            fw = (clip.get("framework") or "").strip().lower()
            if fw:
                seen.append(fw)
    if not seen:
        return False, seen
    if allowed_frameworks is None:
        return True, seen
    allowed = {f.lower() for f in allowed_frameworks}
    return any(f in allowed for f in seen), seen


def has_voiceover(storyboard: Dict[str, Any]) -> bool:
    vo = storyboard.get("voiceover") or {}
    script = (vo.get("script") or "").strip()
    if script:
        return True
    # Fallback: some Director outputs put VO text on individual scenes
    for scene in storyboard.get("scenes") or []:
        if (scene.get("vo_text") or "").strip():
            return True
    return False


def clip_category_set(storyboard: Dict[str, Any]) -> set:
    out = set()
    for _, clip in _iter_clips(storyboard):
        t = (clip.get("type") or "").strip().lower()
        if t:
            out.add(t)
    return out


def is_needs_assets(payload: Dict[str, Any]) -> bool:
    needs = payload.get("needs_assets")
    return bool(needs and isinstance(needs, list) and len(needs) > 0)


def needs_assets_panels(payload: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for entry in payload.get("needs_assets") or []:
        if isinstance(entry, dict):
            panel = (entry.get("panel") or "").strip()
            if panel:
                out.append(panel)
    return out


def has_character_sheet_with_refs(storyboard: Dict[str, Any]) -> bool:
    cs = storyboard.get("character_sheet")
    if not isinstance(cs, dict):
        return False
    refs = cs.get("reference_image_urls") or []
    desc = (cs.get("subject_description") or "").strip()
    return bool(refs) or bool(desc)


# ---------------------------------------------------------------------------
# Test definitions
# ---------------------------------------------------------------------------


def _veo_categories() -> set:
    """Categories that count as 'Veo Ref Fast' or talking-head friendly clips."""
    return {"generate", "asset_image_animate", "seedance_multishot"}


def evaluate_math(payload: Dict[str, Any]) -> Tuple[bool, str]:
    storyboard = payload.get("storyboard") or {}
    if not storyboard:
        return False, "No storyboard returned"
    ok_fw, seen = has_framework_render(storyboard, allowed_frameworks=["manim", "remotion"])
    if not ok_fw:
        return False, (
            f"Expected at least one framework_render clip with framework in (manim, remotion); "
            f"saw frameworks={seen} categories={sorted(clip_category_set(storyboard))}"
        )
    if not has_voiceover(storyboard):
        return False, "Expected voiceover to be present (script or scene vo_text)"
    return True, f"framework_render frameworks={seen} + voiceover present"


def evaluate_influencer_no_photo(payload: Dict[str, Any]) -> Tuple[bool, str]:
    if not is_needs_assets(payload):
        return False, (
            "Expected needs_assets payload (user said 'I want to be in it' with no upload); "
            f"got keys={sorted(payload.keys())}"
        )
    panels = needs_assets_panels(payload)
    if "uploads_character" not in panels:
        return False, f"Expected panel=uploads_character in needs_assets; saw panels={panels}"
    return True, f"needs_assets requested panels={panels}"


def evaluate_saas_demo(payload: Dict[str, Any]) -> Tuple[bool, str]:
    storyboard = payload.get("storyboard") or {}
    if not storyboard:
        return False, "No storyboard returned"
    ok_fw, seen = has_framework_render(storyboard, allowed_frameworks=["remotion", "hyperframes"])
    if not ok_fw:
        return False, (
            f"Expected at least one framework_render clip with framework in (remotion, hyperframes); "
            f"saw frameworks={seen} categories={sorted(clip_category_set(storyboard))}"
        )
    return True, f"framework_render frameworks={seen}"


def evaluate_talking_head(payload: Dict[str, Any]) -> Tuple[bool, str]:
    # Acceptable: either needs_assets (asking for a character upload) OR a valid
    # storyboard built around plain Veo clips with a character_sheet.
    if is_needs_assets(payload):
        panels = needs_assets_panels(payload)
        return True, f"needs_assets requested panels={panels} (acceptable)"
    storyboard = payload.get("storyboard") or {}
    if not storyboard:
        return False, "No storyboard and no needs_assets returned"
    cats = clip_category_set(storyboard)
    veo_like = cats & _veo_categories()
    if not veo_like:
        return False, (
            f"Expected plain Veo-style clips ({sorted(_veo_categories())}) when no needs_assets; "
            f"saw categories={sorted(cats)}"
        )
    if not has_character_sheet_with_refs(storyboard):
        return False, "Expected a character_sheet (description or reference_image_urls) for talking head"
    return True, f"veo-style categories={sorted(veo_like)} + character_sheet present"


def evaluate_brand_promo(payload: Dict[str, Any]) -> Tuple[bool, str]:
    if is_needs_assets(payload):
        return False, (
            "Expected a full storyboard with no needs_assets (brief says 'no assets needed'); "
            f"got needs_assets panels={needs_assets_panels(payload)}"
        )
    storyboard = payload.get("storyboard") or {}
    if not storyboard:
        return False, "No storyboard returned"
    cats = clip_category_set(storyboard)
    veo_like = cats & _veo_categories()
    has_fw, fw_seen = has_framework_render(storyboard)
    if not veo_like:
        return False, f"Expected at least one Veo-style clip; saw categories={sorted(cats)}"
    if not has_fw:
        return False, (
            f"Expected a mix of categories including framework_render; "
            f"saw only categories={sorted(cats)}"
        )
    return True, f"mix of categories: veo={sorted(veo_like)} + framework={fw_seen}"


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------


TESTS: List[Dict[str, Any]] = [
    {
        "name": "MATH",
        "brief": (
            "30s educational video explaining compound interest with formulas and "
            "animated charts. Use clear narration, animated math equations, and "
            "graphs showing balance growth over time."
        ),
        "evaluator": evaluate_math,
    },
    {
        "name": "INFLUENCER_NO_PHOTO",
        "brief": (
            "60s influencer promo for my SaaS — I want to be in it, talking to the "
            "camera. No photo of me attached yet. Energetic and conversational."
        ),
        "evaluator": evaluate_influencer_no_photo,
    },
    {
        "name": "SAAS_DEMO",
        "brief": (
            "45s demo of my project management dashboard with on-screen stats, "
            "feature callouts, and a CTA at the end. Clean, modern, motion-graphic feel."
        ),
        "evaluator": evaluate_saas_demo,
    },
    {
        "name": "TALKING_HEAD",
        "brief": (
            "30s talking head where I just speak to camera. I'll provide the audio "
            "myself (no actual audio file attached right now). Simple framing, "
            "shallow depth of field."
        ),
        "evaluator": evaluate_talking_head,
    },
    {
        "name": "BRAND_PROMO",
        "brief": (
            "30s promo for a fictional coffee brand called LuminBean. Cinematic style, "
            "no real assets needed — invent the visuals. Include a mix of cinematic "
            "shots and a kinetic-typography moment for the tagline."
        ),
        "evaluator": evaluate_brand_promo,
    },
]


def run_one(test: Dict[str, Any]) -> Tuple[bool, str, float]:
    """Run a single brief end-to-end. Returns (passed, detail, elapsed_seconds)."""
    name = test["name"]
    brief = test["brief"]
    evaluator = test["evaluator"]

    t0 = time.time()
    print(f"\n[{name}] starting session…", flush=True)

    sid, start_payload = start_session(brief)
    if not sid:
        return False, f"start_session failed: {start_payload}", time.time() - t0
    print(f"[{name}] session_id={sid}", flush=True)

    # Make sure the brief lands as the latest user message in this session.
    # /studio-chat/start runs one turn so the agent has already seen it — but
    # send an explicit reinforcement so _latest_user_brief() definitely returns
    # the brief text (and not e.g. a stale assistant turn).
    code, msg_payload = send_message(sid, brief)
    if code != 200:
        return False, f"send_message failed (status={code}): {msg_payload}", time.time() - t0

    code, mode_payload = set_mode(sid, "director")
    if code != 200:
        return False, f"set_mode(director) failed (status={code}): {mode_payload}", time.time() - t0

    print(f"[{name}] calling /direct-storyboard…", flush=True)
    code, dir_payload = direct_storyboard(sid)
    if code not in (200, 422):  # 422 might still carry needs_assets in detail
        return False, f"direct-storyboard failed (status={code}): {dir_payload}", time.time() - t0
    if dir_payload.get("_network_error") or dir_payload.get("_http_error"):
        # _http_error can still be a meaningful payload — only fail if there's no useful body
        if not dir_payload.get("storyboard") and not dir_payload.get("needs_assets"):
            return False, f"director call errored: {dir_payload}", time.time() - t0

    try:
        passed, detail = evaluator(dir_payload)
    except Exception as e:
        return False, f"evaluator raised {type(e).__name__}: {e}", time.time() - t0

    return passed, detail, time.time() - t0


def main() -> int:
    print("=" * 72)
    print("Director E2E Suite")
    print(f"  base_url = {BASE_URL}")
    print(f"  api_key  = {API_KEY[:18]}…")
    print(f"  tests    = {len(TESTS)}")
    print("=" * 72, flush=True)

    # Cheap reachability check so we fail fast if the server is down
    code, payload = _request("GET", "/api/health", timeout=10)
    if code == 0:
        print(f"\nServer not reachable at {BASE_URL}: {payload.get('error')}")
        print("Is the Docker container running? Try: docker logs -f video-pipeline")
        return 2
    print(f"Health check: status={code}", flush=True)

    results: List[Tuple[str, bool, str, float]] = []
    for test in TESTS:
        passed, detail, elapsed = run_one(test)
        status = "PASS" if passed else "FAIL"
        print(f"[{test['name']}] {status} ({elapsed:.1f}s) — {detail}", flush=True)
        results.append((test["name"], passed, detail, elapsed))

    pass_count = sum(1 for _, p, _, _ in results if p)
    total = len(results)
    print("\n" + "=" * 72)
    print(f"SUMMARY: {pass_count}/{total} passed")
    print("=" * 72)
    for name, passed, detail, elapsed in results:
        status = "PASS" if passed else "FAIL"
        print(f"  {status:4s}  {name:24s}  ({elapsed:6.1f}s)  {detail}")

    return 0 if pass_count == total else 1


if __name__ == "__main__":
    sys.exit(main())
