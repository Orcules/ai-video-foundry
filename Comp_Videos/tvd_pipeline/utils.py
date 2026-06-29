"""Utility functions for the TVD X1 video pipeline."""

import re
import json
import logging

logger = logging.getLogger(__name__)


def parse_character_urls(cell_value: str) -> list:
    """Parse Character column value into a list of image URLs.

    Supports multiple URLs in one cell separated by comma or newline.
    Each token is stripped; valid URLs (http/https/gs://) are returned.

    Args:
        cell_value: Raw cell value from the Character column.

    Returns:
        List of non-empty valid URLs (may be empty).
    """
    if not cell_value or not isinstance(cell_value, str):
        return []
    urls = []
    for part in re.split(r"[\n,]", cell_value):
        s = part.strip()
        if not s:
            continue
        if s.startswith("http://") or s.startswith("https://") or s.startswith("gs://"):
            urls.append(s)
    return urls


# Simulation placeholders
_SIM_IMAGE = "https://storage.googleapis.com/automatiq/simulation/placeholder.jpg"
_SIM_VIDEO = "https://storage.googleapis.com/automatiq/simulation/placeholder.mp4"
_SIM_AUDIO = "https://storage.googleapis.com/automatiq/simulation/placeholder.mp3"

def snap_duration(video_model: str, requested: float) -> int:
    """Snap to the smallest supported duration >= requested. If none, use max.

    Reads supported durations from models.json via data_loader.
    Tries an exact model match first, then falls back to prefix matching
    (e.g. API model ID "veo-3.1-fast-generate-001" prefix-matches version "veo-3.1-fast").
    """
    from tvd_pipeline.data_loader import get_supported_durations
    durations_map = get_supported_durations()
    supported = durations_map.get(video_model)
    if not supported:
        for key in durations_map:
            if video_model and video_model.startswith(key):
                supported = durations_map[key]
                break
    if not supported:
        return max(4, min(10, int(requested)))  # safe fallback
    for s in sorted(supported):
        if s >= requested:
            return s
    return max(supported)


def is_valid_voice_id(voice_id: str) -> bool:
    """Check if a voice_id is valid (not empty, not #N/A, etc.).

    Args:
        voice_id: The voice ID to validate.

    Returns:
        True if valid, False otherwise.
    """
    if not voice_id:
        return False

    # Check for common invalid values from spreadsheets
    invalid_values = [
        '#n/a', '#na', 'n/a', 'na', '#ref!', '#error!', '#value!',
        'null', 'none', 'undefined', '-', ''
    ]

    normalized = voice_id.strip().lower()
    return normalized not in invalid_values and len(normalized) > 3


def script_only_for_tts(vo_cell_value: str) -> str:
    """Extract only the spoken script from a VO cell value (strip metadata/timing/tags).

    Ensures TTS never reads aloud [Scene N] tags, JSON, break/pause tags, or timestamp lines.
    """
    if not vo_cell_value or not isinstance(vo_cell_value, str):
        return ""
    text = vo_cell_value.strip()
    if not text:
        return ""
    # If it looks like JSON with a script field, extract it
    if text.startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                for key in ("script", "text", "full_script", "vo_text"):
                    if key in data and data[key]:
                        return str(data[key]).strip()
        except (json.JSONDecodeError, TypeError):
            pass
    # Strip SSML/break tags so TTS doesn't read "break time 0.5s" etc. aloud
    text = re.sub(r"<break\s+[^>]*>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"</?break\s*>?", " ", text, flags=re.IGNORECASE)
    # Strip any "pause 0.5", "meta pause" or similar
    text = re.sub(r"\bmeta\s*pause\s*[\d.]*\s*s?\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bpause\s*[\d.]*\s*s\b", " ", text, flags=re.IGNORECASE)
    # Strip [Scene N] and [scene n] tags — but KEEP ElevenLabs v3 Audio Tags like [excited], [whispers], [laughs]
    text = re.sub(r"\[Scene\s*\d+\]\s*", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _word_count_for_duration(text: str) -> int:
    """Count words for duration/wps estimation; strips ElevenLabs audio tags and SSML tags.
    Use this so tags are not counted as spoken words.
    """
    if not text or not isinstance(text, str):
        return 0
    # Remove [...] audio tags (ElevenLabs v3) so they are not counted as words
    stripped = re.sub(r"\[[^\]]*\]", " ", text)
    # Remove <break.../> and other SSML-like tags (e.g. <break time="0.5s" />)
    stripped = re.sub(r"<[^>]+/?>", " ", stripped)
    # Remove ||| beat separators so they are not counted as words
    stripped = stripped.replace("|||", " ")
    stripped = re.sub(r"\s+", " ", stripped).strip()
    return len(stripped.split()) if stripped else 0


def _normalize_animation_model_value(cell_value: str) -> str:
    """Normalize Animation model cell so 'Google 3.1' and variants match reliably."""
    if not cell_value or not isinstance(cell_value, str):
        return ""
    # Replace non-breaking space and collapse spaces, then lower
    s = cell_value.replace("\xa0", " ").replace("\u00a0", " ")
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def get_validated_voice_id(voice_id: str, default_voice_id: str = None) -> str:
    """Get a validated voice_id, falling back to default if invalid.

    Args:
        voice_id: The voice ID to validate.
        default_voice_id: Default to use if voice_id is invalid.

    Returns:
        Valid voice_id or default.
    """
    if is_valid_voice_id(voice_id):
        return voice_id
    # Import here to avoid circular dependency; config is a module-level singleton
    from tvd_pipeline.config import Config
    return default_voice_id or Config().DEFAULT_VOICE_ID


def detect_language(text: str) -> str:
    """Detect language from text.

    Uses langdetect library if available, otherwise falls back to simple heuristics.

    Args:
        text: Text to analyze for language detection.

    Returns:
        ISO 639-1 language code (e.g., 'en', 'de', 'he', 'es', 'fr').
        Defaults to 'en' if detection fails.
    """
    if not text or len(text.strip()) < 10:
        logger.warning("Text too short for language detection, defaulting to English")
        return "en"

    try:
        # Try using langdetect library
        from langdetect import detect as langdetect_detect
        detected = langdetect_detect(text)
        logger.info(f"Detected language: {detected}")
        return detected
    except ImportError:
        # Fallback: simple heuristic based on character sets
        logger.warning("langdetect not installed, using heuristic detection")

        # Hebrew detection (Hebrew characters)
        if re.search(r'[\u0590-\u05FF]', text):
            return "he"
        # Arabic detection
        if re.search(r'[\u0600-\u06FF]', text):
            return "ar"
        # Chinese detection
        if re.search(r'[\u4E00-\u9FFF]', text):
            return "zh"
        # Japanese detection (Hiragana/Katakana)
        if re.search(r'[\u3040-\u30FF]', text):
            return "ja"
        # Korean detection
        if re.search(r'[\uAC00-\uD7AF]', text):
            return "ko"
        # Russian/Cyrillic detection
        if re.search(r'[\u0400-\u04FF]', text):
            return "ru"
        # German detection (common German words)
        german_words = ['und', 'die', 'der', 'das', 'ist', 'für', 'mit', 'von', 'nicht', 'eine']
        text_lower = text.lower()
        german_count = sum(1 for word in german_words if f' {word} ' in f' {text_lower} ')
        if german_count >= 3:
            return "de"
        # French detection
        french_words = ['le', 'la', 'les', 'de', 'et', 'est', 'une', 'que', 'pour', 'dans']
        french_count = sum(1 for word in french_words if f' {word} ' in f' {text_lower} ')
        if french_count >= 3:
            return "fr"
        # Spanish detection
        spanish_words = ['el', 'la', 'los', 'las', 'de', 'que', 'es', 'en', 'un', 'una']
        spanish_count = sum(1 for word in spanish_words if f' {word} ' in f' {text_lower} ')
        if spanish_count >= 3:
            return "es"

        # Default to English
        return "en"
    except Exception as e:
        logger.error(f"Language detection failed: {e}, defaulting to English")
        return "en"


# ---------------------------------------------------------------------------
# Shared cultural adaptation instructions (used by GeminiService + OpenAIService)
# ---------------------------------------------------------------------------

_CULTURAL_MAPPING = {
    'en': {'region': 'North America/Western', 'country': 'United States', 'ethnicity': 'diverse American population - Caucasian, African American, Hispanic, Asian American', 'names': 'American names like Emma, Olivia, Liam, Noah, Michael, Jennifer', 'environment': 'American urban and suburban settings - modern offices, American homes, shopping malls', 'clothing': 'casual American fashion - jeans, t-shirts, sneakers, business casual'},
    'en-US': {'region': 'North America', 'country': 'United States', 'ethnicity': 'diverse American population - Caucasian, African American, Hispanic, Asian American', 'names': 'American names like Emma, Olivia, Liam, Noah, Michael, Jennifer', 'environment': 'American settings - NYC skyline, suburban homes, modern offices, American streets', 'clothing': 'American fashion - casual wear, business casual, athleisure'},
    'en-GB': {'region': 'Western Europe', 'country': 'United Kingdom', 'ethnicity': 'British population - diverse, including British Asian, British African', 'names': 'British names like Oliver, George, Amelia, Charlotte, Harry, Sophie', 'environment': 'British settings - London streets, British homes, UK offices, red brick buildings', 'clothing': 'British fashion - smart casual, conservative, classic styles'},
    'es': {'region': 'Latin America', 'country': 'Latin America', 'ethnicity': 'Hispanic/Latino - Mexican, Colombian, Argentine features, warm skin tones, dark hair', 'names': 'Spanish names like Sofia, Isabella, Diego, Carlos, Maria, Juan, Valentina', 'environment': 'Latin American settings - colorful streets, colonial architecture, warm climates', 'clothing': 'Latin American fashion - vibrant colors, casual and stylish, tropical appropriate'},
    'de': {'region': 'Western Europe', 'country': 'Germany', 'ethnicity': 'German/Central European - fair to light skin, varied hair colors', 'names': 'German names like Lukas, Leon, Mia, Emma, Felix, Hannah, Maximilian', 'environment': 'German settings - modern cities, efficient infrastructure, clean streets, German architecture', 'clothing': 'German fashion - practical, high quality, understated elegance'},
    'fr': {'region': 'Western Europe', 'country': 'France', 'ethnicity': 'French population - diverse including African French, North African French', 'names': 'French names like Emma, Gabriel, Léa, Louis, Chloé, Hugo, Camille', 'environment': 'French settings - Parisian streets, French cafes, elegant architecture, countryside', 'clothing': 'French fashion - chic, elegant, sophisticated, classic styles'},
    'ar': {'region': 'Middle East / North Africa', 'country': 'Arab World (UAE, Saudi Arabia, Egypt)', 'ethnicity': 'Arab/Middle Eastern - olive to brown skin tones, dark hair, Middle Eastern features', 'names': 'Arabic names like Mohammed, Ahmed, Fatima, Aisha, Omar, Layla, Youssef', 'environment': 'Middle Eastern settings - modern Dubai, traditional markets, desert landscapes, Islamic architecture', 'clothing': 'Middle Eastern fashion - modest clothing, traditional and modern mix, hijabs for women (optional)'},
    'he': {'region': 'Middle East', 'country': 'Israel', 'ethnicity': 'Israeli/Jewish - diverse including Ashkenazi, Sephardi, Mizrahi, Ethiopian', 'names': 'Hebrew names like Noam, David, Tamar, Yael, Itai, Maya, Omer', 'environment': 'Israeli settings - Tel Aviv beaches, Jerusalem, modern cities, Mediterranean climate', 'clothing': 'Israeli fashion - casual, relaxed, Mediterranean style'},
    'pt': {'region': 'Latin America', 'country': 'Brazil', 'ethnicity': 'Brazilian - very diverse, mixed race, African Brazilian, European Brazilian', 'names': 'Brazilian names like Pedro, Gabriel, Ana, Julia, Lucas, Maria, Beatriz', 'environment': 'Brazilian settings - Rio beaches, São Paulo urban, tropical nature, vibrant cities', 'clothing': 'Brazilian fashion - colorful, casual, beach-appropriate, trendy'},
    'pt-BR': {'region': 'Latin America', 'country': 'Brazil', 'ethnicity': 'Brazilian - very diverse, mixed race, African Brazilian, European Brazilian', 'names': 'Brazilian names like Pedro, Gabriel, Ana, Julia, Lucas, Maria, Beatriz', 'environment': 'Brazilian settings - Rio beaches, São Paulo urban, tropical nature, vibrant cities', 'clothing': 'Brazilian fashion - colorful, casual, beach-appropriate, trendy'},
    'it': {'region': 'Southern Europe', 'country': 'Italy', 'ethnicity': 'Italian/Mediterranean - olive skin, dark hair, Southern European features', 'names': 'Italian names like Francesco, Leonardo, Sofia, Giulia, Alessandro, Aurora', 'environment': 'Italian settings - Roman streets, Venetian canals, Italian piazzas, Mediterranean coast', 'clothing': 'Italian fashion - stylish, designer-conscious, elegant casual'},
    'ja': {'region': 'East Asia', 'country': 'Japan', 'ethnicity': 'Japanese - East Asian features, typically black hair', 'names': 'Japanese names like Haruto, Yui, Sota, Hina, Ren, Mei, Yuto', 'environment': 'Japanese settings - Tokyo streets, traditional temples, modern cities, anime aesthetic', 'clothing': 'Japanese fashion - modern Tokyo street style, clean lines, minimalist'},
    'ko': {'region': 'East Asia', 'country': 'South Korea', 'ethnicity': 'Korean - East Asian features, typically black hair, K-beauty aesthetic', 'names': 'Korean names like Min-jun, Seo-yeon, Ji-ho, Ha-yun, Joon, Soo-ah', 'environment': 'Korean settings - Seoul streets, K-pop aesthetic, modern cities, cafes', 'clothing': 'Korean fashion - trendy K-fashion, modern, colorful, youthful'},
    'zh': {'region': 'East Asia', 'country': 'China', 'ethnicity': 'Chinese - East Asian features, typically black hair', 'names': 'Chinese names like Wei, Li, Ming, Xiao, Chen, Lin, Zhang', 'environment': 'Chinese settings - modern Shanghai, Beijing, traditional temples, bustling cities', 'clothing': 'Chinese fashion - modern Chinese urban style, mix of traditional and contemporary'},
    'ru': {'region': 'Eastern Europe', 'country': 'Russia', 'ethnicity': 'Russian/Slavic - fair skin, varied hair colors, Eastern European features', 'names': 'Russian names like Dmitri, Anastasia, Ivan, Natalia, Alexei, Olga', 'environment': 'Russian settings - Moscow streets, Russian architecture, winter scenes', 'clothing': 'Russian fashion - practical, layered, fur accents, elegant'},
    'hi': {'region': 'South Asia', 'country': 'India', 'ethnicity': 'Indian/South Asian - brown skin tones, dark hair, diverse Indian features', 'names': 'Indian names like Aarav, Priya, Arjun, Ananya, Vihaan, Diya, Rohan', 'environment': 'Indian settings - Delhi, Mumbai, colorful markets, Bollywood aesthetic', 'clothing': 'Indian fashion - mix of traditional (saree, kurta) and modern Western'},
    'tr': {'region': 'Middle East / Europe', 'country': 'Turkey', 'ethnicity': 'Turkish - Mediterranean to Middle Eastern, olive skin, dark hair', 'names': 'Turkish names like Mehmet, Zeynep, Ali, Elif, Mustafa, Defne', 'environment': 'Turkish settings - Istanbul streets, Turkish bazaars, Bosphorus views', 'clothing': 'Turkish fashion - modern European mixed with traditional elements'},
    'pl': {'region': 'Eastern Europe', 'country': 'Poland', 'ethnicity': 'Polish/Slavic - fair skin, varied hair colors, Eastern European features', 'names': 'Polish names like Jan, Zofia, Jakub, Julia, Kacper, Zuzanna', 'environment': 'Polish settings - Warsaw, Krakow, European cities, Polish architecture', 'clothing': 'Polish fashion - European casual, practical, modern'},
    'th': {'region': 'Southeast Asia', 'country': 'Thailand', 'ethnicity': 'Thai/Southeast Asian - tan skin, dark hair, Southeast Asian features', 'names': 'Thai names like Somchai, Suda, Niran, Ploy, Chai, Kwang', 'environment': 'Thai settings - Bangkok streets, Thai temples, tropical beaches, markets', 'clothing': 'Thai fashion - light fabrics, bright colors, tropical appropriate'},
    'vi': {'region': 'Southeast Asia', 'country': 'Vietnam', 'ethnicity': 'Vietnamese - East/Southeast Asian features, typically black hair', 'names': 'Vietnamese names like Minh, Linh, Huy, Thao, Duc, Mai', 'environment': 'Vietnamese settings - Hanoi, Ho Chi Minh City, Vietnamese streets, tropical', 'clothing': 'Vietnamese fashion - modern Asian style, traditional ao dai for formal'},
}

_CULTURAL_DEFAULT = {
    'region': 'International',
    'country': 'Target country',
    'ethnicity': 'diverse population appropriate for the target region',
    'names': 'culturally appropriate names for the target language',
    'environment': 'settings appropriate for the target country',
    'clothing': 'fashion appropriate for the target culture',
}


def get_cultural_adaptation_instructions(target_language: str) -> str:
    """Get cultural adaptation instructions for image/video prompt generation.

    Shared by GeminiService and OpenAIService so the cultural guidance is
    identical regardless of which LLM provider is used.

    Args:
        target_language: ISO 639-1 language code (e.g., 'en', 'es', 'he').

    Returns:
        Formatted instruction string for embedding in prompts.
    """
    info = _CULTURAL_MAPPING.get(target_language.lower(), _CULTURAL_DEFAULT)
    return (
        f"\n"
        f"CRITICAL - CULTURAL ADAPTATION (MANDATORY!):\n"
        f"YOU MUST CHANGE THE CHARACTERS AND ENVIRONMENT TO MATCH THE TARGET COUNTRY!\n"
        f"\n"
        f"TARGET LANGUAGE: {target_language.upper()}\n"
        f"TARGET COUNTRY/REGION: {info['country']} / {info['region']}\n"
        f"\n"
        f"DO NOT KEEP THE ORIGINAL VIDEO'S CHARACTERS OR ENVIRONMENT!\n"
        f"\n"
        f"REQUIRED CHANGES:\n"
        f"1. CHARACTERS: Use {info['ethnicity']}. Use {info['names']}.\n"
        f"2. ENVIRONMENT: Use {info['environment']}.\n"
        f"3. CLOTHING: Use {info['clothing']}.\n"
        f"4. ALL TEXT & VOICEOVER: Must be in {target_language.upper()}. "
        f"Use culturally appropriate phrases and expressions.\n"
        f"\n"
        f"Your prompts MUST describe characters from {info['country']}, "
        f"environments from {info['region']}, "
        f"and clothing/fashion: {info['clothing']}.\n"
    )
