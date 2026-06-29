"""GeminiService — extracted verbatim from Comp_Videos/video_scene_processor.py.

Lines 1900-5310 of the monolith.
"""

import os
import io
import re
import json
import time
import base64
import logging
import tempfile
import random
import requests
import warnings
from typing import Dict, Any, List, Optional, Tuple

from api_pipeline.services.base.config import config

with warnings.catch_warnings():
    warnings.filterwarnings("ignore", category=FutureWarning, message=".*google.generativeai.*")
    try:
        import google.generativeai as genai
        GEMINI_AVAILABLE = True
    except ImportError:
        GEMINI_AVAILABLE = False
        genai = None

logger = logging.getLogger(__name__)


class GeminiService:
    """Service for Gemini via Vertex AI API - Native video analysis with reasoning."""
    
    # Longer timeout and retries for heavy Gemini calls (e.g. UGC prompt parse, scene prompts)
    GEMINI_REQUEST_TIMEOUT = 300
    GEMINI_MAX_RETRIES = 3
    GEMINI_RETRY_DELAYS = (10, 25, 60)  # seconds between retries

    def __init__(self, api_key: str = None, gcs_storage_service=None):
        """Initialize Gemini service via Vertex AI.
        
        Args:
            api_key: Optional Kie.ai API key (kept for backward compatibility).
            gcs_storage_service: GCS storage service for uploading videos to get public URLs.
        """
        self.gcs_storage_service = gcs_storage_service
        self.initialized = False
        
        self.vertex_api_key = config.VERTEX_AI_API_KEY
        self.model = config.VERTEX_AI_MODEL
        self.project_id = config.VERTEX_AI_PROJECT_ID
        self.location = config.VERTEX_AI_LOCATION
        # When API key is set: Vertex with ?key= (same as Gemini Image service). Else: Vertex with OAuth Bearer.
        self._use_api_key = bool(self.vertex_api_key)
        self._endpoint_template = (
            f"https://aiplatform.googleapis.com/v1/projects/{config.VERTEX_AI_PROJECT_ID}/locations/{config.VERTEX_AI_LOCATION}/publishers/google/models"
        )
        
        self.kie_api_key = api_key
        
        if self._use_api_key:
            pass  # Key provided, use Vertex with key in URL
        elif not self._get_vertex_token_from_adc():
            logger.warning("⚠️ Gemini not available - set VERTEX_AI_API_KEY or run: gcloud auth application-default login")
            return
        
        self.last_usage_metadata = None
        self.initialized = True
        auth_note = "API key" if self._use_api_key else "OAuth"
        logger.info(f"✅ Gemini client initialized (Vertex AI - {self.model}, {auth_note})")
    
    def _get_vertex_token_from_adc(self) -> Optional[str]:
        """Get access token from Application Default Credentials (no VERTEX_AI_API_KEY needed)."""
        try:
            from google.auth import default
            from google.auth.transport.requests import Request
            creds, _ = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            creds.refresh(Request())
            return creds.token
        except Exception:
            return None
    
    def _get_vertex_headers(self) -> Dict[str, str]:
        """Return headers: OAuth Bearer when no API key; else Content-Type only (key in URL)."""
        headers = {"Content-Type": "application/json"}
        if not self._use_api_key:
            token = self._get_vertex_token_from_adc()
            if token:
                headers["Authorization"] = f"Bearer {token}"
        return headers
    
    def _get_vertex_url(self, model: str) -> str:
        """Return Vertex generateContent URL; when API key set, append ?key= (same as Gemini Image)."""
        base = f"{self._endpoint_template}/{model}:generateContent"
        if self._use_api_key:
            return f"{base}?key={self.vertex_api_key}"
        return base

    def _vertex_post_with_retry(
        self, url: str, headers: Dict[str, str], json_payload: Dict[str, Any],
        timeout: int = None, max_retries: int = None
    ) -> requests.Response:
        """POST to Vertex AI with retries on timeout/connection errors and longer timeout."""
        timeout = timeout if timeout is not None else self.GEMINI_REQUEST_TIMEOUT
        max_retries = max_retries if max_retries is not None else self.GEMINI_MAX_RETRIES
        delays = self.GEMINI_RETRY_DELAYS
        last_exc = None
        for attempt in range(max_retries):
            try:
                response = requests.post(url, headers=headers, json=json_payload, timeout=timeout)
                return response
            except requests.exceptions.RequestException as e:
                last_exc = e
                if attempt < max_retries - 1:
                    delay = delays[attempt] if attempt < len(delays) else delays[-1]
                    logger.warning(
                        f"⚠️ Gemini request failed (attempt {attempt + 1}/{max_retries}): {e}. "
                        f"Retrying in {delay}s..."
                    )
                    time.sleep(delay)
                else:
                    raise
        if last_exc:
            raise last_exc
        raise RuntimeError("Unexpected retry loop exit")

    def _upload_video_to_gcs(self, video_path: str) -> Optional[str]:
        """Upload video to GCS and return public URL.
        
        Args:
            video_path: Path to local video file.
            
        Returns:
            Public URL of the uploaded video, or None if failed.
        """
        if not self.gcs_storage_service:
            logger.warning("⚠️ GCS storage service not available for video upload")
            return None
        
        try:
            import uuid
            
            # Generate unique filename
            video_id = str(uuid.uuid4())[:8]
            gcs_key = f"gemini_analysis/{video_id}.mp4"
            
            # Read video file
            with open(video_path, 'rb') as f:
                video_data = f.read()
            
            # Upload to GCS
            logger.info(f"📤 Uploading video to GCS for Gemini analysis...")
            
            # Use the GCS storage service's bucket
            if not self.gcs_storage_service._initialize():
                return None
            
            blob = self.gcs_storage_service.bucket.blob(gcs_key)
            blob.upload_from_string(video_data, content_type='video/mp4')
            
            # Try to make public
            try:
                blob.make_public()
            except Exception:
                pass  # May fail if bucket uses uniform access
            
            # Generate public URL
            video_url = f"https://storage.googleapis.com/{self.gcs_storage_service.bucket_name}/{gcs_key}"
            logger.info(f"✅ Video uploaded to GCS: {video_url[:60]}...")
            
            return video_url
            
        except Exception as e:
            logger.error(f"❌ Error uploading video to GCS: {e}")
            return None
    
    def _cleanup_gcs_video(self, video_url: str):
        """Delete temporary video from GCS.
        
        Args:
            video_url: URL of the video to delete.
        """
        if not self.gcs_storage_service or not video_url:
            return
        
        try:
            # Extract key from URL
            parts = video_url.split('storage.googleapis.com/')
            if len(parts) > 1:
                # Remove bucket name from path
                path_parts = parts[1].split('/', 1)
                if len(path_parts) > 1:
                    gcs_key = path_parts[1]
                    if self.gcs_storage_service._initialize():
                        blob = self.gcs_storage_service.bucket.blob(gcs_key)
                        blob.delete()
                        logger.info("🗑️ Cleaned up GCS video file")
        except Exception:
            pass  # Non-critical, ignore errors
    
    def _get_cultural_adaptation_instructions(self, target_language: str) -> str:
        """Get cultural adaptation instructions based on target language.
        
        CRITICAL: Characters and backgrounds must ALWAYS match the target country,
        regardless of what appears in the original video.
        
        Args:
            target_language: Target language code (e.g., 'en', 'es', 'ar', 'de').
            
        Returns:
            Detailed cultural adaptation instructions for prompts.
        """
        # Map language codes to regions and cultural details
        cultural_mapping = {
            # English - US/UK/AU
            'en': {
                'region': 'North America/Western',
                'country': 'United States',
                'ethnicity': 'diverse American population - Caucasian, African American, Hispanic, Asian American',
                'names': 'American names like Emma, Olivia, Liam, Noah, Michael, Jennifer',
                'environment': 'American urban and suburban settings - modern offices, American homes, shopping malls',
                'clothing': 'casual American fashion - jeans, t-shirts, sneakers, business casual',
            },
            'en-US': {
                'region': 'North America',
                'country': 'United States',
                'ethnicity': 'diverse American population - Caucasian, African American, Hispanic, Asian American',
                'names': 'American names like Emma, Olivia, Liam, Noah, Michael, Jennifer',
                'environment': 'American settings - NYC skyline, suburban homes, modern offices, American streets',
                'clothing': 'American fashion - casual wear, business casual, athleisure',
            },
            'en-GB': {
                'region': 'Western Europe',
                'country': 'United Kingdom',
                'ethnicity': 'British population - diverse, including British Asian, British African',
                'names': 'British names like Oliver, George, Amelia, Charlotte, Harry, Sophie',
                'environment': 'British settings - London streets, British homes, UK offices, red brick buildings',
                'clothing': 'British fashion - smart casual, conservative, classic styles',
            },
            # Spanish
            'es': {
                'region': 'Latin America',
                'country': 'Latin America',
                'ethnicity': 'Hispanic/Latino - Mexican, Colombian, Argentine features, warm skin tones, dark hair',
                'names': 'Spanish names like Sofia, Isabella, Diego, Carlos, Maria, Juan, Valentina',
                'environment': 'Latin American settings - colorful streets, colonial architecture, warm climates',
                'clothing': 'Latin American fashion - vibrant colors, casual and stylish, tropical appropriate',
            },
            # German
            'de': {
                'region': 'Western Europe',
                'country': 'Germany',
                'ethnicity': 'German/Central European - fair to light skin, varied hair colors',
                'names': 'German names like Lukas, Leon, Mia, Emma, Felix, Hannah, Maximilian',
                'environment': 'German settings - modern cities, efficient infrastructure, clean streets, German architecture',
                'clothing': 'German fashion - practical, high quality, understated elegance',
            },
            # French
            'fr': {
                'region': 'Western Europe',
                'country': 'France',
                'ethnicity': 'French population - diverse including African French, North African French',
                'names': 'French names like Emma, Gabriel, Léa, Louis, Chloé, Hugo, Camille',
                'environment': 'French settings - Parisian streets, French cafes, elegant architecture, countryside',
                'clothing': 'French fashion - chic, elegant, sophisticated, classic styles',
            },
            # Arabic
            'ar': {
                'region': 'Middle East / North Africa',
                'country': 'Arab World (UAE, Saudi Arabia, Egypt)',
                'ethnicity': 'Arab/Middle Eastern - olive to brown skin tones, dark hair, Middle Eastern features',
                'names': 'Arabic names like Mohammed, Ahmed, Fatima, Aisha, Omar, Layla, Youssef',
                'environment': 'Middle Eastern settings - modern Dubai, traditional markets, desert landscapes, Islamic architecture',
                'clothing': 'Middle Eastern fashion - modest clothing, traditional and modern mix, hijabs for women (optional)',
            },
            # Hebrew
            'he': {
                'region': 'Middle East',
                'country': 'Israel',
                'ethnicity': 'Israeli/Jewish - diverse including Ashkenazi, Sephardi, Mizrahi, Ethiopian',
                'names': 'Hebrew names like Noam, David, Tamar, Yael, Itai, Maya, Omer',
                'environment': 'Israeli settings - Tel Aviv beaches, Jerusalem, modern cities, Mediterranean climate',
                'clothing': 'Israeli fashion - casual, relaxed, Mediterranean style',
            },
            # Portuguese
            'pt': {
                'region': 'Latin America',
                'country': 'Brazil',
                'ethnicity': 'Brazilian - very diverse, mixed race, African Brazilian, European Brazilian',
                'names': 'Brazilian names like Pedro, Gabriel, Ana, Julia, Lucas, Maria, Beatriz',
                'environment': 'Brazilian settings - Rio beaches, São Paulo urban, tropical nature, vibrant cities',
                'clothing': 'Brazilian fashion - colorful, casual, beach-appropriate, trendy',
            },
            'pt-BR': {
                'region': 'Latin America',
                'country': 'Brazil',
                'ethnicity': 'Brazilian - very diverse, mixed race, African Brazilian, European Brazilian',
                'names': 'Brazilian names like Pedro, Gabriel, Ana, Julia, Lucas, Maria, Beatriz',
                'environment': 'Brazilian settings - Rio beaches, São Paulo urban, tropical nature, vibrant cities',
                'clothing': 'Brazilian fashion - colorful, casual, beach-appropriate, trendy',
            },
            # Italian
            'it': {
                'region': 'Southern Europe',
                'country': 'Italy',
                'ethnicity': 'Italian/Mediterranean - olive skin, dark hair, Southern European features',
                'names': 'Italian names like Francesco, Leonardo, Sofia, Giulia, Alessandro, Aurora',
                'environment': 'Italian settings - Roman streets, Venetian canals, Italian piazzas, Mediterranean coast',
                'clothing': 'Italian fashion - stylish, designer-conscious, elegant casual',
            },
            # Japanese
            'ja': {
                'region': 'East Asia',
                'country': 'Japan',
                'ethnicity': 'Japanese - East Asian features, typically black hair',
                'names': 'Japanese names like Haruto, Yui, Sota, Hina, Ren, Mei, Yuto',
                'environment': 'Japanese settings - Tokyo streets, traditional temples, modern cities, anime aesthetic',
                'clothing': 'Japanese fashion - modern Tokyo street style, clean lines, minimalist',
            },
            # Korean
            'ko': {
                'region': 'East Asia',
                'country': 'South Korea',
                'ethnicity': 'Korean - East Asian features, typically black hair, K-beauty aesthetic',
                'names': 'Korean names like Min-jun, Seo-yeon, Ji-ho, Ha-yun, Joon, Soo-ah',
                'environment': 'Korean settings - Seoul streets, K-pop aesthetic, modern cities, cafes',
                'clothing': 'Korean fashion - trendy K-fashion, modern, colorful, youthful',
            },
            # Chinese
            'zh': {
                'region': 'East Asia',
                'country': 'China',
                'ethnicity': 'Chinese - East Asian features, typically black hair',
                'names': 'Chinese names like Wei, Li, Ming, Xiao, Chen, Lin, Zhang',
                'environment': 'Chinese settings - modern Shanghai, Beijing, traditional temples, bustling cities',
                'clothing': 'Chinese fashion - modern Chinese urban style, mix of traditional and contemporary',
            },
            # Russian
            'ru': {
                'region': 'Eastern Europe',
                'country': 'Russia',
                'ethnicity': 'Russian/Slavic - fair skin, varied hair colors, Eastern European features',
                'names': 'Russian names like Dmitri, Anastasia, Ivan, Natalia, Alexei, Olga',
                'environment': 'Russian settings - Moscow streets, Russian architecture, winter scenes',
                'clothing': 'Russian fashion - practical, layered, fur accents, elegant',
            },
            # Hindi
            'hi': {
                'region': 'South Asia',
                'country': 'India',
                'ethnicity': 'Indian/South Asian - brown skin tones, dark hair, diverse Indian features',
                'names': 'Indian names like Aarav, Priya, Arjun, Ananya, Vihaan, Diya, Rohan',
                'environment': 'Indian settings - Delhi, Mumbai, colorful markets, Bollywood aesthetic',
                'clothing': 'Indian fashion - mix of traditional (saree, kurta) and modern Western',
            },
            # Turkish
            'tr': {
                'region': 'Middle East / Europe',
                'country': 'Turkey',
                'ethnicity': 'Turkish - Mediterranean to Middle Eastern, olive skin, dark hair',
                'names': 'Turkish names like Mehmet, Zeynep, Ali, Elif, Mustafa, Defne',
                'environment': 'Turkish settings - Istanbul streets, Turkish bazaars, Bosphorus views',
                'clothing': 'Turkish fashion - modern European mixed with traditional elements',
            },
            # Polish
            'pl': {
                'region': 'Eastern Europe',
                'country': 'Poland',
                'ethnicity': 'Polish/Slavic - fair skin, varied hair colors, Eastern European features',
                'names': 'Polish names like Jan, Zofia, Jakub, Julia, Kacper, Zuzanna',
                'environment': 'Polish settings - Warsaw, Krakow, European cities, Polish architecture',
                'clothing': 'Polish fashion - European casual, practical, modern',
            },
            # Thai
            'th': {
                'region': 'Southeast Asia',
                'country': 'Thailand',
                'ethnicity': 'Thai/Southeast Asian - tan skin, dark hair, Southeast Asian features',
                'names': 'Thai names like Somchai, Suda, Niran, Ploy, Chai, Kwang',
                'environment': 'Thai settings - Bangkok streets, Thai temples, tropical beaches, markets',
                'clothing': 'Thai fashion - light fabrics, bright colors, tropical appropriate',
            },
            # Vietnamese
            'vi': {
                'region': 'Southeast Asia',
                'country': 'Vietnam',
                'ethnicity': 'Vietnamese - East/Southeast Asian features, typically black hair',
                'names': 'Vietnamese names like Minh, Linh, Huy, Thao, Duc, Mai',
                'environment': 'Vietnamese settings - Hanoi, Ho Chi Minh City, Vietnamese streets, tropical',
                'clothing': 'Vietnamese fashion - modern Asian style, traditional ao dai for formal',
            },
        }
        
        # Get cultural info or use default
        lang_code = target_language.lower()
        cultural_info = cultural_mapping.get(lang_code, {
            'region': 'International',
            'country': 'Target country',
            'ethnicity': 'diverse population appropriate for the target region',
            'names': 'culturally appropriate names for the target language',
            'environment': 'settings appropriate for the target country',
            'clothing': 'fashion appropriate for the target culture',
        })
        
        return f"""
🌍🌍🌍 CRITICAL - CULTURAL ADAPTATION (MANDATORY!) 🌍🌍🌍
═══════════════════════════════════════════════════════════════════════════════════════

⚠️ YOU MUST CHANGE THE CHARACTERS AND ENVIRONMENT TO MATCH THE TARGET COUNTRY! ⚠️

TARGET LANGUAGE: {target_language.upper()}
TARGET COUNTRY/REGION: {cultural_info['country']} / {cultural_info['region']}

🚫 DO NOT KEEP THE ORIGINAL VIDEO'S CHARACTERS OR ENVIRONMENT! 🚫
The original video's people and backgrounds are for a DIFFERENT market.
You MUST create NEW characters and environments for the TARGET market.

✅ REQUIRED CHANGES:

1. **CHARACTERS (MANDATORY CHANGE!):**
   - Use {cultural_info['ethnicity']}
   - Use {cultural_info['names']}
   - DO NOT copy the original video's characters!
   - Example: If original has Arab person → For English US market → Use American person

2. **ENVIRONMENT/BACKGROUND (MANDATORY CHANGE!):**
   - Use {cultural_info['environment']}
   - DO NOT copy the original video's backgrounds!
   - Example: If original has Arabic text/architecture → For US market → Use American settings

3. **CLOTHING & STYLE:**
   - Use {cultural_info['clothing']}
   - Adapt to local fashion and cultural norms

4. **ALL TEXT & VOICEOVER:**
   - Must be in {target_language.upper()}
   - Use culturally appropriate phrases and expressions

📋 EXAMPLE TRANSFORMATION:
Original video: Arab woman in traditional dress, Arabic text, Middle Eastern city background
Target: English (US)
New video: American woman (diverse ethnicity), casual American fashion, American city/suburban background, English text

🎯 YOUR PROMPTS MUST DESCRIBE:
- Characters that look like they're from {cultural_info['country']}
- Environments that look like {cultural_info['region']}
- Clothing and fashion from {cultural_info['clothing']}
- Names like {cultural_info['names']}

THIS IS MANDATORY - DO NOT SKIP CULTURAL ADAPTATION!
"""
    
    def analyze_video_comprehensive(
        self, 
        video_path: str,
        article_content: Dict[str, str] = None,
        manual_instructions: str = "",
        original_transcript: str = "",
        target_language: str = "en",
        article_related_to_video: bool = True
    ) -> Dict[str, Any]:
        """Analyze entire video with Gemini 2.5 Flash via Kie.ai.
        
        This is much more cost-effective than sending individual frames to GPT-4o.
        Gemini processes the entire video natively for comprehensive analysis.
        
        Args:
            video_path: Path to the video file.
            article_content: Optional article content for context.
            manual_instructions: Optional manual instructions.
            original_transcript: Transcript of what's said in the video (from Whisper).
            target_language: Target language code for VO script (e.g., 'en', 'es', 'de').
            article_related_to_video: True if article is similar to video content (adapt video for new offer/language),
                                      False if article is fundamentally different (keep style but create new content).
            
        Returns:
            Comprehensive analysis including:
            - scene_breakdown: List of scenes with timestamps, descriptions, and purposes
            - product_info: Detected product details (type, purpose, usage, appearance)
            - visual_style: Color palette, lighting, composition, mood
            - narrative_structure: Hook, problem, solution, CTA structure
            - usage_contexts: How the product appears/is used in different scenes
            - audio_visual_relationship: How the VO relates to what's shown
            - style_prompt_prefix: Ready-to-use style prefix for generation prompts
        """
        if not self.initialized:
            logger.warning("⚠️ Gemini not initialized, returning empty analysis")
            return self._get_empty_analysis()
        
        video_url = None
        
        try:
            # Upload video to GCS to get public URL
            video_url = self._upload_video_to_gcs(video_path)
            if not video_url:
                logger.warning("⚠️ Could not upload video to GCS, falling back to GPT-4o")
                return self._get_empty_analysis()
            
            # Build the comprehensive analysis prompt
            article_context = ""
            if article_content:
                title = article_content.get("title", "")
                first_p = article_content.get("first_paragraph", "")
                free_text = article_content.get("free_text", "")
                article_text_combined = free_text or f"{title}\n{first_p}"
                
                if title or first_p or free_text:
                    # Get cultural adaptation instructions based on target language
                    cultural_instructions = self._get_cultural_adaptation_instructions(target_language)
                    
                    if article_related_to_video:
                        # YES - Article is SIMILAR to video content
                        # Adapt the video to match the new article with a different offer/language
                        article_context = f"""
🔗 ARTICLE-VIDEO RELATIONSHIP: SIMILAR CONTENT (Article IS related to Video)
═══════════════════════════════════════════════════════════════════════════

ARTICLE CONTENT:
Title: {title}
Summary: {first_p[:500] if first_p else 'N/A'}
Full Text: {free_text[:1000] if free_text else 'N/A'}

✅ ADAPTATION STRATEGY (SIMILAR CONTENT):
The article describes a SIMILAR offer/product to what's shown in the video.
Your goal is to adapt the video for the new offer while keeping visuals SIMILAR to the original:

1. **KEEP THE SAME VISUAL STYLE** - The video's scenes, composition, and style should remain similar
2. **ADAPT THE PRODUCT/OFFER** - Replace the original product with the article's product (similar type)
3. **ADAPT THE MESSAGING** - Update text overlays and voiceover to match the article content
4. **ADAPT THE LANGUAGE** - All text and VO in the target language
5. **KEEP THE NARRATIVE STRUCTURE** - Same story flow (hook, problem, solution, CTA)

Example: Original video shows weight loss patches → Article is about different weight loss patches
→ Keep the visual style, body transformation narrative, but adapt for the new product

{cultural_instructions}

"""
                    else:
                        # NO - Article is FUNDAMENTALLY DIFFERENT from video content
                        # Keep the video's style and atmosphere but create entirely new content
                        article_context = f"""
🔄 ARTICLE-VIDEO RELATIONSHIP: DIFFERENT CONTENT (Article is NOT related to Video)
═══════════════════════════════════════════════════════════════════════════════════

ARTICLE CONTENT (NEW TOPIC):
Title: {title}
Summary: {first_p[:500] if first_p else 'N/A'}
Full Text: {free_text[:1000] if free_text else 'N/A'}

⚠️⚠️⚠️ CRITICAL ADAPTATION STRATEGY (DIFFERENT CONTENT) ⚠️⚠️⚠️
The article describes a COMPLETELY DIFFERENT offer/product than what's shown in the video.
You must CREATE NEW content while KEEPING the video's STYLE and ATMOSPHERE:

1. **KEEP THE VISUAL STYLE** - Preserve the video's aesthetic: lighting, camera work, mood, color palette, framing
2. **KEEP THE ATMOSPHERE** - Same energy level, same emotional tone, same production quality
3. **KEEP THE PACING** - Same scene durations and rhythm
4. **DO NOT USE THE ORIGINAL PRODUCT/OFFER** - The original video's product is IRRELEVANT
5. **CREATE NEW CONTENT FOR THE ARTICLE** - Base all visuals and messaging on the ARTICLE content only

🎯 YOUR MISSION: Create a NEW video that:
- LOOKS AND FEELS like the original (same style, mood, quality)
- But ADVERTISES the article's product/offer (NOT the original video's product)
- Has NEW visuals appropriate for the article content
- Has NEW voiceover based on the article
- Has NEW text overlays based on the article

Example: Original video shows shoe advertisement → Article is about work-from-home jobs
→ Keep the video's professional, energetic style → Create scenes showing people working from home
→ Do NOT show shoes anywhere → Create new narrative about remote work opportunities

IMPORTANT: When generating prompts, describe scenes that would be APPROPRIATE for the article's topic,
using the STYLE elements from the original video (lighting, camera angles, mood, energy).

{cultural_instructions}

"""
            
            # Language context for VO script
            language_context = f"""
⚠️⚠️⚠️ CRITICAL - LANGUAGE REQUIREMENT ⚠️⚠️⚠️
TARGET LANGUAGE: {target_language.upper()}
The new voiceover script (full_script) MUST be written ENTIRELY in {target_language.upper()}.
Do NOT use any other language. The script will be read by a TTS system in {target_language}.
"""
            
            instructions_context = ""
            if manual_instructions:
                instructions_context = f"""
MANUAL INSTRUCTIONS:
{manual_instructions}
"""
            
            # Include transcript if available
            transcript_context = ""
            if original_transcript:
                transcript_context = f"""
AUDIO TRANSCRIPT (what is being said in the video):
\"\"\"{original_transcript}\"\"\"

IMPORTANT: Analyze how the audio/voiceover relates to what's shown visually in each scene.
"""
            
            # Build goal statement based on article-video relationship
            if article_related_to_video:
                goal_statement = """You are an expert video director and storyteller. Your job is to DEEPLY UNDERSTAND this video's story and create PRECISE, ACCURATE prompts that recreate the ORIGINAL video's visuals and story exactly.

⚠️⚠️⚠️ YOUR GOAL: ADAPT the video for a NEW OFFER while keeping SIMILAR visuals ⚠️⚠️⚠️
- Watch the ORIGINAL video carefully - understand its visual style
- The new video should LOOK SIMILAR to the original
- But adapt the product/offer and messaging to match the ARTICLE content
- Your prompts should recreate the visual style while adapting the content"""
            else:
                goal_statement = """You are an expert video director and storyteller. Your job is to understand this video's VISUAL STYLE and create NEW content that matches the article while keeping the same STYLE and ATMOSPHERE.

⚠️⚠️⚠️ YOUR GOAL: CREATE NEW CONTENT with the SAME VISUAL STYLE ⚠️⚠️⚠️
- Watch the ORIGINAL video to understand its STYLE (lighting, camera work, mood, energy, quality)
- DO NOT copy the original video's content/product - it's COMPLETELY DIFFERENT from the article
- CREATE NEW visuals that are appropriate for the ARTICLE content
- The new video should FEEL LIKE the original (same style/mood) but SHOW the article's content
- Your prompts should describe NEW scenes for the article content, using the original's style"""

            # Build workflow steps based on article-video relationship
            if article_related_to_video:
                workflow_steps = """🎬 YOUR MISSION: Understand the VIDEO'S COMPLETE STORY and generate prompts that ADAPT it for the new offer.

⚠️⚠️⚠️ CRITICAL WORKFLOW - FOLLOW THIS EXACTLY ⚠️⚠️⚠️

STEP 1: UNDERSTAND THE COMPLETE STORY (DO THIS FIRST!)
1. **WATCH THE ENTIRE VIDEO** - Don't just analyze frames, watch the complete narrative
2. **IDENTIFY THE STORY TYPE** - Is it transformation? Demo? Testimonial? Problem-solution? Before/after?
3. **UNDERSTAND THE NARRATIVE ARC** - Beginning → Middle → End. What's the journey?
4. **UNDERSTAND SCENE CONNECTIONS** - How do scenes connect? What changes between scenes? Why?
5. **TRACK SUBJECT CHANGES** - Does the subject look different in different scenes? Why? (e.g., weight loss, mood change, clothing change)
6. **UNDERSTAND PRODUCT ROLE** - When does the product appear? What's its role in the story? How does it connect to the narrative?

STEP 2: ANALYZE EACH SCENE INDIVIDUALLY
For EACH scene, watch the ORIGINAL video at that scene's timestamp:
1. What do you ACTUALLY see? (subject appearance, clothing, setting, lighting, camera angle)
2. What's the EXACT visual state? (match the original exactly)
3. Is the product visible? (set product_visible accurately)
4. How does this scene connect to the previous scene? (what changed?)
5. What's the subject's state in THIS scene? (match the original exactly)

STEP 3: CREATE ADAPTED PROMPTS
Only AFTER understanding the complete story AND analyzing each scene, create prompts that:
- KEEP the ORIGINAL video's visual style (camera angles, lighting, mood)
- ADAPT the product to match the ARTICLE's product/offer
- ADAPT the messaging to match the ARTICLE content
- Match the scene structure of the original (same number of scenes, similar durations)
- Include the ARTICLE's product when appropriate (replacing the original product)"""
            else:
                workflow_steps = """🎬 YOUR MISSION: Extract the video's VISUAL STYLE and create NEW content for the article.

⚠️⚠️⚠️ CRITICAL WORKFLOW FOR DIFFERENT CONTENT - FOLLOW THIS EXACTLY ⚠️⚠️⚠️

STEP 1: EXTRACT THE VIDEO'S VISUAL STYLE (DO THIS FIRST!)
1. **WATCH THE ENTIRE VIDEO** - Focus on HOW it looks, not WHAT it shows
2. **IDENTIFY THE STYLE ELEMENTS:**
   - Lighting style (natural, studio, dramatic, soft, etc.)
   - Camera work (static, handheld, smooth movements, etc.)
   - Color palette (warm, cool, vibrant, muted, etc.)
   - Mood/energy (energetic, calm, professional, casual, etc.)
   - Production quality (UGC style, professional, cinematic, etc.)
   - Framing preferences (close-ups, wide shots, etc.)
3. **IDENTIFY THE PACING** - How long are scenes? What's the rhythm?
4. **IDENTIFY THE NARRATIVE STRUCTURE** - Hook → Problem → Solution → CTA?
5. **DO NOT FOCUS ON THE PRODUCT** - The original product is IRRELEVANT for this task

STEP 2: UNDERSTAND THE ARTICLE CONTENT
For EACH piece of information in the article:
1. What is the product/offer? (This is what we're advertising)
2. What are the benefits? (These should be shown in the video)
3. Who is the target audience? (People like this should appear in scenes)
4. What emotions should the video evoke? (Match the article's tone)
5. What call-to-action is needed? (What should viewers do?)

STEP 3: CREATE NEW PROMPTS WITH ORIGINAL STYLE
Create prompts for a NEW video that:
- HAS THE SAME STYLE as the original (lighting, camera, mood, quality, pacing)
- SHOWS NEW CONTENT appropriate for the ARTICLE
- DOES NOT include the original video's product AT ALL
- Features people, settings, and actions relevant to the ARTICLE
- Uses the same narrative structure (hook, problem, solution, CTA) but for the NEW topic
- Has the same number of scenes with similar durations as the original"""

            analysis_prompt = f"""{goal_statement}

{language_context}
{article_context}
{instructions_context}
{transcript_context}

{workflow_steps}

You will output:
1. **VIDEO STORY UNDERSTANDING** - Complete narrative, story type, subject journey, scene connections
2. **IMAGE PROMPTS** - PRECISE prompts for Nano Banana that match the ORIGINAL video's visuals exactly
3. **MOTION PROMPTS** - PRECISE animation prompts for Kling/Runway that match the ORIGINAL video's movement
4. **NEW VOICEOVER SCRIPT** - Complete VO script in the target language that matches the story
5. **CTA BUTTON** - If the video needs a call-to-action button

🔑 CRITICAL RULES FOR PROMPTS:

⚠️⚠️⚠️ PRODUCT VISIBILITY - MOST IMPORTANT ⚠️⚠️⚠️
THE PRODUCT DOES NOT APPEAR IN EVERY SCENE!
- Watch the ORIGINAL video carefully: In which scenes is the product actually VISIBLE?
- If the product is NOT visible in a scene → Set product_visible=false and DO NOT mention the product in the image_prompt
- If the product IS visible in a scene → Set product_visible=true and include it with POSITIVE, SPECIFIC description
- Example: Scene 1 shows product application (product_visible=true), Scene 2 shows person walking away (product_visible=false), Scene 3 shows result with product visible (product_visible=true)

**IMAGE PROMPTS (for Nano Banana) - MUST MATCH ORIGINAL VIDEO EXACTLY:**
- Watch the ORIGINAL video at this scene's timestamp - what do you ACTUALLY see?
- Recreate the EXACT visual: subject appearance, clothing, setting, lighting, camera angle
- ⚠️ ONLY include product if product_visible=true for this scene!
- If product_visible=true: describe product EXACTLY as it appears (color, shape, size, materials, EXACT location on body/object)
- If product_visible=false: DO NOT mention the product at all in the prompt
- ⚠️ CRITICAL: If subject changes between scenes (weight, appearance, mood, clothing) - describe the EXACT state in THIS scene
- Match the ORIGINAL video's visual style: camera angle, framing, lighting, mood
- Be SPECIFIC and DETAILED - the prompt should recreate the original scene visually
- Focus on describing what IS visible: people, objects, environments, lighting, mood, camera angles
- Format: "Photorealistic [exact shot type from original], [exact subject appearance from original], [exact action from original], [exact setting from original], [exact lighting from original], [exact mood from original]"

**MOTION PROMPTS (for video generation) - MUST MATCH ORIGINAL VIDEO EXACTLY:**
- Watch the ORIGINAL video at this scene's timestamp - what movement do you ACTUALLY see?
- Describe the EXACT movement: camera movement, subject motion, speed, direction
- Match the ORIGINAL video's pacing and style
- Keep under 200 characters
- Be SPECIFIC about the movement type (zoom, pan, static, tracking, etc.)
- Format: "[Exact camera movement from original], [exact subject action from original], [exact speed/style from original]"

**PRODUCT LOGIC - CRITICAL FOR ACCURACY:**
⚠️⚠️⚠️ FIRST: Determine if product is visible in EACH scene by watching the original video!

For EACH scene, you must:
1. Watch the original video at that scene's timestamp
2. Check: Is the product VISIBLE in this scene?
3. Set product_visible=true ONLY if product is actually visible
4. Set product_visible=false if product is NOT visible (even if product exists in the video overall)

**PRODUCT VISUAL DESCRIPTION - EXTREMELY DETAILED:**
When describing the product in "visual_description", you MUST provide an EXTREMELY DETAILED 400+ word description that includes:

1. **EXACT SHAPE AND DIMENSIONS:**
   - Precise shape (circular, rectangular, oval, irregular, etc.)
   - Exact dimensions (e.g., "2cm diameter", "3cm x 4cm rectangle", "covers palm of hand")
   - Relative size to body parts or objects (e.g., "half the size of a credit card", "slightly larger than a quarter")

2. **EXACT COLORS (with specific hex codes or precise color names):**
   - Primary color with specific shade (e.g., "bright orange #FF6600", "pale beige #F5F5DC", "deep navy blue #000080")
   - Secondary colors if any (e.g., "white outer ring #FFFFFF", "transparent center")
   - Gradients or color transitions if present
   - NOT just "orange" or "white" - be SPECIFIC!

3. **EXACT MATERIALS AND TEXTURES:**
   - Material type (e.g., "smooth adhesive gel center", "matte white outer ring", "transparent film backing")
   - Texture description (e.g., "glossy surface", "matte finish", "smooth", "textured", "opaque", "semi-transparent")
   - How light interacts (e.g., "reflective surface", "absorbs light", "translucent")

4. **PRODUCT BRANDING (CRITICAL - MUST REMOVE!):**
   ⚠️ DO NOT include any text, logos, or branding on the product surface!
   - If the original product has text/logos → REMOVE them in your prompts
   - The product surface must be CLEAN and PLAIN
   - Describe the product's physical features ONLY (shape, color, material, texture)
   - Example: Instead of "patch with 'SlimFast' logo" → "plain circular patch with orange center"

5. **EXACT PACKAGING (if visible):**
   - Package color, shape, material
   - How product appears in packaging
   - NOTE: Package branding should also be removed/ignored

6. **EXACT PLACEMENT AND ORIENTATION:**
   - Precise location on body/object (e.g., "centered on lower abdomen 5cm below navel", "on right cheekbone")
   - Orientation (e.g., "horizontal", "vertical", "diagonal at 45 degrees")
   - How it sits on surface (e.g., "flat against skin", "slightly raised", "curved to match body contour")

7. **EXACT LIGHTING AND SHADOWS:**
   - How light hits the product (e.g., "soft natural light from above creates subtle highlight on center")
   - Shadow details (e.g., "slight shadow cast on skin below", "no shadow, flush with skin")

8. **EXACT PERSPECTIVE AND CAMERA ANGLE:**
   - Camera angle relative to product (e.g., "top-down view", "45-degree angle from side", "eye-level")
   - How product appears from this angle (e.g., "circular shape appears slightly oval from this angle")

9. **UNIQUE FEATURES:**
   - Any patterns, designs, or distinguishing marks
   - Any special characteristics that make this product identifiable

When product_visible=true, describe it POSITIVELY and SPECIFICALLY:
⚠️ CRITICAL: Use POSITIVE descriptions, NOT negative ones!
❌ BAD: "patch not on forehead, not on clothes"
✅ GOOD: "small circular patch (2cm diameter) with bright orange gel center (#FF6600) and matte white outer ring (#FFFFFF), adhered to the bare skin of the lower abdomen 5cm below navel, visible on clean exposed stomach area, soft natural lighting creates subtle highlight on glossy gel surface"

1. **SKIN PRODUCTS (patches, stickers, creams, serums):**
   - **Patches/stickers for weight loss/slimming:**
     * MUST say: "adhered to the bare skin of the [specific body part: stomach/abdomen, arm, thigh, etc.]"
     * MUST say: "on clean exposed skin" or "on bare skin visible under/above clothing"
     * Example: "small circular patch adhered to the bare skin of the lower abdomen, visible on clean exposed stomach area"
   
   - **Face patches/creams:**
     * MUST say: "applied to the [specific face area: forehead, cheek, under-eye, etc.]"
     * Example: "patch adhered to the forehead" or "cream being massaged into the cheek"
   
   - **Body creams/lotions:**
     * MUST say: "being rubbed/massaged into the [specific body part: arm, leg, stomach, etc.]"
     * Example: "cream being massaged into the bare skin of the arm"

2. **PET PRODUCTS (toys, treats, accessories):**
   - MUST say: "[pet type] actively [action: playing with, chewing, interacting with] the [product]"
   - Example: "Border Collie actively chewing and playing with the colorful ball toy"

3. **FOOD/DRINKS:**
   - MUST say: "[person/pet] [action: eating, drinking, preparing] the [product]"
   - Example: "person drinking from the bottle" or "hands preparing the food"

4. **SUPPLEMENTS/PILLS:**
   - MUST say: "hand holding [product] near mouth" or "[product] being taken from package"

**CRITICAL - PRODUCT VISIBILITY RULES:**
⚠️ THE PRODUCT DOES NOT APPEAR IN EVERY SCENE!
- Analyze the ORIGINAL video carefully: In which scenes is the product VISIBLE?
- In scenes where the product IS visible → Include it with POSITIVE, SPECIFIC description with EXACT colors (hex codes), dimensions, materials, and placement
- In scenes where the product is not visible → Focus on describing what IS visible: the subject, clothing, setting, lighting, mood, camera angle
- Example: If scene 1 shows product application (include product), scene 2 shows person walking (focus on person and setting), scene 3 shows result with product (include product) → Scenes 1 and 3 include product, scene 2 focuses on other visual elements

🚫🚫🚫 CRITICAL - BRANDING AND TEXT RULES 🚫🚫🚫

**PRODUCT SURFACE = NO TEXT/BRANDING (MANDATORY!)**
- Products must be shown COMPLETELY CLEAN - no text, no logos, no branding
- If original product has brand name/logo → REMOVE IT in your prompts
- The product surface must be plain and clean
- Example: Original shows "SlimPatch™" on product → Describe as "plain circular patch" (NO text on product)

**TEXT OVERLAYS - STRICT RULES!**

🚨 RULE 1: CHECK IF ORIGINAL VIDEO HAS TEXT OVERLAY
- Watch the original video carefully
- Does it have text/branding overlays on the screen (not on product)?
- If YES → You may add text overlay (extracted from article)
- If NO → DO NOT add any text overlay! Keep image CLEAN!

🚨 RULE 2: CHECK MANUAL INSTRUCTIONS
- If Manual Instructions say "remove text" or "no text" → NO text overlay at all!
- If Manual Instructions say "add text" → Add text even if original didn't have
- Manual Instructions OVERRIDE the original video analysis

🚨 RULE 3: TEXT MUST BE FROM THE ARTICLE CONTENT
- The text MUST be extracted from the article/Free text content
- Find the main offer, discount, benefit, or call-to-action IN THE ARTICLE
- Write it in the target language

✅ CORRECT EXAMPLES (text extracted from article):
- Article says "50% discount on all models" → Text: "50% OFF ALL MODELS"
- Article says "we're hiring drivers" → Text: "WE'RE HIRING!"
- Article says "free shipping this week" → Text: "FREE SHIPPING"
- Article says "משלוח חינם לכל הארץ" → Text: "משלוח חינם"

❌ WRONG - DON'T DO THIS:
- "promotional text" ← Technical description, not real text!
- "SALE" when article doesn't mention a sale ← Not from article!
- "BUY NOW" when article is about job hiring ← Unrelated!
- Adding text when original video had no text ← Violates Rule 1!

📋 DECISION FLOWCHART:
1. Does Manual Instructions say "remove text"? → NO TEXT
2. Does Manual Instructions say "add text"? → ADD TEXT (from article)
3. Does original video have text overlays? → If NO → NO TEXT
4. If original has text → Extract message from article → ADD THAT TEXT

| Condition | Action |
|-----------|--------|
| Manual says "remove text" | NO text overlay |
| Manual says "add text" | ADD text from article |
| Original has text + no manual override | ADD text from article |
| Original has NO text + no manual override | NO text overlay |

**WHEN DESCRIBING PRODUCTS IN IMAGE PROMPTS - POSITIVE LANGUAGE ONLY:**
⚠️ REMEMBER: Only describe product if product_visible=true for THIS scene!

If product_visible=true:
- ✅ DO: "patch adhered to the bare skin of the stomach area, visible on clean exposed abdomen"
- ❌ DON'T: "patch not on forehead, not on clothes"
- ✅ DO: "small circular patch on the lower abdomen, skin visible around it"
- ❌ DON'T: "patch not floating, not on fabric"
- Be SPECIFIC about EXACT location (stomach, arm, face area, etc.)
- Be SPECIFIC about EXACT action (adhered, being applied, being massaged, etc.)
- Include POSITIVE context (bare skin visible, clean exposed area, etc.)

If product_visible=false:
- ✅ DO: Describe the scene focusing on what IS visible - the subject, clothing, setting, lighting, mood, camera angle
- ✅ DO: "Photorealistic medium shot, young woman with slim athletic build wearing black workout clothes, confidently walking through bright modern bedroom, natural window lighting, energetic happy mood"
- Focus on describing the visual elements that ARE present in the scene

**VOICEOVER SCRIPT:**
- Match the STYLE and TONE of the original
- Use content from the article provided
- Match the video duration
- If original is energetic → new VO should be energetic
- If original is calm → new VO should be calm

Return a JSON object with these sections:

{{
  "scenes": [
    {{
      "scene_number": 1,
      "start_time": "0:00",
      "end_time": "0:03",
      "duration_seconds": 3,
      
      "understanding": {{
        "what_happens": "<describe EXACTLY what happens in this scene - watch the original video at this timestamp and describe what you see>",
        "narrative_role": "<hook/problem/solution/benefit/demo/result/cta/transition>",
        "story_connection": "<How does this scene connect to the previous scene? What changed? What's the progression?>",
        "subject_appearance": "<CRITICAL: How does the subject look in THIS SPECIFIC scene? Be EXACT: body type, clothing, expression, state, position. Match the ORIGINAL video exactly>",
        "visual_details": "<EXACT visual details from original: camera angle, framing, lighting, setting, colors, mood>",
        "text_on_screen": "<ONLY if original video has text overlay on screen: Extract the main offer/message from the article and write it here (e.g., '50% OFF', 'משלוח חינם'). If Manual Instructions say 'remove text' → leave EMPTY. If original has NO text overlay → leave EMPTY. The text MUST come from the article content!>",
        "has_branding_overlay": true | false,
        "product_visible": true | false,
        "product_action": "<what's being done with product, if visible - be specific about the action>",
        "changes_from_previous": "<What changed from the previous scene? Subject appearance? Setting? Mood? Product visibility?>"
      }},
      
      "prompts": {{
        "image_prompt": "<COMPLETE, READY-TO-USE prompt for Nano Banana. Must be photorealistic, detailed, include all visual elements. 
        ⚠️ CRITICAL: Only include the product in the prompt if 'product_visible' is true for this scene!
        🚫 PRODUCT MUST BE CLEAN - NO text, logos, or branding on the product surface!
        
        PRODUCT RULES:
        - If product_visible=true: Include product with POSITIVE, SPECIFIC description with EXACT colors (hex codes), dimensions, materials, and placement. Product must be PLAIN with no text/branding on it.
        - If product_visible=false: Describe the scene focusing on what IS visible - the subject, clothing, setting, lighting, mood, camera angle
        
        🚨 TEXT OVERLAY RULES (STRICT!):
        1. If Manual Instructions say 'remove text' or 'no text' → DO NOT include any text in prompt
        2. If original video has NO text overlays → DO NOT include any text in prompt
        3. ONLY if original has text AND Manual Instructions don't forbid it → Include text FROM THE ARTICLE
        
        When adding text (only if rules 1-3 allow):
        - Extract the actual offer/message from the article content
        - Write the real text, not a description
        - Example: 'small white text 50% OFF in top-right corner' (where 50% OFF is from article)
        
        ❌ NEVER DO:
        - Add text when original video had no text
        - Add text when Manual Instructions say to remove it
        - Write 'promotional text' or 'text overlay' instead of the actual text
        - Use generic text like 'SALE' if article doesn't mention a sale
        
        Example with product (no text): 'Photorealistic medium shot, young woman showing her flat stomach with a small plain circular patch (2.5cm diameter, bright orange #FF6600 center, NO text on patch), bright modern bedroom, natural lighting'
        Example WITH text (only if original had text AND article mentions this offer): 'Photorealistic medium shot, small white text 50% OFF in top-right corner, young woman showing clean product, natural lighting'
        Example NO text (original had no text): 'Photorealistic medium shot, young woman confidently walking through bright modern bedroom, natural window lighting, energetic happy mood'>",
        
        "motion_prompt": "<Animation prompt for Kling/Runway, under 200 chars. Only mention product movement if product_visible=true. Example with product: 'Slow zoom in, woman gently touches the patch on her stomach, soft smile, subtle body movement'. Example without product: 'Slow zoom in, woman walking confidently, soft smile, natural movement'>"
      }}
    }}
  ],
  
  "product": {{
    "detected": true | false,
    "type": "<product category: patch, cream, toy, supplement, etc.>",
    "visual_description": "<EXTREMELY DETAILED 500+ word description for image generation. MUST include ALL of the following in extreme detail:
    
    **COLORS (CRITICAL - BE EXTREMELY SPECIFIC):**
    - Primary color with EXACT hex code (e.g., 'bright orange #FF6600', 'pale beige #F5F5DC', 'deep navy blue #000080')
    - Secondary colors with EXACT hex codes (e.g., 'white outer ring #FFFFFF', 'transparent center #FFFFFF with 30% opacity')
    - Color gradients if present (e.g., 'gradient from #FF6600 at center to #FF8833 at edges')
    - Color patterns if present (e.g., 'striped pattern with alternating #FF6600 and #FFFFFF')
    - Color saturation and brightness (e.g., 'highly saturated bright orange', 'muted pale beige')
    - NOT just 'orange' or 'white' - MUST include hex codes and specific shade descriptions!
    
    **SHAPE AND DIMENSIONS (CRITICAL - BE EXTREMELY SPECIFIC):**
    - Precise shape (circular, rectangular, oval, irregular, etc.) with EXACT measurements
    - Exact dimensions in cm/mm (e.g., '2.5cm diameter circle', '3cm x 4cm rectangle', 'oval 2cm x 3cm')
    - Relative size to body parts or objects (e.g., 'half the size of a credit card', 'covers palm of hand', 'slightly larger than a quarter')
    - Thickness/depth if visible (e.g., '2mm thick', 'flat and flush with skin', 'slightly raised 1mm above skin')
    - Edge details (e.g., 'rounded edges', 'sharp corners', 'beveled edge')
    
    **MATERIALS AND TEXTURES (CRITICAL - BE EXTREMELY SPECIFIC):**
    - Material type with texture details (e.g., 'smooth adhesive gel center with glossy surface', 'matte white outer ring with paper-like texture', 'transparent film backing with slight texture')
    - Surface finish (e.g., 'glossy reflective surface', 'matte non-reflective finish', 'semi-gloss with subtle sheen')
    - Texture description (e.g., 'smooth', 'textured', 'opaque', 'semi-transparent', 'translucent', 'grainy', 'satin finish')
    - How light interacts (e.g., 'reflective surface creates bright highlight', 'absorbs light creating matte appearance', 'translucent allowing light to pass through')
    - Material properties (e.g., 'flexible and conforms to skin', 'rigid and maintains shape', 'stretchy elastic material')
    
    **PRODUCT BRANDING (CRITICAL - MUST BE REMOVED!):**
    ⚠️⚠️⚠️ DO NOT include any text, logos, or branding ON THE PRODUCT SURFACE! ⚠️⚠️⚠️
    - When describing the product, OMIT all text/logos that appear on it
    - The product must be shown as CLEAN and PLAIN - no branding
    - Only describe physical characteristics: shape, colors, materials, textures, dimensions
    - Example: Original has "SlimPatch™" logo → Describe as "plain circular patch" (NO text)
    
    **PACKAGING (if visible):**
    - Package color with hex codes (e.g., 'white box #FFFFFF', 'blue label #0066CC')
    - Package shape and size (e.g., 'rectangular box 5cm x 8cm', 'circular container 4cm diameter')
    - REMOVE package branding/text - describe only physical appearance
    - How product appears in packaging (e.g., 'product visible through transparent window', 'product wrapped in foil')
    
    **PLACEMENT AND ORIENTATION (CRITICAL - BE EXTREMELY SPECIFIC):**
    - Precise location on body/object (e.g., 'centered on lower abdomen 5cm below navel', 'on right cheekbone 2cm from eye', 'on upper arm 10cm from shoulder')
    - Orientation (e.g., 'horizontal alignment', 'vertical alignment', 'diagonal at 45 degrees', 'rotated 30 degrees clockwise')
    - How it sits on surface (e.g., 'flat against skin with no gaps', 'slightly raised 1mm above skin', 'curved to match body contour', 'adhered flush with no visible edges')
    - Relationship to surrounding elements (e.g., 'surrounded by bare skin', 'partially covered by clothing', 'visible through transparent material')
    
    **LIGHTING AND SHADOWS (CRITICAL - BE EXTREMELY SPECIFIC):**
    - How light hits the product (e.g., 'soft natural light from above creates subtle highlight on center', 'harsh directional light creates strong contrast', 'diffused light creates even illumination')
    - Shadow details (e.g., 'slight shadow cast on skin below creating depth', 'no shadow, flush with skin', 'soft shadow around edges')
    - Highlights and reflections (e.g., 'bright highlight on glossy center', 'matte surface shows no reflections', 'reflective surface shows window reflection')
    - Light temperature (e.g., 'warm 3000K lighting', 'cool 6000K daylight', 'neutral 5000K lighting')
    
    **PERSPECTIVE AND CAMERA ANGLE (CRITICAL - BE EXTREMELY SPECIFIC):**
    - Camera angle relative to product (e.g., 'top-down view looking straight down', '45-degree angle from side', 'eye-level view', 'close-up macro view')
    - How product appears from this angle (e.g., 'circular shape appears slightly oval from this angle', 'rectangular shape appears as trapezoid due to perspective')
    - Distance from camera (e.g., 'close-up filling 30% of frame', 'medium shot with product in center', 'wide shot with product as small element')
    - Depth of field (e.g., 'product in sharp focus with blurred background', 'entire scene in focus', 'shallow depth of field with product sharp')
    
    **FUNCTIONALITY AND USAGE (CRITICAL - BE EXTREMELY SPECIFIC):**
    - How the product functions (e.g., 'adhesive patch that sticks to skin', 'cream that is massaged into skin', 'toy that pet interacts with')
    - How it's being used in the scene (e.g., 'being applied to bare skin', 'already adhered and visible', 'being removed from packaging')
    - Interaction with user/environment (e.g., 'person's hand applying the patch', 'patch visible on person's body', 'product being held near face')
    - State of product (e.g., 'new unused product', 'product in use', 'product showing results')
    
    **UNIQUE FEATURES AND DISTINGUISHING MARKS:**
    - Any patterns, designs, or distinguishing marks (e.g., 'circular pattern in center', 'logo in corner', 'serial number visible')
    - Any special characteristics that make this product identifiable (e.g., 'unique color combination', 'distinctive shape', 'characteristic texture')
    - Branding elements (e.g., 'brand logo visible', 'product name printed', 'certification mark')
    
    This description must be so detailed that an AI image generator can recreate the product pixel-perfectly with exact colors, dimensions, materials, and appearance.>",
    "purpose": "<what does this product do?>",
    "usage_method": "<how is it used? step by step>",
    "application_rules": "<CRITICAL: where does it go? bare skin? with pet? etc.>",
    "best_frame_timestamps": ["0:02", "0:08"],
    "product_image_details": "<If product is visible in frames, describe the EXACT visual appearance in the best frame with EXTREME DETAIL:
    - EXACT color composition: Primary color with hex code, secondary colors with hex codes, gradients, patterns, saturation, brightness
    - EXACT shape and proportions: Precise measurements (cm/mm), relative size to known objects, thickness/depth, edge details
    - EXACT text/logos: Transcribe word-for-word, font style, size, position, color with hex code, text effects
    - EXACT material appearance: Material type, surface finish (glossy/matte/semi-gloss), texture (smooth/textured/grainy), opacity (opaque/transparent/translucent), material properties
    - EXACT lighting interaction: How light hits product, shadow details, highlights and reflections, light temperature
    - EXACT position and orientation: Precise location, orientation (horizontal/vertical/diagonal), how it sits on surface, relationship to surrounding elements
    - EXACT perspective: Camera angle, distance, depth of field, how product appears from this angle
    - EXACT functionality: How product functions, how it's being used, interaction with user/environment, state of product
    - Unique features: Patterns, designs, distinguishing marks, branding elements, special characteristics
    This must be detailed enough for pixel-perfect recreation.>"
  }},
  
  "video_story": {{
    "type": "<transformation/demo/testimonial/lifestyle/tutorial/problem_solution/ugc_review>",
    "one_sentence_summary": "<What happens in this video in one sentence - the complete story>",
    "narrative_arc": "<Describe the complete story arc: beginning → middle → end. What's the journey?>",
    "key_moments": [
      "<Important moment 1: what happens and why it matters>",
      "<Important moment 2: what happens and why it matters>"
    ],
    "scene_connections": "<How do scenes connect? What's the progression? What changes between scenes?>",
    "subject_changes": {{
      "has_visible_change": true | false,
      "start_state": "<How subject looks/feels at START - be specific: overweight, tired, messy, clothing, expression, etc.>",
      "end_state": "<How subject looks/feels at END - be specific: slim, energetic, groomed, clothing, expression, etc.>",
      "subject_appearance_per_scene": {{
        "1": "<EXACT appearance in scene 1: body type, clothing, expression, state>",
        "2": "<EXACT appearance in scene 2: body type, clothing, expression, state>",
        "3": "<EXACT appearance in scene 3: body type, clothing, expression, state>"
      }}
    }},
    "product_role_in_story": "<What's the product's role in the story? When does it appear? How does it connect to the narrative?>"
  }},
  
  "new_voiceover": {{
    "full_script": "<COMPLETE voiceover script for the new video. Use article content. Match original style and duration. This is the EXACT text for TTS.>",
    "word_count": <number>,
    "style": "<enthusiastic/calm/professional/friendly - match original>"
  }},
  
  "cta": {{
    "needs_cta": true | false,
    "button_text": "<CTA button text if needed, e.g., 'Shop Now', 'Try Now'>",
    "scene_number": <which scene to add CTA to, usually last>
  }},
  
  "style": {{
    "aesthetic": "<social_media/cinematic/ugc/professional/minimalist>",
    "lighting": "<natural/studio/dramatic/soft>",
    "mood": "<energetic/calm/luxurious/casual/playful>",
    "style_prefix": "<Concise style description for all prompts, e.g., 'UGC social media style, bright natural lighting, handheld camera feel, casual authentic mood'>"
  }},
  
  "audio": {{
    "original_has_vo": true | false,
    "original_vo_style": "<enthusiastic/calm/professional/friendly>",
    "original_vo_gender": "<male/female/unknown>",
    "music_mood": "<energetic/calm/uplifting/dramatic/none>"
  }}
}}

⚠️ CRITICAL - READ CAREFULLY:

0. **STORY ACCURACY - MOST IMPORTANT:**
   - ⚠️⚠️⚠️ YOU MUST WATCH THE ORIGINAL VIDEO CAREFULLY FOR EACH SCENE! ⚠️⚠️⚠️
   - Your prompts MUST recreate the ORIGINAL video's visuals EXACTLY - don't invent new visuals!
   - Match the ORIGINAL: camera angles, framing, lighting, subject appearance, setting, mood, colors, textures
   - Understand the COMPLETE STORY before creating prompts - don't just describe individual scenes in isolation
   - Track how scenes CONNECT - what changes between scenes? Why? What's the story progression?
   - Each prompt should reflect the EXACT visual state from the ORIGINAL video at that timestamp
   - If the original shows a transformation (e.g., overweight → slim), your prompts MUST show the CORRECT state for each scene
   - If the original shows different settings/clothing/mood in different scenes, your prompts MUST match this exactly
   - The goal is to recreate the ORIGINAL video's story and visuals, not create a new story

1. **PRODUCT VISIBILITY - SECOND MOST IMPORTANT:**
   - ⚠️ THE PRODUCT DOES NOT APPEAR IN EVERY SCENE!
   - Watch the ORIGINAL video: In which scenes is the product actually VISIBLE?
   - Set product_visible=true ONLY if product is visible in that specific scene
   - Set product_visible=false if product is NOT visible (even if product exists in video)
   - If product_visible=false → DO NOT mention the product in image_prompt at all
   - If product_visible=true → Include product with POSITIVE, SPECIFIC description and EXACT location

2. **IMAGE PROMPTS MUST MATCH ORIGINAL VIDEO EXACTLY:**
   - Watch the ORIGINAL video at this scene's timestamp - what do you ACTUALLY see?
   - Recreate EXACTLY: subject appearance, clothing, setting, lighting, camera angle, mood
   - ⚠️ ONLY include product if product_visible=true for that scene
   - Be SPECIFIC about subject's physical state in EACH scene (if subject changes, show the CORRECT state for THIS scene)
   - Match the ORIGINAL video's visual style - don't invent new visuals
   - Include ALL details you see in the original: colors, textures, expressions, positions

3. **MOTION PROMPTS MUST MATCH ORIGINAL VIDEO EXACTLY:**
   - Watch the ORIGINAL video at this scene's timestamp - what movement do you ACTUALLY see?
   - Describe the EXACT movement: camera movement, subject motion, speed, direction
   - Match the ORIGINAL video's pacing and style
   - Keep under 200 characters
   - Be SPECIFIC about the movement type (zoom, pan, static, tracking, etc.)

4. **NEW VOICEOVER MUST MATCH VIDEO DURATION AND STORY:**
   - Use article content provided
   - Match the style/energy of the original
   - Match the STORY STRUCTURE of the original (hook, problem, solution, etc.) and keep the narrative CAPTIVATING—every line should pull the viewer in.
   - Calculate approximate word count based on duration (2-3 words per second)

5. **PRODUCT LOGIC IS CRITICAL:**
   - Patches/stickers → BARE SKIN only (not on clothes!) - describe EXACT location
   - Pet products → Pet must be visible and interacting
   - Creams → On visible skin - describe EXACT location
   
6. **TRACK SUBJECT CHANGES ACCURATELY:**
   - If someone is overweight in scene 1 and slim in scene 5 → reflect this EXACTLY in prompts!
   - Each scene's image_prompt should show subject in the CORRECT state for that scene
   - Use subject_appearance_per_scene to track changes accurately
   - Match the ORIGINAL video's subject appearance at each timestamp

Return valid JSON only."""

            logger.info("🔍 Analyzing video with Gemini 3 Pro (via Kie.ai)...")
            
            # Build request payload for Kie.ai Gemini 3 Pro endpoint
            payload = {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": analysis_prompt
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": video_url
                                }
                            }
                        ]
                    }
                ],
                "stream": False,
                "include_thoughts": False,
                "reasoning_effort": "low"  # Fast response for video analysis
            }
            
            # Send request to Vertex AI Gemini endpoint
            response = requests.post(
                self._get_vertex_url(self.model),
                headers=self._get_vertex_headers(),
                json=payload,
                timeout=300  # 5 minute timeout for video analysis
            )
            response.raise_for_status()
            
            result = response.json()
            
            # Log the full response for debugging
            logger.info(f"📥 Gemini response status: {response.status_code}")
            logger.info(f"📥 Gemini response keys: {list(result.keys())}")
            
            if "error" in result:
                logger.error(f"❌ Gemini API error: {result.get('error')}")
                return self._get_empty_analysis()
            
            # Extract content from response
            if "choices" in result and len(result["choices"]) > 0:
                choice = result["choices"][0]
                logger.info(f"📥 Choice keys: {list(choice.keys())}")
                message = choice.get("message", {})
                logger.info(f"📥 Message keys: {list(message.keys())}")
                response_text = message.get("content", "")
                logger.info(f"📥 Content length: {len(response_text)} chars")
            else:
                logger.error(f"❌ No choices in Gemini response. Keys: {list(result.keys())}")
                logger.error(f"❌ Full response: {json.dumps(result)[:1000]}")
                return self._get_empty_analysis()
            
            # Clean up response if needed
            if response_text.startswith("```json"):
                response_text = response_text[7:]
            if response_text.startswith("```"):
                response_text = response_text[3:]
            if response_text.endswith("```"):
                response_text = response_text[:-3]
            
            # Log raw response for debugging
            logger.info(f"📥 Gemini raw response (first 500 chars): {response_text[:500]}")
            
            analysis = json.loads(response_text.strip())
            
            logger.info(f"✅ Gemini video analysis complete:")
            logger.info(f"   - Scenes detected: {len(analysis.get('scenes', []))}")
            logger.info(f"   - Product detected: {analysis.get('product', {}).get('detected', False)}")
            logger.info(f"   - Video type: {analysis.get('video_story', {}).get('type', 'unknown')}")
            logger.info(f"   - Style: {analysis.get('style', {}).get('aesthetic', 'unknown')}")
            
            # Log prompts info
            scenes = analysis.get('scenes', [])
            if scenes:
                logger.info(f"   - First scene image prompt: {scenes[0].get('prompts', {}).get('image_prompt', 'N/A')[:60]}...")
            if analysis.get('new_voiceover', {}).get('full_script'):
                logger.info(f"   - New VO script: {analysis['new_voiceover']['full_script'][:60]}...")
            
            # Clean up the uploaded video from GCS
            self._cleanup_gcs_video(video_url)
            
            return analysis
            
        except json.JSONDecodeError as e:
            logger.error(f"❌ Failed to parse Gemini response as JSON: {e}")
            if video_url:
                self._cleanup_gcs_video(video_url)
            return self._get_empty_analysis()
        except Exception as e:
            logger.error(f"❌ Error in Gemini video analysis: {e}")
            import traceback
            traceback.print_exc()
            if video_url:
                self._cleanup_gcs_video(video_url)
            return self._get_empty_analysis()
    
    def _get_empty_analysis(self) -> Dict[str, Any]:
        """Return empty analysis structure when Gemini is unavailable."""
        return {
            "scenes": [],
            "product": {
                "detected": False,
                "type": "",
                "visual_description": "",
                "purpose": "",
                "usage_method": "",
                "application_rules": "",
                "best_frame_timestamps": []
            },
            "video_story": {
                "type": "unknown",
                "one_sentence_summary": "",
                "subject_changes": {
                    "has_visible_change": False,
                    "start_state": "",
                    "end_state": ""
                }
            },
            "new_voiceover": {
                "full_script": "",
                "word_count": 0,
                "style": ""
            },
            "cta": {
                "needs_cta": False,
                "button_text": "",
                "scene_number": 0
            },
            "style": {
                "aesthetic": "modern",
                "lighting": "",
                "mood": "",
                "style_prefix": ""
            },
            "audio": {
                "original_has_vo": False,
                "original_vo_style": "",
                "original_vo_gender": "unknown",
                "music_mood": ""
            }
        }
    
    def analyze_reference_video_structure(self, video_path: str) -> Dict[str, Any]:
        """Analyze a reference video and return only its narrative structure (scene count, roles, durations).
        
        Used by the product video pipeline when Video reference column has a URL:
        the structure is then passed to generate_product_video_scenes so the new video
        follows the same flow while content comes from the new prompt and product images.
        
        Args:
            video_path: Path to local video file (e.g. downloaded from Video reference URL).
            
        Returns:
            Dict with "scene_count" and "scenes" (list of {"narrative_role", "duration_seconds"}).
            Empty dict {} on failure so the pipeline can continue without reference structure.
        """
        if not self.initialized:
            logger.warning("Gemini not initialized, returning empty reference structure")
            return {}
        
        video_url = None
        try:
            video_url = self._upload_video_to_gcs(video_path)
            if not video_url:
                logger.warning("Could not upload reference video to GCS")
                return {}
            
            structure_prompt = """Watch this video. Extract its narrative structure AND the content/voiceover of each scene so a new video can take inspiration from it.

For each distinct scene or segment, provide:
1. narrative_role: one of hook, problem, solution, benefit, demo, result, cta, transition
2. duration_seconds: approximate length of that scene in seconds
3. content_summary: 1-2 sentences describing what we SEE in this scene and the key visual message (what is shown, mood, action)
4. vo_snippet: the voiceover or key line spoken in this scene (transcribe or paraphrase what is said). If no speech, write "[no speech]" or a short description of the mood (e.g. "upbeat music only").

Return a JSON object with this exact format (no other fields):
{
  "scene_count": <number of scenes>,
  "scenes": [
    { "narrative_role": "<role>", "duration_seconds": <number>, "content_summary": "<what we see and key message>", "vo_snippet": "<what is said or [no speech]>" },
    ...
  ]
}

Preserve the order of scenes as they appear in the video. The sum of duration_seconds should approximate the total video length.
Output ONLY valid JSON, no markdown or explanation."""

            # Vertex AI generateContent: need gs:// URI for fileData
            if "storage.googleapis.com/" in video_url:
                gs_uri = "gs://" + video_url.split("storage.googleapis.com/", 1)[1]
            else:
                gs_uri = video_url
            
            payload = {
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {"fileData": {"mimeType": "video/mp4", "fileUri": gs_uri}},
                            {"text": structure_prompt}
                        ]
                    }
                ],
                "generationConfig": {
                    "temperature": 0.3,
                    "maxOutputTokens": 4096
                }
            }
            
            # Use Vertex model that supports video understanding (e.g. gemini-2.5-flash, gemini-2.5-pro)
            video_model = getattr(config, "GEMINI_VIDEO_ANALYSIS_MODEL", None) or getattr(config, "VERTEX_AI_MODEL", "gemini-2.5-flash") or "gemini-2.5-flash"
            url = self._get_vertex_url(video_model)
            response = requests.post(
                url,
                headers=self._get_vertex_headers(),
                json=payload,
                timeout=300
            )
            
            if not response.ok:
                try:
                    err_body = response.text[:500] if response.text else ""
                    logger.warning(f"Gemini reference structure HTTP {response.status_code}: {err_body}")
                except Exception:
                    pass
                if video_url:
                    self._cleanup_gcs_video(video_url)
                return {}
            
            result = response.json()
            self.last_usage_metadata = result.get("usageMetadata")

            if video_url:
                self._cleanup_gcs_video(video_url)

            if "error" in result:
                logger.warning(f"Gemini reference structure API error: {result.get('error')}")
                return {}
            
            response_text = ""
            candidates = result.get("candidates", [])
            if candidates:
                content = candidates[0].get("content", {})
                for part in content.get("parts", []):
                    if "text" in part:
                        response_text = part["text"]
                        break
            
            if not response_text:
                logger.warning("No content in Gemini reference structure response")
                return {}
            
            # Strip markdown code block if present
            text = response_text.strip()
            if text.startswith("```json"):
                text = text[7:]
            if text.startswith("```"):
                text = text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
            
            data = json.loads(text)
            scene_count = data.get("scene_count", 0)
            scenes = data.get("scenes", [])
            if not isinstance(scenes, list) or scene_count <= 0:
                return {}
            
            # Normalize: ensure each scene has narrative_role, duration_seconds, and optional content_summary/vo_snippet
            out_scenes = []
            for s in scenes:
                if not isinstance(s, dict):
                    continue
                role = s.get("narrative_role") or "transition"
                dur = s.get("duration_seconds")
                if dur is None:
                    dur = s.get("duration", 3)
                try:
                    dur = float(dur) if dur is not None else 3.0
                except (TypeError, ValueError):
                    dur = 3.0
                entry = {"narrative_role": str(role), "duration_seconds": dur}
                if s.get("content_summary"):
                    entry["content_summary"] = str(s.get("content_summary", ""))[:500]
                if s.get("vo_snippet"):
                    entry["vo_snippet"] = str(s.get("vo_snippet", ""))[:400]
                out_scenes.append(entry)
            
            if not out_scenes:
                return {}
            
            logger.info(f"Reference video structure: {len(out_scenes)} scenes extracted")
            return {"scene_count": len(out_scenes), "scenes": out_scenes}
            
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse reference structure JSON: {e}")
            if video_url:
                self._cleanup_gcs_video(video_url)
            return {}
        except Exception as e:
            logger.warning(f"Error analyzing reference video structure: {e}")
            if video_url:
                self._cleanup_gcs_video(video_url)
            return {}
    
    def get_scene_prompt_context(
        self, 
        analysis: Dict[str, Any], 
        scene_number: int
    ) -> Dict[str, Any]:
        """Extract relevant context for a specific scene from the comprehensive analysis.
        
        Args:
            analysis: Full video analysis from analyze_video_comprehensive()
            scene_number: 1-indexed scene number
            
        Returns:
            Context dict with scene-specific and global style information.
        """
        scenes = analysis.get("scenes", [])
        product = analysis.get("product", {})
        style = analysis.get("style", {})
        
        # Find the specific scene
        scene_info = {}
        for scene in scenes:
            if scene.get("scene_number") == scene_number:
                scene_info = scene
                break
        
        # Get prompts from scene
        prompts = scene_info.get("prompts", {})
        understanding = scene_info.get("understanding", {})
        
        return {
            "scene_info": scene_info,
            "understanding": understanding,
            "prompts": prompts,
            "product": product,
            "style": style,
            "style_prefix": style.get("style_prefix", ""),
            "narrative_role": understanding.get("narrative_role", ""),
            "image_prompt": prompts.get("image_prompt", ""),
            "motion_prompt": prompts.get("motion_prompt", ""),
            "product_visible": understanding.get("product_visible", False),
            "product_action": understanding.get("product_action", "")
        }
    
    def parse_product_prompt(
        self, 
        prompt: str, 
        image_urls: List[str] = None,
        video_type_context: str = "product"
    ) -> Dict[str, str]:
        """Parse a product/service description prompt into 4 structured outputs using Gemini.
        
        This method takes a free-form description and breaks it down into
        4 structured sections for video creation:
        - TEXT 1: What is the video about (topic/description)
        - TEXT 2: What is the goal of the video (purpose/objective)
        - TEXT 3: Content and style requirements (tone, visual style, what to avoid)
        - TEXT 4: Video structure (scene-by-scene breakdown)
        
        Args:
            prompt: Free-form description from user
            image_urls: Optional list of reference image URLs
            video_type_context: For logging only - "product" or "UGC" so logs match the Video type column
            
        Returns:
            Dict with keys: text_1, text_2, text_3, text_4
        """
        if not self.initialized:
            logger.warning("⚠️ Gemini not initialized, returning empty results")
            return self._get_empty_prompt_parse_result()
        
        logger.info(f"🧠 Parsing {video_type_context} prompt with Gemini 3 Pro...")
        
        # Build the system prompt
        system_prompt = """You are an expert video production planner. Your task is to analyze a product/service description and break it down into a structured video brief.

You must output a JSON object with exactly 4 fields:

1. "text_1" - WHAT IS THE VIDEO ABOUT:
   - A clear, concise description of the video's main topic
   - Should describe the product/service and what makes it unique
   - 2-4 sentences that capture the essence of what will be shown

2. "text_2" - WHAT IS THE GOAL OF THE VIDEO:
   - The marketing objective (e.g., awareness, conversion, education, engagement)
   - Who is the target audience
   - What action should viewers take after watching
   - 1-3 sentences

3. "text_3" - CONTENT AND STYLE REQUIREMENTS:
   - Tone (e.g., professional, casual, energetic, trustworthy)
   - Visual style (e.g., modern, minimalist, vibrant, corporate)
   - Things to AVOID (e.g., "not hype-driven", "no flashy effects")
   - Any specific text requirements or restrictions
   - Language considerations
   - 3-6 bullet points or sentences

4. "text_4" - VIDEO STRUCTURE (Scene breakdown):
   - Opening/Hook: What grabs attention in the first 3 seconds
   - Problem/Setup: What pain point or need is addressed
   - Solution: How the product/service solves it
   - Features/Benefits: Key points to highlight
   - Call to Action: How the video should end
   - Write this as a scene-by-scene outline, not just bullet points

IMPORTANT:
- Base your analysis on the provided description
- If images are provided, incorporate visual details from them
- Be specific and actionable
- The output must be valid JSON"""

        # Build user content parts for Vertex AI format
        # Vertex AI generateContent accepts only gs:// for fileUri; https URLs cause 400. Use inlineData for non-GCS URLs.
        user_parts = []
        
        if image_urls:
            valid_images = [u for u in image_urls if u and u.strip()]
            for url in valid_images:
                url = url.strip()
                if url.startswith("gs://"):
                    user_parts.append({
                        "fileData": {"mimeType": "image/jpeg", "fileUri": url}
                    })
                else:
                    # External https URL: fetch and send as inlineData (Vertex rejects non-GCS fileUri)
                    try:
                        fetch_headers = {
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                            "Accept-Language": "en-US,en;q=0.9",
                        }
                        resp = requests.get(url, headers=fetch_headers, timeout=30)
                        resp.raise_for_status()
                        ct = resp.headers.get("Content-Type", "").lower()
                        mime = "image/png" if "png" in ct or url.lower().endswith(".png") else "image/webp" if "webp" in ct else "image/jpeg"
                        b64 = base64.b64encode(resp.content).decode("utf-8")
                        user_parts.append({"inlineData": {"mimeType": mime, "data": b64}})
                    except Exception as e:
                        logger.warning(f"   Could not fetch image for parse prompt {url[:50]}...: {e}, skipping")
            if valid_images:
                logger.info(f"   Including {len(valid_images)} reference images")
        
        # Add the text prompt
        user_parts.append({
            "text": f"""Please analyze this product/service description and create a structured video brief:

---
{prompt}
---

Output a valid JSON object with the 4 required fields (text_1, text_2, text_3, text_4).
Do not include any text outside the JSON object."""
        })
        
        try:
            # Vertex AI API format
            payload = {
                "contents": [
                    {
                        "role": "user",
                        "parts": user_parts
                    }
                ],
                "systemInstruction": {
                    "parts": [{"text": system_prompt}]
                },
                "generationConfig": {
                    "temperature": 0.7,
                    "maxOutputTokens": 4000
                }
            }
            
            url = self._get_vertex_url(self.model)
            response = self._vertex_post_with_retry(
                url,
                headers=self._get_vertex_headers(),
                json_payload=payload
            )
            response.raise_for_status()
            
            result = response.json()
            self.last_usage_metadata = result.get("usageMetadata")

            # Vertex AI response format
            candidates = result.get("candidates", [])
            if candidates:
                content_parts = candidates[0].get("content", {}).get("parts", [])
                content = ""
                for part in content_parts:
                    if "text" in part:
                        content += part["text"]
            else:
                content = ""
            
            # Parse JSON from response
            parsed = self._extract_json_from_response(content)
            
            if parsed:
                logger.info("✅ Successfully parsed product prompt into 4 sections")
                return {
                    "text_1": parsed.get("text_1", ""),
                    "text_2": parsed.get("text_2", ""),
                    "text_3": parsed.get("text_3", ""),
                    "text_4": parsed.get("text_4", "")
                }
            else:
                logger.warning("⚠️ Could not parse JSON from Gemini response")
                logger.warning(f"   Raw response (first 1000 chars): {content[:1000]}")
                return self._get_empty_prompt_parse_result()
                
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Error calling Gemini API: {e}")
            return self._get_empty_prompt_parse_result()
        except Exception as e:
            logger.error(f"❌ Error parsing product prompt: {e}")
            return self._get_empty_prompt_parse_result()
    
    def _extract_json_from_response(self, content: str) -> Optional[Dict]:
        """Extract JSON object from Gemini response.
        
        Handles cases where JSON is wrapped in markdown code blocks.
        """
        if not content:
            return None
        
        import re
        
        # Try to find JSON in code blocks first (greedy match to capture nested braces)
        json_match = re.search(r'```(?:json)?\s*(\{[\s\S]*\})\s*```', content)
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except json.JSONDecodeError:
                pass
        
        # Try to find the outermost JSON object (product prompt has text_1, scene generation has scenes)
        first_brace = content.find('{')
        last_brace = content.rfind('}')
        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            potential_json = content[first_brace:last_brace + 1]
            try:
                parsed = json.loads(potential_json)
                if isinstance(parsed, dict) and ('text_1' in parsed or 'scenes' in parsed):
                    return parsed
            except json.JSONDecodeError:
                pass
        
        # Try parsing entire content as JSON
        try:
            # Clean up the content - remove code block markers if present
            cleaned = content.strip()
            if cleaned.startswith('```'):
                # Remove opening code block marker (```json or ```)
                lines = cleaned.split('\n')
                # Remove first line (```json) and last line (```)
                if lines[-1].strip() == '```':
                    cleaned = '\n'.join(lines[1:-1])
                else:
                    cleaned = '\n'.join(lines[1:])
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass
        
        # Log the content for debugging
        logger.debug(f"Could not parse JSON from response: {content[:500]}...")
        
        return None
    
    def _get_empty_prompt_parse_result(self) -> Dict[str, str]:
        """Return empty result structure for prompt parsing."""
        return {
            "text_1": "",
            "text_2": "",
            "text_3": "",
            "text_4": ""
        }
    
    def generate_influencer_prompts(
        self,
        free_text: str,
        reference_images: List[Dict[str, Any]],
        scene_count: int,
        manual_instructions: str = "",
        cta_text: str = "",
        language: str = "en",
        existing_influencer_description: str = "",
        vo_timing: Dict[str, Any] = None,
        visual_style: str = "Auto"
    ) -> Dict[str, Any]:
        """Generate influencer-style video prompts for each scene using Gemini.
        
        Creates prompts for an influencer recommendation video where:
        - Scene 1: Influencer with strong hook
        - Scene 4, 7, 10...: Influencer appears again (identical appearance)
        - Last scene: Influencer with CTA
        - Other scenes: Product/experience with cycling reference images
        
        When vo_timing is provided, scene prompts are aligned to the VO text so that
        what the viewer SEES matches what they HEAR at every moment.
        
        Args:
            free_text: Content describing the product/experience to promote.
            reference_images: List of dicts with 'url' and optional 'base64' and 'analysis'.
            scene_count: Number of scenes to generate.
            manual_instructions: Optional custom instructions.
            cta_text: Call-to-action text for the last scene.
            language: ISO 639-1 language code.
            existing_influencer_description: If provided, use this description instead of generating one.
            vo_timing: Optional dict with pre-split VO scene segments (text, timestamps).
            visual_style: Visual style name (e.g. "Auto", "Modern flat 2d", "Soft 3d clay").
            
        Returns:
            Dict with 'influencer_description', 'scene_prompts' list.
        """
        try:
            logger.info(f"🎭 Generating influencer prompts for {scene_count} scenes (via Gemini)...")
            
            # Language name mapping
            language_names = {
                "en": "English", "de": "German", "es": "Spanish", "fr": "French",
                "it": "Italian", "pt": "Portuguese", "nl": "Dutch", "ru": "Russian",
                "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "ar": "Arabic",
                "pl": "Polish", "hu": "Hungarian", "cs": "Czech", "sk": "Slovak",
                "ro": "Romanian", "bg": "Bulgarian", "uk": "Ukrainian", "hr": "Croatian",
                "sr": "Serbian", "sl": "Slovenian", "bs": "Bosnian", "mk": "Macedonian",
                "sq": "Albanian", "el": "Greek", "tr": "Turkish", "lt": "Lithuanian",
                "lv": "Latvian", "et": "Estonian", "fi": "Finnish", "sv": "Swedish",
                "no": "Norwegian", "da": "Danish", "is": "Icelandic", "ga": "Irish",
                "cy": "Welsh", "mt": "Maltese", "ca": "Catalan", "eu": "Basque",
                "gl": "Galician", "be": "Belarusian", "ka": "Georgian", "hy": "Armenian",
                "az": "Azerbaijani", "kk": "Kazakh", "uz": "Uzbek", "tg": "Tajik",
                "ky": "Kyrgyz", "tk": "Turkmen", "mn": "Mongolian",
                "he": "Hebrew", "fa": "Persian", "ur": "Urdu", "hi": "Hindi",
                "bn": "Bengali", "pa": "Punjabi", "gu": "Gujarati", "mr": "Marathi",
                "ta": "Tamil", "te": "Telugu", "kn": "Kannada", "ml": "Malayalam",
                "si": "Sinhala", "ne": "Nepali", "ps": "Pashto", "ku": "Kurdish",
                "th": "Thai", "vi": "Vietnamese", "id": "Indonesian", "ms": "Malay",
                "tl": "Filipino", "my": "Burmese", "km": "Khmer", "lo": "Lao",
                "sw": "Swahili", "am": "Amharic", "ha": "Hausa", "yo": "Yoruba",
                "ig": "Igbo", "zu": "Zulu", "xh": "Xhosa", "af": "Afrikaans",
                "pt-BR": "Brazilian Portuguese", "zh-CN": "Simplified Chinese",
                "zh-TW": "Traditional Chinese", "en-US": "American English",
                "en-GB": "British English", "es-MX": "Mexican Spanish",
                "fr-CA": "Canadian French"
            }
            language_name = language_names.get(language, "English")
            
            # Style-specific prompt prefix and forbidden words for scene generation.
            # Photorealistic styles use camera/lens language; illustration styles use style-native language.
            _style_scene_config = {
                "Auto": {
                    "prefix": "Ultra photorealistic professional photograph, shot on Canon EOS R5, [LENS]mm lens, [LIGHTING].",
                    "forbidden": ["illustration", "vector", "cartoon", "clay", "render", "flat 2D", "paper cut", "isometric"],
                    "instruction": "Every first_prompt must start with the photorealistic prefix above. Describe REAL textures, lighting, skin pores, fabric weave."
                },
                "Cinematic photography": {
                    "prefix": "Cinematic photograph, shot on Arri Alexa, [LENS]mm anamorphic lens, dramatic [LIGHTING].",
                    "forbidden": ["illustration", "vector", "cartoon", "clay", "render", "flat 2D", "paper cut", "isometric"],
                    "instruction": "Every first_prompt must start with the cinematic prefix above. Use dramatic lighting, shallow DoF, film-like color grading."
                },
                "Modern flat 2d": {
                    "prefix": "Modern flat 2D digital illustration, clean vector composition, bold solid colors, [COLOR PALETTE].",
                    "forbidden": ["photorealistic", "photograph", "camera", "Canon", "lens", "depth of field", "bokeh", "skin pores", "fabric texture", "film grain", "shot on"],
                    "instruction": "Every first_prompt must start with the flat 2D prefix above. Use geometric shapes, solid fills, minimal shadows. NOT a photograph."
                },
                "Modern semi flat 2d": {
                    "prefix": "Modern semi-flat 2D digital illustration, soft gradients, subtle shadows, [COLOR PALETTE].",
                    "forbidden": ["photorealistic", "photograph", "camera", "Canon", "lens", "depth of field", "bokeh", "skin pores", "film grain", "shot on"],
                    "instruction": "Every first_prompt must start with the semi-flat 2D prefix above. Use soft gradients and rounded shapes. NOT a photograph."
                },
                "Minimal line art": {
                    "prefix": "Elegant minimal line art illustration, single-weight continuous lines, [COLOR PALETTE — max 3 colors].",
                    "forbidden": ["photorealistic", "photograph", "camera", "Canon", "lens", "depth of field", "bokeh", "skin pores", "texture", "film grain", "shot on"],
                    "instruction": "Every first_prompt must start with the line art prefix above. Thin elegant lines, white space, very few colors. NOT a photograph."
                },
                "Futuristic isometric Tech Glow": {
                    "prefix": "Futuristic isometric 3D scene, neon glow on dark background, cyberpunk aesthetic, [NEON COLOR].",
                    "forbidden": ["photorealistic", "photograph", "camera", "Canon", "lens", "natural lighting", "skin pores", "film grain", "shot on"],
                    "instruction": "Every first_prompt must start with the isometric tech prefix above. Use dark backgrounds, neon accents, holographic elements, isometric angle."
                },
                "Soft 3d clay": {
                    "prefix": "Soft 3D clay render (claymation), smooth rounded shapes, matte pastel materials, [COLOR PALETTE].",
                    "forbidden": ["photorealistic", "photograph", "camera", "Canon", "lens", "depth of field", "skin pores", "film grain", "shot on", "real texture"],
                    "instruction": "Every first_prompt must start with the clay render prefix above. Pixar-like charm, rounded shapes, matte pastel materials. NOT a photograph."
                },
                "isometric soft vector": {
                    "prefix": "Isometric soft vector illustration, clean geometric perspective, pastel palette, [COLOR PALETTE].",
                    "forbidden": ["photorealistic", "photograph", "camera", "Canon", "lens", "depth of field", "bokeh", "skin pores", "film grain", "shot on"],
                    "instruction": "Every first_prompt must start with the isometric vector prefix above. Strict isometric angle, clean shapes, pastel colors. NOT a photograph."
                },
                "Paper Cut": {
                    "prefix": "Paper cut art (paper craft), layered cut paper effect, colorful craft textures, [COLOR PALETTE].",
                    "forbidden": ["photorealistic", "photograph", "camera", "Canon", "lens", "depth of field", "bokeh", "skin pores", "film grain", "shot on"],
                    "instruction": "Every first_prompt must start with the paper cut prefix above. Visible paper layers with shadows, craft textures, handmade feel. NOT a photograph."
                },
            }

            style_key = visual_style if visual_style in _style_scene_config else "Auto"
            style_cfg = _style_scene_config[style_key]
            style_prompt_prefix = style_cfg["prefix"]
            style_forbidden_csv = ", ".join(f'"{w}"' for w in style_cfg["forbidden"])
            style_instruction = style_cfg["instruction"]

            # Build reference image descriptions (include every Image 1-N so all are used correctly)
            ref_image_descriptions = []
            for i, img in enumerate(reference_images):
                if img.get('analysis'):
                    ref_image_descriptions.append(f"Reference Image {i+1}: {img['analysis'][:500]}")
                else:
                    ref_image_descriptions.append(f"Reference Image {i+1}: [Content not analyzed — use for scenes that match the video topic when this image can support the story]")
            
            ref_images_text = "\n".join(ref_image_descriptions) if ref_image_descriptions else "No reference images provided."
            ref_image_count = len(reference_images)
            
            # Build the prompt (Gemini version - creative scene integration)
            system_prompt = f"""You are a WORLD-CLASS visual storyteller who creates VIRAL UGC-style videos. Your videos get millions of views because every single frame is BREATHTAKING and tells part of a CAPTIVATING STORY that viewers cannot look away from.

*** #1 PRIORITY: TELL A GRIPPING, PRODUCT-RELEVANT STORY ***
The STORY is the single most important thing. But the story must be DIRECTLY CONNECTED to the product's real value proposition.
The narrative arc must be: RELATABLE PROBLEM → TENSION/DISCOVERY → SOLUTION (the product) → POSITIVE OUTCOME.

STORY GROUNDING RULES (CRITICAL — prevents weird/disconnected stories):
1. The story must be about a REAL, RELATABLE situation that the product/service actually solves. Read the product brief carefully — what problem does it solve? For whom? The story must be about THAT.
2. The scenes must follow a LOGICAL, REALISTIC progression — not abstract, surreal, or overly metaphorical. Think "a real person's journey" not "an artistic concept".
3. Each scene must connect to the NEXT one — cause and effect, not random beautiful shots strung together.
4. The viewer should understand by scene 2-3 what the story is about and feel invested in the outcome.
5. The story BUILDS TOWARD the product as a natural solution — the product is the answer to the problem shown in early scenes.
6. AVOID: overly simplified stories (stick figure logic), weird metaphors, surreal imagery disconnected from the product, abstract concepts that don't relate to the real benefit.

STORY QUALITY CHECKLIST — verify for EVERY scene:
1. Does this scene ADVANCE the story or deepen the emotion? (If not, rewrite it.)
2. Would a viewer swipe away, or think "what happens next?"
3. Is there a clear EMOTIONAL BEAT: tension, curiosity, wonder, relief, joy, desire?
4. Is the image SPECIFIC and VIVID — not generic or decorative?
5. Does the scene match EXACTLY what the VO says in this time window?
6. Is this scene REALISTIC and RELATABLE — would a real person experience this?

*** PRODUCT VISIBILITY RULES ***
- Hook and problem scenes (early scenes): Show the RELATABLE SITUATION or PROBLEM that the product solves. No product UI, logo, or brand visible — but the scene IS about the world the product operates in.
- Discovery/solution scenes (middle scenes): The product can appear here as the "aha moment" — this is where product screenshots or product reference images belong. The viewer sees what the solution looks like.
- Result/outcome scenes (late scenes): Show the POSITIVE RESULT of using the product. The product may or may not be visible.
- Last scene (CTA): CRITICAL — must be clearly connected to the video and the offer. Prefer a CLEAN CTA: no characters, professional ending card with a background that fits the product/offer (e.g. product world, benefit, brand feel). Only if the VO is explicitly a personal close ("call me", "message me", "I'm here to help") include the influencer; otherwise keep the CTA scene clean — no character. You MAY add one short phrase in {language_name} if it fits. Body scenes: NO visible text, NO logo. Never the person's name or service name as text in any image.
- Do NOT force the product UI or logo into scenes where the VO is about the problem or the situation — the product appears when the story reaches the SOLUTION.

*** VISUAL-AUDIO COHERENCE + PRECISE TIMING ***
The viewer must SEE what they HEAR at every moment. Each scene has EXACT timestamps—the image for scene N must depict ONLY what the VO says during that scene's time window:
- When a VO timing block is provided: read the VO text for EACH scene. Scene 1 first_prompt = what is said in scene 1 only. Scene 2 first_prompt = what is said in scene 2 only. One-to-one match. No generic or decorative shots.
- Design each first_prompt to visually depict EXACTLY the meaning of that scene's VO text. If the VO in scene 3 says "families looking for a bigger home", the image for scene 3 must show exactly that (e.g. a family in a small apartment). If the VO says "this place changed everything", the image must show the reaction or the place that changed things—not an unrelated beautiful shot.
- PRECISE TIMING: Your first_prompt for each scene must be tightly tied to that scene's words. Do NOT create generic shots that don't relate to what's being said.

*** SHARP MESSAGING – ONE CLEAR MESSAGE PER SCENE ***
Each scene must convey ONE clear message or emotional beat. Be specific and punchy—no vague or generic descriptions.
- Use concrete, vivid details that support that message. Avoid filler or decorative language.
- The video's core messages (from the content brief) must be clearly reflected: each scene should advance the story and reinforce the key points.
- Write first_prompt so a viewer could state in one sentence what the scene is saying.
- The offer/CTA only in the last scene when it fits — never forced. A satisfying story ending without on-screen CTA text is fine.

*** NARRATIVE ARC — PROBLEM → SOLUTION → OUTCOME ***
- The scenes form a single narrative arc tied to the PRODUCT'S VALUE. The story IS about what the product does — told through the lens of a real person's experience.
- Early scenes: the RELATABLE PROBLEM or SITUATION (no product visible, but about the world the product serves).
- Middle scenes: the DISCOVERY or TURNING POINT — this is where the product can visually appear (screenshot, UI, demo).
- Late scenes: the POSITIVE OUTCOME — show the result of using the product. Happy people, solved problems, better life.
- Last scene: CTA/closing moment.
- Every scene must ADVANCE the story. No filler. The viewer should think "what happens next?"

You generate image prompts for AI image generation and motion prompts for video animation.
The image AI receives the influencer's reference photo AND a reference image of the location/product. Your prompts must describe a BRAND NEW scene that COMBINES these into one cohesive, stunning image.

*** ABSOLUTELY CRITICAL - NO BRAND NAMES IN PROMPTS ***
NEVER use specific brand names, character names, or trademarked terms in your prompts. These WILL be blocked by the AI.
BAD: "Mickey Mouse", "Dumbo ride", "Disney castle", "Sleeping Beauty", "Space Mountain"
GOOD: "beloved cartoon character mascot", "colorful flying elephant ride", "fairytale pink castle with golden spires"
Always describe the VISUAL APPEARANCE instead of using the brand/character name.

*** SCENE 1 = THE HOOK ***
Scene 1 MUST stop the scroll INSTANTLY. The hook must be a stunning visual that serves the STORY — showing the relatable problem or an attention-grabbing moment from the product's world. No product UI/logo in the hook — but the scene IS relevant to what the product does.
HOOK TECHNIQUES:
- SHOCK/AWE: An extreme close-up of something STUNNING — glistening food, a magical view, an incredible detail
- MYSTERY: A hand reaching for something dramatic, an eye widening in surprise, a slow reveal
- SENSORY OVERLOAD: Hyper-saturated colors, extreme textures, steam/sparkle/glow
- EMOTION: Pure joy, shock, wonder captured in a split second
- UNEXPECTED ANGLE: Drone-style top-down, extreme low angle, through-glass reflection
The hook must make the viewer think: "What is THAT? I NEED to see more!"

*** EXPERIENTIAL STORYTELLING - MAKE THEM FEEL IT ***
Make the viewer FEEL like they are THERE — living the experience the product enables.
- SENSORY DETAILS: Describe textures you can almost touch, aromas you can almost smell
- EMOTIONAL BEATS: Each scene should evoke a DIFFERENT emotion: wonder, craving, excitement, joy, serenity
- FIRST PERSON FEELING: Compose shots as if the VIEWER is the one experiencing it
- DETAILS OVER OVERVIEWS: A close-up of steam rising from a dish > a wide shot of a restaurant
- CANDID OVER POSED: Capture moments that feel STOLEN, not staged

*** VISUAL STYLE: {style_key} ***
{style_instruction}
- Start every first_prompt with: "{style_prompt_prefix}"
- Think: "If this were a single Instagram post in this style, would it get 100K likes?" If not, make it more dramatic.

*** STYLE FORBIDDEN WORDS (NEVER use these in first_prompt) ***
Do NOT use any of these words/phrases: {style_forbidden_csv}.
Stay strictly within the {style_key} visual language.

*** SCENE VARIETY - MUST BE DIFFERENT ***
Each scene MUST use a DIFFERENT composition + lens + lighting + emotion:
1. EXTREME CLOSE-UP (100mm macro): Textures, food dripping, hands touching, sensory detail
2. MEDIUM SHOT (50-85mm): Influencer interacting with environment, waist-up, body language
3. WIDE ESTABLISHING (24mm): Full environment, architecture, sense of scale
4. POV / FIRST PERSON (35mm): Through the influencer's eyes
5. DRAMATIC ANGLE (24-35mm): Low angle, high angle, Dutch tilt, reflection
6. DETAIL INSERT (100mm macro): The "money shot" - a stunning detail
NO TWO SCENES should have the same composition, lens, or lighting setup.

*** MOTION/ANIMATION (second_prompt) — MATCH THE SCENE ROLE ***
Each scene has a narrative role (hook, problem, discovery, solution, outcome, cta). The motion must fit that role — not generic "slow movement" for every scene.
- **hook**: Dynamic, attention-grabbing — push-in, quick reveal, camera movement that pulls the viewer in. Energy.
- **problem**: Subtle tension — slow push, restrained motion, slight unease. Avoid busy motion.
- **discovery**: Building energy — camera moves toward subject, slight zoom or orbit, curiosity.
- **solution**: Clear, satisfying — smooth reveal, subject interacting with the solution, confident motion.
- **outcome**: Warm, calm — gentle push or pull, relaxed, positive energy.
- **cta**: Slow, minimal — so the viewer sees the full frame; subtle gesture, no distracting movement.
VARY camera movements by role. For influencer scenes: what they DO (never talk). Add ENVIRONMENTAL MOTION where it fits (steam, sparkle, leaves). NEVER mention brand or character names.

*** SMART INFLUENCER PLACEMENT BASED ON VO CONTENT ***
The influencer must NOT appear in every scene. WHEN to show the influencer depends on WHAT the VO says:

- When the VO talks about OTHER PEOPLE or their problems → shows_influencer = FALSE. Show THOSE people or that situation.
- When the VO speaks in FIRST PERSON about the influencer's own actions → shows_influencer = TRUE.
- When the VO describes a RESULT or TRANSFORMATION → shows_influencer = FALSE. Show the outcome.
- LAST SCENE (CTA) — CRITICAL: This scene must be clearly connected to the video and the offer. PREFER a clean CTA: no character (shows_influencer = FALSE), professional ending card with a background that fits the product/offer. Only set shows_influencer = TRUE when the VO is explicitly a personal close (e.g. "call me", "message me", "contact me", "I'm here to help"). Otherwise the CTA must be clean: no characters, just a strong visual tied to the offer (logo, product world, benefit, brand feel).

ADDITIONAL MIX GUIDELINES:
- Set shows_influencer FALSE for: establishing shots, detail shots, atmosphere, POV, scenes about other people, and for the last scene (CTA) unless the VO is clearly a personal "contact me" close.
- Set shows_influencer TRUE for: scenes clearly about the influencer themselves; and for the last scene ONLY when it is genuinely a personal CTA (influencer saying goodbye, inviting contact). When in doubt for CTA → keep it clean, no character.
- Body language in influencer scenes: curiosity, excitement, satisfaction—never passive.

*** RULES ***
1. Write prompts in English (for AI generation) but in {language_name} cultural context.
2. The influencer appearance must be IDENTICAL across all scenes.
3. All scenes vertical format (9:16).
4. NEVER show influencer holding phone, filming, or talking.
5. For influencer scenes: describe a NEW SCENE naturally integrating the influencer INTO the environment.
6. Use reference images as INSPIRATION for the environment - describe the visual elements WITHOUT using brand names.
7. Text only in the last scene (one short phrase in {language_name} if it fits). Body scenes: no text."""

            # Manual instructions addition
            manual_section = ""
            if manual_instructions:
                manual_section = f"\n\nADDITIONAL INSTRUCTIONS FROM USER:\n{manual_instructions}"
            
            # CTA section
            cta_section = ""
            if cta_text:
                cta_section = f"\n\nCTA TEXT (for conceptual reference in last scene): {cta_text}"
            
            # Existing influencer description section
            influencer_section = ""
            if existing_influencer_description:
                multi_note = ""
                if "Person 2:" in existing_influencer_description or "Person 2 :" in existing_influencer_description:
                    multi_note = "\nWhen multiple persons are described (Person 1, Person 2, ...), include ALL of them in scenes where shows_influencer is true; describe each in the scene prompt."
                influencer_section = f"""

IMPORTANT - USE THIS EXACT INFLUENCER DESCRIPTION:
The influencer(s) have been pre-defined. You MUST use this exact description in all influencer scenes:
"{existing_influencer_description}"
{multi_note}

Do NOT create a new influencer appearance. Copy this description exactly into the "influencer_description" field and use it in all scene prompts where shows_influencer is true."""
            
            # Build VO timing block for visual-audio coherence
            vo_timing_block = ""
            if vo_timing and vo_timing.get("scene_segments"):
                scene_segments = vo_timing["scene_segments"]
                vo_total = vo_timing.get("total_duration", 0)
                scene_vo_lines = []
                for seg in scene_segments:
                    scene_vo_lines.append(
                        f"  SCENE {seg['scene_num']} VO ({seg['start_time']:.1f}s - {seg['end_time']:.1f}s, "
                        f"~{seg['duration']:.1f}s):\n"
                        f"    \"{seg['text']}\""
                    )
                scene_vo_str = "\n\n".join(scene_vo_lines)
                vo_timing_block = f"""

=== VOICE-OVER AUDIO (ALREADY RECORDED – PRECISE TIMING REQUIRED) ===
A VO audio track ({vo_total:.1f}s) has ALREADY been recorded. Your output MUST have exactly {len(scene_segments)} scenes.
Each scene has EXACT timestamps. The image for scene N must depict ONLY what is said in that scene's time window—no other content.

PER-SCENE VO TEXT WITH EXACT TIMESTAMPS (match first_prompt 1:1 to each block):
{scene_vo_str}

ABSOLUTE RULE — WHAT YOU SEE = WHAT YOU HEAR:
For each scene number above, your first_prompt MUST depict EXACTLY what the VO says in that scene's time window:
- If the VO says "families struggling to find a home" → the image shows a family in a small apartment. NOT the influencer.
- If the VO says "I decided to help them" → the image shows the influencer in a helping/advisory moment.
- If the VO says "they found their dream home" → the image shows a happy family in a beautiful new home. NOT the influencer.
- If the VO says "call me today" → the image shows the influencer with a warm CTA gesture.

ALSO: shows_influencer must match the VO content. If the VO talks about OTHER people → shows_influencer = false (show those people/situation). If the VO talks about THE INFLUENCER → shows_influencer = true.

Do NOT use generic beauty shots that ignore the VO. The viewer must SEE what they HEAR at EVERY SECOND. This is the #1 priority.
"""
            elif vo_timing and vo_timing.get("full_text"):
                vo_timing_block = f"""

=== VOICE-OVER AUDIO (ALREADY RECORDED – YOUR SCENES MUST MATCH IT) ===
A VO audio track ({vo_timing.get('total_duration', 0):.1f}s, {vo_timing.get('word_count', 0)} words) has ALREADY been recorded.

VO TRANSCRIPT:
\"{vo_timing['full_text']}\"

CRITICAL: Split this VO text across your {scene_count} scenes. Each scene's first_prompt MUST visually 
illustrate what the VO says during that scene. The viewer must SEE what they HEAR.
"""
            
            user_prompt = f"""Generate prompts for a {scene_count}-scene UGC influencer video that will GO VIRAL.{influencer_section}

PRODUCT/EXPERIENCE TO PROMOTE:
{free_text[:3000]}
{vo_timing_block}
SCENE PLAN (build a CAPTIVATING, PRODUCT-RELEVANT STORY — problem → solution → outcome):
- STORY = PRODUCT'S VALUE TOLD AS A NARRATIVE: The story must be DIRECTLY about the real problem the product solves and the real benefit it provides. NOT abstract or surreal. Think: "a real person's journey with this product, told cinematically."
- NARRATIVE ARC: Early scenes show the RELATABLE PROBLEM or SITUATION (no product UI/logo visible, but the scene IS about the world the product serves). Middle scenes show the DISCOVERY or TURNING POINT — product screenshots/UI can appear here as the "aha moment". Late scenes show the POSITIVE OUTCOME. Last scene = CTA.
- ONE CLEAR MESSAGE PER SCENE, TIED TO THE VO: For each scene, the first_prompt must depict exactly what the VO says in that scene's time window. No generic shots. Match scene N to VO scene N. Same order, same meaning.
- SMART INFLUENCER PLACEMENT (based on VO): set shows_influencer TRUE only when the VO talks about the influencer themselves (first person: "I...", "my..."). When the VO talks about other people → shows_influencer = FALSE, show those people/situation instead.
- Scene 1 = THE HOOK: A stunning visual that matches what the VO says in scene 1 — showing the relatable situation or an attention-grabbing moment. No product UI/logo in the hook, but the scene IS about the product's world.
- Middle scenes: Each first_prompt must illustrate that scene's VO text. Show the PEOPLE or SITUATION described in the VO. When the VO reveals the product/solution, you CAN show the product. The influencer appears only when the VO is about them.
- Last scene (Scene {scene_count}) — CTA, MUST connect to the video and offer: Prefer a CLEAN ending — no character (shows_influencer = false), professional card with background that fits the product/offer. Set shows_influencer = true ONLY when the VO is explicitly personal ("contact me", "message me", "call me"). first_prompt must describe a visual that clearly ties to THIS video's offer (e.g. product category, benefit, brand world). When the influencer appears, use a WIDE or MEDIUM shot. No service or person name as text in the image.{manual_section}{cta_section}

IMAGE AND CHARACTER PLACEMENT LOGIC (CRITICAL — think before placing):
You have {ref_image_count} reference image(s) (Image 1, Image 2, Image 3, Image 4{", Image 5" if ref_image_count >= 5 else ""} as provided) with descriptions below, AND a VO script split into scenes. For EACH scene you must decide:

A) REFERENCE IMAGE PLACEMENT ("reference_image_index"):
- Use the reference images — do not leave them unused when they can support a scene. Prefer to assign each of Image 1 to Image {ref_image_count} to at least one scene where it fits the VO and the story.
- Read the VO text for this scene AND each reference image description below.
- If a reference image MATCHES what the VO describes (e.g. VO says "we sat at this restaurant" and Image 2 shows a restaurant interior) → set reference_image_index to that image (0-based).
- If NO reference image fits what the VO says in this scene → set reference_image_index to null. The system will generate a fresh image purely from your prompt. Do NOT force a reference image into a scene where it does not fit the story.
- Can reuse images across scenes when relevant. A reference image can appear in multiple scenes if the story calls for it.

*** CRITICAL — IMAGE TYPE PLACEMENT RULES ***
Each reference image below has a [TYPE] tag. You MUST follow these placement rules:

[PRODUCT_SCREENSHOT] or [PRODUCT_PHOTO] images:
  → Use ONLY in scenes that present the SOLUTION, DISCOVERY, or DEMONSTRATION of the product.
  → This is typically the scene where the VO reveals the answer/product — the "aha moment" (usually middle-to-late scenes).
  → NEVER use product images in: the HOOK (scene 1), PROBLEM scenes, or scenes describing a negative situation.
  → The product image should appear at the moment of DISCOVERY or REVEAL — when the story shifts from problem to solution.

[LOCATION], [LIFESTYLE], [FOOD], [PERSON], [OTHER] images:
  → These show environments, experiences, and people. Use them in ANY scene where they match the VO content.
  → They CAN appear in early scenes, problem scenes, hook scenes — wherever the setting is relevant.
  → Match the image to the MOOD and CONTENT of the VO for that specific scene.

STORY ARC → IMAGE TYPE MAPPING:
  Hook/Problem scenes (early) → LOCATION/LIFESTYLE/FOOD images or generate fresh. NEVER product images here.
  Discovery/Solution scenes (middle) → This is where PRODUCT images belong — the moment the viewer sees the answer.
  Result/Payoff scenes (late) → LOCATION/LIFESTYLE showing the positive outcome. PRODUCT images can also work here.
  CTA scene (last) → Logo/brand moment (handled separately).

REFERENCE IMAGE DESCRIPTIONS:
{ref_images_text}

B) CHARACTER PLACEMENT ("shows_influencer"):
- The character/influencer should appear ONLY when it makes STORY SENSE — not forced into every scene.
- When the VO talks about the character themselves (first person: "I...", "my...", "we...") → shows_influencer = TRUE.
- When the VO talks about OTHER people, abstract concepts, or situations → shows_influencer = FALSE. Show THOSE people/situations.
- Flashback scenes about the past or about other people's stories → shows_influencer = FALSE.
- LAST SCENE (CTA): Must be connected to the video and the offer. Prefer CLEAN CTA: no character (shows_influencer = false), ending card with background that fits the offer. Only set shows_influencer = true when the VO is explicitly a personal close ("contact me", "message me"). Otherwise clean CTA — no characters.
- If the story is about multiple characters, include ALL of them when they are part of the narrative.

C) COMBINING IMAGES AND CHARACTERS:
- When shows_influencer = TRUE AND a reference image fits → the image generator will combine the character reference with the environment reference. Your prompt should describe the character IN that environment.
- When shows_influencer = FALSE AND a reference image fits → just the environment/situation, no character.
- When shows_influencer = TRUE AND reference_image_index = null → the character in a generated scene matching the VO.
- When shows_influencer = FALSE AND reference_image_index = null → a generated scene matching the VO, no character.

IMPORTANT - DESCRIBE VISUALS, NOT BRANDS:
When inspired by reference images, describe what you SEE (colors, shapes, textures, atmosphere) not what it IS by name.
Example: Instead of "the iconic Mickey Mouse parade float", write "a dazzling golden parade float covered in shimmering lights, vibrant flowers, and whimsical sculptures, with a festive crowd cheering in the background"

TEXT RULES: Body scenes (1 to {scene_count - 1}): no on-screen text, no logo, no brand name. Last scene only: you MAY add one short phrase in {language_name} if it fits.

*** PER-SCENE TYPE — WRITE SMARTER PROMPTS BY ROLE ***
For each scene, set "narrative_role" to exactly one of: "hook", "problem", "discovery", "solution", "outcome", "cta".
Then tailor first_prompt and second_prompt to that role:

- **hook** (Scene 1): first_prompt = attention-grabbing, visceral, one clear striking image. second_prompt = dynamic: push-in or quick reveal, energy, movement that pulls the viewer in.
- **problem**: first_prompt = relatable struggle, tension, what's wrong. second_prompt = subtle tension: slow push, slight unease, minimal movement or restrained motion.
- **discovery**: first_prompt = the turn, the moment of finding. second_prompt = building energy: camera moves toward the subject, slight zoom or orbit, curiosity.
- **solution**: first_prompt = the product/answer in use or on display. second_prompt = clear, confident motion: smooth reveal, subject interacting with the solution, satisfying movement.
- **outcome**: first_prompt = positive result, relief, joy. second_prompt = warm, relaxed motion: gentle push or pull, calm, positive energy.
- **cta** (last scene): first_prompt = clean closing card connected to the offer (no character unless VO is personal "contact me"). Background that fits the product/offer. second_prompt = slow, stable, minimal motion; if no character, calm environmental or abstract motion only.

FOR EACH SCENE (every image must match what the VO says in that scene's time window—precise timing):
1. **narrative_role**: One of hook, problem, discovery, solution, outcome, cta — based on this scene's place in the story and the VO content.
2. **first_prompt**: Read this scene's VO text AND its narrative_role. Depict EXACTLY what is being said. Start with "{style_prompt_prefix}". Then describe a VIVID, SENSORY scene that fits the role (hook = one striking image, problem = tension, solution = the answer visible, etc.). Minimum 4 sentences. Use ONLY words consistent with the {style_key} style — NEVER use these forbidden words: {style_forbidden_csv}. Set shows_influencer based on WHO the VO is about. Follow the IMAGE TYPE PLACEMENT RULES above. Body scenes: no text, no logo. Only in the LAST scene you may include one short phrase in {language_name} if it fits.
3. **second_prompt**: Match the narrative_role: hook = dynamic movement; problem = subtle tension; discovery = building energy; solution = clear, satisfying motion; outcome = warm, calm; cta = slow, minimal. Different camera movement per scene. For influencer scenes: what they DO (never talk). NEVER use brand or character names.

RESPONSE FORMAT (JSON):
{{
    "influencer_description": "DETAILED physical description - face, hair, body type, skin tone, style",
    "scene_prompts": [
        {{
            "scene_number": 1,
            "narrative_role": "hook",
            "shows_influencer": true/false,
            "reference_image_index": 0,
            "first_prompt": "...",
            "second_prompt": "..."
        }}
    ]
}}

Now create {scene_count} BREATHTAKING scenes. Scene 1 must be the most VISUALLY STUNNING hook imaginable:"""

            # Build Vertex AI payload
            payload = {
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": user_prompt}]
                    }
                ],
                "systemInstruction": {
                    "parts": [{"text": system_prompt}]
                },
                "generationConfig": {
                    "temperature": 0.7,
                    "maxOutputTokens": 16000,
                    "responseMimeType": "application/json"
                }
            }
            
            url = self._get_vertex_url(self.model)
            response = self._vertex_post_with_retry(
                url,
                headers=self._get_vertex_headers(),
                json_payload=payload
            )
            response.raise_for_status()
            
            api_result = response.json()
            self.last_usage_metadata = api_result.get("usageMetadata")
            candidates = api_result.get("candidates", [])
            if not candidates:
                logger.error("❌ Gemini returned no candidates for influencer prompts")
                return {"influencer_description": "", "scene_prompts": []}
            
            result_text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
            
            # Try to parse JSON, with fallback for truncated responses
            try:
                result = json.loads(result_text)
            except json.JSONDecodeError as je:
                logger.warning(f"⚠️ JSON parse error, attempting to fix truncated response: {je}")
                result = self._fix_truncated_scene_json(result_text)
                if not result:
                    logger.error(f"❌ Could not fix JSON response. Raw text (first 500 chars): {result_text[:500]}")
                    return {"influencer_description": "", "scene_prompts": []}
            
            # Validate and enhance prompts with influencer description
            influencer_desc = existing_influencer_description if existing_influencer_description else result.get("influencer_description", "")
            scene_prompts = result.get("scene_prompts", [])
            
            # Ensure influencer description is embedded in all influencer scenes
            for prompt in scene_prompts:
                if prompt.get("shows_influencer", False) and influencer_desc:
                    original_prompt = prompt.get("first_prompt", "")
                    prompt["first_prompt"] = f"INFLUENCER: {influencer_desc}. SCENE: {original_prompt}"
            
            logger.info(f"✅ Generated {len(scene_prompts)} influencer scene prompts (via Gemini)")
            return {
                "influencer_description": influencer_desc,
                "scene_prompts": scene_prompts
            }
            
        except Exception as e:
            logger.error(f"❌ Failed to generate influencer prompts (Gemini): {e}")
            return {
                "influencer_description": "",
                "scene_prompts": []
            }

    def _fix_truncated_scene_json(self, text: str) -> Optional[Dict]:
        """Fix a truncated JSON response from Gemini for scene prompts.
        
        This robustly handles cases where the JSON is cut off mid-string, mid-object, 
        or mid-array. It tries multiple strategies to recover as many complete scenes 
        as possible.
        """
        import re
        
        # Strategy 1: Find all complete scene objects and reconstruct
        try:
            # Extract influencer_description if present
            desc_match = re.search(r'"influencer_description"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
            influencer_desc = desc_match.group(1) if desc_match else ""
            
            # Find all complete scene objects with regex (narrative_role optional)
            complete_scenes = []
            scene_pattern = re.compile(
                r'\{\s*"scene_number"\s*:\s*(\d+)\s*,\s*'
                r'(?:"narrative_role"\s*:\s*"([^"]*)"\s*,\s*)?'
                r'"shows_influencer"\s*:\s*(true|false)\s*,\s*'
                r'"reference_image_index"\s*:\s*(-?\d+|null)\s*,\s*'
                r'"first_prompt"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,\s*'
                r'"second_prompt"\s*:\s*"((?:[^"\\]|\\.)*)"\s*\}',
                re.DOTALL
            )
            
            for match in scene_pattern.finditer(text):
                narrative_role = match.group(2) if match.lastindex >= 2 and match.group(2) else ""
                scene = {
                    "scene_number": int(match.group(1)),
                    "shows_influencer": match.group(3) == "true",
                    "reference_image_index": None if match.group(4) == "null" else int(match.group(4)),
                    "first_prompt": match.group(5),
                    "second_prompt": match.group(6)
                }
                if narrative_role:
                    scene["narrative_role"] = narrative_role
                complete_scenes.append(scene)
            
            if complete_scenes:
                logger.info(f"✅ Recovered {len(complete_scenes)} complete scenes from truncated JSON")
                return {
                    "influencer_description": influencer_desc,
                    "scene_prompts": complete_scenes
                }
        except Exception as e:
            logger.debug(f"Strategy 1 failed: {e}")
        
        # Strategy 2: Try brute-force closing brackets/braces
        try:
            fixed_text = text.rstrip()
            # Check if we're inside a string (odd number of unescaped quotes)
            in_string = False
            i = 0
            while i < len(fixed_text):
                if fixed_text[i] == '\\' and in_string:
                    i += 2
                    continue
                if fixed_text[i] == '"':
                    in_string = not in_string
                i += 1
            
            if in_string:
                fixed_text += '"'
            
            open_braces = fixed_text.count('{') - fixed_text.count('}')
            open_brackets = fixed_text.count('[') - fixed_text.count(']')
            fixed_text += ']' * max(0, open_brackets) + '}' * max(0, open_braces)
            
            result = json.loads(fixed_text)
            logger.info("✅ Fixed truncated JSON by closing brackets/braces")
            return result
        except json.JSONDecodeError:
            pass
        
        # Strategy 3: Truncate to last complete scene and close
        try:
            last_complete = text.rfind('"second_prompt"')
            if last_complete > 0:
                after_key = text.find(':', last_complete) + 1
                open_q = text.find('"', after_key)
                if open_q > 0:
                    pos = open_q + 1
                    while pos < len(text):
                        if text[pos] == '\\':
                            pos += 2
                            continue
                        if text[pos] == '"':
                            truncated = text[:pos + 1] + '}]}'
                            try:
                                result = json.loads(truncated)
                                logger.info("✅ Fixed truncated JSON by finding last complete scene")
                                return result
                            except json.JSONDecodeError:
                                break
                        pos += 1
        except Exception as e:
            logger.debug(f"Strategy 3 failed: {e}")
        
        return None

    def generate_influencer_vo_script(
        self,
        free_text: str,
        scene_count: int,
        target_duration: float,
        manual_instructions: str = "",
        language: str = "en",
        original_vo_transcript: str = "",
        raw_prompt: str = "",
        text_4: str = ""
    ) -> str:
        """Generate a first-person, scene-structured voice-over script for an influencer video using Gemini.
        
        Generates VO text organized by scenes (separated by '|||') so each scene's VO 
        segment has natural break points aligned with the visual narrative.
        
        Args:
            free_text: Content describing the product/experience (parsed TEXT 1/2/3).
            scene_count: Number of scenes for pacing reference.
            target_duration: Target duration in seconds.
            manual_instructions: Optional custom instructions.
            language: ISO 639-1 language code.
            original_vo_transcript: The original video's VO for style matching.
            raw_prompt: The original raw prompt from the user (for maximum context).
            text_4: Video structure / scene-by-scene breakdown (used to structure VO by scenes).
            
        Returns:
            Voice-over script text suitable for TTS, with '|||' separating scene segments.
        """
        try:
            logger.info(f"🎤 Generating influencer VO script via Gemini (target: {target_duration:.1f}s, language: {language})...")
            
            target_words = int(target_duration * 2.5)
            min_words = max(int(target_words * 0.7), 15)
            
            language_names = {
                "en": "English", "de": "German", "es": "Spanish", "fr": "French",
                "it": "Italian", "pt": "Portuguese", "nl": "Dutch", "ru": "Russian",
                "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "ar": "Arabic",
                "pl": "Polish", "hu": "Hungarian", "cs": "Czech", "sk": "Slovak",
                "ro": "Romanian", "bg": "Bulgarian", "uk": "Ukrainian", "hr": "Croatian",
                "el": "Greek", "tr": "Turkish", "fi": "Finnish", "sv": "Swedish",
                "no": "Norwegian", "da": "Danish", "he": "Hebrew", "fa": "Persian",
                "hi": "Hindi", "th": "Thai", "vi": "Vietnamese", "id": "Indonesian",
                "pt-BR": "Brazilian Portuguese", "zh-CN": "Simplified Chinese",
                "en-US": "American English", "en-GB": "British English"
            }
            language_name = language_names.get(language, "English")
            
            style_guidance = ""
            if original_vo_transcript and len(original_vo_transcript) > 20:
                style_guidance = f"""
STYLE REFERENCE (match tone and energy):
"{original_vo_transcript[:1000]}"
"""
            else:
                style_guidance = """
STYLE: Authentic influencer voice - the speaker is the influencer sharing their personal experience in first person. Genuine, relatable, conversational, as if recommending to a friend ("I went there", "I tried it", "my favorite").
"""
            
            voice_style = ""
            if manual_instructions:
                if "third person" in manual_instructions.lower():
                    voice_style = "Override: Use third person narrator style."
                elif "narrator" in manual_instructions.lower():
                    voice_style = "Override: Use professional narrator style."
            
            # Build raw prompt section if available
            raw_prompt_section = ""
            if raw_prompt and raw_prompt.strip():
                raw_prompt_section = f"""
ORIGINAL USER PROMPT (the raw input about what this video is about):
"{raw_prompt.strip()[:2000]}"
"""
            
            system_prompt = f"""You are a voice-over scriptwriter for UGC influencer videos.

############################################
#  RULE #1 — LENGTH IS NON-NEGOTIABLE     #
############################################
The video is {target_duration:.0f} seconds long. At ~2.5 spoken words per second the VO MUST contain **at least {target_words} words**.
- 30-second video → ~75 words
- 45-second video → ~112 words
- 60-second video → ~150 words
- 90-second video → ~225 words
If you write fewer than {min_words} words your output will be REJECTED. Count your words before finishing. Pad with vivid detail rather than cutting short.

############################################
#  RULE #2 — FIRST PERSON ONLY            #
############################################
The script is the INFLUENCER telling their OWN experience to the camera, like sharing with a friend.
- Use I / me / my / I tried / I went / I loved / I recommend throughout.
- NEVER use third person (they, the product, one could). The speaker IS the influencer.

############################################
#  RULE #3 — CAPTIVATING STORY (EMPHASIS)  #
############################################
- EMPHASIS IS ON TELLING A STORY. Do NOT repeat the professional's name or the service provider's name in every scene. Mention the name at most once (e.g. in the final CTA: "contact X" or "reach out to..."). The rest of the script = the story: problem, journey, emotion, discovery.
- Hook (first 5 seconds): one line that grabs attention.
- Build: setup → discovery → peak moment → payoff. Each part pulls the listener to the next.
- Use vivid, specific details—sights, sounds, feelings.
- End with desire and a clear "you have to try this" feeling.
- Write one continuous flowing monologue, NOT a bullet-point list. Story first, name at the end.

############################################
#  RULE #4 — EXACTLY N SEGMENTS (CRITICAL) #
############################################
You MUST output exactly {scene_count} segments, separated by '|||' (three pipe characters). Not fewer, not more.
- Each segment = one visual scene. The images/video will be generated to match each segment.
- What is SAID in each segment must clearly relate to what the viewer will SEE in that scene.
- Count your segments before finishing: segment 1, segment 2, ... segment {scene_count}.
Example for 3 scenes: "First part of VO... ||| Second part... ||| Final part with CTA..."

############################################
#  RULE #5 — COMPLETE ENDING (MANDATORY)   #
############################################
The LAST segment MUST be a complete, satisfying closing sentence. The video must not feel cut off.
- End with a clear CTA (e.g. "call now", "try it", "you have to see it") or a satisfying conclusion.
- The last segment must end with proper punctuation (. ! ?). Never end mid-sentence or mid-thought.
- If you run out of space, shorten earlier segments—but the ending must always be complete.

ABSOLUTE RULE: Every sentence MUST be about the product/experience described in the user content. STAY ON TOPIC.

Language: {language_name}. Write naturally in {language_name}. ALWAYS write the FULL {target_words}-word script — NEVER cut short.
{"HEBREW: Do NOT use full nikud (ניקוד מלא) on every word. Write Hebrew without vowel points. Only add Masoretic-style nikud (e.g. dagesh בּ כּ פּ) on letters that can be pronounced in more than one way and where nikud is needed to disambiguate. Otherwise use unpointed Hebrew (כתיב ללא ניקוד)." if language and language.lower().startswith("he") else ""}"""
            
            # Build TEXT 4 scene structure section
            text4_section = ""
            if text_4 and text_4.strip():
                text4_section = f"""
--- VIDEO SCENE STRUCTURE (structure your VO around these scenes) ---
{text_4.strip()}
Write one VO segment per scene above, separated by '|||'. Each segment's content must match what that scene is about.
"""
            
            user_prompt = f"""⚠️ LENGTH: This is a {target_duration:.0f}-SECOND video. Write AT LEAST {target_words} words (minimum {min_words}). Shorter = REJECTED.

--- CONTENT (the VO must be 100% about THIS) ---
{raw_prompt_section}
{free_text[:2000]}
{text4_section}
{style_guidance}
{f"SPECIAL INSTRUCTIONS: {manual_instructions}" if manual_instructions else ""}
{voice_style}

--- FORMAT ---
- Language: {language_name}
- First person only (I / me / my)
- You MUST write exactly {scene_count} segments separated by '|||'. Each segment = one scene. Count them.
- The LAST segment must be a complete closing sentence (CTA or conclusion). Do NOT end mid-sentence.
- NO stage directions like (pauses). Use ElevenLabs Audio Tags: [excited], [whispers], [laughs], [gasps], [pause], [softly], [dramatically], [sighs], [happily], [awe]
- Embed 4-6 Audio Tags naturally throughout
- Output ONLY the spoken script + Audio Tags + ||| separators, nothing else

--- STRUCTURE ---
Hook (5s) → Story build → Peak moment → Payoff/CTA (complete ending)
Exactly {scene_count} segments separated by |||. Last segment = satisfying, complete ending. Passionate, genuine, emotional.
Do NOT put the professional's name or service provider's name in every segment. Tell the STORY in each segment; mention the name only once at the end (CTA) if at all. Emphasis = storytelling, not name-dropping.

⚠️ REMINDER: {target_duration:.0f}-second video = {target_words} words minimum. Exactly {scene_count} segments. Ending must be complete. Write the full script now.
{"HEBREW: No full nikud. Use nikud only on letters that have more than one pronunciation (e.g. בּ/ב, כּ/כ, פּ/פ in Masoretic style). Rest: unpointed Hebrew." if language and language.lower().startswith("he") else ""}"""

            # Single Vertex call — no follow-up generations for length/segments (matches monolith policy).
            request_timeout = 120
            payload = {
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": user_prompt}]
                    }
                ],
                "systemInstruction": {
                    "parts": [{"text": system_prompt}]
                },
                "generationConfig": {
                    "temperature": 0.7,
                    "maxOutputTokens": 8192,
                }
            }

            url = self._get_vertex_url(self.model)
            response = None
            for _tries in range(2):  # Transport retry only (timeout / transient HTTP)
                try:
                    response = requests.post(
                        url, headers=self._get_vertex_headers(), json=payload, timeout=request_timeout
                    )
                    response.raise_for_status()
                    break
                except (requests.exceptions.Timeout, requests.exceptions.RequestException) as req_err:
                    if _tries == 0:
                        logger.warning(
                            "⚠️ VO script request failed (timeout/error): %s. Retrying once (%ss timeout)...",
                            req_err,
                            request_timeout,
                        )
                    else:
                        raise
            if response is None:
                raise RuntimeError("No response from VO script request")

            api_result = response.json()
            self.last_usage_metadata = api_result.get("usageMetadata")
            candidates = api_result.get("candidates", [])
            if not candidates:
                logger.error("❌ Gemini returned no candidates for VO script")
                return ""

            script = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()

            # Clean up stage directions but KEEP ElevenLabs v3 Audio Tags [excited], [whispers], [laughs] etc.
            script = re.sub(r'\[Scene\s*\d+\]', '', script, flags=re.IGNORECASE)
            script = re.sub(r'\(.*?\)', '', script)
            script = script.strip()

            word_count = len(script.split())
            segments = [s.strip() for s in script.split("|||") if s.strip()]
            if word_count < min_words:
                logger.warning(
                    "⚠️ VO script below soft minimum (%d/%d words, target ~%d for %.0fs); using first response as-is",
                    word_count,
                    min_words,
                    target_words,
                    target_duration,
                )
            logger.info(
                "✅ Generated influencer VO script (single pass): %d words, %d segments "
                "(target ~%d words, %d scenes, %.0fs) [via Gemini]",
                word_count,
                len(segments),
                target_words,
                scene_count,
                target_duration,
            )
            logger.info("📝 VO Script preview: %s...", script[:200])
            return script
            
        except Exception as e:
            logger.error(f"❌ Failed to generate influencer VO script (Gemini): {e}")
            return ""

    def generate_music_description_from_text(self, content_text: str, vo_script: str = "") -> str:
        """Generate a music description based on content text and VO script using Gemini.
        
        Args:
            content_text: The free text content describing the product/experience.
            vo_script: Optional voice-over script text. Music mood MUST match the VO tone and emotional arc.
            
        Returns:
            A detailed music style description for Suno generation.
        """
        try:
            logger.info("🎵 Generating music description for influencer mode (via Gemini)...")
            
            system_prompt = "You are a professional music director for social media content. You describe trendy, engaging background music for influencer videos. Always instrumental only, no vocals. The music MUST match the emotional tone and arc of the voice-over script."
            
            vo_section = ""
            if vo_script and len(vo_script.strip()) > 20:
                vo_section = f"""

VOICE-OVER SCRIPT (the music plays BEHIND this narration — the mood MUST match):
{vo_script[:2000]}

CRITICAL: Read the VO above carefully. The music must match its emotional arc:
- If the VO starts with a problem/tension → music should feel slightly tense or curious at first
- If the VO builds to a positive discovery → music should build and become uplifting
- If the VO is warm and personal → music should be warm and intimate
- If the VO is energetic and excited → music should be energetic
- The music mood must SUPPORT the VO, not contradict it. If the VO is emotional and personal, do NOT describe generic "upbeat pop" — describe music that matches THAT specific emotion."""
            
            user_prompt = f"""Based on this content and voice-over, describe the perfect background music for an influencer recommendation video.

CONTENT:
{content_text[:1500]}
{vo_section}

Generate a detailed music description that includes:
1. Genre/style that matches the VO mood (e.g., warm acoustic, emotional piano, upbeat pop, modern electronic)
2. Tempo (fast/medium/slow — match the VO energy and pacing)
3. Mood/emotion (must match the VO emotional arc — NOT generic)
4. Key instruments to feature
5. Overall vibe that supports the VO narration

The music MUST be:
- INSTRUMENTAL (absolutely no vocals or singing)
- Suitable as background music (not overpowering the VO)
- Matching the EMOTIONAL TONE of the voice-over script
- Supporting the story arc: tension → discovery → resolution → CTA

Respond with ONLY the music description in 2-3 sentences, nothing else. Be specific and creative."""

            payload = {
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": user_prompt}]
                    }
                ],
                "systemInstruction": {
                    "parts": [{"text": system_prompt}]
                },
                "generationConfig": {
                    "temperature": 0.7,
                    "maxOutputTokens": 250
                }
            }
            
            url = self._get_vertex_url(self.model)
            response = requests.post(url, headers=self._get_vertex_headers(), json=payload, timeout=30)
            response.raise_for_status()
            
            api_result = response.json()
            self.last_usage_metadata = api_result.get("usageMetadata")
            candidates = api_result.get("candidates", [])
            if not candidates:
                logger.warning("⚠️ Gemini returned no candidates for music description")
                return "upbeat trendy electronic music, modern synths with punchy drums, energetic and positive vibe, social media style, no vocals"
            
            description = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
            logger.info(f"🎵 Generated music description: {description}")
            return description
            
        except Exception as e:
            logger.warning(f"⚠️ Could not generate music description (Gemini): {e}")
            return "upbeat trendy electronic music, modern synths with punchy drums, energetic and positive vibe, social media style, no vocals"

    def _character_image_uri_for_vertex(self, image_url: str) -> Optional[str]:
        """Convert image URL to a URI Vertex AI accepts (gs://). Returns None if not GCS."""
        u = image_url.strip()
        if u.startswith("gs://"):
            return u
        if u.startswith("https://storage.googleapis.com/"):
            path = u.replace("https://storage.googleapis.com/", "")
            return f"gs://{path}"
        return None

    def describe_character(self, image_url: str) -> Optional[str]:
        """Analyze a character image and return a brief description.
        
        This is used to understand the character's appearance so it can be
        consistently included in scene generation prompts. A reference image
        of the character will be attached to scene generation requests.
        
        Args:
            image_url: URL of the character image (https, gs://, or storage.googleapis.com).
            
        Returns:
            Brief 1-2 sentence description of the character, or None if failed.
        """
        if not self.initialized:
            logger.warning("⚠️ Gemini not initialized, cannot describe character")
            return None
        
        logger.info("🧑 Analyzing character image with Gemini...")
        
        system_prompt = """Describe the person or people in this image in 1-2 sentences each.
Focus on the most distinctive visual features only (hair color/style, clothing color, gender/age if obvious).
If there is ONE person: output a single brief description.
If there are MULTIPLE people in the same image: describe each as "Person 1: ... Person 2: ..." (one short phrase per person).
A reference of this/these character(s) will be attached to the scene generation prompt, so keep descriptions very brief.
Output ONLY the brief description(s), no JSON, no bullet points, no detailed breakdown."""
        
        def build_parts_with_image(image_part: dict) -> list:
            return [
                image_part,
                {"text": "Briefly describe the person or people in this image. If multiple people: use 'Person 1: ... Person 2: ...'. Focus only on the most obvious visual features (hair, clothing, gender/age)."}
            ]
        
        try:
            # Prefer fetching http/https URLs (including storage.googleapis.com) as inlineData so Vertex
            # does not need GCS access (avoids 404 when object path has %20 or different encoding).
            parts = None
            if image_url.startswith("http://") or image_url.startswith("https://"):
                try:
                    fetch_headers = {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        "Accept": "image/*,*/*;q=0.8",
                    }
                    img_resp = requests.get(image_url.strip(), headers=fetch_headers, timeout=30)
                    img_resp.raise_for_status()
                    ct = img_resp.headers.get("Content-Type", "").lower()
                    mime = "image/png" if "png" in ct else "image/webp" if "webp" in ct else "image/jpeg"
                    b64 = base64.b64encode(img_resp.content).decode("utf-8")
                    parts = build_parts_with_image({
                        "inlineData": {"mimeType": mime, "data": b64}
                    })
                    logger.info("   Using character image from Character column (fetched as inline)")
                except Exception as fetch_err:
                    logger.warning(f"   Could not fetch character image for inline: {fetch_err}")
            
            if not parts:
                # Fallback: gs:// URI for Vertex (only when URL is gs:// or when http fetch failed)
                file_uri = self._character_image_uri_for_vertex(image_url)
                if file_uri:
                    parts = build_parts_with_image({
                        "fileData": {"mimeType": "image/jpeg", "fileUri": file_uri}
                    })
                    logger.info(f"   Using GCS URI for character image: {file_uri[:60]}...")
            
            if not parts:
                logger.warning("   Character image URL not supported (use https or gs://)")
                return None
            
            payload = {
                "contents": [{"role": "user", "parts": parts}],
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "generationConfig": {"temperature": 0.3, "maxOutputTokens": 250}
            }
            
            url = self._get_vertex_url(self.model)
            response = requests.post(
                url,
                headers=self._get_vertex_headers(),
                json=payload,
                timeout=60
            )
            response.raise_for_status()
            
            result = response.json()
            self.last_usage_metadata = result.get("usageMetadata")

            candidates = result.get("candidates", [])
            if candidates:
                content_parts = candidates[0].get("content", {}).get("parts", [])
                for part in content_parts:
                    if "text" in part:
                        description = part["text"].strip()
                        logger.info(f"✅ Character described: {description[:100]}...")
                        return description
            
            # Log why we got no description (safety block, etc.)
            prompt_feedback = result.get("promptFeedback", {})
            block_reason = prompt_feedback.get("blockReason") or prompt_feedback.get("blockReasonMessage")
            if block_reason:
                logger.warning(f"⚠️ No character description: API blocked response (reason: {block_reason})")
            else:
                logger.warning("⚠️ No character description in response (empty candidates or no text part)")
            return None
            
        except requests.exceptions.HTTPError as e:
            err_body = ""
            try:
                err_body = e.response.text[:300] if e.response is not None else ""
            except Exception:
                pass
            logger.error(f"❌ Error describing character (HTTP): {e} | {err_body}")
            return None
        except Exception as e:
            logger.error(f"❌ Error describing character: {e}")
            return None
    
    def describe_characters(self, image_urls: List[str]) -> Optional[str]:
        """Describe one or more character images; returns a single combined description.
        
        For a single URL, returns the same as describe_character(url).
        For multiple URLs, describes each and returns "Person 1: ... Person 2: ...".
        Used when the Character column contains multiple people (comma/newline separated).
        
        Args:
            image_urls: List of character image URLs (http/https/gs://).
            
        Returns:
            Combined description string, or None if all failed or list empty.
        """
        if not image_urls:
            return None
        if len(image_urls) == 1:
            return self.describe_character(image_urls[0])
        descriptions = []
        for i, url in enumerate(image_urls, 1):
            desc = self.describe_character(url)
            if desc:
                descriptions.append(f"Person {i}: {desc}")
        if not descriptions:
            return None
        return " ".join(descriptions)
    
    def generate_product_video_scenes(
        self,
        text_1: str,
        text_2: str,
        text_3: str,
        text_4: str,
        prompt: str = "",
        image_urls: List[str] = None,
        target_duration: int = 30,
        character_description: str = None,
        character_urls: List[str] = None,
        logo_url: str = None,
        slogan_text: str = None,
        reference_video_structure: Dict[str, Any] = None,
        language: str = "en",
        country: str = "",
        vo_timing: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """Generate detailed scene-by-scene prompts for product video creation.
        
        Uses TEXT 1-4 (strongly) and the original Prompt (weakly) to create
        detailed image and motion prompts for each scene.
        
        Args:
            text_1: What the video is about (topic/description)
            text_2: What is the goal of the video (purpose/objective)
            text_3: Content and style requirements (tone, visual style)
            text_4: Video structure (scene-by-scene breakdown)
            prompt: Original product prompt (used weakly for context)
            image_urls: Product reference image URLs
            target_duration: Target video duration in seconds
            
        Returns:
            Dict with scenes list, total_duration, and music_style
        """
        if not self.initialized:
            logger.error("Gemini service not initialized")
            return self._get_empty_scene_result()
        
        logger.info("Generating product video scene prompts with Gemini...")
        
        # Build the system prompt for scene generation
        system_prompt = """You are an expert video production AI that creates detailed scene-by-scene breakdowns for product videos.

Your task is to take a structured video brief (TEXT 1-4) and generate specific prompts for:
1. Image generation (Nano Banana AI) - detailed visual descriptions
2. Motion/animation (Runway/Kling AI) - camera and subject movement
3. Scene timing and product visibility

=== #1 PRIORITY: VISUAL-AUDIO COHERENCE (MOST IMPORTANT RULE) ===
The viewer must SEE what they HEAR at every moment. Each scene's image_prompt MUST directly 
illustrate what the voice-over says during that scene's time window. This is the single most 
important requirement. If a VO timing block is provided below, follow it strictly:
- Read the VO text for each scene segment carefully
- Design the image_prompt to visually depict exactly what the VO describes
- Examples:
  * VO says "tired of waking up with back pain" → image: person in bed, uncomfortable, hand on lower back
  * VO says "this chair changed everything" → image: person sitting comfortably in the product chair, smiling
  * VO says "just look at the results" → image: before/after comparison or satisfied customer
- The story the viewer SEES and the story they HEAR must be the SAME story at the SAME moment
- Do NOT create generic product shots that don't relate to what's being said

CRITICAL RULES:
- Generate photorealistic, professional visuals suitable for e-commerce/social ads
- Each image_prompt must be EXTREMELY detailed with lighting, composition, camera angle
- Motion prompts should describe subtle, elegant movements (no jarring or fast motion)
- Use professional photography language (lens types, lighting setups, etc.)
- NO text, logos, or branding in any visual descriptions
- Maintain consistent style across all scenes

=== ONE SCENE = ONE IMAGE (MANDATORY) ===
Each image_prompt describes exactly ONE still image (one frame, one moment). The image generator creates ONE picture per prompt.
- Describe a SINGLE composition: one shot, one angle, one moment in time.
- Do NOT describe two or more different shots (e.g. "first show X, then the shot transitions to Y").
- Do NOT describe a sequence, a before/after, or multiple actions in one prompt.
- Do NOT use "then", "transitions to", "next we see", "followed by", or similar - that would imply multiple images.
- One image_prompt = one static or single-moment visual. If the story needs two beats, use two separate scenes with two image_prompts.

=== CRITICAL: PRODUCT VISIBILITY LOGIC ===
The product DOES NOT appear in EVERY scene! Use smart narrative logic:

WHEN product_visible = FALSE (do NOT show the product):
- "hook" scenes that show a PROBLEM or PAIN POINT before introducing the solution
  Example: Person with back pain, frustrated with old chair → NO product yet
- "problem" scenes that establish the need/issue
  Example: Messy workspace, uncomfortable sitting → NO product yet  
- "before" scenes in before/after comparisons
- Transition scenes that don't focus on the product

WHEN product_visible = TRUE (DO show the product):
- "solution" scenes where the product solves the problem
  Example: Person sitting comfortably in the NEW chair → YES product
- "benefit" scenes demonstrating product advantages
- "demo" scenes showing product features or usage
- "result" scenes showing the positive outcome with the product
- "cta" (call-to-action) scenes at the end

TYPICAL PRODUCT VIDEO STRUCTURE:
- Scene 1: Hook/Problem → product_visible: FALSE (show the pain point)
- Scene 2-3: Introduce Solution → product_visible: TRUE (reveal the product)
- Scene 4-6: Benefits/Features → product_visible: TRUE
- Scene 7+: Results/CTA → product_visible: TRUE

Think logically for EACH scene: Does it make narrative sense to show the product here?

=== CAPTIVATING STORY (MANDATORY) ===
The video must feel like ONE gripping story that holds attention from first frame to last—not a generic product list. Every scene must advance the narrative or deepen emotion.

1. **Story arc**: Build a clear narrative: hook (grab attention) → problem/tension (relatable, vivid) → turning point (discovery/solution) → climax (transformation or "aha") → payoff (desire + CTA). The viewer should feel "that's me" in the problem and "I need that" in the solution. Each image_prompt should feel like a key moment in this mini-movie.

2. **Visual storytelling**: Every image must tell part of the story. One specific, vivid moment beats three abstract benefits. Instead of "comfortable seating", show a concrete story beat: e.g. "the exact second they finally relax after hours of squirming", or "their face when they first try it". Use sensory details (how it looks, feels, sounds). Scenes should make the viewer lean in and want to see the next one.

3. **Voice-over (vo_text)**: Write like a human telling a compelling story to a friend—not a brochure. Short, punchy sentences. Include at least one memorable line or twist. Avoid bullet-point speak ("It has X. It has Y."). Vary rhythm: urgent, then calm, then a beat for effect. The VO should feel like the soundtrack to the story—captivating and impossible to tune out. Aim for ~2.5 words per second per scene. IMPORTANT: Embed ElevenLabs v3 Audio Tags in the vo_text to control emotion and delivery. Use 4-6 tags total across all scenes. Tags are square-bracketed words: [excited], [whispers], [sighs], [pause], [dramatically], [laughs], [softly], [gasps], [awe], [light chuckle]. Place tags before or within sentences where emotion shifts. Example: "[excited] You won't believe this! [pause] It actually works. [whispers] And the best part?"

4. **Pacing and variety**: Vary the rhythm—quicker, restless for the problem; slower, satisfying for the solution. Build toward a clear climax (the "aha" or transformation) before the CTA. No filler; every scene earns its place in the story.

5. **Relatability and desire**: The hook and problem should feel instantly recognizable—"that moment when...". The solution should feel like a real change and create desire, not just list features.

6. **Avoid**: Generic corporate tone, repetitive benefit lists, flat pacing, VO that sounds like a spec sheet. Prefer one strong emotional story beat per scene over many weak ones. The goal is a video that feels captivating and shareable.

=== COUNTRY & LANGUAGE ADAPTATION ===
When a target country is specified, adapt the visuals to match that country's culture and environment:
- People/characters should look like locals from that country (ethnicity, clothing style, typical appearance)
- Environments and settings should feel authentic to that country (architecture, landscape, indoor style, climate)
- Cultural references and visual cues should resonate with the target audience
- Do NOT use stereotypes; aim for natural, authentic representation

When a target language is specified for vo_text, write ALL vo_text in that language.

OUTPUT FORMAT (strict JSON):
{
  "scenes": [
    {
      "scene_num": 1,
      "duration": 3.0,
      "narrative_role": "hook/problem/solution/benefit/demo/result/cta",
      "image_prompt": "Detailed visual description for image generation...",
      "motion_prompt": "Camera and subject movement description...",
      "product_visible": true_or_false_based_on_narrative_logic,
      "has_character": true_or_false_if_character_should_appear_in_scene,
      "vo_text": "Optional. When reference video is provided, include the voiceover text for this scene (adapt reference VO to new product). ~2.5 words per second.",
      "vo_word_start": 0,
      "vo_word_end": 15
    }
  ],
  "total_duration": 30,
  "music_style": "Detailed music description for Suno AI..."
}
IMPORTANT: When a VO timing block is provided above, you MUST include "vo_word_start" and "vo_word_end" in each scene (word indices from the numbered word list). These are used to automatically calculate exact scene durations from the audio timestamps.
When a REFERENCE VIDEO is provided, you MUST include "vo_text" for each scene, adapting the reference VO to the new product.

=== CHARACTER IN SCENES ===
If a character is provided, use "has_character" field to indicate if the character should appear in that scene.
- The character should appear in scenes where a human/person is naturally part of the narrative
- The character should NOT appear in product-only shots, close-ups of the product, or abstract scenes
- When has_character is true, include the character's appearance in the image_prompt

=== MINIMAL ON-SCREEN TEXT (SMART USE) ===
- Do NOT put the service provider's name, brand name, or any person's name as visible text in the images. Body scenes (all except the last): NO on-screen text—no signs, no labels, no slogans. Clean, cinematic frames. Only the LAST scene (CTA) may include one short phrase.
- All visible text must be in the target language (e.g. Hebrew script for Hebrew, Arabic for Arabic). Never English unless the video language is English.

=== ENDING SCENE WITH LOGO AND SLOGAN ===
For the final CTA scene only:
- If a logo is provided, the ending scene should be designed to accommodate a logo overlay
- Keep the ending scene visually clean with space for branding
- If a slogan is provided, include it in the image_prompt as one short, elegant text overlay in the TARGET LANGUAGE (correct script)
- If no slogan is provided, you may generate one short phrase (3-6 words) in the target language and include it only in the CTA scene image_prompt
- The logo itself will be added as an overlay later - focus on the visual composition; slogan text only in CTA, minimal and in the correct language

=== WHEN A REFERENCE VIDEO STRUCTURE IS PROVIDED (in the user message) ===
The reference video structure has PRIORITY over TEXT 4. You MUST:
- Produce exactly the SAME number of scenes as the reference, in the same order.
- For EACH scene: same narrative_role and approximate duration_seconds as the reference.
- For EACH scene: ADAPT the reference scene's content and message to the NEW product. Same narrative beat, same type of shot (e.g. problem → solution → benefit), same message structure and tone - only the product/topic changes to match the brief. Do NOT invent a different story flow.
- When the reference includes content_summary and vo_snippet per scene, your image_prompt and (if requested) vo_text for that scene must mirror that beat: adapt what was shown and said to the new product. The new video should feel like the same "script" and pacing as the reference, repurposed for the new offer."""

        # Vertex AI generateContent expects gs:// URIs for fileData; https://storage.googleapis.com/ often causes 400
        def to_gs_uri(url: str) -> str:
            u = url.strip()
            if u.startswith("gs://"):
                return u
            if u.startswith("https://storage.googleapis.com/"):
                return "gs://" + u.replace("https://storage.googleapis.com/", "")
            return u
        
        # Build user content with TEXT 1-4 (strong) and prompt (weak)
        user_parts = []
        
        # Add product images if provided (gs:// → fileData, https → inlineData)
        if image_urls:
            valid_images = [u for u in image_urls if u and u.strip()]
            for url in valid_images:
                uri = to_gs_uri(url)
                if uri.startswith("gs://"):
                    user_parts.append({
                        "fileData": {
                            "mimeType": "image/jpeg",
                            "fileUri": uri
                        }
                    })
                else:
                    try:
                        fetch_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Accept": "image/*,*/*;q=0.8"}
                        img_resp = requests.get(url.strip(), headers=fetch_headers, timeout=30)
                        img_resp.raise_for_status()
                        ct = img_resp.headers.get("Content-Type", "").lower()
                        mime = "image/png" if "png" in ct else "image/webp" if "webp" in ct else "image/jpeg"
                        b64 = base64.b64encode(img_resp.content).decode("utf-8")
                        user_parts.append({"inlineData": {"mimeType": mime, "data": b64}})
                    except Exception as fetch_err:
                        logger.warning(f"   Could not fetch product image {url[:80]}: {fetch_err}, skipping")
            if valid_images:
                logger.info(f"   Including {len(valid_images)} product reference images")
        
        # Add character image(s) if provided: use gs:// when GCS; otherwise fetch and send as inlineData (Vertex may reject non-GCS fileUri)
        for character_url in (character_urls or []):
            char_uri = to_gs_uri(character_url)
            if char_uri.startswith("gs://"):
                user_parts.append({
                    "fileData": {
                        "mimeType": "image/jpeg",
                        "fileUri": char_uri
                    }
                })
            else:
                try:
                    fetch_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Accept": "image/*,*/*;q=0.8"}
                    img_resp = requests.get(character_url.strip(), headers=fetch_headers, timeout=30)
                    img_resp.raise_for_status()
                    ct = img_resp.headers.get("Content-Type", "").lower()
                    mime = "image/png" if "png" in ct else "image/webp" if "webp" in ct else "image/jpeg"
                    b64 = base64.b64encode(img_resp.content).decode("utf-8")
                    user_parts.append({"inlineData": {"mimeType": mime, "data": b64}})
                except Exception as fetch_err:
                    logger.warning(f"   Could not fetch character image for scene prompts: {fetch_err}, skipping character image")
        if character_urls:
            logger.info(f"   Including {len(character_urls)} character reference image(s) for scene generation")
        
        # Build the text prompt
        text_prompt = f"""Generate a detailed scene-by-scene breakdown for a product video.

=== VIDEO BRIEF (USE STRONGLY) ===

TEXT 1 - WHAT THE VIDEO IS ABOUT:
{text_1}

TEXT 2 - VIDEO GOAL:
{text_2}

TEXT 3 - CONTENT AND STYLE REQUIREMENTS:
{text_3}

TEXT 4 - VIDEO STRUCTURE:
{text_4}

=== ADDITIONAL CONTEXT (USE WEAKLY) ===
Original product description:
{prompt[:500] if prompt else "Not provided"}

=== CHARACTER INFORMATION ===
{f"CHARACTER(S) PROVIDED - Reference image(s) of the character(s) are attached. Include in relevant scenes:" if character_description else "No character provided - skip has_character field or set to false"}
{character_description if character_description else ""}
{f"When has_character=true, reference 'the character(s) from the reference image(s)' in the image_prompt. The reference image(s) are attached." if character_description else ""}

=== LOGO/BRANDING/SLOGAN ===
{f"Logo provided for ending scene - design CTA scene with clean space for logo overlay" if logo_url else "No logo provided - create generic CTA ending scene"}
{f"SLOGAN PROVIDED: '{slogan_text}' - Include this slogan ONLY in the ending/CTA scene image_prompt, as one short phrase in the TARGET LANGUAGE (correct script). No text in other scenes." if slogan_text else "No slogan provided - Optionally one short phrase (3-6 words) in the TARGET LANGUAGE in the CTA scene only; no text in body scenes."}

=== TARGET COUNTRY & LANGUAGE ===
{f"TARGET COUNTRY: {country}. People, characters, environments, and settings MUST look authentic to {country} (ethnicity, architecture, landscape, indoor style, climate, clothing). Make the visuals feel like they were shot in {country}." if country else "No specific country - use generic/neutral settings."}
{f"TARGET LANGUAGE: {language}. Write ALL vo_text in this language. The VO script must be entirely in this language, NOT in English." if language and language != "en" else "Language: English (default)."}
"""
        # When a reference video structure was analyzed: MANDATORY same structure and adapt content/VO per scene
        ref_block = ""
        ref_has_content = False
        if reference_video_structure and isinstance(reference_video_structure.get("scenes"), list) and reference_video_structure["scenes"]:
            ref_scenes = reference_video_structure["scenes"]
            ref_has_content = any(s.get("content_summary") or s.get("vo_snippet") for s in ref_scenes)
            lines = ["REFERENCE VIDEO (MANDATORY – IGNORE TEXT 4 for structure and flow):",
                     "Your output MUST have exactly the same number of scenes, in the same order. For each scene: same narrative_role and approximate duration.",
                     "ADAPT each reference scene to the NEW product: same story beat, same type of message and shot, same pacing. Only the product/topic changes. Do NOT create a different storyline.",
                     ""]
            for i, s in enumerate(ref_scenes, 1):
                role = s.get("narrative_role", "transition")
                dur = s.get("duration_seconds", s.get("duration", 3))
                line = f"Scene {i}: {role}, {dur}s"
                if s.get("content_summary"):
                    line += f" | Content: {s.get('content_summary', '')}"
                if s.get("vo_snippet"):
                    line += f" | VO: \"{s.get('vo_snippet', '')}\""
                lines.append(line)
            lines.append("")
            if ref_has_content:
                lines.append("For each scene, generate image_prompt AND vo_text that ADAPT the reference content and VO above to the new product (same beat and tone, new product).")
            ref_block = "\n".join(lines) + "\n\n"
        
        if reference_video_structure and reference_video_structure.get("scenes"):
            scene_count_requirement = str(len(reference_video_structure["scenes"]))
        else:
            scene_count_requirement = self._get_scene_count_for_duration(target_duration)
        
        # Build VO timing block if VO was generated first
        # Uses pre-split scene segments (with full text + timestamps) when available
        vo_timing_block = ""
        if vo_timing and vo_timing.get("segments") and vo_timing.get("total_duration", 0) > 0:
            vo_total = vo_timing["total_duration"]
            vo_text_full = vo_timing.get("full_text", "")
            vo_segs = vo_timing["segments"]
            num_words = len(vo_segs)
            scene_segments = vo_timing.get("scene_segments", [])
            
            if scene_segments:
                # FULL per-scene VO text with exact timestamps (from pre-splitting)
                scene_vo_lines = []
                for seg in scene_segments:
                    scene_vo_lines.append(
                        f"  SCENE {seg['scene_num']} VO ({seg['start_time']:.1f}s - {seg['end_time']:.1f}s, "
                        f"~{seg['duration']:.1f}s, words [{seg['word_start_idx']}-{seg['word_end_idx']}]):\n"
                        f"    \"{seg['text']}\""
                    )
                scene_vo_str = "\n\n".join(scene_vo_lines)
                
                vo_timing_block = f"""
=== VOICE-OVER AUDIO (ALREADY GENERATED – YOUR SCENES MUST MATCH IT) ===
A VO audio track ({vo_total:.1f}s, {num_words} words) has ALREADY been recorded.
The VO has been pre-split into {len(scene_segments)} scene segments below. 
Your output MUST have exactly {len(scene_segments)} scenes, one for each VO segment.

=== PER-SCENE VO TEXT WITH EXACT TIMESTAMPS ===
{scene_vo_str}

=== CRITICAL VISUAL-AUDIO MATCHING RULES ===
1. Each scene's image_prompt MUST directly illustrate what the VO SAYS during that scene.
   - If the VO says "struggling with back pain" → image shows a person with back pain
   - If the VO says "this product changed everything" → image shows the product in use
   - If the VO says "imagine waking up refreshed" → image shows a person waking up happy
2. Use the pre-split word ranges as "vo_word_start" and "vo_word_end" for each scene.
3. Scene durations are automatically calculated from timestamps. Set approximate durations based on the timestamps above.
4. Do NOT write new vo_text – the VO is already recorded.
5. The viewer must FEEL that the visuals and audio tell the same story at the same moment.

"""
            else:
                # Fallback: full word list with timestamps (no pre-splitting available)
                # Show every word with its timestamp for precise matching
                word_list_lines = []
                for i in range(0, num_words, max(1, num_words // 30)):
                    ws = vo_segs[i]
                    word_list_lines.append(f"  [{i}] {ws['start_time']:.1f}s \"{ws['text']}\"")
                word_list_lines.append(f"  [{num_words - 1}] {vo_segs[-1]['end_time']:.1f}s \"{vo_segs[-1]['text']}\" (last word)")
                word_list_str = "\n".join(word_list_lines)
                
                vo_timing_block = f"""
=== VOICE-OVER AUDIO (ALREADY GENERATED – YOUR SCENES MUST MATCH IT) ===
A VO audio track ({vo_total:.1f}s, {num_words} words) has ALREADY been recorded. Your scenes MUST be timed to match.

VO TRANSCRIPT:
\"{vo_text_full}\"

WORD INDEX LANDMARKS (word_index → timestamp):
{word_list_str}

=== CRITICAL VISUAL-AUDIO MATCHING RULES ===
1. Split the transcript into your scenes so each scene's visuals MATCH what is being SAID.
   - If the VO says "struggling with back pain" → image shows a person with back pain
   - If the VO says "this product changed everything" → image shows the product in use
2. For each scene, set "vo_word_start" and "vo_word_end" (0-indexed word positions).
   - All {num_words} words must be covered. No gaps, no overlaps.
3. Scene durations are calculated automatically from word timestamps.
4. The image_prompt must visually depict what the VO describes in that word range.
5. Do NOT write new vo_text – the VO is already recorded.

"""
        else:
            vo_timing_block = ""
        
        text_prompt += ref_block + vo_timing_block + f"""
=== REQUIREMENTS ===
- Target duration: {target_duration} seconds{f" (VO already recorded: {vo_timing['total_duration']:.1f}s – the video MUST be at least this long to fit the VO)" if vo_timing and vo_timing.get('total_duration') else ""}
- Generate {scene_count_requirement} scenes that tell a compelling product story with real depth and interest (emotional arc, concrete moments, relatable hook, varied pacing—see STORY DEPTH AND INTEREST in instructions).
- Each scene should be {self._get_scene_duration_range(target_duration)} seconds (CRITICAL: keep each scene between 3-8 seconds so animation clips can cover it. NEVER make a single scene longer than 10 seconds.)
- Each image_prompt = ONE image only: describe a single moment/frame, never two shots or a transition ("then", "transitions to") in one prompt.
- Include a strong, relatable hook in the first scene (a moment the viewer recognizes).
- End with a clear, satisfying call-to-action after a visible emotional or practical payoff.
- Ensure scenes flow naturally and build interest; avoid flat or repetitive tone.
- Reference the product images provided for accurate visual details.
- vo_text: Write like spoken copy—conversational, varied rhythm, one memorable line when possible; not a bullet list.{" (NOTE: VO is already recorded – vo_text is for reference only)" if vo_timing else ""}
- IMPORTANT: The total duration of all scenes MUST sum to approximately {target_duration} seconds. If a VO is provided, the total MUST equal the VO duration + 1-2s buffer.

=== IMPORTANT: PRODUCT VISIBILITY ===
Use SMART narrative logic for product visibility:
- If scene shows a PROBLEM (pain, frustration, discomfort BEFORE using product) → product_visible: false
- If scene shows the SOLUTION, BENEFIT, or RESULT of using the product → product_visible: true
- First scene often shows the problem → product usually NOT visible yet
- Product is revealed when transitioning from problem to solution

Example: For an ergonomic chair video:
- Scene 1 (hook/problem): Person with back pain at desk → product_visible: FALSE
- Scene 2 (solution): New chair is introduced/revealed → product_visible: TRUE
- Scene 3+ (benefits): Comfortable sitting, happy user → product_visible: TRUE

Output ONLY valid JSON with the scenes array, total_duration, and music_style."""

        user_parts.append({"text": text_prompt})
        
        try:
            # Vertex AI API format
            payload = {
                "contents": [
                    {
                        "role": "user",
                        "parts": user_parts
                    }
                ],
                "systemInstruction": {
                    "parts": [{"text": system_prompt}]
                },
                "generationConfig": {
                    "temperature": 0.7,
                    "maxOutputTokens": 32000
                }
            }
            
            url = self._get_vertex_url(self.model)
            response = requests.post(
                url,
                headers=self._get_vertex_headers(),
                json=payload,
                timeout=180
            )
            if not response.ok:
                try:
                    err_body = response.text[:800] if response.text else ""
                    logger.error(f"Vertex generateContent error ({response.status_code}): {err_body}")
                except Exception:
                    pass
                response.raise_for_status()
            
            result = response.json()
            self.last_usage_metadata = result.get("usageMetadata")
            candidates = result.get("candidates", [])
            if candidates:
                content_parts = candidates[0].get("content", {}).get("parts", [])
                content = ""
                for part in content_parts:
                    if "text" in part:
                        content += part["text"]
            else:
                logger.error("No candidates in Gemini response")
                return self._get_empty_scene_result()
            
            # Parse the JSON response
            parsed = self._extract_json_from_response(content)
            
            if parsed and "scenes" in parsed:
                scenes = parsed.get("scenes", [])
                logger.info(f"Generated {len(scenes)} scene prompts")
                return {
                    "scenes": scenes,
                    "total_duration": parsed.get("total_duration", target_duration),
                    "music_style": parsed.get("music_style", "Upbeat, modern, corporate background music")
                }
            else:
                logger.error("Could not parse scene prompts from Gemini response")
                logger.warning(f"Raw response (first 2000 chars): {content[:2000]}")
                return self._get_empty_scene_result()
                
        except Exception as e:
            logger.error(f"Error generating scene prompts: {e}")
            return self._get_empty_scene_result()
    
    def _get_scene_count_for_duration(self, target_duration: int) -> str:
        """Get recommended scene count based on target video duration.
        
        Each scene should be ~4-6 seconds so that animation clips (5-10s) can
        cover the scene via trimming or mild slow-motion (max 2x).
        
        Args:
            target_duration: Target video duration in seconds (10-120)
            
        Returns:
            String describing the recommended number of scenes (e.g., "4-5")
        """
        if target_duration <= 12:
            return "3-4"
        elif target_duration <= 18:
            return "4-5"
        elif target_duration <= 25:
            return "5-7"
        elif target_duration <= 32:
            return "7-9"
        elif target_duration <= 45:
            return "9-12"
        elif target_duration <= 60:
            return "12-15"
        elif target_duration <= 80:
            return "15-18"
        elif target_duration <= 100:
            return "18-20"
        else:  # 100-120
            return "20"
    
    def _get_scene_duration_range(self, target_duration: int) -> str:
        """Get recommended scene duration range based on target video duration.
        
        Scene durations are kept in a range that animation APIs (Runway/Kling)
        can produce: 5s or 10s clips with up to 2x slow-motion = max ~10-20s per scene.
        Shorter scenes (4-6s) are preferred because they're easier to match.
        
        Args:
            target_duration: Target video duration in seconds (10-120)
            
        Returns:
            String describing the recommended scene duration (e.g., "2-3")
        """
        if target_duration <= 15:
            return "2-3.5"
        elif target_duration <= 25:
            return "2.5-4"
        elif target_duration <= 40:
            return "3-5"
        else:  # 41-120: keep scenes manageable for animation clips
            return "4-6"
    
    def _get_empty_scene_result(self) -> Dict[str, Any]:
        """Return empty result structure for scene generation."""
        return {
            "scenes": [],
            "total_duration": 0,
            "music_style": ""
        }
