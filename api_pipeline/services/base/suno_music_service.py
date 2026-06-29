"""SunoMusicService — extracted verbatim from Comp_Videos/video_scene_processor.py.

Lines 14009-14393 of the monolith.
"""

import time
import logging
import requests
from typing import Dict, Any, List, Optional, Tuple

from api_pipeline.services.base.config import config

logger = logging.getLogger(__name__)


class SunoMusicService:
    """Service for generating music using Suno via Kie.ai API."""
    
    def __init__(self, api_key: str, openai_client):
        """Initialize Suno Music service.
        
        Args:
            api_key: Kie.ai API key.
            openai_client: OpenAI client for Whisper transcription.
        """
        self.api_key = api_key
        self.base_url = config.KIE_BASE_URL
        self.openai_client = openai_client
    
    def detect_lyrics_in_audio(self, audio_path: str) -> Tuple[bool, str]:
        """Detect if audio has lyrics using OpenAI Whisper.
        
        Args:
            audio_path: Path to the audio file.
            
        Returns:
            Tuple of (has_lyrics: bool, lyrics_text: str)
        """
        try:
            logger.info("🎤 Detecting lyrics in audio using Whisper...")
            
            with open(audio_path, "rb") as audio_file:
                transcript = self.openai_client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file
                )
            
            lyrics = transcript.text.strip()
            # If more than 20 characters, likely has lyrics (not just noise/artifacts)
            has_lyrics = len(lyrics) > 20
            
            if has_lyrics:
                logger.info(f"   Found lyrics ({len(lyrics)} chars): {lyrics[:100]}...")
            else:
                logger.info("   No lyrics detected (instrumental)")
            
            return has_lyrics, lyrics
            
        except Exception as e:
            logger.warning(f"⚠️ Could not detect lyrics: {e}")
            return False, ""
    
    def generate_instrumental_background(
        self, 
        audio_url: str,
        style: str = None,
        fallback_style: str = None
    ) -> Optional[str]:
        """Generate instrumental background music.
        
        First tries upload-cover with reference audio using creative parameters.
        If that fails (e.g., copyright detection), falls back to pure generation
        using the AI-generated style description.
        
        Args:
            audio_url: URL of the original audio to use as reference.
            style: Style description for the instrumental.
            fallback_style: AI-generated style description for pure generation fallback.
            
        Returns:
            URL of the generated instrumental, or None if all methods failed.
        """
        # Default style if not provided
        if not style:
            style = "upbeat corporate background music, modern, professional, energetic, no vocals"
        
        # Try upload-cover first with creative parameters
        result = self._try_upload_cover(audio_url, style)
        
        if result:
            return result
        
        # Fallback: Generate pure music without reference audio
        # This avoids copyright issues entirely
        fallback_description = fallback_style or style
        logger.info("🔄 Upload-cover failed, falling back to pure music generation (no reference audio)...")
        return self.generate_pure_music(fallback_description)
    
    def _try_upload_cover(self, audio_url: str, style: str) -> Optional[str]:
        """Try to generate music using upload-cover with reference audio.
        
        Uses creative parameters to minimize copyright detection.
        
        Args:
            audio_url: URL of the original audio.
            style: Style description.
            
        Returns:
            URL of generated music, or None if failed.
        """
        try:
            logger.info("🎵 Trying Suno upload-cover (creative params)...")
            
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            
            # CREATIVE PARAMETERS to minimize copyright detection
            request_body = {
                "uploadUrl": audio_url,
                "customMode": True,
                "instrumental": True,
                "style": style[:1000],
                "title": f"BGM_{int(time.time())}",
                "model": "V5",
                # Very creative settings to avoid "matches existing work" error
                "audioWeight": 0.25,       # Very low = minimal similarity to reference
                "styleWeight": 0.80,       # High = more influence from style text
                "weirdnessConstraint": 0.60,  # High = allows more creative deviation
                "callBackUrl": "https://httpbin.org/post"
            }
            
            logger.info(f"   Style: {style[:50]}...")
            logger.info(f"   Source audio: {audio_url[:50]}...")
            logger.info(f"   Creative params: audioWeight=0.25, styleWeight=0.80, weirdness=0.60")
            
            response = requests.post(
                f"{self.base_url}/api/v1/generate/upload-cover",
                headers=headers,
                json=request_body,
                timeout=60
            )
            
            if response.status_code != 200:
                logger.warning(f"⚠️ Suno upload-cover API error: {response.status_code}")
                return None
            
            result = response.json()
            if result.get("code") != 200:
                logger.warning(f"⚠️ Suno upload-cover returned error: {result.get('msg')}")
                return None
            
            task_id = result.get("data", {}).get("taskId")
            if not task_id:
                logger.warning("⚠️ No task ID returned from Suno upload-cover")
                return None
            
            logger.info(f"   Suno upload-cover task started: {task_id}")
            
            return self._wait_for_music(task_id)
            
        except Exception as e:
            logger.warning(f"⚠️ Suno upload-cover error: {e}")
            return None
    
    def generate_pure_music(self, style_description: str) -> Optional[str]:
        """Generate music purely from a text description (no reference audio).
        
        This avoids copyright issues entirely by not using any reference audio.
        Uses the Kie.ai /api/v1/generate endpoint with customMode=True and instrumental=True.
        
        Args:
            style_description: Detailed description of the music style to generate.
            
        Returns:
            URL of the generated music, or None if failed.
        """
        try:
            logger.info("🎵 Generating original music with Suno (pure generation, no reference audio)...")
            logger.info(f"   Style: {style_description[:100]}...")
            
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            
            # Use /api/v1/generate endpoint (correct endpoint for pure generation)
            # With customMode=True and instrumental=True, only style and title are required
            request_body = {
                "customMode": True,
                "instrumental": True,  # Force instrumental (no vocals)
                "style": style_description[:1000],  # V5 supports up to 1000 chars for style
                "title": f"BGM_{int(time.time())}",
                "model": "V5",
                "callBackUrl": "https://httpbin.org/post",
                # Optional: control creativity
                "styleWeight": 0.85,  # Strong adherence to style description
                "weirdnessConstraint": 0.40  # Some creative freedom
            }
            
            response = requests.post(
                f"{self.base_url}/api/v1/generate",
                headers=headers,
                json=request_body,
                timeout=60
            )
            
            if response.status_code != 200:
                logger.error(f"❌ Suno pure generation API error: {response.status_code} - {response.text}")
                return None
            
            result = response.json()
            if result.get("code") != 200:
                logger.error(f"❌ Suno pure generation returned error: {result.get('msg')}")
                return None
            
            task_id = result.get("data", {}).get("taskId")
            if not task_id:
                logger.error("❌ No task ID returned from Suno pure generation")
                return None
            
            logger.info(f"   Suno pure generation task started: {task_id}")
            
            return self._wait_for_music(task_id)
            
        except Exception as e:
            logger.error(f"❌ Suno pure generation error: {e}")
            return None
    
    def generate_cover_music(
        self, 
        audio_url: str, 
        audio_path: str = None, 
        style: str = None
    ) -> Optional[str]:
        """Generate a cover of the audio with similar style using Suno.
        
        This is used when the original video has NO voice-over (music only),
        and we want to create a new version of that music.
        
        Uses creative parameters to avoid copyright issues while still
        capturing the dynamic style/mood of the reference audio.
        
        Args:
            audio_url: URL of the original audio.
            audio_path: Local path to audio (for lyrics detection).
            style: Optional style description.
            
        Returns:
            URL of the generated music, or None if failed.
        """
        try:
            logger.info("🎵 Generating new music with Suno (creative mode)...")
            
            # Detect if audio has lyrics
            has_lyrics = False
            lyrics = ""
            
            if audio_path and os.path.exists(audio_path):
                has_lyrics, lyrics = self.detect_lyrics_in_audio(audio_path)
            
            # Build request based on whether it has lyrics
            # CREATIVE PARAMETERS: Lower audioWeight + higher weirdnessConstraint
            # to create original music inspired by the reference without copying it
            if has_lyrics and lyrics:
                # Vocal cover configuration with CREATIVE settings
                logger.info("   Using VOCAL cover mode (with lyrics) - creative params")
                request_body = {
                    "uploadUrl": audio_url,
                    "customMode": True,
                    "instrumental": False,
                    "prompt": lyrics[:5000],  # Max 5000 chars for V5
                    "style": style or "Same style as original, modern production, fresh interpretation",
                    "title": f"Cover_{int(time.time())}",
                    "model": "V5",
                    # CREATIVE SETTINGS to avoid "matches existing work of art" error:
                    "audioWeight": 0.40,       # LOW = loosely inspired, not copying
                    "styleWeight": 0.65,       # HIGH = more influence from style description
                    "weirdnessConstraint": 0.45,  # HIGH = allows creative deviation
                    "callBackUrl": "https://httpbin.org/post"
                }
            else:
                # Instrumental cover configuration with CREATIVE settings
                logger.info("   Using INSTRUMENTAL cover mode - creative params")
                request_body = {
                    "uploadUrl": audio_url,
                    "customMode": True,
                    "instrumental": True,
                    "style": style or "Same instrumental style, modern production, unique arrangement",
                    "title": f"Cover_{int(time.time())}",
                    "model": "V5",
                    # CREATIVE SETTINGS to avoid "matches existing work of art" error:
                    "audioWeight": 0.35,       # LOW = loosely inspired, not copying
                    "styleWeight": 0.70,       # HIGH = more influence from style description
                    "weirdnessConstraint": 0.50,  # HIGH = allows creative deviation
                    "callBackUrl": "https://httpbin.org/post"
                }
            
            logger.info(f"   Creative params: audioWeight={request_body['audioWeight']}, "
                       f"styleWeight={request_body['styleWeight']}, weirdness={request_body['weirdnessConstraint']}")
            
            # Submit to Kie.ai
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            
            response = requests.post(
                f"{self.base_url}/api/v1/generate/upload-cover",
                headers=headers,
                json=request_body,
                timeout=60
            )
            
            if response.status_code != 200:
                logger.error(f"❌ Suno API error: {response.status_code} - {response.text}")
                return None
            
            result = response.json()
            if result.get("code") != 200:
                logger.error(f"❌ Suno API returned error: {result.get('msg')}")
                return None
            
            task_id = result.get("data", {}).get("taskId")
            if not task_id:
                logger.error("❌ No task ID returned from Suno")
                return None
            
            logger.info(f"   Suno task started: {task_id}")
            
            # Wait for completion
            return self._wait_for_music(task_id)
            
        except Exception as e:
            logger.error(f"❌ Suno music generation error: {e}")
            return None
    
    def _wait_for_music(self, task_id: str, timeout: int = 600) -> Optional[str]:
        """Poll for music generation completion.
        
        Args:
            task_id: Suno task ID.
            timeout: Maximum wait time in seconds.
            
        Returns:
            URL of the generated music, or None if failed/timeout.
        """
        headers = {"Authorization": f"Bearer {self.api_key}"}
        start_time = time.time()
        
        logger.info(f"   Waiting for Suno music generation (timeout: {timeout}s)...")
        
        while time.time() - start_time < timeout:
            try:
                response = requests.get(
                    f"{self.base_url}/api/v1/generate/record-info",
                    headers=headers,
                    params={"taskId": task_id},
                    timeout=30
                )
                
                result = response.json()
                if result.get("code") != 200:
                    logger.warning(f"⚠️ Suno status check error: {result.get('msg')}")
                    time.sleep(15)
                    continue
                
                data = result.get("data", {})
                status = data.get("status", "")
                
                logger.debug(f"   Suno status: {status}")
                
                if status == "SUCCESS":
                    suno_data = data.get("response", {}).get("sunoData", [])
                    if suno_data:
                        audio_url = suno_data[0].get("audioUrl")
                        if audio_url:
                            logger.info(f"✅ Suno music generated: {audio_url}")
                            return audio_url
                            
                elif status in ["CREATE_TASK_FAILED", "GENERATE_AUDIO_FAILED", "CALLBACK_EXCEPTION", "SENSITIVE_WORD_ERROR"]:
                    error_msg = data.get("errorMessage", status)
                    logger.error(f"❌ Suno task failed: {error_msg}")
                    return None
                
                # Still processing (PENDING, TEXT_SUCCESS, FIRST_SUCCESS)
                time.sleep(15)
                
            except Exception as e:
                logger.warning(f"⚠️ Error checking Suno status: {e}")
                time.sleep(15)
        
        logger.error(f"❌ Suno timeout after {timeout}s")
        return None
