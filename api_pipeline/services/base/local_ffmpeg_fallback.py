import os
import time
import logging
import tempfile
import subprocess
from typing import Any, Dict, List, Optional

import requests

from api_pipeline.services.base.config import config


logger = logging.getLogger(__name__)


class LocalFFmpegFallback:
    """Run trim/concat/add_audio/add_music locally with ffmpeg and upload result to GCS."""

    @staticmethod
    def _download_to_temp(url: str, suffix: str = "") -> Optional[str]:
        """Download URL to a temp file; return path or None."""
        try:
            r = requests.get(url, timeout=120)
            r.raise_for_status()
            f = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
            f.write(r.content)
            f.close()
            return f.name
        except Exception as e:
            logger.warning(f"LocalFFmpegFallback: download failed {url[:50]}...: {e}")
            return None

    @staticmethod
    def trim_video(gcs_storage_service: Any, video_url: str, duration: float) -> Optional[str]:
        """Trim to duration; output video-only to avoid :a errors. Returns GCS URL."""
        from api_pipeline.services.base.ffmpeg_processor import FFmpegProcessor
        if not FFmpegProcessor.check_ffmpeg_installed():
            return None
        path = LocalFFmpegFallback._download_to_temp(video_url, ".mp4")
        if not path:
            return None
        out_path = path + "_trimmed.mp4"
        try:
            cmd = [
                "ffmpeg", "-y", "-i", path, "-t", str(round(duration, 3)),
                "-map", "0:v", "-c:v", "libx264", "-preset", "fast", "-crf", str(config.VIDEO_CRF),
                "-an", "-movflags", "+faststart", out_path
            ]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if r.returncode != 0 or not os.path.exists(out_path):
                logger.warning(f"LocalFFmpegFallback trim failed: {r.stderr[:300]}")
                return None
            with open(out_path, "rb") as f:
                data = f.read()
            key = f"local_ffmpeg/trim_{int(time.time())}.mp4"
            url = gcs_storage_service.upload_video_bytes(data, key)
            return url
        except Exception as e:
            logger.warning(f"LocalFFmpegFallback trim error: {e}")
            return None
        finally:
            for p in (path, out_path):
                if p and os.path.exists(p):
                    try:
                        os.unlink(p)
                    except Exception:
                        pass

    @staticmethod
    def concat_video_only(gcs_storage_service: Any, video_data: List[Dict[str, Any]]) -> Optional[str]:
        """Concat videos (video-only). video_data = [{"video_url": u, "duration": d}, ...]. Returns GCS URL."""
        from api_pipeline.services.base.ffmpeg_processor import FFmpegProcessor
        if not FFmpegProcessor.check_ffmpeg_installed() or not video_data:
            return None
        temp_dir = tempfile.mkdtemp()
        paths = []
        try:
            for i, item in enumerate(video_data):
                u = item.get("video_url") if isinstance(item, dict) else item
                d = item.get("duration", 5.0) if isinstance(item, dict) else 5.0
                if not u:
                    continue
                p = LocalFFmpegFallback._download_to_temp(u, f"_i{i}.mp4")
                if not p:
                    continue
                out_p = os.path.join(temp_dir, f"clip_{i:04d}.mp4")
                cmd = [
                    "ffmpeg", "-y", "-i", p, "-t", str(round(d, 3)),
                    "-map", "0:v", "-c:v", "libx264", "-preset", "fast", "-crf", str(config.VIDEO_CRF),
                    "-an", "-movflags", "+faststart", out_p
                ]
                subprocess.run(cmd, capture_output=True, timeout=120)
                if os.path.exists(out_p):
                    paths.append(out_p)
                if p and os.path.exists(p):
                    try:
                        os.unlink(p)
                    except Exception:
                        pass
            if not paths:
                return None
            list_path = os.path.join(temp_dir, "list.txt")
            with open(list_path, "w") as f:
                for p in paths:
                    f.write(f"file '{p}'\n")
            out_path = os.path.join(temp_dir, "concat.mp4")
            cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", out_path]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if r.returncode != 0 or not os.path.exists(out_path):
                logger.warning(f"LocalFFmpegFallback concat failed: {r.stderr[:300]}")
                return None
            with open(out_path, "rb") as f:
                data = f.read()
            key = f"local_ffmpeg/concat_{int(time.time())}.mp4"
            return gcs_storage_service.upload_video_bytes(data, key)
        except Exception as e:
            logger.warning(f"LocalFFmpegFallback concat error: {e}")
            return None
        finally:
            try:
                for p in os.listdir(temp_dir):
                    os.unlink(os.path.join(temp_dir, p))
                os.rmdir(temp_dir)
            except Exception:
                pass

    @staticmethod
    def add_audio_to_video(gcs_storage_service: Any, video_url: str, audio_url: str) -> Optional[str]:
        """Mux video + audio (no [0:a]); returns GCS URL."""
        from api_pipeline.services.base.ffmpeg_processor import FFmpegProcessor
        if not FFmpegProcessor.check_ffmpeg_installed():
            return None
        v_path = LocalFFmpegFallback._download_to_temp(video_url, ".mp4")
        a_path = LocalFFmpegFallback._download_to_temp(audio_url, ".mp3")
        if not v_path or not a_path:
            return None
        out_path = v_path + "_out.mp4"
        try:
            cmd = [
                "ffmpeg", "-y", "-i", v_path, "-i", a_path,
                "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", out_path
            ]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if r.returncode != 0 or not os.path.exists(out_path):
                logger.warning(f"LocalFFmpegFallback add_audio failed: {r.stderr[:300]}")
                return None
            with open(out_path, "rb") as f:
                data = f.read()
            key = f"local_ffmpeg/vo_{int(time.time())}.mp4"
            return gcs_storage_service.upload_video_bytes(data, key)
        except Exception as e:
            logger.warning(f"LocalFFmpegFallback add_audio error: {e}")
            return None
        finally:
            for p in (v_path, a_path, out_path):
                if p and os.path.exists(p):
                    try:
                        os.unlink(p)
                    except Exception:
                        pass

    @staticmethod
    def add_music_to_video(
        gcs_storage_service: Any,
        video_url: str,
        music_url: str,
        music_volume: float = 0.35,
        assume_has_audio: bool = False
    ) -> Optional[str]:
        """Add music to video. If assume_has_audio=True, mix music with existing audio (VO); else replace audio. Returns GCS URL."""
        from api_pipeline.services.base.ffmpeg_processor import FFmpegProcessor
        if not FFmpegProcessor.check_ffmpeg_installed():
            return None
        v_path = LocalFFmpegFallback._download_to_temp(video_url, ".mp4")
        m_path = LocalFFmpegFallback._download_to_temp(music_url, ".mp3")
        if not v_path or not m_path:
            return None
        out_path = v_path + "_out.mp4"
        try:
            if assume_has_audio:
                # Mix voice + music (amix)
                cmd = [
                    "ffmpeg", "-y", "-i", v_path, "-i", m_path,
                    "-filter_complex",
                    f"[0:a]volume=1.0[voice];[1:a]volume={music_volume}[music];[voice][music]amix=inputs=2:duration=first:dropout_transition=2[mixed]",
                    "-map", "0:v", "-map", "[mixed]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", out_path
                ]
            else:
                cmd = [
                    "ffmpeg", "-y", "-i", v_path, "-i", m_path,
                    "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", out_path
                ]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if r.returncode != 0 or not os.path.exists(out_path):
                logger.warning(f"LocalFFmpegFallback add_music failed: {r.stderr[:300]}")
                return None
            with open(out_path, "rb") as f:
                data = f.read()
            key = f"local_ffmpeg/music_{int(time.time())}.mp4"
            return gcs_storage_service.upload_video_bytes(data, key)
        except Exception as e:
            logger.warning(f"LocalFFmpegFallback add_music error: {e}")
            return None
        finally:
            for p in (v_path, m_path, out_path):
                if p and os.path.exists(p):
                    try:
                        os.unlink(p)
                    except Exception:
                        pass
