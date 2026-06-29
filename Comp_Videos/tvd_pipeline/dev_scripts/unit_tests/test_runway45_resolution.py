"""Unit test: Runway Gen4.5 resolution type mismatch bug.

Reproduces the bug where `video_resolution` is passed as "720p" (string)
but `runway_direct.py` line 73 does `resolution <= 720` (int comparison),
causing TypeError: '<=' not supported between instances of 'str' and 'int'.

Usage:
    cd Comp_Videos
    python -m tvd_pipeline.dev_scripts.unit_tests.test_runway45_resolution
"""

import os
import sys

script_dir = os.path.dirname(os.path.abspath(__file__))
comp_videos_dir = os.path.dirname(os.path.dirname(script_dir))
if comp_videos_dir not in sys.path:
    sys.path.insert(0, comp_videos_dir)


def runway_ratio_original(resolution):
    """Original code from runway_direct.py line 73 — reproduces the bug."""
    return "720:1280" if resolution <= 720 else "1080:1920"


def normalize_resolution(resolution) -> int:
    """Proposed fix: normalize resolution to int before comparison."""
    if resolution is None:
        return 720
    res_str = str(resolution).strip().lower().replace("p", "")
    try:
        return int(res_str)
    except (ValueError, TypeError):
        return 720


def runway_ratio_fixed(resolution):
    """Fixed version: normalize then compare."""
    res = normalize_resolution(resolution)
    return "720:1280" if res <= 720 else "1080:1920"


def test_case(label, resolution, expect_error_original, expected_ratio):
    """Run a single test case."""
    # Test original (should TypeError for string inputs)
    original_error = None
    original_result = None
    try:
        original_result = runway_ratio_original(resolution)
    except TypeError as e:
        original_error = str(e)

    # Test fixed
    fixed_result = runway_ratio_fixed(resolution)

    # Validate
    original_ok = (expect_error_original and original_error) or \
                  (not expect_error_original and original_result == expected_ratio)
    fixed_ok = fixed_result == expected_ratio

    status = "PASS" if (original_ok and fixed_ok) else "FAIL"

    print(f"  [{status}] {label}")
    print(f"    Input: {resolution!r} (type={type(resolution).__name__})")
    if original_error:
        print(f"    Original: TypeError — {original_error}")
    else:
        print(f"    Original: {original_result}")
    print(f"    Fixed:    {fixed_result} (expected: {expected_ratio})")

    return status == "PASS"


def main():
    print("=" * 60)
    print("Runway Gen4.5 Resolution Type Mismatch — Unit Test")
    print("=" * 60)
    print()
    print("Bug: runway_direct.py line 73 does `resolution <= 720`")
    print("     but video_scene_processor.py passes resolution='720p' (string)")
    print("     causing TypeError on Veo->Runway failover")
    print()

    all_passed = True

    # --- Bug reproduction ---
    print("--- Bug Reproduction ---")
    all_passed &= test_case(
        'resolution="720p" (the actual bug)',
        "720p", expect_error_original=True, expected_ratio="720:1280",
    )
    all_passed &= test_case(
        'resolution="1080p"',
        "1080p", expect_error_original=True, expected_ratio="1080:1920",
    )
    print()

    # --- Already-working cases ---
    print("--- Already-Working Cases ---")
    all_passed &= test_case(
        "resolution=720 (int, existing behavior)",
        720, expect_error_original=False, expected_ratio="720:1280",
    )
    all_passed &= test_case(
        "resolution=1080 (int)",
        1080, expect_error_original=False, expected_ratio="1080:1920",
    )
    all_passed &= test_case(
        "resolution=480 (int, low res)",
        480, expect_error_original=False, expected_ratio="720:1280",
    )
    print()

    # --- Edge cases ---
    print("--- Edge Cases (fix only) ---")
    all_passed &= test_case(
        'resolution=None (default to 720)',
        None, expect_error_original=True, expected_ratio="720:1280",
    )
    all_passed &= test_case(
        'resolution="" (empty string, default to 720)',
        "", expect_error_original=True, expected_ratio="720:1280",
    )
    all_passed &= test_case(
        'resolution="480p"',
        "480p", expect_error_original=True, expected_ratio="720:1280",
    )
    all_passed &= test_case(
        'resolution="1080P" (uppercase P)',
        "1080P", expect_error_original=True, expected_ratio="1080:1920",
    )
    print()

    # --- Real-world failover scenario ---
    print("--- Simulated Failover Scenario ---")
    print("  video_scene_processor.py line ~382:")
    print("    result = self.runway_direct_service.generate_video(")
    print("        ..., resolution=resolution,  # resolution comes as '720p'")
    print("    )")
    print()

    # Simulate the exact flow from video_scene_processor.py
    resolution = "720p"  # This is what comes from the caller
    try:
        # Original code path
        _ = runway_ratio_original(resolution)
        print("  [UNEXPECTED] Original code did NOT raise TypeError")
        all_passed = False
    except TypeError:
        print("  [CONFIRMED] Original code raises TypeError with '720p'")

    # Fixed code path
    res_int = normalize_resolution(resolution)
    ratio = "720:1280" if res_int <= 720 else "1080:1920"
    print(f"  [FIXED]     normalize_resolution('{resolution}') -> {res_int} -> ratio={ratio}")
    print()

    # --- Proposed fix location ---
    print("--- Proposed Fix ---")
    print("  File: Comp_Videos/video_scene_processor.py, _generate_video() Runway branch")
    print("  Before passing to runway_direct_service.generate_video():")
    print('    _res = int(str(resolution).replace("p", "")) if resolution else 720')
    print("    result = self.runway_direct_service.generate_video(..., resolution=_res)")
    print()

    # Final summary
    print("=" * 60)
    if all_passed:
        print("ALL TESTS PASSED")
    else:
        print("SOME TESTS FAILED")
    print("=" * 60)

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
