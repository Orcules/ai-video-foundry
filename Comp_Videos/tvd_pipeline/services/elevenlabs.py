"""ElevenLabs text-to-speech service."""

import io
import os
import re
import time
import base64
import random
import logging
import zipfile
from typing import Any, Dict, List, Optional, Tuple

import requests

from tvd_pipeline.config import Config
from tvd_pipeline.data_loader import get_elevenlabs_config
from tvd_pipeline.utils import get_validated_voice_id

config = Config()
logger = logging.getLogger(__name__)

class ElevenLabsService:
    """Service for ElevenLabs API interactions."""
    
    def __init__(self, api_key: str, openai_client=None):
        """Initialize ElevenLabs service.
        
        Args:
            api_key: ElevenLabs API key.
            openai_client: Optional OpenAI client for speech detection.
        """
        self.api_key = api_key
        self.base_url = config.ELEVENLABS_BASE_URL
        self.openai_client = openai_client
        self._voice_catalog: Optional[List[Dict[str, Any]]] = None  # Cached voice list
        logger.info("✅ ElevenLabs client initialized")
    
    # -------------------------------------------------------------------------
    # Voice catalog: fetch once, then pick random voice by gender + language
    # -------------------------------------------------------------------------
    def _fetch_voice_catalog(self) -> List[Dict[str, Any]]:
        """Fetch all available voices from ElevenLabs v2 API and cache them.
        
        Returns:
            List of voice dicts with keys: voice_id, name, gender, languages, category.
        """
        if self._voice_catalog is not None:
            return self._voice_catalog
        
        voices: List[Dict[str, Any]] = []
        next_token = None
        page_size = 100
        
        try:
            logger.info("🔊 Fetching ElevenLabs voice catalog...")
            while True:
                url = f"{self.base_url.replace('/v1', '')}/v2/voices"
                params: Dict[str, Any] = {"page_size": page_size, "include_total_count": "false"}
                if next_token:
                    params["next_page_token"] = next_token
                
                resp = requests.get(
                    url,
                    headers={"xi-api-key": self.api_key},
                    params=params,
                    timeout=30
                )
                resp.raise_for_status()
                data = resp.json()
                
                for v in data.get("voices", []):
                    labels = v.get("labels") or {}
                    gender = (labels.get("gender") or "").lower()  # "male" / "female"
                    # Collect verified language codes (e.g. ["en", "es", "de"])
                    langs = [vl.get("language", "") for vl in (v.get("verified_languages") or [])]
                    voices.append({
                        "voice_id": v.get("voice_id", ""),
                        "name": v.get("name", ""),
                        "gender": gender,
                        "languages": langs,
                        "category": v.get("category", ""),
                        "accent": (labels.get("accent") or "").lower(),
                        "age": (labels.get("age") or "").lower(),
                        "use_case": (labels.get("use_case") or "").lower(),
                    })
                
                if not data.get("has_more"):
                    break
                next_token = data.get("next_page_token")
                if not next_token:
                    break
            
            self._voice_catalog = voices
            male_count = sum(1 for v in voices if v["gender"] == "male")
            female_count = sum(1 for v in voices if v["gender"] == "female")
            logger.info(f"✅ Loaded {len(voices)} voices from ElevenLabs (male: {male_count}, female: {female_count})")
            return voices
        except Exception as e:
            logger.warning(f"⚠️ Failed to fetch voice catalog: {e}. Falling back to defaults.")
            self._voice_catalog = []
            return []
    
    # (country name substrings, accent keywords) for filtering voices by country; enables randomization per country+language
    _COUNTRY_ACCENT_KEYS: List[tuple] = [
        (["united states", "usa", " us "], ["american", "us"]),
        (["united kingdom", " uk ", "britain"], ["british", "uk"]),
        (["germany", "german"], ["german"]),
        (["france", "french"], ["french"]),
        (["spain", "spanish"], ["spanish"]),
        (["italy", "italian"], ["italian"]),
        (["brazil", "brazilian"], ["brazilian", "portuguese"]),
        (["mexico", "latin america", "latam"], ["mexican", "latin", "latino"]),
        (["israel", "hebrew"], ["hebrew", "israeli"]),
        (["russia", "russian"], ["russian"]),
        (["japan", "japanese"], ["japanese"]),
        (["china", "chinese"], ["chinese"]),
        (["korea", "korean"], ["korean"]),
        (["india", "indian"], ["indian"]),
        (["australia", "australian"], ["australian"]),
        (["arab", "uae", "saudi", "egypt", "middle east"], ["arabic", "middle eastern"]),
        (["turkey", "turkish"], ["turkish"]),
        (["poland", "polish"], ["polish"]),
        (["netherlands", "dutch"], ["dutch"]),
        (["thailand", "thai"], ["thai"]),
        (["vietnam", "vietnamese"], ["vietnamese"]),
    ]

    def _accent_keys_for_country(self, country: str) -> Optional[List[str]]:
        """Return accent keywords for ElevenLabs label matching, or None if no mapping."""
        if not country or not country.strip():
            return None
        c = " " + country.strip().lower() + " "
        for patterns, accent_keys in self._COUNTRY_ACCENT_KEYS:
            for p in patterns:
                if (" " + p + " " in c) or (p in c):
                    return accent_keys
        return None

    def pick_random_voice(
        self,
        gender: str = "female",
        language: str = None,
        country: str = None
    ) -> Optional[str]:
        """Pick a random voice_id filtered by gender, optional language, and optional country.
        
        Randomization is among all voices that match the filters so each run can get a different
        voice per country+language (not a single fixed voice per locale).
        
        Args:
            gender: "male" or "female" (or "m"/"f" shortcuts).
            language: Optional ISO 639-1 code (e.g. "en", "de"). Prefer voices with this in verified_languages.
            country: Optional country/region name (e.g. "United States", "Israel"). When provided, prefer
                     voices whose accent label matches the country; fall back to language+gender if none match.
        
        Returns:
            A voice_id string, or None if catalog is empty.
        """
        catalog = self._fetch_voice_catalog()
        if not catalog:
            return None
        
        # Normalise gender
        g = gender.strip().lower()
        if g in ("m", "male"):
            g = "male"
        else:
            g = "female"
        
        # Filter by gender
        candidates = [v for v in catalog if v["gender"] == g]
        if not candidates:
            logger.warning(f"⚠️ No voices found for gender '{g}', using full catalog")
            candidates = catalog
        
        # Filter by language if provided
        if language:
            lang = language.strip().lower()
            lang_match = [v for v in candidates if lang in [l.lower() for l in v["languages"]]]
            if lang_match:
                candidates = lang_match
                logger.info(f"🔊 Filtered to {len(candidates)} voices for gender={g}, language={lang}")
            else:
                logger.info(f"🔊 No voices verified for language={lang}, using {len(candidates)} voices for gender={g}")
        else:
            logger.info(f"🔊 {len(candidates)} voices available for gender={g}")
        
        # Optionally narrow by country (accent) so we randomize among voices that match country+language
        if country and candidates:
            accent_keys = self._accent_keys_for_country(country)
            if accent_keys:
                accent_match = [
                    v for v in candidates
                    if v.get("accent") and any(k in v["accent"] for k in accent_keys)
                ]
                if accent_match:
                    candidates = accent_match
                    logger.info(f"🔊 Filtered to {len(candidates)} voices for country '{country.strip()}' (accent match); random choice")
                else:
                    logger.info(f"🔊 No accent match for country '{country.strip()}', using {len(candidates)} voices for gender+language; random choice")
        
        chosen = random.choice(candidates)
        logger.info(f"🎲 Random voice selected: {chosen['name']} ({chosen['voice_id']}) [gender={chosen['gender']}, langs={chosen['languages'][:3]}]")
        return chosen["voice_id"]
    
    # -------------------------------------------------------------------------
    # Voice WPS calibration: measure actual speech rate for a specific voice
    # -------------------------------------------------------------------------
    def calibrate_voice_wps(self, sample_text: str, voice_id: str, language: str = "en") -> float:
        """Send a short sample to ElevenLabs TTS and measure actual words-per-second.

        Uses the existing ``text_to_speech_with_timestamps`` method to get
        word-level timing, then computes ``measured_wps = word_count / duration``.
        Falls back to the default WPS from ``data_maps.json`` on any failure.

        Args:
            sample_text: Short text (~20 words) to synthesize.
            voice_id: The ElevenLabs voice to calibrate.
            language: ISO 639-1 code (used for fallback WPS lookup).

        Returns:
            Measured words-per-second (float), or default WPS on failure.
        """
        from tvd_pipeline.data_loader import get_speech_rate
        default_wps = get_speech_rate(language)

        try:
            result = self.text_to_speech_with_timestamps(sample_text, voice_id, language)
            if not result:
                logger.warning("calibrate_voice_wps: TTS returned None — using default %.2f WPS", default_wps)
                return default_wps

            _audio_bytes, word_segments = result

            if not word_segments or len(word_segments) < 3:
                logger.warning(
                    "calibrate_voice_wps: too few word segments (%d) — using default %.2f WPS",
                    len(word_segments) if word_segments else 0, default_wps,
                )
                return default_wps

            max_end = max(ws["end_time"] for ws in word_segments)
            if max_end <= 0:
                logger.warning("calibrate_voice_wps: max_end_time=0 — using default %.2f WPS", default_wps)
                return default_wps

            measured_wps = len(word_segments) / max_end

            # Sanity bounds
            if measured_wps < 1.0 or measured_wps > 5.0:
                logger.warning(
                    "calibrate_voice_wps: measured=%.2f WPS out of bounds [1.0, 5.0] — using default %.2f WPS",
                    measured_wps, default_wps,
                )
                return default_wps

            logger.info(
                "calibrate_voice_wps: measured=%.2f WPS (default=%.2f, %d words in %.2fs)",
                measured_wps, default_wps, len(word_segments), max_end,
            )
            return measured_wps

        except Exception as e:
            logger.warning("calibrate_voice_wps: error %s — using default %.2f WPS", e, default_wps)
            return default_wps

    def detect_speech_in_audio(self, audio_path: str) -> bool:
        """Detect if audio contains speech/voice-over using OpenAI Whisper.
        
        Args:
            audio_path: Path to the audio file.
            
        Returns:
            True if speech is detected, False if only music/silence.
        """
        try:
            if not self.openai_client:
                logger.warning("⚠️ No OpenAI client for speech detection, assuming speech present")
                return True
            
            logger.info("🔍 Detecting speech in audio...")
            
            with open(audio_path, 'rb') as audio_file:
                # Use Whisper to transcribe - if there's text, there's speech
                transcript = self.openai_client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                    response_format="text"
                )
                
                # Check if meaningful text was transcribed
                transcript_text = transcript.strip() if transcript else ""
                
                # If transcript is mostly empty or just noise artifacts, no speech
                if len(transcript_text) < 10:  # Less than 10 chars = likely no speech
                    logger.info("🎵 No speech detected - audio is music/ambient only")
                    return False
                else:
                    logger.info(f"🎤 Speech detected: '{transcript_text[:50]}...'")
                    return True
                    
        except Exception as e:
            logger.warning(f"⚠️ Speech detection failed: {e}, assuming speech present")
            return True  # Default to processing voice if detection fails
    
    def voice_changer(
        self, 
        audio_path: str, 
        voice_id: str = None
    ) -> Optional[bytes]:
        """Change voice in audio using speech-to-speech.
        
        Args:
            audio_path: Path to the audio file.
            voice_id: ElevenLabs voice ID (uses default if None).
            
        Returns:
            Audio data as bytes, or None if failed.
        """
        try:
            # Use voice_id as-is when non-empty (e.g. from sheet); otherwise default
            voice_id = (voice_id.strip() if voice_id and voice_id.strip() else config.DEFAULT_VOICE_ID)
            logger.info(f"🎤 Changing voice with ElevenLabs (voice_id: {voice_id})...")
            
            url = f"{self.base_url}/speech-to-speech/{voice_id}"
            
            headers = {
                "xi-api-key": self.api_key
            }
            
            with open(audio_path, 'rb') as audio_file:
                files = {
                    'audio': ('audio.mp3', audio_file, 'audio/mpeg')
                }
                
                data = {
                    'model_id': get_elevenlabs_config()["sts_model"],
                    'remove_background_noise': 'false'
                }
                
                response = requests.post(
                    url,
                    headers=headers,
                    files=files,
                    data=data,
                    params={'output_format': 'mp3_44100_128'},
                    timeout=300
                )
                response.raise_for_status()
                
                audio_data = response.content
                
                if len(audio_data) > 1000:
                    logger.info(f"✅ Voice changed: {len(audio_data)} bytes")
                    return audio_data
                else:
                    logger.error(f"❌ Voice change failed: audio too small")
                    return None
                    
        except Exception as e:
            logger.error(f"❌ Error changing voice: {e}")
            return None
    
    def separate_stems(
        self,
        audio_path: str,
        output_dir: str
    ) -> Optional[str]:
        """Separate audio into stems and extract clean vocals.
        
        Uses ElevenLabs Stem Separation API to separate vocals from music.
        
        Args:
            audio_path: Path to the audio file.
            output_dir: Directory to save the extracted vocals.
            
        Returns:
            Path to the clean vocals file, or None if failed.
        """
        try:
            logger.info("🎵 Separating stems with ElevenLabs...")
            
            url = f"{self.base_url}/music/stem-separation"
            
            headers = {
                "xi-api-key": self.api_key
            }
            
            with open(audio_path, 'rb') as audio_file:
                files = {
                    'file': ('audio.mp3', audio_file, 'audio/mpeg')
                }
                
                # Use two_stems_v1 to get vocals and instrumental only
                response = requests.post(
                    url,
                    headers=headers,
                    files=files,
                    params={
                        'output_format': 'mp3_44100_128',
                        'stem_variation_id': 'two_stems_v1'
                    },
                    timeout=600  # Stem separation can take a while
                )
                response.raise_for_status()
                
                # Response is a ZIP file containing the stems
                zip_data = io.BytesIO(response.content)
                
                vocals_path = None
                
                with zipfile.ZipFile(zip_data, 'r') as zip_ref:
                    # List files in the ZIP
                    file_list = zip_ref.namelist()
                    logger.info(f"📦 Stems in ZIP: {file_list}")
                    
                    # Find the vocals stem (usually named 'vocals' or similar)
                    vocals_file = None
                    for filename in file_list:
                        lower_name = filename.lower()
                        if 'vocal' in lower_name or 'voice' in lower_name:
                            vocals_file = filename
                            break
                    
                    if not vocals_file:
                        # If no explicit vocals file, take the first one that's not instrumental
                        for filename in file_list:
                            lower_name = filename.lower()
                            if 'instrumental' not in lower_name and 'music' not in lower_name and 'accomp' not in lower_name:
                                vocals_file = filename
                                break
                    
                    if vocals_file:
                        # Extract vocals to output directory
                        vocals_path = os.path.join(output_dir, "clean_vocals.mp3")
                        with zip_ref.open(vocals_file) as src:
                            with open(vocals_path, 'wb') as dst:
                                dst.write(src.read())
                        logger.info(f"✅ Vocals extracted: {vocals_path}")
                    else:
                        logger.error("❌ Could not find vocals stem in ZIP")
                        return None
                
                return vocals_path
                
        except requests.exceptions.HTTPError as e:
            logger.error(f"❌ Stem separation HTTP error: {e.response.status_code} - {e.response.text}")
            return None
        except Exception as e:
            logger.error(f"❌ Error separating stems: {e}")
            return None
    
    @staticmethod
    def _insert_sentence_pauses(text: str, pause_seconds: float) -> str:
        """Insert a short pause (ElevenLabs break) between sentences for smoother VO flow.
        
        Inserts <break time="Xs" /> after each sentence-ending punctuation (. ! ?).
        The script saved to the sheet stays unchanged; only the copy sent to TTS is modified.
        """
        if not text or pause_seconds <= 0:
            return text
        # Insert break after . ! ? when followed by space (between sentences)
        break_tag = f' <break time="{pause_seconds}s" /> '
        return re.sub(r'([.!?])\s+', r'\1' + break_tag, text)
    
    def text_to_speech(
        self,
        text: str,
        voice_id: str = None,
        language: str = "en",
        expressive: bool = False,
        sentence_pause_seconds: float = 0.0
    ) -> Optional[bytes]:
        """Generate speech from text using ElevenLabs TTS API.
        
        Args:
            text: Text to convert to speech.
            voice_id: ElevenLabs voice ID (uses default if None).
            language: ISO 639-1 language code for voice selection.
            expressive: If True, use higher expressiveness (passion, warmth) - lower stability, higher style.
            sentence_pause_seconds: If > 0, insert a pause of this length between sentences (smoother VO flow).
            
        Returns:
            Audio data as bytes, or None if failed.
        """
        try:
            if sentence_pause_seconds > 0:
                text = self._insert_sentence_pauses(text, sentence_pause_seconds)
            
            # Validate voice_id (handles #N/A, empty, etc.)
            voice_id = get_validated_voice_id(voice_id, config.DEFAULT_VOICE_ID)
            logger.info(f"🔊 Generating speech with ElevenLabs TTS (voice_id: {voice_id}, language: {language}, expressive: {expressive})...")
            
            url = f"{self.base_url}/text-to-speech/{voice_id}"
            
            headers = {
                "xi-api-key": self.api_key,
                "Content-Type": "application/json"
            }
            
            el_cfg = get_elevenlabs_config()
            model_id = el_cfg["tts_model"]

            # Voice settings from 11_labs.json (normal vs expressive preset)
            preset = "expressive" if expressive else "normal"
            voice_settings = dict(el_cfg["voice_settings"][preset])
            
            payload = {
                "text": text,
                "model_id": model_id,
                "voice_settings": voice_settings
            }
            
            logger.info(f"🔊 Using ElevenLabs model: {model_id}")
            
            max_retries = 4
            retry_delay = getattr(config, "ELEVENLABS_TTS_RATE_LIMIT_WAIT", 30)
            from tvd_pipeline.external_api_log import log_external_api_call, log_external_api_result

            for attempt in range(max_retries):
                if attempt == 0:
                    log_external_api_call(
                        "elevenlabs",
                        "text_to_speech",
                        method="POST",
                        model=model_id,
                        detail=f"chars={len(text or '')}",
                    )
                t_req = time.perf_counter()
                response = requests.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=120
                )
                if response.status_code == 429 and attempt < max_retries - 1:
                    logger.warning(f"Rate limit (429). Waiting {retry_delay}s before retry {attempt + 1}/{max_retries - 1}...")
                    time.sleep(retry_delay)
                    continue
                response.raise_for_status()
                audio_bytes = response.content
                log_external_api_result(
                    "elevenlabs",
                    "text_to_speech",
                    duration_ms=max(0, int((time.perf_counter() - t_req) * 1000)),
                    method="POST",
                    model=model_id,
                    http_status=response.status_code,
                    ok=True,
                    detail=f"bytes={len(audio_bytes)}",
                )
                logger.info(f"✅ TTS generated: {len(audio_bytes)} bytes")
                return audio_bytes
            
            return None
            
        except requests.exceptions.HTTPError as e:
            logger.error(f"❌ TTS HTTP error: {e.response.status_code} - {e.response.text}")
            return None
        except Exception as e:
            logger.error(f"❌ Error generating TTS: {e}")
            return None
    
    def text_to_speech_with_timestamps(
        self,
        text: str,
        voice_id: str = None,
        language: str = "en"
    ) -> Optional[Tuple[bytes, List[Dict]]]:
        """Generate speech from text with word-level timestamps using ElevenLabs.
        
        Uses the /with-timestamps endpoint to get precise character-level timing,
        then converts to word-level segments for subtitle synchronization.
        
        Args:
            text: Text to convert to speech.
            voice_id: ElevenLabs voice ID (uses default if None).
            language: ISO 639-1 language code for voice selection.
            
        Returns:
            Tuple of (audio_bytes, word_segments) or None if failed.
            word_segments is a list of dicts with:
                - text: the word
                - type: "word"
                - start_time: start time in seconds
                - end_time: end time in seconds
        """
        try:
            # Validate voice_id (handles #N/A, empty, etc.)
            voice_id = get_validated_voice_id(voice_id, config.DEFAULT_VOICE_ID)
            logger.info(f"🔊 Generating speech with timestamps (voice_id: {voice_id}, language: {language})...")
            
            url = f"{self.base_url}/text-to-speech/{voice_id}/with-timestamps"
            
            headers = {
                "xi-api-key": self.api_key,
                "Content-Type": "application/json"
            }
            
            el_cfg = get_elevenlabs_config()
            model_id = el_cfg["tts_model"]

            payload = {
                "text": text,
                "model_id": model_id,
                "voice_settings": dict(el_cfg["voice_settings"]["normal"])
            }
            
            logger.info(f"🔊 Using ElevenLabs model: {model_id} with timestamps")
            
            max_retries = 4
            retry_delay = getattr(config, "ELEVENLABS_TTS_RATE_LIMIT_WAIT", 30)
            from tvd_pipeline.external_api_log import log_external_api_call, log_external_api_result

            for attempt in range(max_retries):
                if attempt == 0:
                    log_external_api_call(
                        "elevenlabs",
                        "text_to_speech_with_timestamps",
                        method="POST",
                        model=model_id,
                        detail=f"chars={len(text or '')}",
                    )
                t_req = time.perf_counter()
                response = requests.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=120
                )
                if response.status_code == 429 and attempt < max_retries - 1:
                    logger.warning(f"Rate limit (429). Waiting {retry_delay}s before retry {attempt + 1}/{max_retries - 1}...")
                    time.sleep(retry_delay)
                    continue
                response.raise_for_status()
                log_external_api_result(
                    "elevenlabs",
                    "text_to_speech_with_timestamps",
                    duration_ms=max(0, int((time.perf_counter() - t_req) * 1000)),
                    method="POST",
                    model=model_id,
                    http_status=response.status_code,
                    ok=True,
                    detail="with_timestamps",
                )
                break

            # Parse JSON response with audio and alignment
            data = response.json()
            
            # Decode audio from base64
            audio_base64 = data.get("audio_base64", "")
            audio_bytes = base64.b64decode(audio_base64)
            logger.info(f"✅ TTS generated: {len(audio_bytes)} bytes")
            
            # Extract alignment data (ElevenLabs: character_start_times_seconds / character_end_times_seconds)
            alignment = data.get("alignment") or data.get("normalized_alignment") or {}
            if isinstance(alignment, list):
                alignment = {}
            characters = alignment.get("characters", [])
            char_starts = alignment.get("character_start_times_seconds") or alignment.get("character_start_times", [])
            char_ends = alignment.get("character_end_times_seconds") or alignment.get("character_end_times", [])
            # If times are in milliseconds, convert to seconds
            if char_starts and char_ends and len(char_starts) == len(char_ends):
                if any(t > 1000 for t in char_starts[:5]) or any(t > 1000 for t in char_ends[:5]):
                    char_starts = [t / 1000.0 for t in char_starts]
                    char_ends = [t / 1000.0 for t in char_ends]
            
            if not characters or not char_starts or not char_ends:
                logger.warning("⚠️ No alignment data in ElevenLabs response (alignment=%s), returning audio only; ZapCap will use auto-transcription",
                              "null" if not data.get("alignment") and not data.get("normalized_alignment") else "empty/partial")
                return audio_bytes, []
            
            # Convert character-level alignment to word-level segments
            word_segments = self._convert_alignment_to_word_segments(
                characters, char_starts, char_ends
            )
            
            logger.info(f"✅ Extracted {len(word_segments)} word segments from timestamps")
            return audio_bytes, word_segments
            
        except requests.exceptions.HTTPError as e:
            logger.error(f"❌ TTS with timestamps HTTP error: {e.response.status_code} - {e.response.text}")
            return None
        except Exception as e:
            logger.error(f"❌ Error generating TTS with timestamps: {e}")
            return None
    
    def _convert_alignment_to_word_segments(
        self,
        characters: List[str],
        char_starts: List[float],
        char_ends: List[float]
    ) -> List[Dict]:
        """Convert character-level alignment to word-level segments.
        
        Groups characters into words by splitting on spaces and punctuation,
        and calculates word start/end times from the character timing.
        
        Args:
            characters: List of characters from ElevenLabs alignment.
            char_starts: List of start times for each character.
            char_ends: List of end times for each character.
            
        Returns:
            List of word segments with text, type, start_time, end_time.
        """
        if len(characters) != len(char_starts) or len(characters) != len(char_ends):
            logger.warning("⚠️ Mismatched alignment arrays")
            return []
        
        word_segments = []
        current_word = ""
        word_start = None
        word_end = None
        
        for i, char in enumerate(characters):
            # Skip if this character is a space or common punctuation that separates words
            if char in " \t\n\r":
                # If we have a word accumulated, save it
                if current_word and word_start is not None:
                    word_segments.append({
                        "text": current_word,
                        "type": "word",
                        "start_time": round(word_start, 3),
                        "end_time": round(word_end, 3)
                    })
                # Reset for next word
                current_word = ""
                word_start = None
                word_end = None
            else:
                # Add character to current word
                if word_start is None:
                    word_start = char_starts[i]
                current_word += char
                word_end = char_ends[i]
        
        # Don't forget the last word
        if current_word and word_start is not None:
            word_segments.append({
                "text": current_word,
                "type": "word",
                "start_time": round(word_start, 3),
                "end_time": round(word_end, 3)
            })
        
        logger.info(f"📝 Converted {len(characters)} characters to {len(word_segments)} words")
        return word_segments
    
    def get_transcript_from_audio(self, audio_path: str) -> Optional[str]:
        """Get transcript from audio using OpenAI Whisper.
        
        Args:
            audio_path: Path to the audio file.
            
        Returns:
            Transcript text, or None if failed.
        """
        try:
            if not self.openai_client:
                logger.warning("⚠️ No OpenAI client for transcription")
                return None
            
            logger.info("📝 Transcribing audio with Whisper...")
            
            with open(audio_path, 'rb') as audio_file:
                transcript = self.openai_client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                    response_format="text"
                )
                
                transcript_text = transcript.strip() if transcript else ""
                
                if transcript_text:
                    logger.info(f"✅ Transcribed: {len(transcript_text)} characters")
                    return transcript_text
                else:
                    return None
                    
        except Exception as e:
            logger.error(f"❌ Transcription failed: {e}")
            return None
    
    def detect_vo_presence(self, audio_path: str) -> Tuple[bool, Optional[str]]:
        """Detect if audio has meaningful voice-over (speech).
        
        Args:
            audio_path: Path to the audio file.
            
        Returns:
            Tuple of (has_vo: bool, transcript: Optional[str])
            - has_vo: True if meaningful speech detected, False otherwise
            - transcript: The transcript text if speech found, None otherwise
        """
        try:
            transcript = self.get_transcript_from_audio(audio_path)
            
            if not transcript:
                logger.info("🔇 No speech detected in audio (empty transcription)")
                return False, None
            
            # Check if transcript has meaningful content
            # Only filter out if truly no speech (0-1 words might be noise)
            word_count = len(transcript.split())
            
            if word_count < 2:
                logger.info(f"🔇 No meaningful speech detected ({word_count} words) - treating as no VO")
                return False, None
            
            logger.info(f"🎤 Voice-over detected: {word_count} words")
            return True, transcript
            
        except Exception as e:
            logger.error(f"❌ VO detection failed: {e}")
            return False, None
    
    def detect_vo_gender(self, audio_path: str) -> Tuple[Optional[str], Optional[str]]:
        """Detect the gender of the narrator from audio.
        
        Uses Whisper for transcription and OpenAI to analyze voice characteristics.
        
        Args:
            audio_path: Path to the audio file.
            
        Returns:
            Tuple of (gender: Optional[str], transcript: Optional[str])
            - gender: 'm' for male, 'f' for female, None if no VO detected
            - transcript: The transcript text if speech found
        """
        try:
            # First check if there's VO
            has_vo, transcript = self.detect_vo_presence(audio_path)
            
            if not has_vo:
                return None, None
            
            # Use OpenAI to analyze the audio for gender
            # We need to send the audio file directly for voice analysis
            if not self.openai_client:
                logger.warning("⚠️ No OpenAI client for gender detection")
                return None, transcript
            
            logger.info("🔍 Detecting narrator gender with OpenAI...")
            
            # Read audio file and encode to base64
            with open(audio_path, 'rb') as f:
                audio_data = f.read()
            
            audio_base64 = base64.b64encode(audio_data).decode('utf-8')
            
            # Determine audio format from file extension
            audio_format = "mp3"
            if audio_path.lower().endswith('.wav'):
                audio_format = "wav"
            elif audio_path.lower().endswith('.m4a'):
                audio_format = "m4a"
            
            # Use GPT-4o-audio to analyze the voice
            try:
                response = self.openai_client.chat.completions.create(
                    model="gpt-4o-audio-preview",
                    messages=[
                        {
                            "role": "system",
                            "content": "You are an expert at identifying voice characteristics. Listen to the audio and determine the gender of the main speaker/narrator. Respond with ONLY a single letter: 'm' for male voice or 'f' for female voice. If you cannot determine the gender or there's no clear narrator, respond with 'u' for unknown."
                        },
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_audio",
                                    "input_audio": {
                                        "data": audio_base64,
                                        "format": audio_format
                                    }
                                },
                                {
                                    "type": "text",
                                    "text": "What is the gender of the narrator/speaker in this audio? Reply with only 'm' for male or 'f' for female."
                                }
                            ]
                        }
                    ],
                    max_tokens=10
                )
                
                gender_response = response.choices[0].message.content.strip().lower()
                
                # Parse the response
                if 'm' in gender_response and 'f' not in gender_response:
                    gender = 'm'
                elif 'f' in gender_response and 'm' not in gender_response:
                    gender = 'f'
                elif gender_response in ['m', 'f']:
                    gender = gender_response
                else:
                    # Try to extract from longer response
                    if 'male' in gender_response and 'female' not in gender_response:
                        gender = 'm'
                    elif 'female' in gender_response:
                        gender = 'f'
                    else:
                        logger.warning(f"⚠️ Could not parse gender from response: {gender_response}")
                        gender = None
                
                if gender:
                    gender_name = "male" if gender == 'm' else "female"
                    logger.info(f"✅ Detected narrator gender: {gender_name} ({gender})")
                
                return gender, transcript
                
            except Exception as e:
                # Fallback: try using text analysis of transcript
                logger.warning(f"⚠️ Audio gender detection failed: {e}, trying text-based fallback...")
                
                try:
                    response = self.openai_client.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[
                            {
                                "role": "system",
                                "content": "Based on the transcript and context, try to determine if the speaker is likely male or female. This is a voice-over transcript. Respond with ONLY 'm' for male or 'f' for female. If unsure, respond with 'm' as default."
                            },
                            {
                                "role": "user",
                                "content": f"Transcript: {transcript[:500]}"
                            }
                        ],
                        max_tokens=5
                    )
                    
                    gender = response.choices[0].message.content.strip().lower()
                    if gender not in ['m', 'f']:
                        gender = 'm'  # Default to male if unclear
                    
                    gender_name = "male" if gender == 'm' else "female"
                    logger.info(f"✅ Detected narrator gender (from text): {gender_name} ({gender})")
                    return gender, transcript
                    
                except Exception as e2:
                    logger.error(f"❌ Fallback gender detection also failed: {e2}")
                    return None, transcript
                    
        except Exception as e:
            logger.error(f"❌ Gender detection failed: {e}")
            return None, None

    # -------------------------------------------------------------------------
    # Voice Design: generate custom voices from a text description
    # -------------------------------------------------------------------------
    # API schema: max 1000; stay well under (unicode / gateway quirks).
    _VOICE_DESCRIPTION_MAX_LEN = 960

    @staticmethod
    def _truncate_voice_description_field(text: str, max_len: int = 960) -> str:
        s = (text or "").strip()
        if len(s) <= max_len:
            return s
        return s[: max_len - 1].rstrip() + "…"

    def design_voice(
        self,
        voice_description: str,
        auto_generate_text: bool = True,
        loudness: float = 0.5,
        guidance_scale: float = 5.0,
        seed: Optional[int] = None,
        model_id: str = "eleven_multilingual_ttv_v2",
    ) -> Tuple[Optional[Dict], Optional[str]]:
        """Call ElevenLabs POST /v1/text-to-voice/design.

        Returns:
            ``(response_dict, None)`` on success. The dict has ``previews`` (list) and ``text``.
            ``(None, error_message)`` on failure (for API / HTTPException detail).
        """
        if not (self.api_key or "").strip():
            return None, "ElevenLabs API key is missing (set ELEVEN_LABS_API_KEY / config)."
        try:
            voice_description = self._truncate_voice_description_field(
                voice_description, self._VOICE_DESCRIPTION_MAX_LEN
            )
            url = f"{self.base_url}/text-to-voice/design?output_format=mp3_22050_32"
            headers = {
                "xi-api-key": self.api_key,
                "Content-Type": "application/json",
            }
            payload: Dict[str, Any] = {
                "voice_description": voice_description,
                "model_id": model_id,
                "auto_generate_text": auto_generate_text,
                "loudness": loudness,
                "guidance_scale": guidance_scale,
            }
            if seed is not None:
                payload["seed"] = seed

            from tvd_pipeline.external_api_log import log_external_api_call, log_external_api_result
            log_external_api_call(
                "elevenlabs",
                "design_voice",
                method="POST",
                model=model_id,
                detail=f"desc_len={len(voice_description)}",
            )
            t_req = time.perf_counter()
            resp = requests.post(url, headers=headers, json=payload, timeout=60)
            log_external_api_result(
                "elevenlabs",
                "design_voice",
                duration_ms=max(0, int((time.perf_counter() - t_req) * 1000)),
                method="POST",
                model=model_id,
                http_status=resp.status_code,
                ok=resp.ok,
            )
            if not resp.ok:
                snippet = (resp.text or "")[:400].replace("\n", " ")
                logger.warning("design_voice HTTP %s: %s", resp.status_code, resp.text[:300])
                return None, f"ElevenLabs HTTP {resp.status_code}: {snippet}"
            data = resp.json()
            raw_previews = data.get("previews") or []
            previews = []
            for p in raw_previews:
                if not isinstance(p, dict):
                    continue
                norm = dict(p)
                if not norm.get("generated_voice_id") and norm.get("generatedVoiceId"):
                    norm["generated_voice_id"] = norm.get("generatedVoiceId")
                if not norm.get("audio_base_64") and norm.get("audioBase64"):
                    norm["audio_base_64"] = norm.get("audioBase64")
                if norm.get("duration_secs") is None and norm.get("durationSecs") is not None:
                    norm["duration_secs"] = norm.get("durationSecs")
                if not norm.get("media_type") and norm.get("mediaType"):
                    norm["media_type"] = norm.get("mediaType")
                previews.append(norm)
            if not previews:
                # 200 with empty list happens on rate limits / transient API quirks; treat as failure so callers retry or keep UI cache.
                logger.warning(
                    "design_voice: HTTP 200 but empty previews (body snippet): %s",
                    str(data)[:500],
                )
                return None, "ElevenLabs returned 200 with no previews (quota, rate limit, or invalid voice_description)."
            data["previews"] = previews
            logger.info("design_voice: %d previews returned", len(previews))
            return data, None
        except Exception as exc:
            logger.warning("design_voice failed: %s", exc)
            return None, str(exc)[:400]

    def stream_voice_preview(self, generated_voice_id: str) -> Optional[bytes]:
        """Fetch preview audio bytes for a generated_voice_id via GET /v1/text-to-voice/{id}/stream.

        Returns raw audio bytes on success, or None on failure.
        """
        try:
            url = f"{self.base_url}/text-to-voice/{generated_voice_id}/stream"
            headers = {"xi-api-key": self.api_key}

            from tvd_pipeline.external_api_log import log_external_api_call, log_external_api_result
            log_external_api_call(
                "elevenlabs",
                "stream_voice_preview",
                method="GET",
                detail=f"voice={generated_voice_id}",
            )
            t_req = time.perf_counter()
            resp = requests.get(url, headers=headers, timeout=60, stream=False)
            log_external_api_result(
                "elevenlabs",
                "stream_voice_preview",
                duration_ms=max(0, int((time.perf_counter() - t_req) * 1000)),
                method="GET",
                http_status=resp.status_code,
                ok=resp.ok,
            )
            if not resp.ok:
                logger.warning("stream_voice_preview HTTP %s for %s", resp.status_code, generated_voice_id)
                return None
            return resp.content
        except Exception as exc:
            logger.warning("stream_voice_preview failed: %s", exc)
            return None

    def save_designed_voice(
        self,
        generated_voice_id: str,
        voice_name: str = "Studio Custom Voice",
        voice_description: str = "",
    ) -> Tuple[Optional[str], Optional[str]]:
        """Save a designed voice (generated_voice_id) to the ElevenLabs library.

        Official API: POST /v1/text-to-voice with voice_name, voice_description, generated_voice_id.
        (Legacy helper path POST .../create-voice-from-preview is retried only if the primary call returns 404.)

        Returns ``(permanent_voice_id, None)`` on success, or ``(None, error_detail)`` on failure.
        """
        if not (self.api_key or "").strip():
            return None, "ElevenLabs API key is missing (set ELEVEN_LABS_API_KEY / config)."
        if not (generated_voice_id or "").strip():
            return None, "generated_voice_id is empty."
        desc_raw = (voice_description or "").strip() or (voice_name or "").strip() or "Custom voice from TVD Studio"
        payload: Dict[str, Any] = {
            "generated_voice_id": generated_voice_id.strip(),
            "voice_name": (voice_name or "Studio Custom Voice").strip() or "Studio Custom Voice",
            "voice_description": self._truncate_voice_description_field(
                desc_raw, self._VOICE_DESCRIPTION_MAX_LEN
            ),
        }
        headers = {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json",
        }

        def _parse_voice_id(data: Any) -> Optional[str]:
            if not isinstance(data, dict):
                return None
            vid = data.get("voice_id")
            if not vid and isinstance(data.get("voice"), dict):
                vid = (data.get("voice") or {}).get("voice_id")
            if not vid and isinstance(data.get("voice"), dict):
                vid = (data.get("voice") or {}).get("voiceId")
            if vid:
                return str(vid).strip() or None
            return None

        try:
            from tvd_pipeline.external_api_log import log_external_api_call, log_external_api_result

            urls_try = [
                f"{self.base_url}/text-to-voice",
                f"{self.base_url}/text-to-voice/create-voice-from-preview",
            ]
            last_http_detail: Optional[str] = None
            for idx, url in enumerate(urls_try):
                log_external_api_call(
                    "elevenlabs",
                    "save_designed_voice",
                    method="POST",
                    detail=f"url={url.split('/')[-1]} gen={generated_voice_id[:12]}...",
                )
                t_req = time.perf_counter()
                resp = requests.post(url, headers=headers, json=payload, timeout=60)
                log_external_api_result(
                    "elevenlabs",
                    "save_designed_voice",
                    duration_ms=max(0, int((time.perf_counter() - t_req) * 1000)),
                    method="POST",
                    http_status=resp.status_code,
                    ok=resp.ok,
                )
                if not resp.ok:
                    snippet = (resp.text or "").replace("\n", " ")[:400]
                    logger.warning("save_designed_voice HTTP %s: %s", resp.status_code, resp.text[:400])
                    # ElevenLabs returns 400 "already been created" when the voice was saved in a
                    # prior attempt.  Treat this as success — the generated_voice_id is already the
                    # permanent voice_id in the library.
                    if resp.status_code == 400 and "already been created" in (resp.text or ""):
                        import re as _re
                        m = _re.search(r"Voice with id ([A-Za-z0-9]+) has already been created", resp.text or "")
                        already_id = m.group(1) if m else generated_voice_id.strip()
                        logger.info("save_designed_voice: voice already exists, reusing id=%s", already_id)
                        return already_id, None
                    last_http_detail = f"ElevenLabs HTTP {resp.status_code}: {snippet}"
                    if idx == 0 and resp.status_code == 404:
                        logger.warning("save_designed_voice: primary URL 404, retrying legacy path")
                        continue
                    return None, last_http_detail
                try:
                    data = resp.json()
                except Exception:
                    return None, "ElevenLabs returned non-JSON body for save-voice response."
                voice_id = _parse_voice_id(data)
                if voice_id:
                    logger.info("save_designed_voice: saved as permanent voice_id=%s", voice_id)
                    return voice_id, None
                logger.warning(
                    "save_designed_voice: 200 but no voice_id in body keys=%s",
                    list(data.keys())[:20] if isinstance(data, dict) else type(data),
                )
                if idx < len(urls_try) - 1:
                    logger.warning("save_designed_voice: retrying alternate URL after missing voice_id")
                    continue
                return None, "ElevenLabs returned success but no voice_id (unexpected response shape)."
            return None, last_http_detail or "ElevenLabs save voice failed."
        except Exception as exc:
            logger.warning("save_designed_voice failed: %s", exc)
            return None, str(exc)[:400]


