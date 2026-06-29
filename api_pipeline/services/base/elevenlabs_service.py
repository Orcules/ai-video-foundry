"""ElevenLabsService — extracted verbatim from Comp_Videos/video_scene_processor.py.

Lines 12551-13339 of the monolith.
"""

import io
import os
import re
import json
import time
import random
import base64
import logging
import tempfile
import zipfile
import requests
from typing import Dict, Any, List, Optional, Tuple

from openai import OpenAI
from api_pipeline.services.base.config import config

logger = logging.getLogger(__name__)


class ElevenLabsService:
    """Service for ElevenLabs API interactions."""
    
    def __init__(self, api_key: str, openai_client: OpenAI = None):
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
    
    def pick_random_voice(self, gender: str = "female", language: str = None) -> Optional[str]:
        """Pick a random voice_id filtered by gender and optionally language.
        
        Args:
            gender: "male" or "female" (or "m"/"f" shortcuts).
            language: Optional ISO 639-1 code (e.g. "en", "de"). If provided, prefer
                      voices that have this language in verified_languages; fall back to
                      all voices of the given gender if none match.
        
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
        
        chosen = random.choice(candidates)
        logger.info(f"🎲 Random voice selected: {chosen['name']} ({chosen['voice_id']}) [gender={chosen['gender']}, langs={chosen['languages'][:3]}]")
        return chosen["voice_id"]
    
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
                    'model_id': 'eleven_multilingual_sts_v2',
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
            
            # Use Eleven v3 for all languages (best quality, supports Hebrew)
            model_id = "eleven_v3"
            
            # Voice settings: ElevenLabs only accepts stability in [0.0, 0.5, 1.0] (Creative / Natural / Robust)
            if expressive:
                voice_settings = {
                    "stability": 0.0,  # Creative for more expressiveness (was 0.4 - invalid, caused 400)
                    "similarity_boost": 0.75,
                    "style": 0.55,
                    "use_speaker_boost": True
                }
            else:
                voice_settings = {
                    "stability": 0.5,
                    "similarity_boost": 0.75,
                    "style": 0.0,
                    "use_speaker_boost": True
                }
            
            payload = {
                "text": text,
                "model_id": model_id,
                "voice_settings": voice_settings
            }
            
            logger.info(f"🔊 Using ElevenLabs model: {model_id}")
            
            max_retries = 4
            retry_delay = getattr(config, "ELEVENLABS_TTS_RATE_LIMIT_WAIT", 30)
            for attempt in range(max_retries):
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
            
            # Use Eleven v3 for all languages (best quality, supports Hebrew)
            model_id = "eleven_v3"
            
            # Voice settings optimized for natural speech
            payload = {
                "text": text,
                "model_id": model_id,
                "voice_settings": {
                    "stability": 0.5,
                    "similarity_boost": 0.75,
                    "style": 0.0,
                    "use_speaker_boost": True
                }
            }
            
            logger.info(f"🔊 Using ElevenLabs model: {model_id} with timestamps")
            
            max_retries = 4
            retry_delay = getattr(config, "ELEVENLABS_TTS_RATE_LIMIT_WAIT", 30)
            for attempt in range(max_retries):
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
