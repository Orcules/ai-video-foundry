"""Unit tests for the PIL-based end card overlay generator.

Tests _create_end_card_overlay_png() for various inputs, dimensions,
edge cases, visual properties, and configurable accent colors.
Includes video compositing tests that render the overlay onto real mp4s.
"""

import sys
import os
import subprocess
import tempfile
import unittest
from io import BytesIO

import numpy as np
from PIL import Image

# Ensure tvd_pipeline is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from tvd_pipeline.pipelines._helpers import (
    _create_end_card_overlay_png,
    _resolve_end_card_color,
)
from tvd_pipeline.config import get_pipeline_defaults

# Output dir for visual inspection artifacts
_TEST_OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "test_output")


class TestEndCardOverlayPNG(unittest.TestCase):
    """Tests for _create_end_card_overlay_png()."""

    def _load(self, png_bytes: bytes) -> Image.Image:
        return Image.open(BytesIO(png_bytes))

    # ------------------------------------------------------------------
    # Basic output validity
    # ------------------------------------------------------------------

    def test_returns_valid_png_bytes(self):
        png = _create_end_card_overlay_png("Test", "123 Main St", "+1 555-0100")
        self.assertIsInstance(png, bytes)
        self.assertGreater(len(png), 0)
        # PNG magic bytes
        self.assertTrue(png[:4] == b"\x89PNG")

    def test_output_is_rgba(self):
        img = self._load(
            _create_end_card_overlay_png("Name", "Addr", "Phone")
        )
        self.assertEqual(img.mode, "RGBA")

    def test_default_dimensions(self):
        img = self._load(
            _create_end_card_overlay_png("Name", "Addr", "Phone")
        )
        self.assertEqual(img.size, (1080, 1920))

    def test_custom_dimensions(self):
        img = self._load(
            _create_end_card_overlay_png("Name", "Addr", "Phone", width=720, height=1280)
        )
        self.assertEqual(img.size, (720, 1280))

    # ------------------------------------------------------------------
    # Transparency: minimal style = no background box, high transparency
    # ------------------------------------------------------------------

    def test_mostly_transparent(self):
        """Minimal style has no card box — >90% should be transparent."""
        img = self._load(
            _create_end_card_overlay_png("Biz", "Street", "Phone")
        )
        arr = np.array(img)
        total = arr.shape[0] * arr.shape[1]
        transparent = (arr[:, :, 3] == 0).sum()
        ratio = transparent / total
        self.assertGreater(ratio, 0.90, f"Only {ratio:.1%} transparent — expected >90%")

    def test_has_non_transparent_pixels(self):
        """There should be some visible content (the text + shadows)."""
        img = self._load(
            _create_end_card_overlay_png("Name", "Addr", "Phone")
        )
        arr = np.array(img)
        visible = (arr[:, :, 3] > 0).sum()
        self.assertGreater(visible, 100)

    # ------------------------------------------------------------------
    # Card position: should be in the bottom ~25% of the frame
    # ------------------------------------------------------------------

    def test_card_in_bottom_region(self):
        """All non-transparent pixels should be in the bottom 30% of the frame."""
        img = self._load(
            _create_end_card_overlay_png("Name", "Addr", "Phone", end_card_position="bottom")
        )
        arr = np.array(img)
        non_transparent_rows = np.where(arr[:, :, 3] > 0)[0]
        top_visible_row = non_transparent_rows.min()
        height = arr.shape[0]
        # The text should start no higher than 70% from the top
        self.assertGreater(
            top_visible_row / height, 0.70,
            f"Text starts at row {top_visible_row} ({top_visible_row/height:.1%}) — expected >70%",
        )

    def test_card_horizontally_centered(self):
        """The text should be roughly centered horizontally."""
        img = self._load(
            _create_end_card_overlay_png("Name", "Addr", "Phone")
        )
        arr = np.array(img)
        non_transparent_cols = np.where(arr[:, :, 3] > 0)[1]
        left = non_transparent_cols.min()
        right = non_transparent_cols.max()
        center = (left + right) / 2
        frame_center = arr.shape[1] / 2
        # Should be within 5% of frame center
        self.assertAlmostEqual(
            center / frame_center, 1.0, delta=0.05,
            msg=f"Text center at {center}, frame center at {frame_center}",
        )

    # ------------------------------------------------------------------
    # Edge cases
    # ------------------------------------------------------------------

    def test_empty_strings_returns_transparent(self):
        """All empty strings should produce a fully transparent PNG."""
        img = self._load(_create_end_card_overlay_png("", "", ""))
        arr = np.array(img)
        self.assertEqual((arr[:, :, 3] > 0).sum(), 0)

    def test_name_only(self):
        """Name only (no address/phone) should still produce visible text."""
        img = self._load(_create_end_card_overlay_png("My Business", "", ""))
        arr = np.array(img)
        visible = (arr[:, :, 3] > 0).sum()
        self.assertGreater(visible, 100)

    def test_phone_only(self):
        """Phone only should still produce visible text."""
        img = self._load(_create_end_card_overlay_png("", "", "+1 555-0100"))
        arr = np.array(img)
        visible = (arr[:, :, 3] > 0).sum()
        self.assertGreater(visible, 100)

    def test_long_business_name(self):
        """Very long name should not crash and text should stay within frame."""
        long_name = "A" * 200
        img = self._load(
            _create_end_card_overlay_png(long_name, "Short addr", "Phone")
        )
        arr = np.array(img)
        non_transparent_cols = np.where(arr[:, :, 3] > 0)[1]
        # Text should not extend beyond frame width
        self.assertLessEqual(non_transparent_cols.max(), img.size[0] - 1)

    def test_unicode_text(self):
        """Unicode characters (Hebrew, CJK, emoji) should not crash."""
        try:
            png = _create_end_card_overlay_png("בית קפה", "רחוב הראשי 5", "054-1234567")
            self.assertIsInstance(png, bytes)
            self.assertGreater(len(png), 0)
        except Exception as e:
            self.fail(f"Unicode text crashed: {e}")

    # ------------------------------------------------------------------
    # Different resolutions
    # ------------------------------------------------------------------

    def test_landscape_resolution(self):
        """Should work for landscape (16:9) resolution too."""
        img = self._load(
            _create_end_card_overlay_png("Biz", "Street", "Phone", width=1920, height=1080)
        )
        self.assertEqual(img.size, (1920, 1080))
        arr = np.array(img)
        self.assertGreater((arr[:, :, 3] > 0).sum(), 100)

    def test_square_resolution(self):
        img = self._load(
            _create_end_card_overlay_png("Biz", "Street", "Phone", width=1080, height=1080)
        )
        self.assertEqual(img.size, (1080, 1080))

    # ------------------------------------------------------------------
    # More lines = more visible content
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Position presets
    # ------------------------------------------------------------------

    def test_position_top(self):
        """end_card_position='top' → all non-transparent pixels in top 30%."""
        img = self._load(
            _create_end_card_overlay_png("Name", "Addr", "Phone", end_card_position="top")
        )
        arr = np.array(img)
        non_transparent_rows = np.where(arr[:, :, 3] > 0)[0]
        bottom_visible_row = non_transparent_rows.max()
        height = arr.shape[0]
        self.assertLess(
            bottom_visible_row / height, 0.30,
            f"Text ends at row {bottom_visible_row} ({bottom_visible_row/height:.1%}) — expected <30%",
        )

    def test_position_middle(self):
        """end_card_position='middle' → text center within 10% of frame center."""
        img = self._load(
            _create_end_card_overlay_png("Name", "Addr", "Phone", end_card_position="middle")
        )
        arr = np.array(img)
        non_transparent_rows = np.where(arr[:, :, 3] > 0)[0]
        text_center = (non_transparent_rows.min() + non_transparent_rows.max()) / 2
        frame_center = arr.shape[0] / 2
        self.assertAlmostEqual(
            text_center / frame_center, 1.0, delta=0.10,
            msg=f"Text center at {text_center}, frame center at {frame_center}",
        )

    def test_position_default_is_middle(self):
        """Default (no position arg) → text centered (middle)."""
        img = self._load(
            _create_end_card_overlay_png("Name", "Addr", "Phone")
        )
        arr = np.array(img)
        non_transparent_rows = np.where(arr[:, :, 3] > 0)[0]
        text_center = (non_transparent_rows.min() + non_transparent_rows.max()) / 2
        frame_center = arr.shape[0] / 2
        self.assertAlmostEqual(
            text_center / frame_center, 1.0, delta=0.10,
            msg=f"Default position should be middle: text center at {text_center}, frame center at {frame_center}",
        )

    # ------------------------------------------------------------------
    # More lines = more visible content
    # ------------------------------------------------------------------

    def test_more_lines_produces_more_visible_pixels(self):
        """Full text (3 lines) should have more visible pixels than name-only."""
        img_full = self._load(
            _create_end_card_overlay_png("Biz", "Street 123", "+1 555")
        )
        img_name = self._load(
            _create_end_card_overlay_png("Biz", "", "")
        )
        vis_full = (np.array(img_full)[:, :, 3] > 0).sum()
        vis_name = (np.array(img_name)[:, :, 3] > 0).sum()
        self.assertGreater(vis_full, vis_name)


class TestResolveEndCardColor(unittest.TestCase):
    """Tests for _resolve_end_card_color()."""

    def test_hex_color_direct(self):
        """Hex color like #FF6B9D should parse directly."""
        rgb = _resolve_end_card_color("#FF6B9D")
        self.assertEqual(rgb, (255, 107, 157))

    def test_hex_color_uppercase(self):
        rgb = _resolve_end_card_color("#FFD700")
        self.assertEqual(rgb, (255, 215, 0))

    def test_hex_color_lowercase(self):
        rgb = _resolve_end_card_color("#ff6b9d")
        self.assertEqual(rgb, (255, 107, 157))

    def test_preset_white(self):
        rgb = _resolve_end_card_color("white")
        self.assertEqual(rgb, (255, 255, 255))

    def test_preset_pink(self):
        rgb = _resolve_end_card_color("pink")
        self.assertEqual(rgb, (255, 107, 157))

    def test_preset_gold(self):
        rgb = _resolve_end_card_color("gold")
        self.assertEqual(rgb, (255, 215, 0))

    def test_preset_cyan(self):
        rgb = _resolve_end_card_color("cyan")
        self.assertEqual(rgb, (0, 229, 255))

    def test_unknown_color_falls_back_to_white(self):
        """Unknown preset name should default to white."""
        rgb = _resolve_end_card_color("doesnotexist")
        self.assertEqual(rgb, (255, 255, 255))

    def test_empty_string_falls_back_to_white(self):
        rgb = _resolve_end_card_color("")
        self.assertEqual(rgb, (255, 255, 255))

    def test_detail_color_independent(self):
        """Name=pink and detail=gold should resolve independently."""
        name_rgb = _resolve_end_card_color("pink")
        detail_rgb = _resolve_end_card_color("gold")
        self.assertNotEqual(name_rgb, detail_rgb)
        self.assertEqual(name_rgb, (255, 107, 157))
        self.assertEqual(detail_rgb, (255, 215, 0))

    def test_all_presets_are_valid(self):
        """Every preset in config should resolve to a valid RGB tuple."""
        defaults = get_pipeline_defaults()
        presets = defaults.get("end_card_color_presets", {})
        self.assertGreater(len(presets), 0, "No presets found in config")
        for name in presets:
            rgb = _resolve_end_card_color(name)
            self.assertIsInstance(rgb, tuple)
            self.assertEqual(len(rgb), 3)
            for c in rgb:
                self.assertGreaterEqual(c, 0)
                self.assertLessEqual(c, 255)


class TestEndCardVideoComposite(unittest.TestCase):
    """Render the overlay onto a real video via local FFmpeg.

    Produces test_output/end_card_composite.mp4 for visual inspection.
    """

    @classmethod
    def setUpClass(cls):
        os.makedirs(_TEST_OUTPUT_DIR, exist_ok=True)
        # Generate a 3-second synthetic 1080x1920 video (gradient + slow zoom)
        cls.src_video = os.path.join(_TEST_OUTPUT_DIR, "end_card_src.mp4")
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "lavfi",
                "-i", (
                    "color=c=#2a4a6b:s=1080x1920:d=3:r=30,"
                    "drawtext=text='End Card Scene':fontsize=60:"
                    "fontcolor=white@0.3:x=(w-text_w)/2:y=h*0.45"
                ),
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                "-pix_fmt", "yuv420p",
                cls.src_video,
            ],
            check=True,
            capture_output=True,
        )

    def test_composite_video_output(self):
        """Overlay PNG onto the synthetic video and verify the output exists."""
        # 1. Generate overlay PNG and save to disk
        png_bytes = _create_end_card_overlay_png(
            "OISHI HOUSE",
            "Spalena St, Nove Mesto, Prague",
            "+420 123 456 789",
        )
        overlay_path = os.path.join(_TEST_OUTPUT_DIR, "end_card_overlay.png")
        with open(overlay_path, "wb") as f:
            f.write(png_bytes)

        # 2. Composite with FFmpeg (same filter as Rendi would use)
        out_path = os.path.join(_TEST_OUTPUT_DIR, "end_card_composite.mp4")
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", self.src_video,
                "-i", overlay_path,
                "-filter_complex", "[0:v][1:v]overlay=0:0[out]",
                "-map", "[out]",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                out_path,
            ],
            capture_output=True,
        )
        self.assertEqual(
            result.returncode, 0,
            f"FFmpeg failed:\n{result.stderr.decode(errors='replace')}",
        )
        self.assertTrue(os.path.isfile(out_path))
        self.assertGreater(os.path.getsize(out_path), 1000)

        # 3. Probe the output to verify dimensions and duration
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height,duration",
                "-of", "csv=p=0",
                out_path,
            ],
            capture_output=True, text=True,
        )
        parts = probe.stdout.strip().split(",")
        self.assertEqual(parts[0], "1080")
        self.assertEqual(parts[1], "1920")

        print(f"\n  >>> Composite video saved: {out_path}")
        print(f"  >>> Open it to inspect the end card overlay visually.")

    def test_composite_on_colorful_backgrounds(self):
        """Render overlays on 3 different background colors for readability check."""
        colors = [
            ("bright", "#e8c547"),   # yellow/bright
            ("dark", "#1a1a2e"),     # very dark
            ("nature", "#3a7d44"),   # green
        ]
        png_bytes = _create_end_card_overlay_png(
            "Sunrise Bakery",
            "123 Ocean Drive, Miami Beach, FL",
            "+1 (305) 555-0199",
        )
        overlay_path = os.path.join(_TEST_OUTPUT_DIR, "end_card_overlay_multi.png")
        with open(overlay_path, "wb") as f:
            f.write(png_bytes)

        for label, color in colors:
            src = os.path.join(_TEST_OUTPUT_DIR, f"bg_{label}.mp4")
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-f", "lavfi", "-i",
                    f"color=c={color}:s=1080x1920:d=2:r=24",
                    "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", src,
                ],
                check=True, capture_output=True,
            )
            out = os.path.join(_TEST_OUTPUT_DIR, f"end_card_{label}.mp4")
            result = subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-i", src,
                    "-i", overlay_path,
                    "-filter_complex", "[0:v][1:v]overlay=0:0[out]",
                    "-map", "[out]",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                    "-pix_fmt", "yuv420p", out,
                ],
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, f"FFmpeg failed for {label}")
            self.assertTrue(os.path.isfile(out))
            print(f"  >>> {label} background: {out}")

    def test_all_preset_name_colors_on_video(self):
        """Render one video per preset color for visual comparison."""
        defaults = get_pipeline_defaults()
        presets = defaults.get("end_card_color_presets", {})
        self.assertGreater(len(presets), 0)

        for name in presets:
            png_bytes = _create_end_card_overlay_png(
                "OISHI HOUSE",
                "Spalena St, Nove Mesto, Prague",
                "+420 123 456 789",
                end_card_color=name,
                end_card_detail_color="white",
            )
            overlay_path = os.path.join(_TEST_OUTPUT_DIR, f"overlay_color_{name}.png")
            with open(overlay_path, "wb") as f:
                f.write(png_bytes)

            out = os.path.join(_TEST_OUTPUT_DIR, f"end_card_color_{name}.mp4")
            result = subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-i", self.src_video,
                    "-i", overlay_path,
                    "-filter_complex", "[0:v][1:v]overlay=0:0[out]",
                    "-map", "[out]",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                    "-pix_fmt", "yuv420p", out,
                ],
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, f"FFmpeg failed for color={name}")
            self.assertTrue(os.path.isfile(out))
            self.assertGreater(os.path.getsize(out), 1000)
            print(f"  >>> Color preset '{name}': {out}")

    def test_all_positions_on_video(self):
        """Render 3 videos (bottom/top/middle) for visual comparison."""
        for position in ("bottom", "top", "middle"):
            png_bytes = _create_end_card_overlay_png(
                "OISHI HOUSE",
                "Spalena St, Nove Mesto, Prague",
                "+420 123 456 789",
                end_card_position=position,
            )
            overlay_path = os.path.join(_TEST_OUTPUT_DIR, f"overlay_pos_{position}.png")
            with open(overlay_path, "wb") as f:
                f.write(png_bytes)

            out = os.path.join(_TEST_OUTPUT_DIR, f"end_card_pos_{position}.mp4")
            result = subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-i", self.src_video,
                    "-i", overlay_path,
                    "-filter_complex", "[0:v][1:v]overlay=0:0[out]",
                    "-map", "[out]",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                    "-pix_fmt", "yuv420p", out,
                ],
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, f"FFmpeg failed for position={position}")
            self.assertTrue(os.path.isfile(out))
            self.assertGreater(os.path.getsize(out), 1000)
            print(f"  >>> Position '{position}': {out}")

    def test_hex_color_on_video(self):
        """Render a hex color (#FF6B9D) overlay on video."""
        png_bytes = _create_end_card_overlay_png(
            "OISHI HOUSE",
            "Spalena St, Nove Mesto, Prague",
            "+420 123 456 789",
            end_card_color="#FF6B9D",
        )
        overlay_path = os.path.join(_TEST_OUTPUT_DIR, "overlay_hex_pink.png")
        with open(overlay_path, "wb") as f:
            f.write(png_bytes)

        out = os.path.join(_TEST_OUTPUT_DIR, "end_card_hex_pink.mp4")
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", self.src_video,
                "-i", overlay_path,
                "-filter_complex", "[0:v][1:v]overlay=0:0[out]",
                "-map", "[out]",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                "-pix_fmt", "yuv420p", out,
            ],
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertTrue(os.path.isfile(out))
        print(f"\n  >>> Hex color #FF6B9D: {out}")


if __name__ == "__main__":
    unittest.main()
