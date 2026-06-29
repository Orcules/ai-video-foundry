import os
import re
import time
import json
import random
import logging
import tempfile
from typing import Dict, List, Optional

import requests

from api_pipeline.services.base.config import config


logger = logging.getLogger(__name__)


class ZapCapService:
    """Service for adding subtitles via ZapCap API."""

    # List of available template IDs for random selection
    TEMPLATE_IDS = [
        "your-zapcap-template-id",
        "50cdfac1-0a7a-48dd-af14-4d24971e213a",
        "55267be2-9eec-4d06-aff8-edcb401b112e",
        "5de632e7-0b02-4d15-8137-e004871e861b",
        "7b946549-ae16-4085-9dd3-c20c82504daa",
        "982ad276-a76f-4d80-a4e2-b8fae0038464",
        "a51c5222-47a7-4c37-b052-7b9853d66bf6",
        "ca050348-e2d0-49a7-9c75-7a5e8335c67d",
        "d46bb0da-cce0-4507-909d-fa8904fb8ed7",
        "dfe027d9-bd9d-4e55-a94f-d57ed368a060",
        "e659ee0c-53bb-497e-869c-90f8ec0a921f",
        "d2018215-2125-41c1-940e-f13b411fff5c",
        "1c0c9b65-47c4-41bf-a187-25a8305fd0dd",
        "a104df87-5b1a-4490-8cca-62e504a84615",
        "6255949c-4a52-4255-8a67-39ebccfaa3ef",
        "a6760d82-72c1-4190-bfdb-7d9c908732f1"
    ]

    def __init__(self, api_key: str, template_id: str = None):
        """Initialize ZapCap service.

        Args:
            api_key: ZapCap API key.
            template_id: Optional template ID for caption styling (if not provided, will be randomly selected per request).
        """
        self.api_key = api_key
        self.base_url = config.ZAPCAP_BASE_URL
        self.default_template_id = template_id or config.ZAPCAP_TEMPLATE_ID

    def _get_random_template_id(self) -> str:
        """Get a random template ID from the available list.

        Returns:
            A random template ID string.
        """
        return random.choice(self.TEMPLATE_IDS)

    @staticmethod
    def _strip_audio_tags(text: str) -> str:
        """Remove audio/stage direction tags like [dramatically], [sighs], [laughs], etc.

        Handles tags that may span the whole word or be embedded in text.
        Also handles ElevenLabs splitting tags across multiple words
        (e.g. word = "[dramatically]" or word = "[" or word = "]").
        """
        if not text:
            return ""
        # Remove complete [tag] blocks (including nested content)
        cleaned = re.sub(r'\[[^\]]*\]', '', text)
        # Remove stray brackets that remain after split-word tags
        cleaned = cleaned.replace('[', '').replace(']', '')
        # Remove ||| segment separators that might leak into word text
        cleaned = cleaned.replace('|||', '')
        return cleaned.strip()

    def _normalize_transcript_for_zapcap(self, transcript: List[Dict]) -> Optional[List[Dict]]:
        """Normalize word segments from ElevenLabs to ZapCap BYOT format.

        ZapCap UpdateWordEntryDto requires exactly:
          - text (str, required)
          - type (str, required) - "word"
          - start_time (float, required) - seconds
          - end_time (float, required) - seconds

        Entries must be sequentially consistent (no overlaps, start < end).
        Audio tags like [dramatically], [sighs] etc. are stripped entirely.
        """
        if not transcript or not isinstance(transcript, list):
            return None
        out = []
        for seg in transcript:
            if not isinstance(seg, dict):
                continue
            raw_text = seg.get("text", "").strip()
            if not raw_text:
                continue
            # Strip audio tags ([excited], [whispers], [Scene 1], [laughs], etc.)
            text = self._strip_audio_tags(raw_text)
            if not text:
                continue  # Entire word was a tag — skip it
            start = seg.get("start_time") if seg.get("start_time") is not None else seg.get("start")
            end = seg.get("end_time") if seg.get("end_time") is not None else seg.get("end")
            if start is None or end is None:
                continue
            try:
                start_f = float(start)
                end_f = float(end)
            except (TypeError, ValueError):
                continue
            if end_f <= start_f:
                end_f = start_f + 0.01
            out.append({"start_f": start_f, "end_f": end_f, "text": text})
        if not out:
            return None
        out.sort(key=lambda x: (x["start_f"], x["end_f"]))
        # Enforce sequential consistency: no overlaps, each end_time <= next start_time
        result = []
        last_end = -0.001
        for seg in out:
            start_f = seg["start_f"]
            end_f = seg["end_f"]
            if start_f < last_end:
                start_f = last_end
            if end_f <= start_f:
                end_f = start_f + 0.01
            last_end = end_f
            # ZapCap UpdateWordEntryDto — only the 4 required fields
            result.append({
                "text": seg["text"],
                "type": "word",
                "start_time": round(start_f, 2),
                "end_time": round(end_f, 2)
            })
        if result:
            logger.info(f"   ZapCap BYOT: {len(result)} words (stripped {len(transcript) - len(result)} tags/empty), "
                        f"span {result[0]['start_time']:.1f}s - {result[-1]['end_time']:.1f}s")
        return result

    def add_subtitles(
        self,
        video_url: str,
        language: str = "en",
        transcript: List[Dict] = None
    ) -> Optional[str]:
        """Add subtitles to video using ZapCap.

        Args:
            video_url: URL of the video to add subtitles to.
            language: Language code for subtitles (default: "en").
            transcript: Optional list of word segments with timing for "Bring Your Own Transcript".
                       Each segment: {"text": "word", "type": "word", "start_time": 0.0, "end_time": 0.5}
                       When provided, ZapCap skips auto-transcription and uses these values.

        Returns:
            URL of the captioned video, or None if failed.
        """
        try:
            # Normalize transcript for ZapCap: ensure each segment has text + start/end (seconds); some APIs also expect "start"/"end" keys
            zapcap_transcript = self._normalize_transcript_for_zapcap(transcript) if transcript else None
            if zapcap_transcript:
                logger.info(f"📝 Adding subtitles via ZapCap with custom transcript ({len(zapcap_transcript)} words, language: {language})...")
                sample = zapcap_transcript[0]
                logger.info(f"   Transcript first segment (for verification): {sample}")
            else:
                if transcript:
                    logger.warning("⚠️ ZapCap: transcript provided but normalized to empty (filtered/invalid); using auto-transcription")
                else:
                    logger.warning("⚠️ ZapCap: no transcript provided - using auto-transcription (video may return without subtitles if speech unclear)")
                logger.info(f"📝 Adding subtitles via ZapCap (language: {language})...")

            # Log API key status (masked) for debugging
            key_preview = f"{self.api_key[:8]}...{self.api_key[-4:]}" if self.api_key and len(self.api_key) > 12 else "EMPTY/SHORT"
            logger.info(f"   ZapCap API key: {key_preview} ({len(self.api_key)} chars)")

            # Step 1: Download video
            logger.info(f"   ZapCap Step 1: Downloading video from {video_url[:80]}...")
            video_data = self._download_video(video_url)
            if not video_data:
                logger.error("❌ Failed to download video for subtitles (ZapCap Step 1)")
                return None

            logger.info(f"   ZapCap Step 1 ✅: Downloaded {len(video_data)} bytes")

            # Step 2: Upload to ZapCap
            headers = {"x-api-key": self.api_key}

            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as temp_file:
                temp_file.write(video_data)
                temp_path = temp_file.name

            try:
                file_size_mb = len(video_data) / (1024 * 1024)
                upload_timeout = max(300, int(file_size_mb * 5))  # ~5s per MB, minimum 300s
                logger.info(f"   ZapCap Step 2: Uploading {file_size_mb:.0f}MB to ZapCap (timeout: {upload_timeout}s)...")

                upload_ok = False
                for upload_attempt in range(2):  # Retry once on timeout
                    try:
                        with open(temp_path, "rb") as f:
                            files = {"file": (f"video_{int(time.time())}.mp4", f, "video/mp4")}
                            response = requests.post(
                                f"{self.base_url}/videos",
                                headers=headers,
                                files=files,
                                timeout=upload_timeout
                            )
                        upload_ok = True
                        break
                    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as upload_err:
                        if upload_attempt == 0:
                            logger.warning(f"   ZapCap upload attempt 1 failed ({upload_err}), retrying...")
                        else:
                            raise

                if not upload_ok:
                    logger.error("❌ ZapCap upload failed after 2 attempts")
                    return None

                logger.info(f"   ZapCap Step 2: Upload response status={response.status_code}")
                if response.status_code not in [200, 201]:
                    logger.error(f"❌ ZapCap upload failed: {response.status_code} - {response.text[:500]}")
                    logger.error(f"   → Check if ZAPCAP_API_KEY is the API Key (not the Webhook Secret). Webhook secrets don't work for API calls.")
                    return None

                video_id = response.json().get("id")
                logger.info(f"   ZapCap Step 2 ✅: Uploaded, video_id={video_id}")

                # Step 3: Create caption task
                # Select a random template ID for each request
                selected_template_id = self._get_random_template_id()
                logger.info(f"   Using random ZapCap template: {selected_template_id}")

                task_body = {
                    "templateId": selected_template_id,
                    "language": language.lower(),
                    "autoApprove": True,
                    "renderOptions": {
                        "subsOptions": {
                            "emoji": True,
                            "emojiAnimation": True,
                            "emphasizeKeywords": True
                        },
                        "styleOptions": {
                            "top": 50,
                            "fontUppercase": False,
                            "fontSize": 42,
                            "fontWeight": 700,
                            "fontColor": "#FFFFFF",
                            "fontShadow": "m",
                            "stroke": "s",
                            "strokeColor": "#000000"
                        }
                    }
                }

                # Add custom transcript if provided (Bring Your Own Transcript)
                if zapcap_transcript:
                    task_body["transcript"] = zapcap_transcript
                    logger.info(f"   Sending custom transcript to ZapCap: {len(zapcap_transcript)} segments in task body (keys: {list(task_body.keys())})")

                task_response = requests.post(
                    f"{self.base_url}/videos/{video_id}/task",
                    headers=headers,
                    json=task_body,
                    timeout=60
                )

                if task_response.status_code not in [200, 201]:
                    logger.error(f"❌ ZapCap task creation failed: status={task_response.status_code}, body={task_response.text[:500]}")
                    return None

                task_result = task_response.json()
                if zapcap_transcript and (task_result.get("taskId") or task_result.get("id")):
                    logger.info(f"   ZapCap task created with custom transcript (task_id={task_result.get('taskId') or task_result.get('id')})")
                task_id = task_result.get("taskId") or task_result.get("id")
                logger.info(f"   Task created: task_id={task_id}")

                # Step 4: Wait for completion
                return self._wait_for_completion(video_id, task_id)

            finally:
                try:
                    os.unlink(temp_path)
                except:
                    pass

        except Exception as e:
            logger.error(f"❌ ZapCap error: {e}")
            return None

    def _wait_for_completion(self, video_id: str, task_id: str, timeout: int = 600) -> Optional[str]:
        """Wait for ZapCap to finish processing.

        Args:
            video_id: ZapCap video ID.
            task_id: ZapCap task ID.
            timeout: Maximum wait time in seconds.

        Returns:
            URL of the captioned video, or None if failed/timeout.
        """
        headers = {"x-api-key": self.api_key}
        start_time = time.time()

        logger.info(f"   Waiting for ZapCap processing (timeout: {timeout}s)...")

        while time.time() - start_time < timeout:
            try:
                response = requests.get(
                    f"{self.base_url}/videos/{video_id}/task/{task_id}",
                    headers=headers,
                    timeout=30
                )
                response.raise_for_status()

                data = response.json()
                status = data.get("status", "").lower()

                if status == "completed":
                    download_url = data.get("downloadUrl")
                    if download_url:
                        logger.info(f"✅ ZapCap subtitles added: {download_url}")
                        return download_url
                    else:
                        logger.warning("⚠️ ZapCap completed but no download URL")
                        return None

                elif status == "failed":
                    error_msg = data.get("error", "Unknown error")
                    logger.error(f"❌ ZapCap task failed: {error_msg}")
                    return None

                # Still processing
                time.sleep(10)

            except Exception as e:
                logger.warning(f"⚠️ Error checking ZapCap status: {e}")
                time.sleep(10)

        logger.error(f"❌ ZapCap timeout after {timeout}s")
        return None

    def _download_video(self, url: str) -> Optional[bytes]:
        """Download video from URL.

        Args:
            url: URL of the video to download.

        Returns:
            Video data as bytes, or None if failed.
        """
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            response = requests.get(url, headers=headers, timeout=120)
            response.raise_for_status()
            if len(response.content) < 1000:
                logger.warning(f"⚠️ Downloaded video is very small ({len(response.content)} bytes) - might not be valid")
            return response.content
        except Exception as e:
            logger.error(f"❌ Video download failed ({url[:80]}...): {e}")
            return None
