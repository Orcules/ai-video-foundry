import re
import time
import logging
from typing import Any, Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from api_pipeline.services.base.config import config


logger = logging.getLogger(__name__)


class RendiService:
    """Service for Rendi.dev API interactions."""

    def __init__(self, api_key: str):
        """Initialize Rendi service.

        Args:
            api_key: Rendi.dev API key.
        """
        self.api_key = api_key
        self.base_url = config.RENDI_BASE_URL
        self.headers = {
            "X-API-KEY": api_key,
            "Content-Type": "application/json"
        }
        logger.info("✅ Rendi.dev client initialized")

    def detect_scenes_cloud(self, video_url: str, threshold: float = 0.1) -> List[float]:
        """Detect scene changes using Rendi.dev cloud FFmpeg.

        Args:
            video_url: URL of the video to analyze.
            threshold: Scene change detection threshold (0-1, lower = more sensitive).

        Returns:
            List of timestamps (in seconds) where scenes start.
        """
        try:
            logger.info(f"🌐 Detecting scenes via Rendi.dev cloud (threshold={threshold})...")

            url = f"{self.base_url}/v1/run-ffmpeg-command"

            # Use FFmpeg's scene detection filter with metadata output
            # Write scene timestamps to a text file using the metadata filter
            # Note: comma needs to be escaped in the select filter
            ffmpeg_command = (
                f"-i {{{{in_1}}}} "
                f"-vf \"select=gt(scene\\,{threshold}),metadata=mode=print:file={{{{out_1}}}}\" "
                f"-an -f null -"
            )

            payload = {
                "input_files": {"in_1": video_url},
                "output_files": {"out_1": "scene_metadata.txt"},
                "ffmpeg_command": ffmpeg_command,
                "max_command_run_seconds": 300,
                "vcpu_count": 4
            }

            response = requests.post(url, headers=self.headers, json=payload, timeout=60)
            response.raise_for_status()

            result = response.json()

            if "command_id" in result:
                command_id = result["command_id"]
                logger.info(f"✅ Scene detection task created: {command_id}")

                # Wait for completion and get scene data
                return self._wait_for_scene_detection(command_id)
            else:
                logger.error(f"❌ Rendi scene detection error: {result}")
                return [0.0]

        except Exception as e:
            logger.error(f"❌ Error detecting scenes via cloud: {e}")
            return [0.0]

    def _wait_for_scene_detection(self, command_id: str, timeout: int = 300) -> List[float]:
        """Wait for scene detection to complete and parse results.

        Args:
            command_id: Command ID to poll.
            timeout: Maximum wait time in seconds.

        Returns:
            List of scene timestamps.
        """
        start_time = time.time()
        check_interval = 5

        while time.time() - start_time < timeout:
            try:
                url = f"{self.base_url}/v1/commands/{command_id}"
                response = requests.get(url, headers=self.headers, timeout=30)
                response.raise_for_status()

                result = response.json()
                status = result.get("status", "").upper()

                if status == "SUCCESS":
                    timestamps = [0.0]  # Always start with 0

                    # Get the output file URL (scene_metadata.txt)
                    output_files = result.get("output_files", {})
                    scene_info_url = None

                    if "out_1" in output_files:
                        scene_info_url = output_files["out_1"].get("storage_url")

                    # Download and parse the scene_metadata.txt file
                    if scene_info_url:
                        try:
                            logger.info(f"📥 Downloading scene detection results...")
                            file_response = requests.get(scene_info_url, timeout=60)
                            file_response.raise_for_status()
                            scene_info_content = file_response.text

                            logger.info(f"📄 Scene metadata file content length: {len(scene_info_content)} chars")

                            # Parse metadata print output for pts_time values
                            # metadata=print outputs: frame:0    pts:0    pts_time:0.000000
                            # Also handles showinfo format: [Parsed...] n:0 pts:0 pts_time:0.000000
                            pts_pattern = r"pts_time[=:\s]+(\d+\.?\d*)"
                            matches = re.findall(pts_pattern, scene_info_content)

                            for match in matches:
                                ts = float(match)
                                if ts not in timestamps and ts > 0:
                                    timestamps.append(ts)

                            logger.info(f"📊 Found {len(matches)} scene change entries in metadata file")

                        except Exception as e:
                            logger.warning(f"⚠️ Could not download/parse scene info file: {e}")

                    timestamps.sort()
                    logger.info(f"✅ Cloud scene detection complete: {len(timestamps)} scenes detected")
                    return timestamps[:config.MAX_SCENES]

                elif status == "FAILED":
                    error_msg = result.get("error_message", "Unknown error")
                    logger.error(f"❌ Scene detection failed: {error_msg}")
                    return [0.0]

            except Exception as e:
                logger.error(f"❌ Error polling scene detection: {e}")

            time.sleep(check_interval)

        logger.error("❌ Scene detection timeout")
        return [0.0]

    def get_video_duration_cloud(self, video_url: str) -> float:
        """Get video duration using Rendi.dev cloud FFmpeg.

        Args:
            video_url: URL of the video to analyze.

        Returns:
            Video duration in seconds, or 30.0 as default.
        """
        try:
            logger.info("🌐 Getting video duration via Rendi.dev cloud...")

            url = f"{self.base_url}/v1/run-ffmpeg-command"

            # Remux the video with stream copy (very fast, no re-encoding)
            # Rendi returns duration metadata for video outputs
            ffmpeg_command = "-i {{in_1}} -c copy {{out_1}}"

            payload = {
                "input_files": {"in_1": video_url},
                "output_files": {"out_1": "duration_check.mp4"},
                "ffmpeg_command": ffmpeg_command,
                "max_command_run_seconds": 120,
                "vcpu_count": 2
            }

            response = requests.post(url, headers=self.headers, json=payload, timeout=60)
            response.raise_for_status()

            result = response.json()

            if "command_id" in result:
                command_id = result["command_id"]

                # Wait for completion and get duration from output file metadata
                start_time = time.time()
                while time.time() - start_time < 120:
                    check_url = f"{self.base_url}/v1/commands/{command_id}"
                    check_response = requests.get(check_url, headers=self.headers, timeout=30)
                    check_response.raise_for_status()

                    check_result = check_response.json()
                    status = check_result.get("status", "").upper()

                    if status == "SUCCESS":
                        # Get duration from output file metadata
                        output_files = check_result.get("output_files", {})
                        if "out_1" in output_files:
                            duration = output_files["out_1"].get("duration")
                            if duration:
                                logger.info(f"✅ Video duration: {duration:.2f}s")
                                return float(duration)
                        break

                    elif status == "FAILED":
                        break

                    time.sleep(3)

            logger.warning("⚠️ Could not determine video duration, using default 30s")
            return 30.0

        except Exception as e:
            logger.error(f"❌ Error getting video duration: {e}")
            return 30.0

    def get_audio_duration_cloud(self, audio_url: str) -> float:
        """Get audio duration using Rendi.dev cloud FFmpeg.

        Args:
            audio_url: URL of the audio file to analyze.

        Returns:
            Audio duration in seconds, or 0.0 if failed.
        """
        try:
            logger.info("🌐 Getting audio duration via Rendi.dev cloud...")

            url = f"{self.base_url}/v1/run-ffmpeg-command"

            # Convert audio to get duration metadata
            ffmpeg_command = "-i {{in_1}} -c copy {{out_1}}"

            payload = {
                "input_files": {"in_1": audio_url},
                "output_files": {"out_1": "duration_check.mp3"},
                "ffmpeg_command": ffmpeg_command,
                "max_command_run_seconds": 60,
                "vcpu_count": 2
            }

            response = requests.post(url, headers=self.headers, json=payload, timeout=60)
            response.raise_for_status()

            result = response.json()

            if "command_id" in result:
                command_id = result["command_id"]

                # Wait for completion and get duration from output file metadata
                start_time = time.time()
                while time.time() - start_time < 60:
                    check_url = f"{self.base_url}/v1/commands/{command_id}"
                    check_response = requests.get(check_url, headers=self.headers, timeout=30)
                    check_response.raise_for_status()

                    check_result = check_response.json()
                    status = check_result.get("status", "").upper()

                    if status == "SUCCESS":
                        # Get duration from output file metadata
                        output_files = check_result.get("output_files", {})
                        if "out_1" in output_files:
                            duration = output_files["out_1"].get("duration")
                            if duration:
                                logger.info(f"✅ Audio duration: {duration:.2f}s")
                                return float(duration)
                        break

                    elif status == "FAILED":
                        break

                    time.sleep(2)

            logger.warning("⚠️ Could not determine audio duration")
            return 0.0

        except Exception as e:
            logger.error(f"❌ Error getting audio duration: {e}")
            return 0.0

    def loop_video_to_duration(self, video_url: str, target_duration: float) -> Optional[str]:
        """Loop a video to reach a target duration using Rendi.dev.

        The video will be looped as many times as needed to reach/exceed the target duration.

        Args:
            video_url: URL of the video to loop.
            target_duration: Target duration in seconds.

        Returns:
            URL of the looped video, or None if failed.
        """
        try:
            logger.info(f"🔁 Looping video to reach {target_duration:.2f}s...")

            url = f"{self.base_url}/v1/run-ffmpeg-command"

            # Use stream_loop to loop the video, then trim to exact duration
            # -stream_loop -1 loops indefinitely, -t limits to target duration
            ffmpeg_command = (
                f"-stream_loop -1 -i {{{{in_1}}}} -t {target_duration:.3f} "
                f"-c:v libx264 -preset fast -crf {config.VIDEO_CRF} "
                f"-c:a aac -b:a 128k "
                f"-movflags +faststart "
                f"{{{{out_1}}}}"
            )

            payload = {
                "input_files": {"in_1": video_url},
                "output_files": {"out_1": "looped_video.mp4"},
                "ffmpeg_command": ffmpeg_command,
                "max_command_run_seconds": 300,
                "vcpu_count": 4
            }

            response = requests.post(url, headers=self.headers, json=payload, timeout=60)
            response.raise_for_status()

            result = response.json()

            if "command_id" in result:
                command_id = result["command_id"]
                logger.info(f"✅ Loop task created: {command_id}")
                return self._wait_for_command(command_id)
            else:
                logger.error(f"❌ Rendi loop error: {result}")
                return None

        except Exception as e:
            logger.error(f"❌ Error looping video: {e}")
            return None

    def trim_video(self, video_url: str, duration: float, has_audio: Optional[bool] = None) -> Optional[str]:
        """Trim a video to a specific duration using Rendi.dev.

        Re-encodes the video to ensure clean cut points (no freezing on keyframes).

        Args:
            video_url: URL of the video to trim.
            duration: Target duration in seconds.
            has_audio: If True, keep audio (do not probe). If False, output video-only. If None, probe.

        Returns:
            URL of the trimmed video, or None if failed.
        """
        try:
            logger.info(f"✂️ Trimming video to {duration:.2f}s (with re-encode for clean cuts)...")

            url = f"{self.base_url}/v1/run-ffmpeg-command"

            if has_audio is None:
                has_audio = self.validate_video_has_audio(video_url)
            if has_audio:
                ffmpeg_command = (
                    f"-i {{{{in_1}}}} -t {duration:.3f} "
                    f"-map 0:v -map 0:a -c:v libx264 -preset fast -crf {config.VIDEO_CRF} -c:a aac -b:a 128k "
                    f"-movflags +faststart {{{{out_1}}}}"
                )
            else:
                ffmpeg_command = (
                    f"-i {{{{in_1}}}} -t {duration:.3f} "
                    f"-map 0:v -c:v libx264 -preset fast -crf {config.VIDEO_CRF} -an "
                    f"-movflags +faststart {{{{out_1}}}}"
                )

            payload = {
                "input_files": {"in_1": video_url},
                "output_files": {"out_1": "trimmed_video.mp4"},
                "ffmpeg_command": ffmpeg_command,
                "max_command_run_seconds": 180,
                "vcpu_count": 4
            }

            response = requests.post(url, headers=self.headers, json=payload, timeout=60)
            response.raise_for_status()

            result = response.json()

            if "command_id" in result:
                command_id = result["command_id"]
                logger.info(f"✅ Trim task created: {command_id}")
                return self._wait_for_command(command_id)
            else:
                logger.error(f"❌ Rendi trim error: {result}")
                return None

        except Exception as e:
            logger.error(f"❌ Error trimming video: {e}")
            return None

    # Ken Burns effect variants — cycled per scene for visual variety.
    # Uses simple frame-based expressions (on/{frames}) for maximum FFmpeg compatibility.
    # Avoids the `zoom` variable which is unreliable on some FFmpeg builds.
    _KB_EFFECTS = [
        {
            "name": "zoom_in_center",
            # Linear zoom 1.0 → 1.4 centered
            "z": "1.0+0.4*on/({frames})",
            "x": "iw/2-(iw/(1.0+0.4*on/({frames}))/2)",
            "y": "ih/2-(ih/(1.0+0.4*on/({frames}))/2)",
        },
        {
            "name": "zoom_out_center",
            # Linear zoom 1.4 → 1.0 centered
            "z": "1.4-0.4*on/({frames})",
            "x": "iw/2-(iw/(1.4-0.4*on/({frames}))/2)",
            "y": "ih/2-(ih/(1.4-0.4*on/({frames}))/2)",
        },
        {
            "name": "pan_left_to_right",
            # Steady zoom 1.25, pan from left to right
            "z": "1.25",
            "x": "iw*0.08*on/({frames})",
            "y": "ih/2-(ih/1.25/2)",
        },
        {
            "name": "pan_right_to_left",
            # Steady zoom 1.25, pan from right to left
            "z": "1.25",
            "x": "iw/1.25-iw*0.08*on/({frames})",
            "y": "ih/2-(ih/1.25/2)",
        },
        {
            "name": "zoom_in_top",
            # Linear zoom 1.0 → 1.35, focused on top third (faces)
            "z": "1.0+0.35*on/({frames})",
            "x": "iw/2-(iw/(1.0+0.35*on/({frames}))/2)",
            "y": "ih/3-(ih/(1.0+0.35*on/({frames}))/3)",
        },
        {
            "name": "pan_down_slow",
            # Steady zoom 1.22, downward pan
            "z": "1.22",
            "x": "iw/2-(iw/1.22/2)",
            "y": "ih*0.08*on/({frames})",
        },
    ]
    # Last scene: full frame visible, very slow zoom only (so viewer sees the whole scene)
    _KB_LAST_SCENE_EFFECT = {
        "name": "last_scene_subtle_zoom",
        "z": "1.0+0.06*on/({frames})",  # Very slow zoom 1.0 -> 1.06 so full scene stays visible
        "x": "iw/2-(iw/(1.0+0.06*on/({frames}))/2)",
        "y": "ih/2-(ih/(1.0+0.06*on/({frames}))/2)",
    }
    _kb_counter = 0  # class-level counter for cycling effects

    def create_video_from_image(
        self,
        image_url: str,
        duration: float = 4.0,
        fps: int = 30,
        width: int = 1080,
        height: int = 1920,
        subtle_for_last_scene: bool = False
    ) -> Optional[str]:
        """Create a video from a static image with a varied Ken Burns effect.

        Cycles through different motion styles (zoom-in, zoom-out, pan left/right,
        pan down) to keep the video visually interesting. For the last (CTA) scene,
        use subtle_for_last_scene=True so the full frame is visible with only a
        very slow zoom.

        Args:
            image_url: URL of the source image.
            duration: Target video duration in seconds.
            fps: Frames per second for the output.
            width: Output width in pixels.
            height: Output height in pixels.
            subtle_for_last_scene: If True, use a very slow zoom (1.0->1.06) so the
                whole scene stays visible; no aggressive zoom or pan.

        Returns:
            URL of the generated video, or None if failed.
        """
        try:
            total_frames = int(fps * duration)

            # Last scene: full frame visible, very slow zoom only
            if subtle_for_last_scene:
                effect = {
                    "name": self._KB_LAST_SCENE_EFFECT["name"],
                    "z": self._KB_LAST_SCENE_EFFECT["z"].replace("{frames}", str(total_frames)),
                    "x": self._KB_LAST_SCENE_EFFECT["x"].replace("{frames}", str(total_frames)),
                    "y": self._KB_LAST_SCENE_EFFECT["y"].replace("{frames}", str(total_frames)),
                }
                effect_name = effect["name"]
            else:
                # Pick the next effect variant (cycle through list)
                effect = self._KB_EFFECTS[RendiService._kb_counter % len(self._KB_EFFECTS)]
                RendiService._kb_counter += 1
                effect_name = effect["name"]
                effect = {k: v.replace("{frames}", str(total_frames)) if isinstance(v, str) else v for k, v in effect.items()}

            logger.info(f"🖼️ Creating Ken Burns video from image ({duration:.1f}s, {total_frames} frames, effect={effect_name})...")

            url = f"{self.base_url}/v1/run-ffmpeg-command"

            # Build zoompan filter with the chosen effect
            z_expr = effect["z"].replace("{frames}", str(total_frames))
            x_expr = effect["x"].replace("{frames}", str(total_frames))
            y_expr = effect["y"].replace("{frames}", str(total_frames))

            ffmpeg_command = (
                f"-loop 1 -i {{{{in_1}}}} "
                f"-vf \"scale={width*2}:{height*2},zoompan=z='{z_expr}'"
                f":x='{x_expr}':y='{y_expr}'"
                f":d={total_frames}:s={width}x{height}:fps={fps}\" "
                f"-t {duration:.3f} -c:v libx264 -preset fast -crf {config.VIDEO_CRF} -pix_fmt yuv420p "
                f"-an -movflags +faststart {{{{out_1}}}}"
            )
            logger.info(f"   Ken Burns FFmpeg filter: z='{z_expr[:60]}' effect={effect_name}")

            payload = {
                "input_files": {"in_1": image_url},
                "output_files": {"out_1": "image_video.mp4"},
                "ffmpeg_command": ffmpeg_command,
                "max_command_run_seconds": 120,
                "vcpu_count": 4
            }

            response = requests.post(url, headers=self.headers, json=payload, timeout=60)
            response.raise_for_status()

            result = response.json()

            if "command_id" in result:
                command_id = result["command_id"]
                logger.info(f"✅ Ken Burns video task created: {command_id} ({effect_name})")
                return self._wait_for_command(command_id)
            else:
                logger.error(f"❌ Rendi image-to-video error: {result}")
                return None

        except Exception as e:
            logger.error(f"❌ Error creating video from image: {e}")
            return None

    def slow_motion_video(
        self,
        video_url: str,
        speed_factor: float,
        target_duration: float = None,
        keep_audio: bool = False
    ) -> Optional[str]:
        """Apply slow motion to a video using Rendi.dev.

        Slows down the video by the given factor to extend its duration.
        Uses setpts filter for smooth slow motion effect.

        Args:
            video_url: URL of the video to slow down.
            speed_factor: Speed multiplier (0.5 = half speed/2x duration, 0.8 = 80% speed/1.25x duration).
                         Should be between 0.5 and 1.0 for subtle slow motion.
            target_duration: Optional target duration. If provided, will trim to exact duration after slowing.
            keep_audio: If True, preserve and stretch audio with atempo so it matches the slowed video (for per-scene VO).

        Returns:
            URL of the slowed video, or None if failed.
        """
        try:
            # Clamp speed factor to reasonable range (0.5 to 1.0 for slow motion)
            speed_factor = max(0.5, min(1.0, speed_factor))

            if speed_factor >= 0.99:
                logger.info(f"⏸️ Speed factor {speed_factor:.2f} is too close to 1.0, skipping slow motion")
                return video_url

            slowdown_percent = (1.0 - speed_factor) * 100
            logger.info(f"⏸️ Applying slow motion ({slowdown_percent:.0f}% slower, speed={speed_factor:.2f}x)...")

            url = f"{self.base_url}/v1/run-ffmpeg-command"

            # setpts=PTS/speed_factor slows down video (e.g., PTS/0.8 = 25% slower)
            pts_factor = 1.0 / speed_factor
            # atempo=speed_factor slows audio to match (e.g. 0.8 = 20% slower)
            atempo_val = min(2.0, max(0.5, speed_factor))

            if keep_audio:
                # Preserve audio: stretch with atempo so it matches the slowed video duration
                if target_duration:
                    ffmpeg_command = (
                        f"-i {{{{in_1}}}} -filter_complex \"[0:v]setpts={pts_factor:.4f}*PTS[v];[0:a]atempo={atempo_val:.4f}[a]\" "
                        f"-map \"[v]\" -map \"[a]\" -t {target_duration:.3f} "
                        f"-c:v libx264 -preset fast -crf {config.VIDEO_CRF} -c:a aac -b:a 128k -movflags +faststart {{{{out_1}}}}"
                    )
                else:
                    ffmpeg_command = (
                        f"-i {{{{in_1}}}} -filter_complex \"[0:v]setpts={pts_factor:.4f}*PTS[v];[0:a]atempo={atempo_val:.4f}[a]\" "
                        f"-map \"[v]\" -map \"[a]\" -c:v libx264 -preset fast -crf {config.VIDEO_CRF} -c:a aac -b:a 128k -movflags +faststart {{{{out_1}}}}"
                    )
            else:
                # Video-only (no audio) to avoid ':a' errors when input may have no audio
                if target_duration:
                    ffmpeg_command = (
                        f"-i {{{{in_1}}}} -vf \"setpts={pts_factor:.4f}*PTS\" -t {target_duration:.3f} "
                        f"-an -c:v libx264 -preset fast -crf {config.VIDEO_CRF} -movflags +faststart {{{{out_1}}}}"
                    )
                else:
                    ffmpeg_command = (
                        f"-i {{{{in_1}}}} -vf \"setpts={pts_factor:.4f}*PTS\" "
                        f"-an -c:v libx264 -preset fast -crf {config.VIDEO_CRF} -movflags +faststart {{{{out_1}}}}"
                    )

            payload = {
                "input_files": {"in_1": video_url},
                "output_files": {"out_1": "slowmo_video.mp4"},
                "ffmpeg_command": ffmpeg_command,
                "max_command_run_seconds": 300,
                "vcpu_count": 4
            }

            response = requests.post(url, headers=self.headers, json=payload, timeout=60)
            response.raise_for_status()

            result = response.json()

            if "command_id" in result:
                command_id = result["command_id"]
                logger.info(f"✅ Slow motion task created: {command_id}")
                return self._wait_for_command(command_id)
            else:
                logger.error(f"❌ Rendi slow motion error: {result}")
                return None

        except Exception as e:
            logger.error(f"❌ Error applying slow motion: {e}")
            return None

    def trim_videos_batch(
        self,
        video_durations: List[Dict[str, Any]],
        add_buffer_except_last: bool = True,
        buffer_duration: float = 0.5,
        videos_have_audio: Optional[bool] = None
    ) -> List[Dict[str, Any]]:
        """Trim multiple videos to their target durations in PARALLEL.

        Args:
            video_durations: List of dicts with 'video_url' and 'duration' keys.
            add_buffer_except_last: If True, add buffer_duration to all scenes except the last one.
            buffer_duration: Duration of the buffer in seconds (default 0.5s).
            videos_have_audio: If True, keep audio when trimming (e.g. clips have per-scene VO). If None, probe each clip.

        Returns:
            List of dicts with 'video_url' and 'duration' keys (trimmed URLs with their durations).
        """
        if not video_durations:
            return []

        # Filter out any items that have image_url but no video_url (only trim videos, not images)
        filtered_video_durations = []
        for item in video_durations:
            if isinstance(item, dict):
                # Only include if it has video_url (not just image_url)
                if item.get("video_url"):
                    filtered_video_durations.append(item)
                elif item.get("image_url"):
                    # Skip images - we only trim videos
                    logger.warning(f"⚠️ Skipping image in trim_videos_batch (only videos should be trimmed): {item.get('image_url', '')[:60]}...")
            else:
                # If it's a string, assume it's a video URL
                filtered_video_durations.append(item)

        if not filtered_video_durations:
            logger.warning("⚠️ No video URLs found for trimming (only images were provided)")
            return []

        logger.info(f"✂️ Trimming {len(filtered_video_durations)} videos in PARALLEL...")

        num_videos = len(filtered_video_durations)

        # Add index to track original order
        # Add buffer to all scenes except the last one if add_buffer_except_last is True
        items_with_index = []
        for i, item in enumerate(filtered_video_durations):
            base_duration = item.get("duration", 5.0)
            # Add buffer to all except the last scene - this allows video to play 0.5s without VO at transitions
            if add_buffer_except_last and i < num_videos - 1:
                target_duration = base_duration + buffer_duration
                logger.info(f"   Scene {i+1}: Adding {buffer_duration}s buffer (target: {base_duration:.2f}s + {buffer_duration}s = {target_duration:.2f}s)")
            else:
                target_duration = base_duration
            items_with_index.append({
                "index": i,
                "video_url": item.get("video_url"),
                "duration": target_duration,
                "original_duration": base_duration  # Keep track of original for result
            })

        def trim_single(item: Dict[str, Any]) -> Dict[str, Any]:
            """Trim or loop a single video to match target duration."""
            video_url = item.get("video_url")
            target_duration = item.get("duration", 5.0)
            idx = item.get("index", 0)

            if not video_url:
                return {
                    "video_url": None,
                    "duration": target_duration,
                    "index": idx,
                    "success": False
                }

            try:
                # Get actual video duration
                actual_duration = self.get_video_duration_cloud(video_url)

                if actual_duration <= 0:
                    # Fallback: assume video is 10 seconds (typical Runway/Kling output)
                    actual_duration = 10.0
                    logger.warning(f"⚠️ Scene {idx + 1}: Could not get video duration, assuming {actual_duration}s")

                logger.info(f"   Scene {idx + 1}: actual={actual_duration:.2f}s, target={target_duration:.2f}s")

                # IMPORTANT: Never loop videos - looping causes jarring jumps/repeats
                # Use slow motion to extend if needed (up to 1.5x duration increase)
                # Only trim if video is longer than needed
                if target_duration < actual_duration:
                    # Video is longer than needed - trim it
                    logger.info(f"   Scene {idx + 1}: Trimming video from {actual_duration:.2f}s to {target_duration:.2f}s")
                    final_url = self.trim_video(video_url, target_duration, has_audio=videos_have_audio)
                    final_duration = target_duration
                elif target_duration > actual_duration:
                    # Video is shorter than target - use slow motion if within reasonable bounds
                    # Max slow motion: 2x duration (speed factor 0.5)
                    duration_ratio = target_duration / actual_duration
                    max_slowdown = 2.0  # Maximum 100% slower (2x duration) (1.5x duration)

                    if duration_ratio <= max_slowdown:
                        # Apply slow motion to match target duration
                        speed_factor = actual_duration / target_duration  # e.g., 5s/7s = 0.71
                        logger.info(f"   Scene {idx + 1}: Applying slow motion ({(1-speed_factor)*100:.0f}% slower) to extend from {actual_duration:.2f}s to {target_duration:.2f}s")

                        slowmo_url = self.slow_motion_video(
                            video_url=video_url,
                            speed_factor=speed_factor,
                            target_duration=target_duration,
                            keep_audio=bool(videos_have_audio)
                        )

                        if slowmo_url:
                            final_url = slowmo_url
                            final_duration = target_duration
                        else:
                            # Slow motion failed, use original
                            logger.warning(f"   ⚠️ Scene {idx + 1}: Slow motion failed, using original video")
                            final_url = video_url
                            final_duration = actual_duration
                    else:
                        # Too much slowdown needed (would look unnatural), use as-is
                        logger.info(f"   Scene {idx + 1}: Video too short for slow motion ({duration_ratio:.2f}x needed > {max_slowdown:.1f}x max), using as-is ({actual_duration:.2f}s)")
                        final_url = video_url
                        final_duration = actual_duration
                else:
                    # Duration matches exactly
                    logger.info(f"   Scene {idx + 1}: Duration matches ({actual_duration:.2f}s), using as-is")
                    final_url = video_url
                    final_duration = actual_duration

                if final_url:
                    return {
                        "video_url": final_url,
                        "duration": final_duration,  # Return actual duration used
                        "index": idx,
                        "success": True
                    }
                else:
                    logger.warning(f"⚠️ Scene {idx + 1}: Processing failed, using original")
                    return {
                        "video_url": video_url,
                        "duration": actual_duration,  # Use actual duration of original video
                        "index": idx,
                        "success": False
                    }
            except Exception as e:
                logger.error(f"❌ Scene {idx + 1}: Error processing video: {e}")
                # Use original if processing fails - assume 10s (typical Runway/Kling output)
                return {
                    "video_url": video_url,
                    "duration": 10.0,  # Fallback to typical Runway/Kling output duration
                    "index": idx,
                    "success": False
                }

        trimmed_results = []

        # Use ThreadPoolExecutor for parallel trimming
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(trim_single, item): item for item in items_with_index}

            for future in as_completed(futures):
                try:
                    result = future.result()
                    if result and result.get("video_url"):
                        trimmed_results.append(result)
                        status = "✅" if result.get("success") else "⚠️"
                        logger.info(f"   {status} Scene {result['index'] + 1}: trimmed to {result['duration']:.2f}s")
                except Exception as e:
                    item = futures[future]
                    logger.error(f"   ❌ Scene {item['index'] + 1}: trim failed - {e}")
                    # Use original on error
                    trimmed_results.append({
                        "video_url": item.get("video_url"),
                        "duration": item.get("duration", 5.0),
                        "index": item.get("index", 0)
                    })

        # Sort by original index to maintain order
        trimmed_results.sort(key=lambda x: x.get("index", 0))

        # Remove duplicates by video_url to prevent same video appearing twice
        seen_urls = set()
        unique_trimmed_results = []
        for v in trimmed_results:
            video_url = v.get("video_url")
            if video_url and video_url not in seen_urls:
                seen_urls.add(video_url)
                unique_trimmed_results.append(v)
            elif video_url in seen_urls:
                logger.warning(f"⚠️ Duplicate video URL in trimmed results (index {v.get('index', 0)}): {video_url[:60]}... - removing duplicate")

        if len(unique_trimmed_results) < len(trimmed_results):
            logger.warning(f"⚠️ Removed {len(trimmed_results) - len(unique_trimmed_results)} duplicate videos from trimmed results")

        # Remove index from final output
        trimmed_videos = [
            {"video_url": v["video_url"], "duration": v["duration"]}
            for v in unique_trimmed_results
        ]

        logger.info(f"✅ Parallel trimming complete: {len(trimmed_videos)} videos")

        return trimmed_videos

    def concatenate_videos(
        self,
        video_data: List[Dict[str, Any]],
        use_transitions: bool = False,
        video_only: bool = False,
        dissolve_seconds: float = 0.0,
        assume_clips_have_audio: bool = False
    ) -> Optional[str]:
        """Concatenate multiple videos into one.

        Args:
            video_data: List of dicts with 'video_url' and 'duration' keys.
            use_transitions: If True, use xfade transitions (can cause issues).
            video_only: If True, use video-only concat (for silent videos without audio).
            dissolve_seconds: If > 0 and video_only, use gentle fade between clips (e.g. 0.45).
            assume_clips_have_audio: If True, use concat with audio (do not probe); use when clips have per-scene VO.

        Returns:
            URL of the concatenated video, or None if failed.
        """
        try:
            if not video_data:
                logger.error("❌ No video data provided for concatenation")
                return None

            # Extract URLs and durations - ONLY videos, NOT images
            # Filter out any items that have image_url but no video_url
            filtered_video_data = []
            for item in video_data:
                if isinstance(item, dict):
                    # Only include if it has video_url (not just image_url)
                    if item.get("video_url"):
                        filtered_video_data.append(item)
                    elif item.get("image_url"):
                        # Skip images - we only want videos for concatenation
                        logger.warning(f"⚠️ Skipping image URL in concatenation (only videos should be concatenated): {item.get('image_url', '')[:60]}...")
                else:
                    # If it's a string, assume it's a video URL
                    filtered_video_data.append(item)

            if not filtered_video_data:
                logger.error("❌ No video URLs found for concatenation (only images were provided)")
                return None

            video_urls = [item["video_url"] if isinstance(item, dict) else item for item in filtered_video_data]
            durations = [item.get("duration", 5.0) if isinstance(item, dict) else 5.0 for item in filtered_video_data]

            num_videos = len(video_urls)
            logger.info(f"🎬 Concatenating {num_videos} videos with Rendi (transitions={use_transitions})...")
            for i, dur in enumerate(durations):
                logger.info(f"   Video {i+1}: {dur:.2f}s")

            url = f"{self.base_url}/v1/run-ffmpeg-command"

            # Build input files dict
            input_files = {f"in_{i+1}": video_url for i, video_url in enumerate(video_urls)}

            if video_only:
                # Use video-only concat for silent videos (e.g., Veo 3 output)
                if dissolve_seconds > 0 and num_videos > 1:
                    return self._concatenate_video_only_with_dissolve(video_urls, durations, dissolve_seconds)
                return self._concatenate_video_only(video_urls, durations)
            # When we know clips have audio (e.g. per-scene VO), use concat with audio without probing
            if assume_clips_have_audio:
                if use_transitions and num_videos > 1:
                    return self._concatenate_with_transitions(video_urls, durations)
                return self._concatenate_simple(video_urls)
            # Otherwise probe: ensure ALL clips have audio before using [i:a]
            all_have_audio = True
            for u in video_urls:
                if not self.validate_video_has_audio(u):
                    all_have_audio = False
                    break
            if not all_have_audio:
                logger.warning("⚠️ One or more clips have no audio; using video-only concat to avoid ':a' filter error")
                if dissolve_seconds > 0 and num_videos > 1:
                    return self._concatenate_video_only_with_dissolve(video_urls, durations, dissolve_seconds)
                return self._concatenate_video_only(video_urls, durations)
            if use_transitions and num_videos > 1:
                return self._concatenate_with_transitions(video_urls, durations)
            return self._concatenate_simple(video_urls)

        except Exception as e:
            logger.error(f"❌ Error concatenating videos: {e}")
            return None

    def _concatenate_video_only(self, video_urls: List[str], durations: List[float] = None) -> Optional[str]:
        """Concatenate videos without audio streams (for silent videos like Veo 3 output).

        This method normalizes all videos to the same format before concatenating.
        Only processes video streams, ignoring any audio.
        Each video is trimmed to its target duration to control final video length.

        Args:
            video_urls: List of video URLs to concatenate.
            durations: Optional list of target durations for each video (trims if provided).
        """
        try:
            # Remove duplicates while preserving order and matching durations
            unique_urls = []
            unique_durations = []
            seen_urls = set()
            for i, url_val in enumerate(video_urls):
                if url_val and url_val not in seen_urls:
                    unique_urls.append(url_val)
                    unique_durations.append(durations[i] if durations and i < len(durations) else None)
                    seen_urls.add(url_val)

            num_videos = len(unique_urls)
            total_target = sum(d for d in unique_durations if d) if unique_durations else 0
            logger.info(f"🎬 Using video-only concat method for {num_videos} videos (trimming to ~{total_target:.0f}s total)...")

            url = f"{self.base_url}/v1/run-ffmpeg-command"
            input_files = {f"in_{i+1}": video_url for i, video_url in enumerate(unique_urls)}

            # Build filter_complex for VIDEO ONLY (no audio processing)
            filter_parts = []

            # Normalize each video stream (fps, resolution, pixel format) + TRIM to target duration
            # Uses scale+crop (increase) instead of scale+pad (decrease) to avoid black borders
            for i in range(num_videos):
                trim_filter = ""
                if unique_durations[i]:
                    trim_filter = f"trim=0:{unique_durations[i]:.2f},setpts=PTS-STARTPTS,"
                filter_parts.append(
                    f"[{i}:v]{trim_filter}fps=30,scale=1080:1920:force_original_aspect_ratio=increase,"
                    f"crop=1080:1920,setsar=1,format=yuv420p[v{i}]"
                )

            # Concat video streams only
            video_concat_inputs = "".join([f"[v{i}]" for i in range(num_videos)])
            filter_parts.append(f"{video_concat_inputs}concat=n={num_videos}:v=1:a=0[outv]")

            filter_complex = ";".join(filter_parts)

            # Build FFmpeg command - map only video output (no audio)
            input_args = " ".join([f"-i {{{{in_{i+1}}}}}" for i in range(num_videos)])
            ffmpeg_command = (
                f"{input_args} -filter_complex \"{filter_complex}\" "
                f"-map \"[outv]\" -c:v libx264 -preset fast -crf {config.VIDEO_CRF} "
                f"-movflags +faststart {{{{out_1}}}}"
            )

            payload = {
                "input_files": input_files,
                "output_files": {"out_1": "concatenated_video.mp4"},
                "ffmpeg_command": ffmpeg_command,
                "max_command_run_seconds": 600,
                "vcpu_count": 8
            }

            response = requests.post(url, headers=self.headers, json=payload, timeout=60)
            response.raise_for_status()

            result = response.json()

            if "command_id" in result:
                command_id = result["command_id"]
                logger.info(f"✅ Rendi video-only concat task created: {command_id}")
                return self._wait_for_command(command_id)
            else:
                logger.error(f"❌ Rendi API error: {result}")
                return None

        except Exception as e:
            logger.error(f"❌ Error in video-only concat: {e}")
            return None

    def _concatenate_video_only_with_dissolve(
        self, video_urls: List[str], durations: List[float], dissolve_seconds: float = 0.45
    ) -> Optional[str]:
        """Concatenate video-only clips with gentle dissolve (xfade) between each shot.

        Same as _concatenate_video_only but uses xfade=transition=fade for soft transitions.
        """
        try:
            unique_urls = []
            unique_durations = []
            seen_urls = set()
            for i, url_val in enumerate(video_urls):
                if url_val and url_val not in seen_urls:
                    unique_urls.append(url_val)
                    unique_durations.append(durations[i] if durations and i < len(durations) else None)
                    seen_urls.add(url_val)

            num_videos = len(unique_urls)
            if num_videos == 0:
                return None
            if num_videos == 1:
                return self._concatenate_video_only(unique_urls, unique_durations)

            # Cap dissolve so it doesn't exceed half of shortest clip; use config default for smooth transitions
            min_dur = min(d for d in unique_durations if d) or 4.0
            d = min(float(dissolve_seconds or getattr(config, "CONCAT_DISSOLVE_SECONDS", 0.4)), min_dur * 0.5, 0.6)

            logger.info(f"🎬 Using video-only concat with gentle dissolve ({d:.2f}s) for {num_videos} videos...")

            url = f"{self.base_url}/v1/run-ffmpeg-command"
            input_files = {f"in_{i+1}": video_url for i, video_url in enumerate(unique_urls)}
            filter_parts = []

            for i in range(num_videos):
                trim_filter = ""
                if unique_durations[i]:
                    trim_filter = f"trim=0:{unique_durations[i]:.2f},setpts=PTS-STARTPTS,"
                filter_parts.append(
                    f"[{i}:v]{trim_filter}fps=30,scale=1080:1920:force_original_aspect_ratio=increase,"
                    f"crop=1080:1920,setsar=1,format=yuv420p[v{i}]"
                )

            xfade_type = getattr(config, "RENDI_DISSOLVE_TRANSITION", "dissolve")
            cumulative_time = unique_durations[0] - d if unique_durations[0] else 4.0 - d
            first_out = "[outv]" if num_videos == 2 else "[tmp1]"
            filter_parts.append(
                f"[v0][v1]xfade=transition={xfade_type}:duration={d:.3f}:offset={cumulative_time:.3f}{first_out}"
            )
            for i in range(2, num_videos):
                cumulative_time += (unique_durations[i - 1] - d) if unique_durations[i - 1] else (4.0 - d)
                out_label = "[outv]" if i == num_videos - 1 else f"[tmp{i}]"
                filter_parts.append(
                    f"[tmp{i-1}][v{i}]xfade=transition={xfade_type}:duration={d:.3f}:offset={cumulative_time:.3f}{out_label}"
                )

            filter_complex = ";".join(filter_parts)
            input_args = " ".join([f"-i {{{{in_{j+1}}}}}" for j in range(num_videos)])
            ffmpeg_command = (
                f"{input_args} -filter_complex \"{filter_complex}\" "
                f"-map \"[outv]\" -c:v libx264 -preset fast -crf {config.VIDEO_CRF} "
                f"-movflags +faststart {{{{out_1}}}}"
            )
            payload = {
                "input_files": input_files,
                "output_files": {"out_1": "concatenated_video.mp4"},
                "ffmpeg_command": ffmpeg_command,
                "max_command_run_seconds": 600,
                "vcpu_count": 8
            }
            response = requests.post(url, headers=self.headers, json=payload, timeout=60)
            response.raise_for_status()
            result = response.json()
            if "command_id" in result:
                logger.info(f"✅ Rendi video-only dissolve concat task created: {result['command_id']}")
                return self._wait_for_command(result["command_id"])
            logger.error(f"❌ Rendi API error: {result}")
            return None
        except Exception as e:
            logger.error(f"❌ Error in video-only dissolve concat: {e}")
            return None

    def _concatenate_simple(self, video_urls: List[str]) -> Optional[str]:
        """Concatenate videos using concat filter (simple, reliable, no repetition).

        This method normalizes all videos to the same format before concatenating.
        Ensures clean cuts without repetition or weird transitions.
        Preserves audio tracks from all videos using a two-step approach:
        1. Normalize and concat video streams
        2. Concat audio streams separately
        3. Merge them together
        """
        try:
            # Remove duplicates to prevent same video appearing twice
            unique_urls = []
            seen_urls = set()
            for url in video_urls:
                if url and url not in seen_urls:
                    unique_urls.append(url)
                    seen_urls.add(url)
                elif url in seen_urls:
                    logger.warning(f"⚠️ Duplicate video URL detected and removed: {url[:60]}...")

            num_videos = len(unique_urls)
            if num_videos != len(video_urls):
                logger.warning(f"⚠️ Removed {len(video_urls) - num_videos} duplicate videos from concatenation")

            logger.info(f"🎬 Using simple concat method for {num_videos} videos (with audio, buffer handled at trim stage)...")

            url = f"{self.base_url}/v1/run-ffmpeg-command"
            input_files = {f"in_{i+1}": video_url for i, video_url in enumerate(unique_urls)}

            # Build filter_complex with SEPARATE video and audio concat chains
            # This is more reliable than interleaved concat
            # NOTE: Buffer between scenes is now handled at the trim stage (videos are trimmed 0.5s longer)
            # This allows video to continue playing without VO instead of freezing the last frame
            filter_parts = []

            # Normalize each video stream (fps, resolution, pixel format)
            # Uses scale+crop (increase) instead of scale+pad (decrease) to avoid black borders
            for i in range(num_videos):
                filter_parts.append(
                    f"[{i}:v]fps=30,scale=1080:1920:force_original_aspect_ratio=increase,"
                    f"crop=1080:1920,setsar=1,format=yuv420p[v{i}]"
                )

            # Normalize each audio stream (resample to consistent format)
            # No audio padding needed - the video buffer handles the transition smoothly
            for i in range(num_videos):
                filter_parts.append(
                    f"[{i}:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[a{i}]"
                )

            # Concat video streams
            video_concat_inputs = "".join([f"[v{i}]" for i in range(num_videos)])
            filter_parts.append(f"{video_concat_inputs}concat=n={num_videos}:v=1:a=0[outv]")

            # Concat audio streams
            audio_concat_inputs = "".join([f"[a{i}]" for i in range(num_videos)])
            filter_parts.append(f"{audio_concat_inputs}concat=n={num_videos}:v=0:a=1[outa]")

            filter_complex = ";".join(filter_parts)

            # Build FFmpeg command - map both video and audio outputs
            input_args = " ".join([f"-i {{{{in_{i+1}}}}}" for i in range(num_videos)])
            ffmpeg_command = (
                f"{input_args} -filter_complex \"{filter_complex}\" "
                f"-map \"[outv]\" -map \"[outa]\" -c:v libx264 -preset fast -crf {config.VIDEO_CRF} "
                f"-c:a aac -b:a 192k -movflags +faststart {{{{out_1}}}}"
            )

            payload = {
                "input_files": input_files,
                "output_files": {"out_1": "concatenated_video.mp4"},
                "ffmpeg_command": ffmpeg_command,
                "max_command_run_seconds": 600,
                "vcpu_count": 8
            }

            response = requests.post(url, headers=self.headers, json=payload, timeout=60)
            response.raise_for_status()

            result = response.json()

            if "command_id" in result:
                command_id = result["command_id"]
                logger.info(f"✅ Rendi concat task created: {command_id}")
                return self._wait_for_command(command_id)
            else:
                logger.error(f"❌ Rendi API error: {result}")
                return None

        except Exception as e:
            logger.error(f"❌ Error in simple concat: {e}")
            return None

    def _concatenate_with_transitions(
        self,
        video_urls: List[str],
        durations: List[float]
    ) -> Optional[str]:
        """Concatenate videos with xfade transitions.

        Calculates correct offsets based on actual video durations.
        """
        try:
            num_videos = len(video_urls)
            logger.info(f"🎬 Using xfade transitions for {num_videos} videos...")

            url = f"{self.base_url}/v1/run-ffmpeg-command"
            input_files = {f"in_{i+1}": video_url for i, video_url in enumerate(video_urls)}

            filter_parts = []
            # Softer transition (0.35s) for smoother cuts between shots; use config transition type
            transition_duration = min(0.35, getattr(config, "CONCAT_DISSOLVE_SECONDS", 0.4))
            xfade_type = getattr(config, "RENDI_DISSOLVE_TRANSITION", "dissolve")

            # Normalize each video - crop to fill frame (no black borders)
            for i in range(num_videos):
                filter_parts.append(
                    f"[{i}:v]fps=30,scale=1080:1920:force_original_aspect_ratio=increase,"
                    f"crop=1080:1920,setsar=1,format=yuv420p[v{i}]"
                )

            if num_videos == 1:
                filter_parts.append(f"[v0]copy[outv]")
            else:
                # Calculate correct xfade offsets based on actual durations
                # For xfade, offset is the time in the FIRST video where transition starts
                # Transition 0->1: starts at duration[0] - transition_duration
                # etc.

                cumulative_time = durations[0] - transition_duration

                xfade_expr = f"[v0][v1]xfade=transition={xfade_type}:duration={transition_duration}:offset={cumulative_time:.3f}[tmp1]"
                filter_parts.append(xfade_expr)

                # For subsequent transitions, we need to calculate the offset in the accumulated video
                # Each transition starts at: previous cumulative time + (previous video duration - transition_duration)
                for i in range(2, num_videos):
                    # The offset is the time in the accumulated video (tmp[i-1]) where transition starts
                    # This is: cumulative time of all previous videos minus all previous transitions
                    cumulative_time += durations[i-1] - transition_duration

                    if i == num_videos - 1:
                        xfade_expr = f"[tmp{i-1}][v{i}]xfade=transition={xfade_type}:duration={transition_duration}:offset={cumulative_time:.3f}[outv]"
                    else:
                        xfade_expr = f"[tmp{i-1}][v{i}]xfade=transition={xfade_type}:duration={transition_duration}:offset={cumulative_time:.3f}[tmp{i}]"
                    filter_parts.append(xfade_expr)

            filter_complex = ";".join(filter_parts)

            input_args = " ".join([f"-i {{{{in_{i+1}}}}}" for i in range(num_videos)])
            ffmpeg_command = (
                f"{input_args} -filter_complex \"{filter_complex}\" "
                f"-map \"[outv]\" -c:v libx264 -preset fast -crf {config.VIDEO_CRF} "
                f"-movflags +faststart {{{{out_1}}}}"
            )

            payload = {
                "input_files": input_files,
                "output_files": {"out_1": "concatenated_video.mp4"},
                "ffmpeg_command": ffmpeg_command,
                "max_command_run_seconds": 600,
                "vcpu_count": 8
            }

            response = requests.post(url, headers=self.headers, json=payload, timeout=60)
            response.raise_for_status()

            result = response.json()

            if "command_id" in result:
                command_id = result["command_id"]
                logger.info(f"✅ Rendi xfade task created: {command_id}")
                return self._wait_for_command(command_id)
            else:
                logger.error(f"❌ Rendi API error: {result}")
                return None

        except Exception as e:
            logger.error(f"❌ Error in xfade concat: {e}")
            return None

    def add_audio_to_video(self, video_url: str, audio_url: str) -> Optional[str]:
        """Add audio track to a video.

        Args:
            video_url: URL of the video.
            audio_url: URL of the audio file.

        Returns:
            URL of the video with audio, or None if failed.
        """
        try:
            logger.info(f"🎬 Adding audio to video with Rendi...")

            url = f"{self.base_url}/v1/run-ffmpeg-command"

            # Explicit mapping: video from first input, audio from second. Works when video has no audio (e.g. Veo).
            ffmpeg_command = "-i {{in_1}} -i {{in_2}} -map 0:v -map 1:a -c:v copy -c:a aac -b:a 192k -shortest {{out_1}}"

            payload = {
                "ffmpeg_command": ffmpeg_command,
                "input_files": {
                    "in_1": video_url,
                    "in_2": audio_url
                },
                "output_files": {
                    "out_1": "video_with_audio.mp4"
                },
                "vcpu_count": 4,
                "max_command_run_seconds": 300
            }

            response = requests.post(url, headers=self.headers, json=payload, timeout=60)
            response.raise_for_status()

            result = response.json()

            if "command_id" in result:
                command_id = result["command_id"]
                logger.info(f"✅ Rendi audio task created: {command_id}")
                return self._wait_for_command(command_id)
            else:
                logger.error(f"❌ Rendi API error: {result}")
                return None

        except Exception as e:
            logger.error(f"❌ Error adding audio to video: {e}")
            return None

    def validate_video_has_audio(self, video_url: str) -> bool:
        """Check if a video has an audio track using FFprobe via Rendi.

        Args:
            video_url: URL of the video to check.

        Returns:
            True if video has audio track, False otherwise.
        """
        try:
            logger.info(f"🔍 Validating video has audio track...")

            url = f"{self.base_url}/v1/run-ffmpeg-command"

            # Use ffprobe to check for audio streams
            # This command outputs audio stream info if present
            ffprobe_command = (
                "-v quiet -select_streams a:0 -show_entries stream=codec_type "
                "-of default=noprint_wrappers=1:nokey=1 {{in_1}}"
            )

            payload = {
                "ffmpeg_command": f"ffprobe {ffprobe_command}",
                "input_files": {
                    "in_1": video_url
                },
                "output_files": {},
                "vcpu_count": 1,
                "max_command_run_seconds": 30
            }

            response = requests.post(url, headers=self.headers, json=payload, timeout=30)

            if response.status_code == 200:
                result = response.json()
                status = result.get("status", "")
                # If probe failed (e.g. no audio stream), treat as no audio
                if status == "failed":
                    logger.info("🔇 Video has no audio track (probe failed)")
                    return False
                if status == "completed" and "audio" in str(result).lower():
                    logger.info("✅ Video has audio track")
                    return True

            # Inconclusive: assume no audio so add_background_music uses video-only path (works in both cases)
            logger.info("🔇 Assuming video has no audio (probe inconclusive)")
            return False

        except Exception as e:
            logger.warning(f"⚠️ Could not validate audio track: {e}, assuming no audio")
            return False

    def add_background_music_to_video(
        self,
        video_url: str,
        music_url: str,
        music_volume: float = 0.35,
        assume_has_audio: bool = False
    ) -> Optional[str]:
        """Add background music to a video that already has audio (voice-over).

        This overlays the music track on top of existing video audio.

        Args:
            video_url: URL of the video (already has voice audio).
            music_url: URL of the background music.
            music_volume: Volume level for music (0.0 to 1.0). Default 0.25.
            assume_has_audio: If True, treat video as having audio and mix (amix) without probing.
                Use when concat was done with assume_clips_have_audio so we know VO is present.

        Returns:
            URL of the video with mixed audio, or None if failed.
        """
        try:
            logger.info(f"🎵 Adding background music to video (music_volume={music_volume})...")

            url = f"{self.base_url}/v1/run-ffmpeg-command"

            # If caller knows video has audio (e.g. concat with per-scene VO), use amix without probe
            if assume_has_audio:
                has_audio = True
            else:
                # If video has no audio (e.g. Veo/concat video-only), use simple map to avoid [0:a] error
                has_audio = self.validate_video_has_audio(video_url)
            if not has_audio:
                ffmpeg_command = (
                    f"-i {{{{in_1}}}} -i {{{{in_2}}}} "
                    f"-map 0:v -map 1:a -c:v copy -c:a aac -b:a 192k -shortest {{{{out_1}}}}"
                )
            else:
                # Overlay music on video's existing audio (voice + music mix)
                ffmpeg_command = (
                    f"-i {{{{in_1}}}} -i {{{{in_2}}}} "
                    f"-filter_complex \"[0:a]volume=1.0[voice];[1:a]volume={music_volume}[music];[voice][music]amix=inputs=2:duration=first:dropout_transition=2[mixed]\" "
                    f"-map 0:v -map \"[mixed]\" -c:v copy -c:a aac -b:a 192k -shortest {{{{out_1}}}}"
                )

            payload = {
                "ffmpeg_command": ffmpeg_command,
                "input_files": {
                    "in_1": video_url,
                    "in_2": music_url
                },
                "output_files": {
                    "out_1": "video_with_music.mp4"
                },
                "vcpu_count": 4,
                "max_command_run_seconds": 300
            }

            response = requests.post(url, headers=self.headers, json=payload, timeout=60)
            response.raise_for_status()

            result = response.json()

            if "command_id" in result:
                command_id = result["command_id"]
                logger.info(f"✅ Rendi background music task created: {command_id}")
                return self._wait_for_command(command_id)
            else:
                logger.error(f"❌ Rendi API error: {result}")
                return None

        except Exception as e:
            logger.error(f"❌ Error adding background music: {e}")
            return None

    def overlay_logo_on_video(
        self,
        video_url: str,
        logo_url: str,
        position: str = "bottom-right",
        duration_seconds: float = None,
        logo_scale: float = 0.15,
        margin: int = 30
    ) -> Optional[str]:
        """Overlay a logo on video, optionally only in the last N seconds.

        Args:
            video_url: URL of the input video.
            logo_url: URL of the logo image (PNG with transparency recommended).
            position: Logo position - "bottom-right", "bottom-center", "bottom-left",
                     "top-right", "top-center", "top-left". Default: "bottom-right".
            duration_seconds: If set, logo appears only in last N seconds. If None, shows whole video.
            logo_scale: Logo size as fraction of video width (0.0 to 1.0). Default: 0.15.
            margin: Margin in pixels from video edges. Default: 30.

        Returns:
            URL of the video with logo overlay, or None if failed.
        """
        try:
            logger.info(f"🏷️ Overlaying logo on video (position={position}, scale={logo_scale})...")

            url = f"{self.base_url}/v1/run-ffmpeg-command"

            # Calculate position coordinates
            # Video is assumed 1080x1920 (9:16)
            position_map = {
                "bottom-right": f"x=W-w-{margin}:y=H-h-{margin}",
                "bottom-center": f"x=(W-w)/2:y=H-h-{margin}",
                "bottom-left": f"x={margin}:y=H-h-{margin}",
                "top-right": f"x=W-w-{margin}:y={margin}",
                "top-center": f"x=(W-w)/2:y={margin}",
                "top-left": f"x={margin}:y={margin}",
            }

            pos_expr = position_map.get(position, position_map["bottom-right"])

            # Build enable expression for timing
            if duration_seconds and duration_seconds > 0:
                enable_expr = f":enable='gte(t,main_w-{duration_seconds})'"
                logger.info(f"   Logo will appear in last {duration_seconds}s of video")
                enable_expr = ""  # Show entire video
            else:
                enable_expr = ""  # Show entire video

            # FFmpeg filter to overlay logo
            # Scale logo to fraction of video width while maintaining aspect ratio
            ffmpeg_command = (
                f"-i {{{{in_1}}}} -i {{{{in_2}}}} "
                f"-filter_complex \"[1:v]scale=iw*{logo_scale}:-1[logo];[0:v][logo]overlay={pos_expr}{enable_expr}[outv]\" "
                f"-map \"[outv]\" -map 0:a? -c:v libx264 -preset fast -crf {config.VIDEO_CRF} -c:a copy {{{{out_1}}}}"
            )

            payload = {
                "ffmpeg_command": ffmpeg_command,
                "input_files": {
                    "in_1": video_url,
                    "in_2": logo_url
                },
                "output_files": {
                    "out_1": "video_with_logo.mp4"
                },
                "vcpu_count": 4,
                "max_command_run_seconds": 300
            }

            response = requests.post(url, headers=self.headers, json=payload, timeout=60)
            response.raise_for_status()

            result = response.json()

            if "command_id" in result:
                command_id = result["command_id"]
                logger.info(f"✅ Rendi logo overlay task created: {command_id}")
                return self._wait_for_command(command_id)
            else:
                logger.error(f"❌ Rendi API error: {result}")
                return None

        except Exception as e:
            logger.error(f"❌ Error overlaying logo: {e}")
            return None

    def add_vo_and_music_to_video(
        self,
        video_url: str,
        vo_url: str,
        music_url: str,
        vo_volume: float = 1.0,
        music_volume: float = 0.2
    ) -> Optional[str]:
        """Add both voice-over and background music to a video without audio.

        The output duration matches the VIDEO length (not the VO). VO plays in full;
        music continues for the rest of the video after the VO ends.
        - amix duration=longest: mix is as long as the longest input (music)
        - -shortest: output = min(video, mix) = video length
        - Result: full video length, with VO + music while VO plays, then music only to the end.

        Args:
            video_url: URL of the video (no audio or will be replaced).
            vo_url: URL of the voice-over audio.
            music_url: URL of the background music.
            vo_volume: Volume level for VO (0.0 to 1.0). Default 1.0.
            music_volume: Volume level for music (0.0 to 1.0). Default 0.2.

        Returns:
            URL of the video with mixed audio, or None if failed.
        """
        try:
            logger.info(f"🎵🎤 Adding VO + music to video (vo={vo_volume}, music={music_volume})...")

            url = f"{self.base_url}/v1/run-ffmpeg-command"

            # FFmpeg: mix VO + music; output duration = VIDEO length (so video does not end when VO ends)
            # in_1 = video, in_2 = VO, in_3 = music
            # duration=longest: mix length = max(VO, music) so music continues after VO ends
            # -shortest: output = min(video, mix) = video length → full video with VO then music to end
            ffmpeg_command = (
                f"-i {{{{in_1}}}} -i {{{{in_2}}}} -i {{{{in_3}}}} "
                f"-filter_complex \"[1:a]volume={vo_volume},apad[vo];[2:a]volume={music_volume}[music];[vo][music]amix=inputs=2:duration=longest:dropout_transition=2[mixed]\" "
                f"-map 0:v -map \"[mixed]\" -c:v copy -c:a aac -b:a 192k -shortest {{{{out_1}}}}"
            )

            payload = {
                "ffmpeg_command": ffmpeg_command,
                "input_files": {
                    "in_1": video_url,  # Video
                    "in_2": vo_url,     # Voice-over
                    "in_3": music_url   # Background music
                },
                "output_files": {
                    "out_1": "video_with_vo_and_music.mp4"
                },
                "vcpu_count": 4,
                "max_command_run_seconds": 300
            }

            response = requests.post(url, headers=self.headers, json=payload, timeout=60)
            response.raise_for_status()

            result = response.json()

            if "command_id" in result:
                command_id = result["command_id"]
                logger.info(f"✅ Rendi VO+music task created: {command_id}")
                return self._wait_for_command(command_id)
            else:
                logger.error(f"❌ Rendi API error: {result}")
                return None

        except Exception as e:
            logger.error(f"❌ Error adding VO + music: {e}")
            return None

    def overlay_cta_on_video(
        self,
        video_url: str,
        cta_image_url: str,
        position: str = "bottom_center"
    ) -> Optional[str]:
        """Overlay a CTA button image on a video with floating animation.

        Args:
            video_url: URL of the video to overlay on.
            cta_image_url: URL of the CTA button image (PNG with transparency).
            position: Position of the overlay ('bottom_center', 'center', etc.).

        Returns:
            URL of the video with CTA overlay, or None if failed.
        """
        try:
            logger.info(f"🔘 Overlaying CTA button on video with floating effect (position: {position})...")

            url = f"{self.base_url}/v1/run-ffmpeg-command"

            # Floating animation: button moves up and down using sine wave
            # Base Y position at bottom-center (10% from bottom)
            # sin(t*3) creates smooth oscillation, *10 is the amplitude (10 pixels up/down)
            base_y = "main_h-overlay_h-main_h*0.10"
            float_offset = "sin(t*3)*10"  # 3 cycles per second, 10 pixels amplitude

            if position == "bottom_center":
                overlay_filter = f"overlay=x=(main_w-overlay_w)/2:y={base_y}+{float_offset}"
            elif position == "center":
                overlay_filter = f"overlay=x=(main_w-overlay_w)/2:y=(main_h-overlay_h)/2+{float_offset}"
            elif position == "bottom_right":
                overlay_filter = f"overlay=x=main_w-overlay_w-main_w*0.05:y={base_y}+{float_offset}"
            else:
                overlay_filter = f"overlay=x=(main_w-overlay_w)/2:y={base_y}+{float_offset}"

            # FFmpeg command to overlay PNG image on video with floating animation
            # Scale the overlay to be ~50% of video width while maintaining aspect ratio
            ffmpeg_command = (
                f"-i {{{{in_1}}}} -i {{{{in_2}}}} "
                f"-filter_complex \"[1:v]scale=iw*0.5:-1[scaled];[0:v][scaled]{overlay_filter}[out]\" "
                f"-map \"[out]\" -map 0:a? -c:v libx264 -preset fast -crf {config.VIDEO_CRF} -c:a copy {{{{out_1}}}}"
            )

            payload = {
                "ffmpeg_command": ffmpeg_command,
                "input_files": {
                    "in_1": video_url,      # Video
                    "in_2": cta_image_url   # CTA button image
                },
                "output_files": {
                    "out_1": "video_with_cta.mp4"
                },
                "vcpu_count": 4,
                "max_command_run_seconds": 300
            }

            response = requests.post(url, headers=self.headers, json=payload, timeout=60)
            response.raise_for_status()

            result = response.json()

            if "command_id" in result:
                command_id = result["command_id"]
                logger.info(f"✅ Rendi CTA overlay task created: {command_id}")
                return self._wait_for_command(command_id)
            else:
                logger.error(f"❌ Rendi API error: {result}")
                return None

        except Exception as e:
            logger.error(f"❌ Error overlaying CTA on video: {e}")
            return None

    def overlay_cta_on_video_timed(
        self,
        video_url: str,
        cta_image_url: str,
        position: str = "bottom_center",
        start_time: float = 0.0,
        end_time: float = None
    ) -> Optional[str]:
        """Overlay a CTA button image on a video only during a specific time range.

        Args:
            video_url: URL of the video to overlay on.
            cta_image_url: URL of the CTA button image (PNG with transparency).
            position: Position of the overlay ('bottom_center', 'center', etc.).
            start_time: Start time in seconds when CTA should appear.
            end_time: End time in seconds when CTA should disappear (None = end of video).

        Returns:
            URL of the video with CTA overlay, or None if failed.
        """
        try:
            logger.info(f"🔘 Overlaying CTA button on video (position: {position}, start: {start_time:.1f}s)...")

            url = f"{self.base_url}/v1/run-ffmpeg-command"

            # Floating animation: button moves up and down using sine wave
            base_y = "main_h-overlay_h-main_h*0.10"
            float_offset = "sin(t*3)*10"

            if position == "bottom_center":
                overlay_filter = f"overlay=x=(main_w-overlay_w)/2:y={base_y}+{float_offset}"
            elif position == "center":
                overlay_filter = f"overlay=x=(main_w-overlay_w)/2:y=(main_h-overlay_h)/2+{float_offset}"
            elif position == "bottom_right":
                overlay_filter = f"overlay=x=main_w-overlay_w-main_w*0.05:y={base_y}+{float_offset}"
            else:
                overlay_filter = f"overlay=x=(main_w-overlay_w)/2:y={base_y}+{float_offset}"

            # Add timing condition: enable overlay only between start_time and end_time
            # Using 'between(t,start,end)' function in FFmpeg
            if end_time is not None:
                timing_condition = f":enable='between(t,{start_time},{end_time})'"
            else:
                timing_condition = f":enable='gte(t,{start_time})'"

            overlay_filter += timing_condition

            # FFmpeg command with timed overlay
            ffmpeg_command = (
                f"-i {{{{in_1}}}} -i {{{{in_2}}}} "
                f"-filter_complex \"[1:v]scale=iw*0.5:-1[scaled];[0:v][scaled]{overlay_filter}[out]\" "
                f"-map \"[out]\" -map 0:a? -c:v libx264 -preset fast -crf {config.VIDEO_CRF} -c:a copy {{{{out_1}}}}"
            )

            payload = {
                "ffmpeg_command": ffmpeg_command,
                "input_files": {
                    "in_1": video_url,
                    "in_2": cta_image_url
                },
                "output_files": {
                    "out_1": "video_with_cta_timed.mp4"
                },
                "vcpu_count": 4,
                "max_command_run_seconds": 300
            }

            response = requests.post(url, headers=self.headers, json=payload, timeout=60)
            response.raise_for_status()

            result = response.json()

            if "command_id" in result:
                command_id = result["command_id"]
                logger.info(f"✅ Rendi CTA timed overlay task created: {command_id}")
                return self._wait_for_command(command_id)
            else:
                logger.error(f"❌ Rendi API error: {result}")
                return None

        except Exception as e:
            logger.error(f"❌ Error overlaying timed CTA on video: {e}")
            return None

    def add_text_overlay_to_video(
        self,
        video_url: str,
        text: str,
        start_time: float = 0.0,
        end_time: float = None,
        position: str = "bottom_center",
        font_size: int = 60,
        font_color: str = "white",
        background_color: str = "black@0.5"
    ) -> Optional[str]:
        """Add text overlay to video using FFmpeg drawtext filter.

        Args:
            video_url: URL of the video to add text to.
            text: Text to display.
            start_time: Start time in seconds when text should appear.
            end_time: End time in seconds when text should disappear (None = end of video).
            position: Position of the text ('bottom_center', 'center', 'top_center', etc.).
            font_size: Font size in pixels.
            font_color: Font color (e.g., 'white', 'black', '#FFFFFF').
            background_color: Background color for text box (e.g., 'black@0.5' for semi-transparent black).

        Returns:
            URL of the video with text overlay, or None if failed.
        """
        try:
            logger.info(f"📝 Adding text overlay to video: '{text[:50]}...' (position: {position}, start: {start_time:.1f}s)")

            url = f"{self.base_url}/v1/run-ffmpeg-command"

            # Escape special characters in text for FFmpeg
            escaped_text = text.replace("'", "\\'").replace(":", "\\:").replace("\\", "\\\\")

            # Position calculation
            if position == "bottom_center":
                x_expr = "(main_w-text_w)/2"
                y_expr = "main_h-text_h-50"
            elif position == "center":
                x_expr = "(main_w-text_w)/2"
                y_expr = "(main_h-text_h)/2"
            elif position == "top_center":
                x_expr = "(main_w-text_w)/2"
                y_expr = "50"
            elif position == "bottom_left":
                x_expr = "50"
                y_expr = "main_h-text_h-50"
            elif position == "bottom_right":
                x_expr = "main_w-text_w-50"
                y_expr = "main_h-text_h-50"
            else:
                x_expr = "(main_w-text_w)/2"
                y_expr = "main_h-text_h-50"

            # Build drawtext filter
            # Add background box for better readability
            drawtext_filter = (
                f"drawtext=text='{escaped_text}'"
                f":fontsize={font_size}"
                f":fontcolor={font_color}"
                f":box=1:boxcolor={background_color}:boxborderw=10"
                f":x={x_expr}"
                f":y={y_expr}"
            )

            # Add timing if specified
            if end_time is not None:
                drawtext_filter += f":enable='between(t,{start_time},{end_time})'"
            elif start_time > 0:
                drawtext_filter += f":enable='gte(t,{start_time})'"

            ffmpeg_command = (
                f"-i {{{{in_1}}}} "
                f"-vf \"{drawtext_filter}\" "
                f"-c:v libx264 -preset fast -crf {config.VIDEO_CRF} "
                f"-c:a copy "
                f"-movflags +faststart "
                f"{{{{out_1}}}}"
            )

            payload = {
                "ffmpeg_command": ffmpeg_command,
                "input_files": {
                    "in_1": video_url
                },
                "output_files": {
                    "out_1": "video_with_text_overlay.mp4"
                },
                "vcpu_count": 4,
                "max_command_run_seconds": 300
            }

            response = requests.post(url, headers=self.headers, json=payload, timeout=60)
            response.raise_for_status()

            result = response.json()

            if "command_id" in result:
                command_id = result["command_id"]
                logger.info(f"✅ Rendi text overlay task created: {command_id}")
                return self._wait_for_command(command_id)
            else:
                logger.error(f"❌ Rendi API error: {result}")
                return None

        except Exception as e:
            logger.error(f"❌ Error adding text overlay to video: {e}")
            return None

    def _wait_for_command(self, command_id: str, timeout: int = 600) -> Optional[str]:
        """Wait for Rendi command to complete.

        Args:
            command_id: Command ID to poll.
            timeout: Maximum wait time in seconds.

        Returns:
            URL of the output video, or None if failed/timeout.
        """
        start_time = time.time()
        check_interval = 10

        while time.time() - start_time < timeout:
            try:
                url = f"{self.base_url}/v1/commands/{command_id}"
                response = requests.get(url, headers=self.headers, timeout=30)
                response.raise_for_status()

                result = response.json()
                status = result.get("status", "").lower()

                if status in ["completed", "success"]:
                    output_files = result.get("output_files", {})
                    if "out_1" in output_files and "storage_url" in output_files["out_1"]:
                        video_url = output_files["out_1"]["storage_url"]
                        logger.info(f"✅ Rendi command completed")
                        return video_url

                elif status == "failed":
                    error_msg = result.get("error_message", "Unknown error")
                    logger.error(f"❌ Rendi command failed: {error_msg}")
                    return None

            except Exception as e:
                logger.error(f"❌ Error polling Rendi command: {e}")

            time.sleep(check_interval)

        logger.error("❌ Rendi command timeout")
        return None
