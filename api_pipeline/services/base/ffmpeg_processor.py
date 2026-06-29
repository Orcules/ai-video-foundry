import os
import time
import logging
import subprocess
from typing import Dict, Any, List, Optional, Tuple

import requests

from api_pipeline.services.base.config import config

# PySceneDetect for better scene detection
try:
    from scenedetect import detect, ContentDetector, AdaptiveDetector
    PYSCENEDETECT_AVAILABLE = True
except ImportError:
    PYSCENEDETECT_AVAILABLE = False

logger = logging.getLogger(__name__)


class FFmpegProcessor:
    """Service for video processing using FFmpeg (local or cloud via Rendi.dev)."""

    _ffmpeg_available: Optional[bool] = None
    _rendi_api_key: Optional[str] = None

    @classmethod
    def check_ffmpeg_installed(cls) -> bool:
        """Check if FFmpeg is installed locally.

        Returns:
            True if FFmpeg is available, False otherwise.
        """
        if cls._ffmpeg_available is not None:
            return cls._ffmpeg_available

        try:
            result = subprocess.run(
                ['ffmpeg', '-version'],
                capture_output=True,
                timeout=10
            )
            cls._ffmpeg_available = result.returncode == 0
            if cls._ffmpeg_available:
                logger.info("✅ FFmpeg is installed locally")
            else:
                logger.warning("⚠️ FFmpeg not found, will use cloud processing via Rendi.dev")
            return cls._ffmpeg_available
        except Exception:
            cls._ffmpeg_available = False
            logger.warning("⚠️ FFmpeg not installed locally, will use cloud processing via Rendi.dev")
            return False

    @classmethod
    def set_rendi_api_key(cls, api_key: str) -> None:
        """Set Rendi API key for cloud fallback.

        Args:
            api_key: Rendi.dev API key.
        """
        cls._rendi_api_key = api_key

    @staticmethod
    def download_video(video_url: str, output_path: str) -> bool:
        """Download a video from URL.

        Args:
            video_url: URL of the video to download.
            output_path: Path to save the downloaded video.

        Returns:
            True if successful, False otherwise.
        """
        try:
            logger.info(f"📥 Downloading video from: {video_url[:50]}...")
            response = requests.get(video_url, stream=True, timeout=120)
            response.raise_for_status()

            with open(output_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

            logger.info(f"✅ Video downloaded to: {output_path}")
            return True

        except Exception as e:
            logger.error(f"❌ Error downloading video: {e}")
            return False

    @staticmethod
    def detect_scenes(
        video_path: str,
        threshold: float = 5.0,
        min_scene_duration: float = 1.0,
        use_adaptive: bool = True
    ) -> List[float]:
        """Detect scene changes in a video using PySceneDetect (preferred) or FFmpeg fallback.

        Args:
            video_path: Path to the video file.
            threshold: Scene change detection threshold.
                       For PySceneDetect ContentDetector: 20-35 is typical (higher = less sensitive)
                       For FFmpeg: 0.1-0.5 (lower = more sensitive)
            min_scene_duration: Minimum scene duration in seconds to prevent over-detection.
            use_adaptive: If True, uses AdaptiveDetector which adjusts to video content.

        Returns:
            List of timestamps (in seconds) where scenes start.
        """
        # Try PySceneDetect first (better results)
        if PYSCENEDETECT_AVAILABLE:
            try:
                return FFmpegProcessor._detect_scenes_pyscenedetect(
                    video_path,
                    threshold=threshold,
                    min_scene_duration=min_scene_duration,
                    use_adaptive=use_adaptive
                )
            except Exception as e:
                logger.warning(f"⚠️ PySceneDetect failed: {e}, falling back to FFmpeg")

        # Fallback to FFmpeg
        return FFmpegProcessor._detect_scenes_ffmpeg(video_path, threshold=0.3)

    @staticmethod
    def _detect_scenes_pyscenedetect(
        video_path: str,
        threshold: float = 27.0,
        min_scene_duration: float = 1.0,
        use_adaptive: bool = True
    ) -> List[float]:
        """Detect scenes using PySceneDetect library (more accurate).

        Args:
            video_path: Path to the video file.
            threshold: Detection threshold (20-35 typical, higher = less sensitive).
            min_scene_duration: Minimum scene duration in seconds.
            use_adaptive: Use AdaptiveDetector (better for varying content).

        Returns:
            List of timestamps (in seconds) where scenes start.
        """
        logger.info(f"🎬 Detecting scenes with PySceneDetect: {video_path}")
        logger.info(f"   Threshold: {threshold}, Min duration: {min_scene_duration}s, Adaptive: {use_adaptive}")

        try:
            # Choose detector
            if use_adaptive:
                # AdaptiveDetector adjusts threshold based on local content
                # Good for videos with varying scene types
                detector = AdaptiveDetector(
                    adaptive_threshold=threshold,
                    min_scene_len=int(min_scene_duration * 30)  # Assuming ~30fps
                )
                logger.info("   Using AdaptiveDetector")
            else:
                # ContentDetector uses fixed threshold
                # Good when you know the video style
                detector = ContentDetector(
                    threshold=threshold,
                    min_scene_len=int(min_scene_duration * 30)
                )
                logger.info("   Using ContentDetector")

            # Detect scenes
            scene_list = detect(video_path, detector)

            # Get video duration for filtering short last scenes
            # Try OpenCV first (doesn't require FFmpeg), then FFmpeg, then fallback
            video_duration = 0
            try:
                import cv2
                cap = cv2.VideoCapture(video_path)
                if cap.isOpened():
                    fps = cap.get(cv2.CAP_PROP_FPS)
                    frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
                    if fps > 0 and frame_count > 0:
                        video_duration = frame_count / fps
                        logger.info(f"   Video duration (OpenCV): {video_duration:.2f}s")
                    cap.release()
            except Exception as e:
                logger.debug(f"   Could not get duration via OpenCV: {e}")

            # Fallback to FFmpeg if OpenCV didn't work
            if video_duration <= 0:
                video_duration = FFmpegProcessor.get_video_duration(video_path)

            # Final fallback
            if video_duration <= 0:
                video_duration = 30.0
                logger.warning(f"   ⚠️ Could not determine video duration, using fallback: {video_duration}s")

            # Extract start timestamps
            timestamps = [0.0]  # Always include start
            for scene in scene_list:
                start_time = scene[0].get_seconds()
                if start_time > 0 and start_time not in timestamps:
                    timestamps.append(start_time)

            # Sort timestamps
            timestamps.sort()

            # Get max scene duration from config
            max_scene_duration = config.PYSCENEDETECT_MAX_SCENE_DURATION

            # Step 1: Split scenes that are too long
            split_timestamps = []
            for i, ts in enumerate(timestamps):
                split_timestamps.append(ts)

                if i + 1 < len(timestamps):
                    next_ts = timestamps[i + 1]
                else:
                    next_ts = video_duration

                scene_duration = next_ts - ts

                # If scene is too long, split it into smaller segments
                if scene_duration > max_scene_duration:
                    num_splits = int(scene_duration / max_scene_duration)
                    split_duration = scene_duration / (num_splits + 1)

                    for j in range(1, num_splits + 1):
                        new_ts = ts + (j * split_duration)
                        if new_ts < next_ts - 0.5:  # Don't add if too close to next scene
                            split_timestamps.append(new_ts)
                            logger.info(f"   ✂️ Splitting long scene: added timestamp at {new_ts:.2f}s")

            # Sort after splitting
            split_timestamps.sort()

            # Step 2: Filter out scenes that would be too short
            filtered_timestamps = []
            for i, ts in enumerate(split_timestamps):
                if i + 1 < len(split_timestamps):
                    scene_duration = split_timestamps[i + 1] - ts
                else:
                    scene_duration = video_duration - ts  # Last scene duration

                if scene_duration >= min_scene_duration or i == 0:  # Always keep first scene
                    filtered_timestamps.append(ts)
                else:
                    logger.info(f"   ⏭️ Filtering out scene at {ts:.2f}s (duration: {scene_duration:.2f}s < {min_scene_duration}s)")

            timestamps = filtered_timestamps[:config.MAX_SCENES]

            logger.info(f"✅ PySceneDetect found {len(timestamps)} scenes (after filtering)")
            for i, ts in enumerate(timestamps):
                logger.info(f"   Scene {i+1}: starts at {ts:.2f}s")

            return timestamps

        except Exception as e:
            logger.error(f"❌ PySceneDetect error: {e}")
            raise

    @staticmethod
    def _detect_scenes_ffmpeg(video_path: str, threshold: float = 0.3) -> List[float]:
        """Detect scene changes using FFmpeg (fallback method).

        Args:
            video_path: Path to the video file.
            threshold: Scene change threshold (0-1, lower = more sensitive).

        Returns:
            List of timestamps (in seconds) where scenes start.
        """
        # Check if FFmpeg is available
        if not FFmpegProcessor.check_ffmpeg_installed():
            logger.info("🌐 Using estimated scene intervals (no local FFmpeg)")
            return FFmpegProcessor._get_equal_intervals_simple(config.MAX_SCENES)

        try:
            logger.info(f"🎬 Detecting scenes with FFmpeg: {video_path}")

            # Use ffprobe to get scene changes
            cmd = [
                'ffprobe',
                '-v', 'quiet',
                '-show_entries', 'frame=pkt_pts_time',
                '-select_streams', 'v',
                '-of', 'csv=p=0',
                '-f', 'lavfi',
                f"movie={video_path},select='gt(scene,{threshold})'"
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

            if result.returncode != 0:
                logger.warning("⚠️ FFmpeg scene detection failed, using equal intervals")
                return FFmpegProcessor._get_equal_intervals(video_path)

            # Parse timestamps
            timestamps = [0.0]  # Always include start
            for line in result.stdout.strip().split('\n'):
                if line:
                    try:
                        timestamp = float(line)
                        timestamps.append(timestamp)
                    except ValueError:
                        continue

            logger.info(f"✅ FFmpeg detected {len(timestamps)} scenes")
            return timestamps[:config.MAX_SCENES]

        except Exception as e:
            logger.error(f"❌ FFmpeg scene detection error: {e}")
            return FFmpegProcessor._get_equal_intervals(video_path)

    @staticmethod
    def _get_equal_intervals_simple(num_scenes: int, assumed_duration: float = 30.0) -> List[float]:
        """Get equal time intervals without needing FFmpeg.

        Args:
            num_scenes: Number of scenes to create.
            assumed_duration: Assumed video duration if unknown.

        Returns:
            List of timestamps for equal intervals.
        """
        interval = assumed_duration / num_scenes
        return [i * interval for i in range(num_scenes)]

    @staticmethod
    def _get_equal_intervals(video_path: str) -> List[float]:
        """Get equal time intervals for a video.

        Args:
            video_path: Path to the video file.

        Returns:
            List of timestamps for equal intervals.
        """
        try:
            duration = FFmpegProcessor.get_video_duration(video_path)
            if duration <= 0:
                duration = 30.0  # Default 30 seconds

            num_scenes = min(config.MAX_SCENES, max(1, int(duration / 5)))
            interval = duration / num_scenes

            return [i * interval for i in range(num_scenes)]

        except Exception as e:
            logger.error(f"❌ Error getting equal intervals: {e}")
            return [0.0]

    @staticmethod
    def get_video_duration(video_path: str) -> float:
        """Get the duration of a video in seconds.

        Args:
            video_path: Path to the video file.

        Returns:
            Duration in seconds.
        """
        if not FFmpegProcessor.check_ffmpeg_installed():
            return 30.0  # Default assumed duration

        try:
            cmd = [
                'ffprobe',
                '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1',
                video_path
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

            if result.returncode == 0:
                return float(result.stdout.strip())

            return 30.0

        except Exception as e:
            logger.error(f"❌ Error getting video duration: {e}")
            return 30.0

    @staticmethod
    def get_audio_duration(audio_path_or_url: str) -> float:
        """Get audio duration in seconds via ffprobe (local file or URL). Returns 0.0 on failure."""
        if not FFmpegProcessor.check_ffmpeg_installed():
            return 0.0
        try:
            cmd = [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", audio_path_or_url
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if result.returncode == 0 and result.stdout.strip():
                return float(result.stdout.strip())
        except Exception as e:
            logger.debug(f"get_audio_duration failed: {e}")
        return 0.0

    @staticmethod
    def extract_frames_entire_video(
        video_path: str,
        video_duration: float,
        output_dir: str,
        fps: int = 1
    ) -> List[Tuple[float, str]]:
        """Extract frames from the entire video at specified FPS.

        Args:
            video_path: Path to the video file.
            video_duration: Total video duration in seconds.
            output_dir: Directory to save extracted frames.
            fps: Frames per second to extract (default 1).

        Returns:
            List of (timestamp, frame_path) tuples.
        """
        os.makedirs(output_dir, exist_ok=True)

        # Calculate frame timestamps
        frame_interval = 1.0 / fps
        num_frames = max(1, int(video_duration * fps))

        logger.info(f"🎬 Extracting {num_frames} frames from entire video ({fps}/sec)...")

        frames_with_timestamps = []

        # Check if local FFmpeg is available
        if FFmpegProcessor.check_ffmpeg_installed():
            # Use local FFmpeg
            for i in range(num_frames):
                timestamp = (i * frame_interval) + (frame_interval / 2)  # Center of each slot
                if timestamp >= video_duration:
                    break

                output_path = os.path.join(output_dir, f"frame_{i:04d}_{timestamp:.1f}s.jpg")

                cmd = [
                    'ffmpeg',
                    '-y',
                    '-ss', str(timestamp),
                    '-i', video_path,
                    '-vframes', '1',
                    '-q:v', '2',
                    output_path
                ]

                result = subprocess.run(cmd, capture_output=True, timeout=30)

                if result.returncode == 0 and os.path.exists(output_path):
                    frames_with_timestamps.append((timestamp, output_path))

            logger.info(f"✅ Extracted {len(frames_with_timestamps)} frames (local FFmpeg)")
        else:
            # Use OpenCV (available via PySceneDetect)
            try:
                import cv2
                cap = cv2.VideoCapture(video_path)

                if not cap.isOpened():
                    logger.error("❌ Could not open video with OpenCV")
                    return []

                video_fps = cap.get(cv2.CAP_PROP_FPS)

                for i in range(num_frames):
                    timestamp = (i * frame_interval) + (frame_interval / 2)
                    if timestamp >= video_duration:
                        break

                    # Seek to frame
                    frame_number = int(timestamp * video_fps)
                    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_number)

                    ret, frame = cap.read()
                    if ret:
                        output_path = os.path.join(output_dir, f"frame_{i:04d}_{timestamp:.1f}s.jpg")
                        cv2.imwrite(output_path, frame)
                        frames_with_timestamps.append((timestamp, output_path))

                cap.release()
                logger.info(f"✅ Extracted {len(frames_with_timestamps)} frames (OpenCV)")

            except Exception as e:
                logger.error(f"❌ Error extracting frames with OpenCV: {e}")
                return []

        return frames_with_timestamps

    @staticmethod
    def extract_frames_entire_video_cloud(
        video_url: str,
        video_duration: float,
        output_dir: str,
        rendi_api_key: str,
        fps: int = 1
    ) -> List[Tuple[float, str]]:
        """Extract frames from entire video using Rendi.dev cloud.

        Args:
            video_url: URL of the video.
            video_duration: Total video duration in seconds.
            output_dir: Local directory to save downloaded frames.
            rendi_api_key: Rendi.dev API key.
            fps: Frames per second to extract (default 1).

        Returns:
            List of (timestamp, frame_path) tuples.
        """
        os.makedirs(output_dir, exist_ok=True)

        frame_interval = 1.0 / fps
        num_frames = max(1, int(video_duration * fps))

        logger.info(f"🌐 Extracting {num_frames} frames from video via Rendi.dev cloud...")

        frames_with_timestamps = []
        headers = {
            "X-API-KEY": rendi_api_key,
            "Content-Type": "application/json"
        }
        base_url = config.RENDI_BASE_URL

        for i in range(num_frames):
            timestamp = (i * frame_interval) + (frame_interval / 2)
            if timestamp >= video_duration:
                break

            # Create FFmpeg command to extract single frame
            ffmpeg_command = f"-ss {timestamp} -i {{{{in_1}}}} -vframes 1 -q:v 2 {{{{out_1}}}}"

            payload = {
                "ffmpeg_command": ffmpeg_command,
                "input_files": {"in_1": video_url},
                "output_files": {"out_1": f"frame_{i}.jpg"},
                "vcpu_count": 2,
                "max_command_run_seconds": 60
            }

            try:
                response = requests.post(
                    f"{base_url}/v1/run-ffmpeg-command",
                    headers=headers,
                    json=payload,
                    timeout=60
                )

                if response.status_code == 200:
                    result = response.json()
                    command_id = result.get("command_id")

                    if command_id:
                        frame_url = FFmpegProcessor._wait_for_rendi_frame(
                            command_id, headers, base_url
                        )

                        if frame_url:
                            local_path = os.path.join(output_dir, f"frame_{i:04d}_{timestamp:.1f}s.jpg")
                            if FFmpegProcessor._download_frame(frame_url, local_path):
                                frames_with_timestamps.append((timestamp, local_path))
                                if (i + 1) % 5 == 0:  # Log every 5 frames
                                    logger.info(f"   ✅ Extracted {i+1}/{num_frames} frames...")

                time.sleep(0.3)  # Small delay between requests

            except Exception as e:
                logger.warning(f"⚠️ Failed to extract frame at {timestamp:.1f}s: {e}")

        logger.info(f"✅ Extracted {len(frames_with_timestamps)} frames via cloud")
        return frames_with_timestamps

    @staticmethod
    def extract_frames(
        video_path: str,
        start_time: float,
        end_time: float,
        output_dir: str
    ) -> List[str]:
        """Extract frames from a video segment (1 frame per second).

        Args:
            video_path: Path to the video file.
            start_time: Start time in seconds.
            end_time: End time in seconds.
            output_dir: Directory to save extracted frames.

        Returns:
            List of paths to extracted frame images.
        """
        if not FFmpegProcessor.check_ffmpeg_installed():
            # Use Rendi.dev cloud extraction
            return FFmpegProcessor._extract_frames_cloud(
                video_path, start_time, end_time, output_dir
            )

        try:
            duration = end_time - start_time
            if duration <= 0:
                duration = 5.0  # Default 5 seconds

            # Extract frames based on FRAMES_PER_SECOND config
            fps = config.FRAMES_PER_SECOND
            num_frames = max(1, int(duration * fps))
            frame_interval = 1.0 / fps  # Time between frames
            frame_paths = []

            logger.info(f"🎬 Extracting {num_frames} frames ({fps}/sec) from scene [{start_time:.1f}s - {end_time:.1f}s]")

            for i in range(num_frames):
                # Calculate timestamp: start + (frame_index * interval) + half_interval (center of each slot)
                timestamp = start_time + (i * frame_interval) + (frame_interval / 2)
                if timestamp >= end_time:
                    break

                output_path = os.path.join(output_dir, f"frame_{int(start_time)}_{i}.jpg")

                cmd = [
                    'ffmpeg',
                    '-y',
                    '-ss', str(timestamp),
                    '-i', video_path,
                    '-vframes', '1',
                    '-q:v', '2',
                    output_path
                ]

                result = subprocess.run(cmd, capture_output=True, timeout=30)

                if result.returncode == 0 and os.path.exists(output_path):
                    frame_paths.append(output_path)
                    logger.debug(f"✅ Extracted frame at {timestamp:.2f}s")

            logger.info(f"✅ Extracted {len(frame_paths)} frames ({fps}/sec) from scene [{start_time:.1f}s - {end_time:.1f}s]")
            return frame_paths

        except Exception as e:
            logger.error(f"❌ Error extracting frames: {e}")
            return []

    @staticmethod
    def _extract_frames_cloud(
        video_path: str,
        start_time: float,
        end_time: float,
        output_dir: str
    ) -> List[str]:
        """Extract frames using Rendi.dev cloud FFmpeg (1 frame per second).

        This method uploads the video URL to Rendi and extracts frames in the cloud.
        """
        if not FFmpegProcessor._rendi_api_key:
            logger.error("❌ Rendi API key not set for cloud frame extraction")
            return []

        try:
            # For cloud extraction, we need the original video URL, not local path
            # This is a limitation - we'll return empty and let the pipeline handle it
            logger.warning("⚠️ Cloud frame extraction requires video URL - using URL-based extraction")
            return []

        except Exception as e:
            logger.error(f"❌ Error in cloud frame extraction: {e}")
            return []

    @staticmethod
    def extract_frames_from_url(
        video_url: str,
        start_time: float,
        end_time: float,
        output_dir: str,
        rendi_api_key: str
    ) -> List[str]:
        """Extract frames from video URL using Rendi.dev cloud FFmpeg (1 per second).

        Args:
            video_url: URL of the video.
            start_time: Start time of scene in seconds.
            end_time: End time of scene in seconds.
            output_dir: Local directory to save downloaded frames.
            rendi_api_key: Rendi.dev API key.

        Returns:
            List of paths to extracted frame images.
        """
        try:
            duration = end_time - start_time
            fps = config.FRAMES_PER_SECOND
            num_frames = max(1, int(duration * fps))  # Frames based on config FPS
            frame_interval = 1.0 / fps  # Time between frames

            # Generate timestamps based on configured FPS
            timestamps = []
            for i in range(num_frames):
                # Calculate timestamp: start + (frame_index * interval) + half_interval (center of slot)
                timestamp = start_time + (i * frame_interval) + (frame_interval / 2)
                if timestamp < end_time:
                    timestamps.append(timestamp)

            if not timestamps:
                timestamps = [start_time + duration / 2]  # At least middle frame

            logger.info(f"🌐 Extracting {len(timestamps)} frames ({fps}/sec) via Rendi.dev cloud...")

            frame_paths = []
            headers = {
                "X-API-KEY": rendi_api_key,
                "Content-Type": "application/json"
            }
            base_url = config.RENDI_BASE_URL

            for i, timestamp in enumerate(timestamps):
                # Create FFmpeg command to extract single frame
                ffmpeg_command = f"-ss {timestamp} -i {{{{in_1}}}} -vframes 1 -q:v 2 {{{{out_1}}}}"

                payload = {
                    "ffmpeg_command": ffmpeg_command,
                    "input_files": {"in_1": video_url},
                    "output_files": {"out_1": f"frame_{i}.jpg"},
                    "vcpu_count": 2,
                    "max_command_run_seconds": 60
                }

                response = requests.post(
                    f"{base_url}/v1/run-ffmpeg-command",
                    headers=headers,
                    json=payload,
                    timeout=60
                )

                if response.status_code == 200:
                    result = response.json()
                    command_id = result.get("command_id")

                    if command_id:
                        # Poll for completion
                        frame_url = FFmpegProcessor._wait_for_rendi_frame(
                            command_id, headers, base_url
                        )

                        if frame_url:
                            # Download frame locally
                            local_path = os.path.join(output_dir, f"frame_{i}.jpg")
                            if FFmpegProcessor._download_frame(frame_url, local_path):
                                frame_paths.append(local_path)
                                logger.info(f"✅ Extracted frame {i+1}/{len(timestamps)} at {timestamp:.1f}s")

                # Small delay between requests
                time.sleep(0.5)

            logger.info(f"✅ Extracted {len(frame_paths)} frames ({fps}/sec) via cloud")
            return frame_paths

        except Exception as e:
            logger.error(f"❌ Error extracting frames from URL: {e}")
            return []

    @staticmethod
    def _wait_for_rendi_frame(
        command_id: str,
        headers: Dict,
        base_url: str,
        timeout: int = 120
    ) -> Optional[str]:
        """Wait for Rendi frame extraction to complete."""
        start_time = time.time()

        while time.time() - start_time < timeout:
            try:
                response = requests.get(
                    f"{base_url}/v1/commands/{command_id}",
                    headers=headers,
                    timeout=30
                )

                if response.status_code == 200:
                    result = response.json()
                    status = result.get("status", "").lower()

                    if status in ["completed", "success"]:
                        output_files = result.get("output_files", {})
                        if "out_1" in output_files:
                            return output_files["out_1"].get("storage_url")

                    elif status == "failed":
                        return None

                time.sleep(3)

            except Exception:
                time.sleep(3)

        return None

    @staticmethod
    def _download_frame(url: str, output_path: str) -> bool:
        """Download a frame image from URL."""
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()

            with open(output_path, 'wb') as f:
                f.write(response.content)

            return os.path.exists(output_path)

        except Exception:
            return False

    @staticmethod
    def extract_audio(video_path: str, output_path: str) -> bool:
        """Extract audio track from video.

        Args:
            video_path: Path to the video file.
            output_path: Path to save the extracted audio.

        Returns:
            True if successful, False otherwise.
        """
        if not FFmpegProcessor.check_ffmpeg_installed():
            logger.warning("⚠️ FFmpeg not available for local audio extraction")
            return False

        try:
            cmd = [
                'ffmpeg',
                '-y',
                '-i', video_path,
                '-vn',
                '-acodec', 'libmp3lame',
                '-q:a', '2',
                output_path
            ]

            result = subprocess.run(cmd, capture_output=True, timeout=300)

            if result.returncode == 0 and os.path.exists(output_path):
                logger.info(f"✅ Extracted audio to: {output_path}")
                return True

            return False

        except Exception as e:
            logger.error(f"❌ Error extracting audio: {e}")
            return False

    @staticmethod
    def extract_audio_from_url(
        video_url: str,
        output_path: str,
        rendi_api_key: str
    ) -> Optional[str]:
        """Extract audio from video URL using Rendi.dev cloud FFmpeg.

        Args:
            video_url: URL of the video.
            output_path: Local path to save audio (used for naming).
            rendi_api_key: Rendi.dev API key.

        Returns:
            URL of the extracted audio, or None if failed.
        """
        try:
            logger.info("🌐 Extracting audio via Rendi.dev cloud...")

            headers = {
                "X-API-KEY": rendi_api_key,
                "Content-Type": "application/json"
            }
            base_url = config.RENDI_BASE_URL

            ffmpeg_command = "-i {{in_1}} -vn -acodec libmp3lame -q:a 2 {{out_1}}"

            payload = {
                "ffmpeg_command": ffmpeg_command,
                "input_files": {"in_1": video_url},
                "output_files": {"out_1": "extracted_audio.mp3"},
                "vcpu_count": 2,
                "max_command_run_seconds": 300
            }

            response = requests.post(
                f"{base_url}/v1/run-ffmpeg-command",
                headers=headers,
                json=payload,
                timeout=60
            )

            if response.status_code == 200:
                result = response.json()
                command_id = result.get("command_id")

                if command_id:
                    # Poll for completion
                    audio_url = FFmpegProcessor._wait_for_rendi_audio(
                        command_id, headers, base_url
                    )
                    return audio_url

            return None

        except Exception as e:
            logger.error(f"❌ Error extracting audio from URL: {e}")
            return None

    @staticmethod
    def _wait_for_rendi_audio(
        command_id: str,
        headers: Dict,
        base_url: str,
        timeout: int = 300
    ) -> Optional[str]:
        """Wait for Rendi audio extraction to complete."""
        start_time = time.time()

        while time.time() - start_time < timeout:
            try:
                response = requests.get(
                    f"{base_url}/v1/commands/{command_id}",
                    headers=headers,
                    timeout=30
                )

                if response.status_code == 200:
                    result = response.json()
                    status = result.get("status", "").lower()

                    if status in ["completed", "success"]:
                        output_files = result.get("output_files", {})
                        if "out_1" in output_files:
                            audio_url = output_files["out_1"].get("storage_url")
                            logger.info(f"✅ Audio extracted via cloud")
                            return audio_url

                    elif status == "failed":
                        logger.error("❌ Cloud audio extraction failed")
                        return None

                time.sleep(5)

            except Exception:
                time.sleep(5)

        logger.error("❌ Audio extraction timeout")
        return None
