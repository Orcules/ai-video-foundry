"""OpenAIService — extracted verbatim from Comp_Videos/video_scene_processor.py.

Lines 6041-9002 of the monolith.
"""

import os
import re
import json
import time
import base64
import logging
import threading
import requests
from typing import Dict, Any, List, Optional, Tuple
from openai import OpenAI

from api_pipeline.services.base.config import config

logger = logging.getLogger(__name__)


class OpenAIService:
    """Service for OpenAI API interactions."""
    
    def __init__(self, api_key: str):
        """Initialize OpenAI service.
        
        Args:
            api_key: OpenAI API key.
        """
        self.client = OpenAI(api_key=api_key)
        logger.info("✅ OpenAI client initialized")
    
    def _get_cultural_style_instructions(self, language: str) -> str:
        """Get cultural style instructions for image/video prompts based on target language.
        
        CRITICAL: Characters and environments MUST be changed to match the target country.
        DO NOT keep the original video's characters or backgrounds!
        
        Args:
            language: Target language code (e.g., 'es', 'de', 'hu').
            
        Returns:
            String with detailed cultural adaptation instructions.
        """
        # Get region from language
        region = config.REGION_MAPPING.get(language, 'namer')
        cultural_style = config.CULTURAL_STYLES.get(region, {})
        
        if not cultural_style:
            return """
⚠️⚠️⚠️ CULTURAL ADAPTATION (MANDATORY!) ⚠️⚠️⚠️
You MUST change the characters and environment to match the target language/country.
DO NOT keep the original video's characters or backgrounds!
Use diverse, multicultural representations appropriate for the target market.
"""
        
        instructions = f"""
🌍🌍🌍 CRITICAL - CULTURAL ADAPTATION (MANDATORY!) 🌍🌍🌍

⚠️ YOU MUST CHANGE THE CHARACTERS AND ENVIRONMENT! ⚠️
DO NOT keep the original video's people or backgrounds!
Create NEW characters and environments for the TARGET market: {region.upper().replace('_', ' ')}

**MANDATORY CHANGES:**

1. **REPLACE CHARACTERS (DO NOT USE ORIGINAL!):**
   - Use: {cultural_style.get('ethnicity', 'diverse features appropriate for target region')}
   - Clothing: {cultural_style.get('clothing', 'modern casual fashion for target region')}
   - Names if needed: {cultural_style.get('names', 'culturally appropriate names')}
   
   Example: If original has Arab woman → For US market → Use American woman
   Example: If original has Asian man → For German market → Use German/European man

2. **REPLACE ENVIRONMENT (DO NOT USE ORIGINAL!):**
   - Use: {cultural_style.get('environment', 'settings appropriate for target country')}
   - Remove any text, signs, or architecture from the original country
   - Add environment details matching the target country

3. **CULTURAL STYLE:**
   - Tone: {cultural_style.get('style', 'confident and professional')}
   - All text in target language
   - Culturally appropriate expressions and gestures

⚠️ THIS IS MANDATORY - DO NOT SKIP! ⚠️
"""
        return instructions
    
    def _analyze_article_video_relevance(self, article_text: str, video_description: str) -> Dict[str, Any]:
        """Analyze the relevance between article content and video content.
        
        This helps determine the best blending strategy for content adaptation.
        
        Args:
            article_text: The article content.
            video_description: Description of what's shown in the video.
            
        Returns:
            Dict with relevance_score (0-1), common_themes, blend_strategy.
        """
        try:
            if not article_text or not video_description:
                return {
                    "relevance_score": 0.5,
                    "common_themes": [],
                    "blend_strategy": "video_priority",
                    "blend_instructions": "Focus on video content, use article for general context only."
                }
            
            response = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": """You are an expert at analyzing content relevance.
Compare the article content with the video description and determine:
1. How related they are (0-1 score)
2. What common themes exist
3. Best strategy to blend them

Return JSON:
{
  "relevance_score": 0.0-1.0,
  "common_themes": ["theme1", "theme2"],
  "video_subject": "what the video shows",
  "article_subject": "what the article is about",
  "blend_strategy": "full_blend" | "partial_blend" | "video_priority",
  "blend_instructions": "specific instructions for content creators"
}

STRATEGIES:
- full_blend (score > 0.7): Article and video are about the same topic. Use article content fully.
- partial_blend (score 0.3-0.7): Some overlap. Keep video visuals, adapt messaging to find common ground.
- video_priority (score < 0.3): No connection. Ignore article, focus on video content only."""
                    },
                    {
                        "role": "user",
                        "content": f"""ARTICLE CONTENT:
{article_text[:1000]}

VIDEO DESCRIPTION:
{video_description[:500]}

Analyze the relevance and provide blending strategy."""
                    }
                ],
                max_tokens=300,
                temperature=0.3,
                response_format={"type": "json_object"}
            )
            
            result = json.loads(response.choices[0].message.content)
            logger.info(f"📊 Article-Video Relevance: {result.get('relevance_score', 0):.2f} - Strategy: {result.get('blend_strategy', 'unknown')}")
            return result
            
        except Exception as e:
            logger.warning(f"⚠️ Could not analyze article-video relevance: {e}")
            return {
                "relevance_score": 0.5,
                "common_themes": [],
                "blend_strategy": "partial_blend",
                "blend_instructions": "Try to find common ground between video and article content."
            }
    
    def analyze_scene_frames(
        self, 
        frame_paths: List[str],
        manual_instructions: str = ""
    ) -> Dict[str, Any]:
        """Analyze scene frames and generate prompts using two separate OpenAI calls.
        
        Args:
            frame_paths: List of paths to frame images (1 per second of scene).
            manual_instructions: Optional custom instructions from user.
            
        Returns:
            Dict containing analysis, first_prompt (image), second_prompt (motion).
        """
        try:
            logger.info(f"🔍 Analyzing {len(frame_paths)} frames with OpenAI (2 calls)...")
            
            # Encode images to base64
            image_contents = []
            for frame_path in frame_paths:
                with open(frame_path, 'rb') as f:
                    image_data = base64.b64encode(f.read()).decode('utf-8')
                    image_contents.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_data}",
                            "detail": "high"  # Use high detail for text extraction
                        }
                    })
            
            # Call 1: Generate Image Prompt (using gpt-4o-mini)
            logger.info("📸 Generating image prompt with gpt-4o-mini...")
            image_result = self._generate_image_prompt(image_contents, manual_instructions)
            
            # Call 2: Generate Motion Prompt (using gpt-4o)
            logger.info("🎬 Generating motion prompt with gpt-4o...")
            motion_result = self._generate_motion_prompt(image_contents, manual_instructions)
            
            # Combine results
            result = {
                "analysis": image_result.get("analysis", ""),
                "text_content": image_result.get("text_content", {}),
                "first_prompt": image_result.get("first_prompt", ""),
                "second_prompt": motion_result.get("second_prompt", "")
            }
            
            logger.info("✅ Scene analysis complete (both prompts generated)")
            return result
            
        except Exception as e:
            logger.error(f"❌ Error analyzing scene: {e}")
            return {
                "analysis": "Unable to analyze scene",
                "text_content": {"exact_text": "", "language": "", "position": "", "style": ""},
                "first_prompt": "",
                "second_prompt": ""
            }
    
    # =========================================================================
    # PRODUCT DETECTION FUNCTIONS
    # =========================================================================
    
    def detect_product_in_frames(
        self, 
        frame_paths: List[str],
        min_confidence: float = 0.7,
        audio_transcript: str = "",
        video_duration: float = 0
    ) -> Dict[str, Any]:
        """Comprehensive video analysis: detect product, understand narrative, and correlate with VO.
        
        Analyzes 60 frames spread across the video + audio transcript to understand:
        - What the product IS (type, brand, visual details)
        - What the product DOES (purpose, function, how it's used)
        - How it APPEARS in different contexts (static, being applied, in-use, etc.)
        - SEQUENTIAL narrative: what happens from start to finish
        - Audio-Visual correlation: what is said when what is shown
        
        Args:
            frame_paths: List of paths to frame images (60 frames spread across video).
            min_confidence: Minimum confidence threshold (0-1).
            audio_transcript: The transcribed VO/audio from the video.
            video_duration: Total video duration in seconds.
            
        Returns:
            Dict with comprehensive video understanding including:
                - Sequential narrative breakdown
                - Product info with usage contexts
                - Audio-visual correlation
        """
        try:
            logger.info(f"🔍 [VIDEO ANALYSIS] Analyzing {len(frame_paths)} frames + audio for comprehensive understanding...")
            
            # Encode images to base64
            image_contents = []
            for frame_path in frame_paths:
                if not os.path.exists(frame_path):
                    logger.warning(f"⚠️ [PRODUCT] Frame not found: {frame_path}")
                    continue
                    
                with open(frame_path, 'rb') as f:
                    image_data = base64.b64encode(f.read()).decode('utf-8')
                    image_contents.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_data}",
                            "detail": "high"
                        }
                    })
            
            if not image_contents:
                logger.warning("⚠️ [PRODUCT] No valid frames to analyze")
                return {"has_product": False}
            
            # Build audio context if available
            audio_context = ""
            if audio_transcript and len(audio_transcript) > 10:
                # Calculate approximate timing per frame
                frames_count = len(frame_paths)
                seconds_per_frame = video_duration / frames_count if video_duration > 0 and frames_count > 0 else 0.5
                audio_context = f"""
=== AUDIO/VOICEOVER TRANSCRIPT ===
"{audio_transcript}"

Video duration: {video_duration:.1f} seconds
Frames analyzed: {frames_count} (1 frame every ~{seconds_per_frame:.2f} seconds)

IMPORTANT: Correlate what is SAID in the VO with what is SHOWN in frames.
Frame 0 = start of video (0:00), Frame {frames_count-1} = end of video ({video_duration:.1f}s)
=================================
"""
            
            # Enhanced system prompt for comprehensive video analysis
            system_prompt = """You are an expert video analyst specializing in advertising and commercial content.

Your task is to provide a COMPREHENSIVE ANALYSIS of the video by:
1. UNDERSTANDING THE NARRATIVE: What story does the video tell from start to end?
2. IDENTIFYING THE PRODUCT: What is being sold? How does it look exactly?
3. ANALYZING EACH SCENE: What happens in each part of the video?
4. CORRELATING AUDIO-VISUAL: What is said when what is shown?

You are viewing 60 FRAMES spread EVENLY across the entire video.
- Frame 0 = START of video
- Frame 30 = MIDDLE of video  
- Frame 59 = END of video
- Analyze them SEQUENTIALLY to understand the flow!

Examples of what to identify:
- Weight loss patch: Hook shows problem → Demo shows application → Results shown → CTA
- Dog toy: Hook shows excited dog → Problem shows bored dog → Solution shows toy → Demo shows play
- Skincare: Problem shows skin issue → Solution introduces product → Demo shows application → Results

Be EXTREMELY detailed - this analysis will be used to recreate similar videos!"""

            # Build user prompt (avoiding f-string issues with JSON template)
            frame_count = len(frame_paths)
            user_prompt_parts = [
                f"Analyze these {frame_count} video frames SEQUENTIALLY to understand the COMPLETE VIDEO.",
                audio_context,
                """
=== PART 1: SEQUENTIAL VIDEO NARRATIVE ===
Analyze frames in ORDER (0 to last) and describe what happens at each stage:
- OPENING (first 20%): What's the hook? How does the video start?
- BUILD-UP (20-40%): What problem/story is introduced?
- CORE MESSAGE (40-70%): What's the main demonstration/solution?
- RESOLUTION (70-85%): What benefits/results are shown?
- CLOSING (last 15%): What's the CTA? How does it end?

=== PART 2: ULTRA-DETAILED PRODUCT IDENTIFICATION ===
Describe the product for AI image generation:
- EXACT SHAPE: Round? Square? Curved? Dimensions?
- EXACT COLORS: Specific shades (not "blue" but "deep navy with cyan accents")
- EXACT SIZE: Compare to common objects (credit card sized, palm-sized, etc.)
- EXACT MATERIALS: Matte/glossy plastic? Fabric? Metal?
- EXACT TEXTURES: Smooth? Ridged? Perforated?
- BRANDING: Logos, text, patterns?
- PACKAGING: Colors, design, text on packaging?

=== PART 3: PRODUCT USAGE CONTEXTS ===
For each context type, identify which frames show it:
- "static_display": Product alone on surface
- "in_packaging": Product in box/wrapper
- "being_applied": Product being used/applied
- "in_hand": Held by person
- "close_up": Detail shot of product
- "before_after": Comparison shots
- "lifestyle": Product in real-life context
- "not_visible": Product not in frame

=== PART 4: AUDIO-VISUAL CORRELATION ===
Match what is SAID to what is SHOWN:
- When does the VO mention the product? What's shown then?
- When are benefits mentioned? What visuals accompany?
- When is the CTA spoken? What's on screen?

Return JSON:"""
            ]
            
            user_prompt = "\n".join(user_prompt_parts) + """
{
    "has_product": true/false,
    "product_detected": "patch/cream/device/supplement/toy/accessory/etc",
    
    "video_narrative": {
        "video_type": "product_demo/testimonial/lifestyle/before_after/tutorial/ugc",
        "opening_hook": "What happens in the first 2-3 seconds to grab attention",
        "main_story": "The core narrative/message of the video",
        "climax": "The key moment - product reveal, transformation, or benefit demonstration",
        "closing": "How the video ends - CTA, final message",
        "emotional_journey": "The emotional arc: curiosity to problem to hope to solution to action",
        "pacing": "fast/medium/slow",
        "style": "professional/ugc/influencer/cinematic/casual"
    },
    
    "sequential_breakdown": [
        {
            "segment": "opening/build_up/core/resolution/closing",
            "frame_range": [0, 10],
            "timestamp_range": "0:00-0:05",
            "what_happens": "Detailed description of what happens in this segment",
            "product_visibility": "none/glimpse/partial/full/close_up",
            "audio_content": "What is being said during this segment (from transcript)",
            "key_visuals": ["visual element 1", "visual element 2"],
            "purpose": "hook/problem/solution/demo/benefit/cta"
        }
    ],
    
    "audio_visual_sync": [
        {
            "vo_text": "The exact text being spoken",
            "frame_range": [15, 25],
            "visual_description": "What is shown while this is said",
            "sync_quality": "perfect/good/loose",
            "key_message": "The main point being communicated"
        }
    ],
    
    "product_description": "EXTREMELY DETAILED 300+ word VISUAL description for AI image generation. Include exact shape, dimensions, colors (with specific shades), materials, textures, branding, and unique features.",
    
    "product_purpose": "DETAILED explanation of what the product does, benefits, target audience, and problem it solves.",
    
    "product_usage_method": "STEP-BY-STEP usage instructions with body positioning and actions.",
    
    "product_details": {
        "type": "specific product type",
        "brand": "brand name if visible, or unbranded",
        "shape": "exact shape description",
        "dimensions": "approximate dimensions",
        "colors": {
            "primary": "main color with exact shade",
            "secondary": "secondary color",
            "accent": "accent colors",
            "packaging_colors": ["packaging colors"]
        },
        "materials": ["material descriptions"],
        "textures": ["texture descriptions"],
        "packaging": "detailed packaging description",
        "branding_elements": ["logo", "text", "patterns"],
        "distinctive_features": ["unique features"]
    },
    
    "usage_contexts": [
        {
            "context_type": "static_display/being_applied/in_hand/close_up/lifestyle/before_after",
            "description": "How product appears in this context",
            "visual_elements": "Other elements in frame",
            "action_description": "Movement/action happening",
            "frame_indices": [0, 3, 5],
            "vo_during_context": "What is said during this context"
        }
    ],
    
    "key_frames": {
        "best_product_frame": 0,
        "best_usage_frame": 0,
        "best_result_frame": 0,
        "hook_frame": 0,
        "cta_frame": 0
    },
    
    "overall_confidence": 0.0-1.0,
    
    "recreation_notes": "Key insights for recreating a similar video - what makes this video effective, what elements to preserve"
}

If NO product detected:
{
    "has_product": false,
    "product_detected": null,
    ...
}

REMEMBER: The product_description will be used DIRECTLY for image generation. Make it so detailed that an artist could draw the exact product without ever seeing it!"""

            # Build message content
            user_content = [{"type": "text", "text": user_prompt}] + image_contents
            
            response = self.client.chat.completions.create(
                model="gpt-4o",  # Use gpt-4o for best vision capability
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}
                ],
                max_tokens=8000,  # Increased for comprehensive video analysis with 60 frames
                temperature=0.2,
                response_format={"type": "json_object"}
            )
            
            content = response.choices[0].message.content
            if content is None:
                logger.warning("⚠️ [PRODUCT] OpenAI returned None content")
                return {"has_product": False}
            
            result = json.loads(content.strip())
            
            # Check confidence threshold
            if result.get("has_product") and result.get("overall_confidence", 0) < min_confidence:
                logger.info(f"ℹ️ [PRODUCT] Product detected but confidence too low: {result.get('overall_confidence'):.2f} < {min_confidence}")
                result["has_product"] = False
            
            # Log result
            if result.get("has_product"):
                logger.info(f"✅ [PRODUCT] Detected: {result.get('product_detected')}")
                logger.info(f"   Brand: {result.get('product_details', {}).get('brand', 'unknown')}")
                logger.info(f"   Purpose: {result.get('product_purpose', 'unknown')[:100]}...")
                logger.info(f"   Confidence: {result.get('overall_confidence', 0):.2f}")
                logger.info(f"   Best frame: {result.get('best_frame_index')}")
                
                # Log usage contexts found
                usage_contexts = result.get("usage_contexts", [])
                if usage_contexts:
                    context_types = [c.get("context_type") for c in usage_contexts]
                    logger.info(f"   Usage contexts: {', '.join(context_types)}")
            else:
                logger.info("ℹ️ [PRODUCT] No product detected, continuing with standard flow")
            
            return result
            
        except Exception as e:
            logger.error(f"❌ [PRODUCT] Detection error: {e}")
            return {"has_product": False, "error": str(e)}
    
    def analyze_video_structure(
        self,
        frame_paths: List[str],
        article_content: Dict[str, str] = None,
        manual_instructions: str = "",
        product_info: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """Analyze the video's narrative structure and plan scene content based on article and product.
        
        This function scans the entire video to understand:
        1. The video's narrative flow (intro, problem, solution, CTA, etc.)
        2. How scenes transition and what each scene communicates
        3. How to adapt each scene to the article content
        4. Where the product should appear and how (static, being used, etc.)
        
        Args:
            frame_paths: List of frame paths from across the video.
            article_content: Dict with keys: 'free_text', 'title', 'first_paragraph', 'rest_content'
            manual_instructions: Optional manual instructions from the sheet.
            product_info: Product detection results (if available).
            
        Returns:
            Dict containing:
                - video_structure: Overall narrative structure type
                - scene_plan: List of planned scenes with content assignments
                - content_mapping: How article content maps to scenes
        """
        try:
            logger.info("📊 [STRUCTURE] Analyzing video structure with article context...")
            
            # Prepare article content
            article = article_content or {}
            free_text = article.get('free_text', '')
            title = article.get('title', '')
            first_para = article.get('first_paragraph', '')
            rest_content = article.get('rest_content', '')
            
            # Combine article content for context
            full_article = ""
            if free_text:
                full_article = free_text
            else:
                parts = [p for p in [title, first_para, rest_content] if p]
                full_article = "\n\n".join(parts)
            
            # Prepare product context
            product_context = ""
            if product_info and product_info.get("has_product"):
                product_context = f"""
PRODUCT DETECTED:
- Type: {product_info.get('product_detected', 'unknown')}
- Purpose: {product_info.get('product_purpose', 'unknown')}
- Usage method: {product_info.get('product_usage_method', 'unknown')}
- Usage contexts in video: {', '.join([c.get('context_type', '') for c in product_info.get('usage_contexts', [])])}
"""
            
            # Encode sample frames (use 5 evenly distributed)
            image_contents = []
            sample_indices = [0, len(frame_paths)//4, len(frame_paths)//2, (len(frame_paths)*3)//4, len(frame_paths)-1]
            sample_indices = list(set([min(i, len(frame_paths)-1) for i in sample_indices]))
            
            for idx in sorted(sample_indices)[:5]:
                frame_path = frame_paths[idx] if idx < len(frame_paths) else frame_paths[-1]
                if os.path.exists(frame_path):
                    with open(frame_path, 'rb') as f:
                        image_data = base64.b64encode(f.read()).decode('utf-8')
                        image_contents.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{image_data}",
                                "detail": "low"
                            }
                        })
            
            if not image_contents:
                logger.warning("⚠️ [STRUCTURE] No frames available for analysis")
                return {"video_structure": "unknown", "scene_plan": []}
            
            # System prompt for structure analysis
            system_prompt = """You are an expert video advertising analyst. Your job is to analyze video structure and plan how to adapt it to new content.

You must understand:
1. VIDEO STRUCTURE: What type of advertising video is this? (problem-solution, testimonial, product demo, lifestyle, before-after, etc.)
2. SCENE NARRATIVE: What role does each scene play? (hook, problem statement, solution reveal, product showcase, CTA, etc.)
3. CONTENT MAPPING: How should the article content be distributed across scenes?
4. PRODUCT PLACEMENT: In which scenes should the product appear, and how?

Common video structures:
- "problem_solution": Hook → Problem → Solution (product) → Benefits → CTA
- "testimonial": Hook → Personal story → Product discovery → Transformation → CTA
- "product_demo": Hook → Product intro → Features → How to use → CTA
- "lifestyle": Aspirational scenes → Product integration → Benefits → CTA
- "before_after": Before state → Product use → After state → CTA"""

            # Build the user prompt
            user_prompt = f"""Analyze this advertising video's structure and plan content adaptation.

**ARTICLE CONTENT TO ADAPT:**
Title: {title if title else '[Not provided]'}
First Paragraph: {first_para[:500] if first_para else '[Not provided]'}
Rest of Content: {rest_content[:500] if rest_content else '[Not provided]'}
Free Text (if provided, use this instead): {free_text[:500] if free_text else '[Not provided]'}

**MANUAL INSTRUCTIONS (MUST FOLLOW):**
{manual_instructions if manual_instructions else '[No manual instructions]'}

{product_context if product_context else '**NO PRODUCT DETECTED**'}

**ANALYZE THE VIDEO FRAMES AND RETURN:**

1. Video structure type
2. For each scene (based on frames), determine:
   - Scene role in narrative (hook, problem, solution, etc.)
   - What content from article should appear (title, key benefit, CTA, etc.)
   - If product detected: should product appear here? How? (static, being_applied, in_hand, etc.)
   - Suggested visual elements

Return JSON:
{{
    "video_structure": "problem_solution" | "testimonial" | "product_demo" | "lifestyle" | "before_after" | "mixed",
    "narrative_summary": "Brief description of video's story arc",
    "scene_plan": [
        {{
            "scene_number": 1,
            "estimated_time_range": "0-3s",
            "narrative_role": "hook" | "problem" | "solution" | "benefit" | "cta" | "transition",
            "article_content_to_use": "Which part of article content fits here",
            "product_appearance": "static_display" | "being_applied" | "in_hand" | "lifestyle" | "not_visible" | null,
            "visual_suggestion": "Description of what this scene should show",
            "key_message": "The main point this scene communicates"
        }}
    ],
    "content_distribution": {{
        "title_usage": "Which scene(s) should feature the title",
        "key_benefits": ["Benefit 1 → Scene X", "Benefit 2 → Scene Y"],
        "cta_placement": "Which scene(s) for call-to-action"
    }},
    "product_integration_plan": {{
        "total_product_scenes": number,
        "primary_showcase_scene": number,
        "application_scenes": [scene numbers where product is being used],
        "lifestyle_scenes": [scene numbers with product in context]
    }}
}}"""

            # Build message content
            user_content = [{"type": "text", "text": user_prompt}] + image_contents
            
            response = self.client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}
                ],
                max_tokens=2500,
                temperature=0.3,
                response_format={"type": "json_object"}
            )
            
            content = response.choices[0].message.content
            if content is None:
                logger.warning("⚠️ [STRUCTURE] Analysis returned None")
                return {"video_structure": "unknown", "scene_plan": []}
            
            result = json.loads(content.strip())
            
            # Log results
            logger.info(f"✅ [STRUCTURE] Video type: {result.get('video_structure')}")
            logger.info(f"   Narrative: {result.get('narrative_summary', '')[:100]}...")
            scene_plan = result.get("scene_plan", [])
            logger.info(f"   Planned {len(scene_plan)} scenes")
            
            return result
            
        except Exception as e:
            logger.error(f"❌ [STRUCTURE] Analysis error: {e}")
            return {"video_structure": "unknown", "scene_plan": [], "error": str(e)}
    
    def analyze_video_style(
        self,
        frame_paths: List[str],
        video_duration: float = 0
    ) -> Dict[str, Any]:
        """Comprehensive video style analysis to match the original video's visual style.
        
        Analyzes the entire video to extract:
        - Color palette and grading
        - Lighting style (natural, studio, warm, cool)
        - Composition patterns (close-up, wide, etc.)
        - Camera movement tendencies
        - Overall mood and atmosphere
        - Scene transition styles
        - Subject framing preferences
        
        This creates a "style guide" used to generate videos that match the original.
        
        Args:
            frame_paths: List of frame paths from across the video.
            video_duration: Total video duration in seconds.
            
        Returns:
            Dict with comprehensive style analysis.
        """
        try:
            logger.info("🎨 [STYLE] Analyzing video visual style for matching...")
            
            # Sample frames evenly across the video for style analysis
            num_frames = min(8, len(frame_paths))
            if len(frame_paths) > num_frames:
                indices = [int(i * (len(frame_paths) - 1) / (num_frames - 1)) for i in range(num_frames)]
                sample_paths = [frame_paths[i] for i in indices]
            else:
                sample_paths = frame_paths
            
            # Encode frames
            image_contents = []
            for frame_path in sample_paths:
                try:
                    with open(frame_path, "rb") as f:
                        img_data = base64.b64encode(f.read()).decode("utf-8")
                        image_contents.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{img_data}", "detail": "low"}
                        })
                except Exception as e:
                    logger.warning(f"Could not encode frame {frame_path}: {e}")
            
            if not image_contents:
                return {"error": "No frames to analyze"}
            
            system_prompt = """You are an expert cinematographer and visual style analyst.
Analyze these video frames to create a comprehensive VISUAL STYLE GUIDE that can be used to recreate videos with the EXACT SAME visual style.

Your analysis must capture every visual detail that makes this video unique, so AI-generated content will look like it belongs to the same video.

Focus on:
1. **Color Palette**: Exact dominant colors, color temperature, saturation levels, contrast
2. **Lighting**: Type (natural/studio/mixed), direction, intensity, shadows, highlights
3. **Composition**: Framing style, rule of thirds usage, negative space, subject placement
4. **Camera**: Typical angles, distances (close-up/medium/wide), movement patterns
5. **Mood/Atmosphere**: Overall feeling, energy level, professional vs casual
6. **Subjects**: How people/products are typically shown in this video
7. **Background Style**: Types of backgrounds, blur levels, environment style
8. **Quality/Finish**: Resolution feel, film grain, sharpness, professional polish level"""

            user_prompt = f"""Analyze these {len(image_contents)} frames from a video and create a DETAILED VISUAL STYLE GUIDE.

This style guide will be used to generate NEW images and videos that MUST look like they belong to the same video.

Return JSON:
{{
    "color_palette": {{
        "dominant_colors": ["#HEXCODE1", "#HEXCODE2", "..."],
        "color_temperature": "warm" | "cool" | "neutral",
        "saturation": "high" | "medium" | "low",
        "contrast": "high" | "medium" | "low",
        "color_description": "Detailed description of the color grading"
    }},
    "lighting": {{
        "type": "natural" | "studio" | "mixed" | "ambient",
        "direction": "front" | "side" | "back" | "overhead" | "mixed",
        "intensity": "bright" | "medium" | "dim" | "moody",
        "shadow_style": "soft" | "hard" | "minimal",
        "lighting_description": "Detailed description of lighting"
    }},
    "composition": {{
        "primary_framing": "close-up" | "medium" | "wide" | "extreme close-up" | "varied",
        "subject_placement": "centered" | "rule-of-thirds" | "off-center" | "varied",
        "negative_space": "minimal" | "balanced" | "abundant",
        "depth_of_field": "shallow" | "medium" | "deep",
        "composition_description": "Detailed description of composition style"
    }},
    "camera_style": {{
        "typical_angles": ["eye-level", "low-angle", "high-angle", "dutch"],
        "typical_distances": ["close-up", "medium", "wide"],
        "movement_tendency": "static" | "subtle" | "dynamic" | "handheld",
        "camera_description": "How the camera typically behaves"
    }},
    "mood_atmosphere": {{
        "overall_mood": "energetic" | "calm" | "professional" | "casual" | "intimate" | "dramatic",
        "energy_level": "high" | "medium" | "low",
        "style_category": "lifestyle" | "product-focused" | "testimonial" | "tutorial" | "artistic",
        "mood_description": "The emotional feeling of the video"
    }},
    "subject_presentation": {{
        "human_subjects": "present" | "hands-only" | "none",
        "human_style": "Description of how people appear (age, style, ethnicity, clothing)",
        "product_presentation": "in-use" | "displayed" | "both",
        "focus_subject": "product" | "person" | "balanced"
    }},
    "background_style": {{
        "environment": "indoor" | "outdoor" | "mixed" | "abstract",
        "background_type": "home" | "studio" | "nature" | "urban" | "minimal",
        "blur_level": "bokeh" | "slight" | "sharp",
        "background_description": "Typical background characteristics"
    }},
    "quality_finish": {{
        "resolution_feel": "cinematic" | "social-media" | "professional" | "amateur",
        "post_processing": "heavy" | "moderate" | "minimal" | "raw",
        "overall_polish": "highly-polished" | "natural" | "casual"
    }},
    "style_prompt_prefix": "A 50-word prompt prefix that captures the EXACT visual style to prepend to any image generation prompt",
    "style_prompt_suffix": "A 30-word prompt suffix with technical details to append to any image generation prompt",
    "motion_style_guide": "Description of how motion/animation should feel to match this video's style"
}}"""

            user_content = [{"type": "text", "text": user_prompt}] + image_contents
            
            response = self.client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}
                ],
                max_tokens=3000,
                temperature=0.3,
                response_format={"type": "json_object"}
            )
            
            content = response.choices[0].message.content
            if content is None:
                logger.warning("⚠️ [STYLE] Analysis returned None")
                return {}
            
            result = json.loads(content.strip())
            
            # Log key findings
            logger.info(f"✅ [STYLE] Analysis complete:")
            logger.info(f"   Color temp: {result.get('color_palette', {}).get('color_temperature', 'unknown')}")
            logger.info(f"   Lighting: {result.get('lighting', {}).get('type', 'unknown')}")
            logger.info(f"   Composition: {result.get('composition', {}).get('primary_framing', 'unknown')}")
            logger.info(f"   Mood: {result.get('mood_atmosphere', {}).get('overall_mood', 'unknown')}")
            
            # Log the style prompt that will be used
            style_prefix = result.get("style_prompt_prefix", "")
            if style_prefix:
                logger.info(f"   Style prefix: {style_prefix[:80]}...")
            
            return result
            
        except Exception as e:
            logger.error(f"❌ [STYLE] Analysis error: {e}")
            return {"error": str(e)}
    
    def enhance_prompt_with_product(
        self,
        original_prompt: str,
        product_description: str,
        article_text: str = "",
        product_info: Dict[str, Any] = None,
        scene_context: str = None,
        video_style: Dict[str, Any] = None
    ) -> str:
        """Enhance an image generation prompt with product details, usage context, AND video style matching.
        
        This enhanced version understands:
        - HOW the product should appear in each scene (static, being used, lifestyle)
        - The VISUAL STYLE of the original video (colors, lighting, composition)
        - Generates prompts that create images matching the original video's look
        
        Args:
            original_prompt: The original image prompt from scene analysis.
            product_description: Detailed product description from detection.
            article_text: Optional new article content to adapt the scene to.
            product_info: Full product detection result with usage contexts.
            scene_context: Specific context for this scene (e.g., "being_applied", "static_display").
            video_style: Visual style analysis from analyze_video_style().
            
        Returns:
            Enhanced prompt string with product emphasis, context, and style matching.
        """
        try:
            logger.info("🎨 [PRODUCT] Enhancing prompt with product details and context...")
            
            # Extract additional context from product_info
            product_purpose = ""
            product_usage_method = ""
            usage_contexts = []
            scene_plan_info = None
            
            if product_info:
                product_purpose = product_info.get("product_purpose", "")
                product_usage_method = product_info.get("product_usage_method", "")
                usage_contexts = product_info.get("usage_contexts", [])
                scene_plan_info = product_info.get("scene_plan")  # From video structure analysis
            
            # Extract story context info if available (with type safety)
            story_context_info = product_info.get("story_context", {}) if product_info else {}
            # Ensure story_context_info is a dict, not a string
            if not isinstance(story_context_info, dict):
                story_context_info = {}
            story_type = story_context_info.get("story_type", "")
            story_summary = story_context_info.get("story_summary", "")
            scene_subject_appearance = story_context_info.get("scene_subject_appearance", "")
            has_visible_change = story_context_info.get("has_visible_change", False)
            start_state = story_context_info.get("start_state", "")
            end_state = story_context_info.get("end_state", "")
            essential_beats = story_context_info.get("essential_story_beats", [])
            must_preserve = story_context_info.get("must_preserve", [])
            
            # Scene-specific details (with type safety)
            scene_details = story_context_info.get("scene_details", {})
            if not isinstance(scene_details, dict):
                scene_details = {}
            scene_physical_state = scene_details.get("physical_state", "")
            scene_action = scene_details.get("action", "")
            scene_purpose = scene_details.get("purpose", "")
            scene_emotional_beat = scene_details.get("emotional_beat", "")
            
            # Determine the scene context from original prompt if not provided
            if not scene_context:
                # Try to infer context from original prompt
                prompt_lower = original_prompt.lower()
                if any(word in prompt_lower for word in ["apply", "applying", "putting", "placing", "stick", "press"]):
                    scene_context = "being_applied"
                elif any(word in prompt_lower for word in ["hold", "holding", "hand", "hands", "showing"]):
                    scene_context = "in_hand"
                elif any(word in prompt_lower for word in ["close", "detail", "zoom", "macro"]):
                    scene_context = "close_up"
                elif any(word in prompt_lower for word in ["before", "after", "result", "transform"]):
                    scene_context = "before_after"
                else:
                    scene_context = "static_display"
            
            # System prompt for enhancement with INTELLIGENT product usage logic
            system_prompt = """You are an expert visual director. Your job is to create prompts that show products EXACTLY as they appear in the original video, with LOGICAL real-world usage.

⚠️⚠️⚠️ TWO ABSOLUTE RULES - NEVER BREAK THESE ⚠️⚠️⚠️

RULE 1: PRODUCT MUST MATCH ORIGINAL EXACTLY
- Copy the EXACT visual description provided (color, shape, size, materials, patterns)
- Do NOT invent new designs or change the product's appearance
- The generated image must show the SAME product from the original video

RULE 2: USAGE MUST BE PHYSICALLY LOGICAL
- Think: "In real life, how would someone ACTUALLY use this product?"
- Apply common sense for each product type

========================================
ABSOLUTE LOGIC RULES (ZERO TOLERANCE):
========================================

🩹 **ADHESIVE PATCHES/STICKERS (weight loss, pain relief, etc.):**
   THEY STICK TO SKIN, NOT FABRIC!
   
   ✅ CORRECT PLACEMENT:
   - Bare stomach (shirt lifted or no shirt)
   - Bare upper arm
   - Bare thigh
   - Bare back/shoulder
   
   ❌ ABSOLUTELY FORBIDDEN:
   - On top of shirt/clothing
   - On pants/jeans
   - On shoes
   - On any fabric
   
   📝 HOW TO SHOW APPLICATION:
   - Person lifts shirt to expose bare stomach → applies patch to skin
   - Person in tank top → patch on bare shoulder
   - Close-up of bare skin with patch adhered to it

🐕 **PET PRODUCTS (toys, food, accessories):**
   THE PET MUST BE PRESENT AND INTERACTING!
   
   ✅ CORRECT:
   - Dog actively playing with/chewing the toy
   - Cat batting/chasing the toy
   - Pet eating from bowl
   
   ❌ FORBIDDEN:
   - Toy sitting alone on table/floor
   - Pet product without any pet visible

👐 **HANDHELD PRODUCTS:**
   - Natural grip, correct orientation
   - Size proportional to hand
   - Being used for its actual purpose

💍 **WEARABLES:**
   - On correct body part
   - Properly fitted, not floating

========================================
YOUR THOUGHT PROCESS:
========================================
Before writing, mentally simulate:
1. "I am holding this product. Where would I PUT it?"
2. "If this goes on skin, the skin must be VISIBLE and BARE"
3. "If this is for a pet, the PET must be VISIBLE"
4. "Does my prompt pass the 'common sense' test?"

========================================
🎬 VIDEO STORYTELLING AWARENESS:
========================================
Videos tell STORIES. Subjects may appear DIFFERENTLY in different scenes because:
- They change over time (weight loss, skin improvement, mood change)
- They're shown in different situations (before using product vs after)
- The story has an arc (problem → solution → result)

YOUR JOB: Follow the STORY CONTEXT provided for each scene.
- If told "subject appears overweight in this scene" → show them overweight
- If told "subject appears slim and confident" → show them slim and confident
- If told "subject is applying the product" → show that action

The same person CAN and SHOULD look different between scenes if the story requires it!

Keep prompt under 4000 characters."""

            # Determine product type for specific logic
            product_type = product_info.get('product_detected', 'unknown').lower() if product_info else 'unknown'
            is_patch = any(word in product_type for word in ['patch', 'sticker', 'adhesive', 'bandage'])
            is_cream = any(word in product_type for word in ['cream', 'lotion', 'gel', 'serum', 'ointment'])
            is_pet_product = any(word in product_type for word in ['dog', 'cat', 'pet', 'toy']) or (product_purpose and any(word in product_purpose.lower() for word in ['dog', 'cat', 'pet']))
            
            # Build specific warning based on product type
            product_specific_warning = ""
            if is_patch:
                product_specific_warning = """
⚠️⚠️⚠️ THIS IS AN ADHESIVE PATCH - CRITICAL RULES ⚠️⚠️⚠️
This product STICKS TO BARE SKIN. It CANNOT stick to fabric/clothing.

YOU MUST SHOW:
- BARE SKIN visible (stomach, arm, thigh, back)
- Patch applied DIRECTLY to skin surface
- If showing application: person lifts clothing to expose bare skin

YOU MUST NOT SHOW:
- Patch on top of shirt/clothing
- Patch on fabric of any kind
- Patch floating or not adhered to anything
"""
            elif is_cream:
                product_specific_warning = """
⚠️ THIS IS A CREAM/GEL - IT GOES ON BARE SKIN
Show application to visible bare skin (face, arms, body).
Do NOT show cream on clothing.
"""
            elif is_pet_product:
                product_specific_warning = """
⚠️ THIS IS A PET PRODUCT - THE PET MUST BE VISIBLE
Show a real dog/cat actively interacting with the product.
Do NOT show the product alone without the pet.
"""
            
            # Build story context instruction - DYNAMIC based on video analysis
            story_instruction = ""
            if story_context_info:
                story_instruction = f"""
🎬 VIDEO STORY CONTEXT:
Story Type: {story_type if story_type else 'commercial/advertisement'}
Story Summary: {story_summary if story_summary else 'Product advertisement'}

"""
                # Add scene-specific subject appearance if available
                if scene_subject_appearance:
                    story_instruction += f"""
⚠️⚠️⚠️ CRITICAL - SUBJECT APPEARANCE FOR THIS SCENE ⚠️⚠️⚠️
In THIS specific scene, the subject(s) MUST appear as:
{scene_subject_appearance}

This is EXACTLY how they should look - follow this description precisely!
"""
                elif scene_physical_state:
                    story_instruction += f"""
⚠️ SUBJECT STATE IN THIS SCENE:
Physical state: {scene_physical_state}
Action: {scene_action if scene_action else 'As shown in original'}
"""
                
                # Add scene purpose context
                if scene_purpose:
                    story_instruction += f"""
📍 SCENE PURPOSE: {scene_purpose}
Emotional beat: {scene_emotional_beat if scene_emotional_beat else 'Match the original mood'}
"""
                
                # If video has visible subject changes, note it
                if has_visible_change and (start_state or end_state):
                    story_instruction += f"""
📊 NOTE - Subject changes throughout video:
- Start of video: {start_state}
- End of video: {end_state}
Make sure this scene matches the CORRECT state for its position in the story!
"""
                
                # Add must-preserve elements
                if must_preserve:
                    story_instruction += f"""
🔒 MUST PRESERVE in this scene: {', '.join(must_preserve[:3])}
"""
            
            # Build the user prompt with STORY CONTEXT and LOGIC CHECK
            user_prompt = f"""Create a prompt that recreates this scene while maintaining the VIDEO'S STORY.

{story_instruction}
{product_specific_warning}

**THE PRODUCT (maintain visual consistency):**
Type: {product_type}
Visual Description: {product_description}
Purpose: {product_purpose if product_purpose else "Commercial product"}
How It's Used: {product_usage_method if product_usage_method else "Standard usage"}

**ORIGINAL SCENE TO RECREATE:**
{original_prompt}

**RECREATION RULES:**
1. SUBJECT APPEARANCE: Follow the story context above - subjects may look different in different scenes!
2. PRODUCT ACCURACY: Show product exactly as described
3. SCENE PURPOSE: This scene serves a specific purpose in the story - preserve that purpose
4. PHYSICAL LOGIC: Apply common sense (patches on bare skin, pets with pet products, etc.)

**LOGIC CHECK:**
- If it's a patch/sticker: Is it shown on BARE SKIN? If not, FIX IT.
- If it's a pet product: Is there a pet interacting? If not, ADD ONE.
- If subject appearance is specified above: Does the prompt match that appearance? If not, FIX IT.

**CREATE THE SCENE:**
Scene Context: {scene_context}

"""
            
            # Add specific context instructions WITH STRICT LOGIC
            if scene_context == "being_applied":
                if is_patch:
                    user_prompt += """
**APPLICATION SCENE FOR PATCH:**
🩹 REQUIRED: Show patch being applied to BARE SKIN
- Person lifts shirt → bare stomach visible → patch placed on bare stomach
- OR bare arm/shoulder visible → patch on bare arm
- The SKIN must be VISIBLE where the patch is placed
- ❌ NEVER show patch on clothing/shirt/fabric
"""
                elif is_pet_product:
                    user_prompt += """
**APPLICATION SCENE FOR PET PRODUCT:**
🐕 REQUIRED: Show pet actively interacting with the product
- Dog/cat must be visible in the scene
- Pet is playing with, chewing, or using the product
- ❌ NEVER show product alone without pet
"""
                else:
                    user_prompt += """
**APPLICATION SCENE:**
- Show product being used for its actual purpose
- Realistic hand/body positioning
- Logical, believable action
"""
            elif scene_context == "in_hand":
                user_prompt += """
**IN-HAND SCENE:**
- Natural hand grip, product clearly visible
- Correct scale relative to hand
"""
            elif scene_context == "static_display":
                user_prompt += """
**STATIC DISPLAY:**
- Product prominently displayed
- Clear, detailed view
"""
            elif scene_context == "close_up":
                user_prompt += """
**CLOSE-UP:**
- Detailed view of product features
- Match original product exactly
"""
            elif scene_context == "lifestyle":
                if is_patch:
                    user_prompt += """
**LIFESTYLE SCENE FOR PATCH:**
🩹 If patch is visible, it MUST be on BARE SKIN
- Person going about daily life with patch on bare stomach/arm
- Skin must be exposed where patch is shown
"""
                elif is_pet_product:
                    user_prompt += """
**LIFESTYLE SCENE FOR PET PRODUCT:**
🐕 Pet must be visible and happy with the product
"""
                else:
                    user_prompt += """
**LIFESTYLE SCENE:**
- Product in natural, everyday context
- Realistic usage scenario
"""
            
            # Add scene plan information if available (from video structure analysis)
            if scene_plan_info:
                user_prompt += f"""
**SCENE NARRATIVE ROLE:** {scene_plan_info.get('narrative_role', 'general')}
**KEY MESSAGE FOR THIS SCENE:** {scene_plan_info.get('key_message', 'Show the product')}
**VISUAL SUGGESTION:** {scene_plan_info.get('visual_suggestion', '')}
"""
            
            # Add video style matching instructions if available
            if video_style and not video_style.get("error"):
                style_prefix = video_style.get("style_prompt_prefix", "")
                style_suffix = video_style.get("style_prompt_suffix", "")
                color_info = video_style.get("color_palette", {})
                lighting_info = video_style.get("lighting", {})
                composition_info = video_style.get("composition", {})
                mood_info = video_style.get("mood_atmosphere", {})
                
                user_prompt += f"""

🎨 **CRITICAL: MATCH ORIGINAL VIDEO STYLE** 🎨
The generated image MUST match the visual style of the original video:

**COLOR STYLE:**
- Temperature: {color_info.get('color_temperature', 'neutral')}
- Saturation: {color_info.get('saturation', 'medium')}
- Contrast: {color_info.get('contrast', 'medium')}
- {color_info.get('color_description', '')}

**LIGHTING:**
- Type: {lighting_info.get('type', 'natural')}
- Direction: {lighting_info.get('direction', 'front')}
- Intensity: {lighting_info.get('intensity', 'medium')}
- {lighting_info.get('lighting_description', '')}

**COMPOSITION:**
- Framing: {composition_info.get('primary_framing', 'medium')}
- Subject placement: {composition_info.get('subject_placement', 'centered')}
- Depth of field: {composition_info.get('depth_of_field', 'medium')}
- {composition_info.get('composition_description', '')}

**MOOD:**
- Overall: {mood_info.get('overall_mood', 'professional')}
- Energy: {mood_info.get('energy_level', 'medium')}

**USE THIS STYLE PREFIX:** {style_prefix}
**USE THIS STYLE SUFFIX:** {style_suffix}

INCORPORATE these style elements into your enhanced prompt!
"""
            
            if article_text:
                article_summary = article_text[:800] + "..." if len(article_text) > 800 else article_text
                user_prompt += f"""
**NEW CONTEXT/ARTICLE TO ADAPT TO:**
{article_summary}
"""
            
            user_prompt += """
**FINAL INSTRUCTIONS:**
Generate a prompt that:
1. Shows LOGICAL, REALISTIC product usage (fix any illogical placements)
2. Keeps the product's EXACT visual appearance (colors, shape, materials)
3. Places product in correct context for {scene_context}
4. Makes physical and common sense

⚠️ REALITY CHECK before output:
- "Is this how a real person would use this product?" 
- "Does this placement make physical sense?"
- If NO → FIX IT to be realistic

OUTPUT ONLY the corrected, realistic prompt text.""".format(scene_context=scene_context)

            response = self.client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                max_tokens=1200,
                temperature=0.3
            )
            
            content = response.choices[0].message.content
            if content is None:
                logger.warning("⚠️ [PRODUCT] Enhancement returned None, using original prompt")
                return original_prompt
            
            enhanced_prompt = content.strip()
            
            # Extract size and usage details from product_info
            size_info = ""
            usage_action = ""
            if product_info:
                details = product_info.get("product_details", {})
                if details:
                    dims = details.get("dimensions", "")
                    shape = details.get("shape", "")
                    if dims or shape:
                        size_info = f"SIZE: {shape}, {dims}"
                
                usage_method = product_info.get("product_usage_method", "")
                if usage_method:
                    usage_action = f"USAGE ACTION: {usage_method[:200]}"
            
            # Wrap with emphasis for image generator - with SIZE and USAGE emphasis
            final_prompt = f"""[CRITICAL - PRODUCT SIZE AND APPEARANCE MUST BE EXACT]
PRODUCT VISUAL: {product_description[:350]}
{size_info}
SCENE TYPE: {scene_context}
{usage_action if scene_context == "being_applied" else ""}

{enhanced_prompt}

[IMPORTANT: Product must be shown at CORRECT PROPORTIONAL SIZE relative to hands/body. If being applied, show the EXACT application action described above.]"""
            
            # Truncate if needed (Nano Banana limit is 4000 chars)
            if len(final_prompt) > 4000:
                final_prompt = final_prompt[:3997] + "..."
            
            logger.info(f"✅ [PRODUCT] Prompt enhanced ({len(final_prompt)} chars) - context: {scene_context}")
            return final_prompt
            
        except Exception as e:
            logger.error(f"❌ [PRODUCT] Enhancement error: {e}")
            # Return original prompt with basic product emphasis
            return f"[Product: {product_description[:200]}] {original_prompt}"
    
    def enhance_motion_prompt_with_product(
        self,
        original_motion_prompt: str,
        product_info: Dict[str, Any],
        scene_context: str = None,
        video_style: Dict[str, Any] = None
    ) -> str:
        """Enhance motion/animation prompt to accurately show product usage AND match video style.
        
        This ensures the video animation correctly shows:
        - How the product is being applied/used
        - The correct size and proportions in motion
        - The proper action sequence
        - Camera movements matching the original video's style
        
        Args:
            original_motion_prompt: The original motion prompt.
            product_info: Product detection results with usage details.
            scene_context: How the product appears in this scene.
            video_style: Visual style analysis from analyze_video_style().
            
        Returns:
            Enhanced motion prompt with accurate product usage and style matching.
        """
        if not product_info or not product_info.get("has_product"):
            return original_motion_prompt
        
        try:
            # Extract product details
            product_type = product_info.get("product_detected", "product")
            usage_method = product_info.get("product_usage_method", "")
            product_details = product_info.get("product_details", {})
            
            # Get size info
            size_info = ""
            if product_details:
                shape = product_details.get("shape", "")
                dims = product_details.get("dimensions", "")
                if shape or dims:
                    size_info = f"{shape}, {dims}"
            
            # Build context-specific motion instructions
            motion_instruction = ""
            
            if scene_context == "being_applied":
                # Describe the application action with ABSOLUTE LOGIC
                is_patch_product = "patch" in product_type.lower() or "sticker" in product_type.lower() or "adhesive" in product_type.lower()
                is_cream_product = "cream" in product_type.lower() or "lotion" in product_type.lower() or "gel" in product_type.lower()
                is_pet_toy = "toy" in product_type.lower() and ("dog" in product_type.lower() or "pet" in product_type.lower() or (usage_method and any(pet in usage_method.lower() for pet in ["dog", "cat", "pet"])))
                
                if is_patch_product:
                    motion_instruction = f"""MOTION: Patch application to BARE SKIN

⚠️⚠️⚠️ ABSOLUTE RULE: PATCH GOES ON BARE SKIN, NOT CLOTHING ⚠️⚠️⚠️

REQUIRED SEQUENCE:
1. Hands holding {product_type} ({size_info})
2. Person LIFTS SHIRT to expose BARE STOMACH (or bare arm/thigh visible)
3. Hands move patch toward BARE SKIN surface
4. Press patch onto BARE SKIN with gentle pressure
5. Smooth edges onto BARE SKIN
6. Patch is now ADHERED TO SKIN, not floating

❌ FORBIDDEN: Patch touching any fabric/clothing
✅ REQUIRED: Visible bare skin where patch is applied

Camera: Close-up showing bare skin clearly"""
                    
                elif is_cream_product:
                    motion_instruction = f"""MOTION: Cream application to BARE SKIN
1. Dispense product onto fingertips
2. Apply to BARE SKIN (face/arms/body - skin must be visible)
3. Gentle massage motion
Camera: Focus on bare skin and application"""
                    
                elif is_pet_toy:
                    motion_instruction = f"""MOTION: Dog/Pet playing with toy

⚠️ REQUIRED: A DOG/PET MUST BE VISIBLE AND INTERACTING ⚠️

SEQUENCE:
1. Dog sees the {product_type} ({size_info})
2. Dog excitedly approaches/grabs the toy
3. Dog plays - tugging, chewing, shaking
4. Joyful pet interaction throughout
5. Toy and dog move together dynamically

❌ FORBIDDEN: Toy alone without pet
✅ REQUIRED: Happy dog actively playing with toy

Camera: Follow dog and toy interaction"""
                    
                else:
                    motion_instruction = f"""MOTION: Product in realistic use
1. Product ({product_type}) held/used naturally
2. Show actual intended purpose
3. Logical, believable movement
USAGE: {usage_method[:150] if usage_method else 'Standard usage'}"""
                    
            elif scene_context == "in_hand":
                motion_instruction = f"""MOTION: Product showcase in hand:
1. Hand holding {product_type} ({size_info}) - product fills frame appropriately
2. Slight rotation or movement to show product details
3. Stable, professional presentation
Camera: Focus on product, slight movement for dynamism"""
                
            elif scene_context == "static_display":
                motion_instruction = f"""MOTION: Static product beauty shot:
1. {product_type} ({size_info}) displayed prominently
2. Subtle camera movement (slow zoom or pan)
3. Product remains centered and sharp
Camera: Smooth, cinematic movement around product"""
                
            elif scene_context == "lifestyle":
                motion_instruction = f"""MOTION: Lifestyle scene with product:
1. Natural environment movement
2. {product_type} visible and in-scale with surroundings
3. Organic camera movement
USAGE: {usage_method[:100] if usage_method else 'Product in natural context'}"""
            
            else:
                motion_instruction = f"""MOTION: Show {product_type} ({size_info}):
- Product clearly visible and correctly sized
- Smooth, professional camera movement
{f'USAGE: {usage_method[:100]}' if usage_method else ''}"""
            
            # Add video style matching if available
            style_motion_guide = ""
            if video_style and not video_style.get("error"):
                camera_style = video_style.get("camera_style", {})
                mood = video_style.get("mood_atmosphere", {})
                motion_guide = video_style.get("motion_style_guide", "")
                
                style_motion_guide = f"""
CAMERA STYLE TO MATCH:
- Movement: {camera_style.get('movement_tendency', 'subtle')}
- Typical angles: {', '.join(camera_style.get('typical_angles', ['eye-level']))}
- Energy: {mood.get('energy_level', 'medium')}
- {motion_guide if motion_guide else ''}
"""
            
            # Combine with original prompt
            enhanced_motion = f"""{motion_instruction}

ORIGINAL SCENE: {original_motion_prompt}
{style_motion_guide}
[CRITICAL: Product must be CORRECT SIZE relative to hands/body. {product_type} is {size_info}]"""
            
            # Truncate if too long
            if len(enhanced_motion) > 2500:
                enhanced_motion = enhanced_motion[:2497] + "..."
            
            logger.info(f"✅ [PRODUCT] Motion prompt enhanced for {scene_context}")
            return enhanced_motion
            
        except Exception as e:
            logger.error(f"❌ [PRODUCT] Motion enhancement error: {e}")
            return original_motion_prompt
    
    def _generate_image_prompt(
        self, 
        image_contents: List[Dict],
        manual_instructions: str = ""
    ) -> Dict[str, Any]:
        """Generate image recreation prompt using gpt-4o-mini.
        
        Args:
            image_contents: List of base64 encoded images.
            manual_instructions: Optional custom instructions.
            
        Returns:
            Dict with analysis, text_content, and first_prompt.
        """
        try:
            # Comprehensive analysis prompt for image recreation
            analysis_prompt = """Analyze this image in comprehensive detail. You must extract and describe ALL of the following:

**1. TEXT CONTENT (CRITICAL - TRANSCRIBE EXACTLY):**
- Transcribe ALL text visible in the image EXACTLY as written, preserving:
  - The exact wording (letter by letter)
  - The language it's written in
  - Line breaks and text positioning
  - Font style (bold, italic, etc.)
  - Text color
  - Text size (relative - large headline, medium body, small caption)
  - Text placement on the image (top, center, bottom, left, right)

**2. UI ELEMENTS (BUTTONS, LABELS, BADGES):**
- Describe any buttons: shape, color, text on button, position
- Describe any labels, badges, or tags
- Describe any call-to-action elements
- Note the exact text on each UI element

**3. VISUAL STYLE:**
- Overall style (photographic, illustrated, etc.)
- Color palette and mood
- Background description
- Lighting and atmosphere

**4. LAYOUT AND COMPOSITION:**
- How elements are arranged
- Text overlay positioning relative to background
- Any graphic elements (shapes, lines, icons)

**5. DESIGN ELEMENTS:**
- Any overlays, gradients, or effects on the background
- Shadow or glow effects on text
- Border or frame elements

**6. MOTION ANALYSIS (from frame sequence):**
- Identify camera movement (pan, zoom, tilt, static)
- Subject movement or animation
- Any transitions or effects between frames

**7. BACKGROUND:**
- Elements (buildings, nature, interior, abstract, etc.)
- Weather conditions (sunny, cloudy, rainy, foggy, night, etc.)
- Dominant colors and color scheme

**8. PEOPLE/CHARACTERS:**
- Estimated age range
- Gender
- Hair color and style
- Clothing description (type, color, style)
- Body position and movement

**9. OBJECTS:**
- Type of objects visible
- Purpose/use of each object
- Color of objects
- Size (small, medium, large, relative to frame)

**10. CAMERA:**
- Camera angle (eye-level, low angle, high angle, bird's eye, Dutch angle)
- Camera lens type (wide-angle, telephoto, macro, fisheye, standard)"""

            # Base system prompt for image recreation
            base_system_prompt = """You are an expert video content analyst and prompt engineer specializing in image recreation.

Based on your detailed analysis of the video frames, generate a text-to-image prompt to recreate the starting frame.

**FLEXIBLE GUIDELINES FOR first_prompt (adapt based on user instructions):**
The following are DEFAULT guidelines. User instructions may ask you to EXCLUDE or MODIFY certain elements. 
Always follow user instructions - they take priority over these defaults:

DEFAULT elements to include (unless user says otherwise):
- Text/typography if present (exact wording, positioning, font style, colors)
- Background and visual style
- UI elements (buttons, labels, badges)
- People/characters with accurate details
- Objects with their positions
- Camera angle and perspective

**CRITICAL RULES:**
1. If user instructions say to EXCLUDE something (e.g., "no text", "remove UI elements"), you MUST NOT include it in the first_prompt
2. Be CONSISTENT - apply user instructions to ALL aspects of your output
3. The first_prompt MUST be under 4000 characters maximum
4. Still do your analysis in the 'analysis' field, but the 'first_prompt' must respect user exclusions

Return your response as JSON with these keys:
- analysis: Your detailed analysis following the categories from the user prompt (for reference)
- text_content: {exact_text: string, language: string, position: string, style: string} (can be empty if user excludes text)
- first_prompt: Complete prompt to recreate the starting frame (for text-to-image) - MAX 4000 characters, respecting user exclusions"""

            # Prepend user's manual instructions to system prompt if provided
            if manual_instructions:
                system_prompt = f"""**🚨 USER INSTRUCTIONS (HIGHEST PRIORITY - MUST FOLLOW):**
{manual_instructions}

These instructions OVERRIDE any conflicting default guidelines below. Apply them consistently to your entire output, especially the 'first_prompt'.

---

{base_system_prompt}"""
            else:
                system_prompt = base_system_prompt

            # Build user content
            instruction_text = "Analyze these frames from a video scene and generate an image recreation prompt:"
            
            user_content = [
                {"type": "text", "text": instruction_text},
                {"type": "text", "text": analysis_prompt}
            ] + image_contents
            
            response = self.client.chat.completions.create(
                model="gpt-5-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}
                ],
                max_completion_tokens=128000,  # gpt-5-mini supports up to 128k output tokens
                #temperature=0.2,
                response_format={"type": "json_object"}
            )
            
            # Check for None content (can happen with API issues or content filtering)
            content = response.choices[0].message.content
            if content is None:
                logger.warning("⚠️ OpenAI returned None content for image prompt")
                return {"analysis": "", "text_content": {}, "first_prompt": ""}
            
            result_text = content.strip()
            result = json.loads(result_text)
            
            logger.info("✅ Image prompt generated successfully")
            return result
            
        except Exception as e:
            logger.error(f"❌ Error generating image prompt: {e}")
            return {
                "analysis": "",
                "text_content": {},
                "first_prompt": ""
            }
    
    def _generate_motion_prompt(
        self, 
        image_contents: List[Dict],
        manual_instructions: str = ""
    ) -> Dict[str, Any]:
        """Generate motion/animation prompt using gpt-4o.
        
        Args:
            image_contents: List of base64 encoded images.
            manual_instructions: Optional custom instructions.
            
        Returns:
            Dict with second_prompt (motion prompt).
        """
        try:
            # Motion-focused analysis prompt
            motion_analysis_prompt = """Analyze these video frames to understand the motion and animation.

Focus ONLY on:
1. Camera movement (pan left/right, tilt up/down, zoom in/out, dolly, crane, static)
2. Subject movement (walking, running, gesturing, facial expressions)
3. Object movement (falling, flying, rotating, scaling)
4. Transitions and effects between frames
5. Speed and timing of movements
6. Direction of movement"""

            # System prompt for motion generation
            system_prompt = """You are an expert in video motion analysis and prompt engineering for AI video generation.

Based on your analysis of the frame sequence, generate a motion prompt for Runway AI video generation.

For the motion prompt (second_prompt):
- Describe the camera movement precisely (pan, tilt, zoom, dolly, etc.)
- Describe any subject movement or animation
- **CRITICAL: If there are people in the scene, emphasize expressive and dynamic facial expressions:**
  * Describe specific emotions: confident smiles, curious looks, excited expressions, thoughtful gazes
  * Include micro-expressions and natural human reactions
  * Add subtle head movements, eyebrow raises, or lip movements
- **Make animations engaging and lively, NOT static or boring**
- Include timing and speed information
- Make it suitable for Runway AI video generation
- Keep it concise but descriptive

Return your response as JSON with this key:
- second_prompt: Motion prompt to create the same movement (for image-to-video)"""

            # Build user content
            instruction_text = "Analyze the motion in these frames and generate a Runway motion prompt:"
            if manual_instructions:
                instruction_text += f"\n\n**SPECIAL INSTRUCTIONS FROM USER:**\n{manual_instructions}"
            
            user_content = [
                {"type": "text", "text": instruction_text},
                {"type": "text", "text": motion_analysis_prompt}
            ] + image_contents
            
            response = self.client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}
                ],
                max_tokens=2000,  # Motion prompts are shorter
                temperature=0.3,
                response_format={"type": "json_object"}
            )
            
            # Check for None content (can happen with API issues or content filtering)
            content = response.choices[0].message.content
            if content is None:
                logger.warning("⚠️ OpenAI returned None content for motion prompt")
                return {"second_prompt": ""}
            
            result_text = content.strip()
            result = json.loads(result_text)
            
            logger.info("✅ Motion prompt generated successfully")
            return result
            
        except Exception as e:
            logger.error(f"❌ Error generating motion prompt: {e}")
            return {
                "second_prompt": ""
            }
    
    def analyze_full_video(
        self,
        frame_paths_with_timestamps: List[Tuple[float, str]],
        pyscenedetect_timestamps: List[float],
        video_duration: float,
        manual_instructions: str = "",
        cta_button: bool = False,
        cta_text: str = "",
        row_num: int = 0,
        article_text: str = "",
        vertical: str = "",
        article_language: str = "",
        article_related_to_video: bool = True
    ) -> Dict[str, Any]:
        """Analyze entire video and generate scene timestamps + prompts in a single call.
        
        This unified approach sends all frames to OpenAI, which then:
        1. Validates/corrects PySceneDetect scene times
        2. Generates image prompts for each scene
        3. Generates motion prompts for each scene
        
        If article content is provided, prompts will be adapted to match the article's
        topic, location, characters, and cultural context.
        
        Args:
            frame_paths_with_timestamps: List of (timestamp, frame_path) tuples for entire video.
            pyscenedetect_timestamps: Initial scene start times from PySceneDetect.
            video_duration: Total video duration in seconds.
            manual_instructions: Optional custom instructions from user.
            cta_button: Whether to include a CTA button in image prompts.
            cta_text: Text for the CTA button.
            row_num: Row number for logging purposes.
            article_text: Optional article content to adapt prompts to.
            vertical: Optional vertical/offer name for content adaptation.
            article_language: Optional language code for content adaptation.
            article_related_to_video: True if article is similar to video (adapt), False if different (create new).
            
        Returns:
            Dict with corrected_scenes and scene_prompts.
        """
        row_prefix = f"[Row {row_num}] " if row_num > 0 else ""
        try:
            logger.info(f"🎬 {row_prefix}Analyzing full video with OpenAI (unified call)...")
            logger.info(f"   {row_prefix}Frames: {len(frame_paths_with_timestamps)}")
            logger.info(f"   {row_prefix}PySceneDetect scenes: {len(pyscenedetect_timestamps)}")
            logger.info(f"   {row_prefix}Video duration: {video_duration:.2f}s")
            
            # Encode images to base64 with timestamp labels
            image_contents = []
            for timestamp, frame_path in frame_paths_with_timestamps:
                try:
                    with open(frame_path, 'rb') as f:
                        image_data = base64.b64encode(f.read()).decode('utf-8')
                        # Add timestamp label before each image
                        image_contents.append({
                            "type": "text",
                            "text": f"[Frame at {timestamp:.1f}s]"
                        })
                        image_contents.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{image_data}",
                                "detail": "high"
                            }
                        })
                except Exception as e:
                    logger.warning(f"⚠️ Could not read frame at {timestamp:.1f}s: {e}")
            
            if not image_contents:
                logger.error("❌ No frames could be loaded")
                return self._empty_video_analysis_result(pyscenedetect_timestamps, video_duration)
            
            # Format PySceneDetect timestamps for the prompt
            pyscene_info = "PySceneDetect detected scene changes at these timestamps:\n"
            for i, ts in enumerate(pyscenedetect_timestamps):
                if i + 1 < len(pyscenedetect_timestamps):
                    end_ts = pyscenedetect_timestamps[i + 1]
                else:
                    end_ts = video_duration
                duration = end_ts - ts
                pyscene_info += f"  Scene {i+1}: {ts:.2f}s - {end_ts:.2f}s (duration: {duration:.2f}s)\n"
            
            # System prompt for unified video analysis
            system_prompt = f"""You are an expert video analyst and prompt engineer. You will analyze video frames and generate scene-based prompts.

**VIDEO INFO:**
- Total duration: {video_duration:.2f} seconds
- Frames provided: 1 per second (with timestamps)

**YOUR TASKS:**

1. **VALIDATE/CORRECT SCENE TIMESTAMPS:**
   Review the PySceneDetect timestamps against the actual frames. Adjust scene boundaries if needed.
   
   RULES:
   - Each scene MUST be between {config.PYSCENEDETECT_MIN_SCENE_DURATION}-{config.PYSCENEDETECT_MAX_SCENE_DURATION} seconds
   - Scenes should align with actual visual/content changes in the frames
   - **IMPORTANT: As long as it's the same character(s) on screen, it's the same scene** - Don't split a scene just because of camera angle changes or minor visual transitions if the same person/character remains the focus
   - A new scene starts when: the main character changes, the location completely changes, or there's a clear cut to different content
   - The first scene always starts at 0.0s
   - The last scene ends at {video_duration:.2f}s
   - If a scene is too long (>{config.PYSCENEDETECT_MAX_SCENE_DURATION}s), split it at a natural point
   - If a scene is too short (<{config.PYSCENEDETECT_MIN_SCENE_DURATION}s), merge it with adjacent scene
   - Maximum {config.MAX_SCENES} scenes total

2. **FOR EACH SCENE, ANALYZE THESE CATEGORIES IN DETAIL:**

   **VISUAL STYLE:**
   - Overall style (photographic, illustrated, cinematic, animated, 3D rendered, etc.)
   - Color palette and mood (warm, cool, vibrant, muted, etc.)
   - Lighting type and atmosphere (natural, studio, dramatic, soft, harsh, etc.)

   **BACKGROUND:**
   - Environment elements (buildings, nature, interior, abstract, urban, rural, etc.)
   - Weather/environment conditions (sunny, cloudy, rainy, foggy, night, day, etc.)
   - Dominant colors and color scheme

   **PEOPLE/CHARACTERS:**
   - Gender (male, female, non-binary, unclear)
   - Estimated age range (child, teen, young adult, middle-aged, elderly)
   - Ethnicity/skin tone (for accurate recreation)
   - Hair color, length, and style
   - Facial features and expressions
   - Clothing description (type, color, style, brand if visible)
   - Body position, pose, and movement
   - Number of people in frame

   **OBJECTS:**
   - Type of objects visible
   - Purpose/use of each object
   - Color, texture, and material of objects
   - Size (small, medium, large, relative to frame)
   - Position in frame

   **VISUAL CONTENT:**
   - Focus on describing the VISUAL CONTENT: people, places, objects, environments
   - Describe what you SEE in the frame: colors, shapes, lighting, mood, composition

   **CAMERA:**
   - Camera angle (eye-level, low angle, high angle, bird's eye, Dutch angle, worm's eye)
   - Camera lens type (wide-angle, telephoto, macro, fisheye, standard, anamorphic)
   - Depth of field (shallow/bokeh, deep focus)
   - Framing (close-up, medium shot, wide shot, extreme close-up, full body)

   **MOTION (from frame sequence):**
   - Camera movement type (pan left/right, tilt up/down, zoom in/out, dolly, truck, crane, handheld, static)
   - Speed of camera movement (slow, medium, fast)
   - Subject movement or animation
   - Direction of movement
   - Any effects or transitions visible

3. **FOR EACH SCENE, GENERATE:**

   a) **image_prompt** (for text-to-image generation):
      - Describe the first frame of the scene in comprehensive detail
      - Include ALL of the following:
        * Visual style, colors, lighting, and atmosphere
        * Background environment with specific details
        * People/characters with gender, age, ethnicity, hair, clothing, pose
        * Objects with their positions, colors, and sizes
        * Camera angle, lens type, and framing
      - Make it detailed enough to recreate the image faithfully
      
      **YOUR IMAGE PROMPT MUST:**
      - Describe the visual scene: people, places, objects, environments, backgrounds
      - Include visual details: colors, shapes, lighting, mood, composition, camera angles
      - Focus on what IS visible in the scene: natural visual elements, subjects, settings
      
      - Maximum 4000 characters
   
   b) **motion_prompt** (for image-to-video generation):
      - Describe the camera movement precisely:
        * Type: pan, tilt, zoom, dolly, truck, crane, orbit, etc.
        * Direction: left, right, up, down, in, out
        * Speed: slow, medium, fast, gradual
      - Describe subject movement or animation:
        * What moves and how
        * Direction and speed of movement
      - **CRITICAL FOR PEOPLE: Create expressive and dynamic facial animations:**
        * Describe specific emotions: confident smiles, curious looks, excited expressions, thoughtful gazes
        * Include natural micro-expressions and reactions (eyebrow raises, subtle smiles, blinking)
        * Add natural head movements, slight nods, or turning towards camera
        * Make characters feel ALIVE and engaging, NOT static or robotic
      - Include timing information if relevant
      - Describe any effects or transitions
      - If the image has text overlays, describe them as static (text should NOT animate or move)
      - Keep it suitable for Runway AI video generation

**RETURN FORMAT (JSON):**
{{
  "corrected_scenes": [
    {{"scene_num": 1, "start": 0.0, "end": 3.5, "reason": "Original timing was accurate"}},
    {{"scene_num": 2, "start": 3.5, "end": 7.2, "reason": "Adjusted end to match visual change"}},
    ...
  ],
  "scene_prompts": [
    {{"scene_num": 1, "image_prompt": "...", "motion_prompt": "..."}},
    {{"scene_num": 2, "image_prompt": "...", "motion_prompt": "..."}},
    ...
  ]
}}"""

            # NOTE: CTA button is now handled separately via overlay, not embedded in prompts
            
            # Add article adaptation instructions if provided
            if article_text:
                article_summary = article_text[:2000]  # Limit article length in prompt
                # Note: vertical_info removed - we only use article CONTENT, not metadata
                language_info = f"TARGET LANGUAGE: {article_language}" if article_language else ""
                
                if article_related_to_video:
                    # YES - Article is SIMILAR to video content
                    article_section = f"""
**🔗 ARTICLE-VIDEO RELATIONSHIP: SIMILAR CONTENT (Article IS related to Video)**
═══════════════════════════════════════════════════════════════════════════════

{language_info}

ARTICLE CONTENT:
{article_summary}

✅ ADAPTATION STRATEGY (SIMILAR CONTENT):
The article describes a SIMILAR offer/product to what's shown in the video.
Adapt the video for the new offer while keeping visuals SIMILAR to the original:

1. **KEEP THE SAME VISUAL STYLE** - The video's scenes, composition, and style should remain similar
2. **ADAPT THE PRODUCT/OFFER** - Replace the original product with the article's product (similar type)
3. **ADAPT THE MESSAGING** - Update text overlays and voiceover to match the article content
4. **ADAPT THE LANGUAGE** - ALL text must be in: {article_language}
5. **KEEP THE NARRATIVE STRUCTURE** - Same story flow (hook, problem, solution, CTA)

**🎭 PEOPLE & CULTURE (ALWAYS APPLY):**
{self._get_cultural_style_instructions(article_language)}

**In your image prompts:**
- Describe people with appropriate ethnicity for the target region
- Use culturally appropriate clothing and fashion
- Include environment details that match the target culture

---

"""
                else:
                    # NO - Article is FUNDAMENTALLY DIFFERENT from video content
                    article_section = f"""
**🔄 ARTICLE-VIDEO RELATIONSHIP: DIFFERENT CONTENT (Article is NOT related to Video)**
═══════════════════════════════════════════════════════════════════════════════════════

{language_info}

ARTICLE CONTENT (NEW TOPIC):
{article_summary}

⚠️⚠️⚠️ CRITICAL ADAPTATION STRATEGY (DIFFERENT CONTENT) ⚠️⚠️⚠️
The article describes a COMPLETELY DIFFERENT offer/product than what's shown in the video.
You must CREATE NEW content while KEEPING the video's STYLE and ATMOSPHERE:

1. **EXTRACT THE VIDEO'S STYLE** - Analyze: lighting, camera work, mood, energy, color palette
2. **KEEP THE VISUAL STYLE** - New video should FEEL like the original (same quality, mood, pacing)
3. **DO NOT USE THE ORIGINAL PRODUCT/OFFER** - The original video's product is IRRELEVANT
4. **CREATE NEW VISUALS FOR THE ARTICLE** - All scenes must be appropriate for the article's topic
5. **ALL TEXT AND VO IN TARGET LANGUAGE** - {article_language}

🎯 YOUR MISSION:
- Create prompts for a NEW video that LOOKS LIKE the original (same style/mood)
- But SHOWS content appropriate for the ARTICLE (NOT the original video's product)
- Features people, settings, and actions relevant to the ARTICLE
- Uses the same production quality and pacing as the original

Example: Original video = shoe advertisement → Article = work-from-home jobs
→ Keep: professional style, energetic mood, production quality
→ Create: scenes showing people working from home, home office setups
→ Do NOT: show shoes, running, sports themes

**🎭 PEOPLE & CULTURE (ALWAYS APPLY):**
{self._get_cultural_style_instructions(article_language)}

---

"""
                system_prompt = article_section + system_prompt
            
            # Add manual instructions if provided
            if manual_instructions:
                system_prompt = f"""**🚨 USER INSTRUCTIONS (HIGHEST PRIORITY - MUST FOLLOW):**
{manual_instructions}

Apply these instructions to ALL scene prompts consistently.

---

{system_prompt}"""

            # Build user content
            user_content = [
                {"type": "text", "text": pyscene_info},
                {"type": "text", "text": "\nHere are the video frames (1 per second):"},
            ] + image_contents
            
            logger.info(f"📡 {row_prefix}Sending unified request to gpt-5-mini...")
            
            import time as _time
            start_time = _time.time()
            
            response = self.client.chat.completions.create(
                model="gpt-5-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}
                ],
                max_completion_tokens=128000,
                response_format={"type": "json_object"}
            )
            
            elapsed = _time.time() - start_time
            logger.info(f"✅ {row_prefix}OpenAI responded in {elapsed:.1f}s")
            
            # Check for None content
            content = response.choices[0].message.content
            if content is None:
                logger.warning(f"⚠️ {row_prefix}OpenAI returned None content")
                return self._empty_video_analysis_result(pyscenedetect_timestamps, video_duration)
            
            result = json.loads(content.strip())
            
            # Validate and log results
            corrected_scenes = result.get("corrected_scenes", [])
            scene_prompts = result.get("scene_prompts", [])
            
            logger.info(f"✅ {row_prefix}OpenAI analysis complete:")
            logger.info(f"   {row_prefix}Corrected scenes: {len(corrected_scenes)}")
            for scene in corrected_scenes:
                logger.info(f"     {row_prefix}Scene {scene.get('scene_num')}: {scene.get('start'):.2f}s - {scene.get('end'):.2f}s")
            logger.info(f"   {row_prefix}Prompts generated: {len(scene_prompts)}")
            
            return result
            
        except Exception as e:
            logger.error(f"❌ {row_prefix}Error in unified video analysis: {e}")
            logger.error(f"   {row_prefix}Exception type: {type(e).__name__}")
            import traceback
            logger.error(f"   {row_prefix}Traceback:\n{traceback.format_exc()}")
            return self._empty_video_analysis_result(pyscenedetect_timestamps, video_duration)
    
    def _empty_video_analysis_result(
        self, 
        pyscenedetect_timestamps: List[float], 
        video_duration: float
    ) -> Dict[str, Any]:
        """Create empty/fallback result using original PySceneDetect timestamps."""
        corrected_scenes = []
        scene_prompts = []
        
        for i, ts in enumerate(pyscenedetect_timestamps):
            if i + 1 < len(pyscenedetect_timestamps):
                end_ts = pyscenedetect_timestamps[i + 1]
            else:
                end_ts = video_duration
            
            corrected_scenes.append({
                "scene_num": i + 1,
                "start": ts,
                "end": end_ts,
                "reason": "Fallback - using original PySceneDetect timing"
            })
            scene_prompts.append({
                "scene_num": i + 1,
                "image_prompt": "",
                "motion_prompt": ""
            })
        
        return {
            "corrected_scenes": corrected_scenes,
            "scene_prompts": scene_prompts
        }
    
    def generate_music_description(self, scene_prompts: List[Dict]) -> str:
        """Generate a music description based on video content analysis.
        
        Uses OpenAI to analyze the video scenes and describe appropriate
        background music that matches the video's mood, style, and content.
        
        Args:
            scene_prompts: List of scene prompts with image descriptions.
            
        Returns:
            A detailed music style description for Suno generation.
        """
        try:
            # Build context from scene prompts
            scenes_summary = "\n".join([
                f"Scene {i+1}: {sp.get('image_prompt', '')[:300]}"
                for i, sp in enumerate(scene_prompts[:6])  # First 6 scenes
            ])
            
            if not scenes_summary.strip():
                logger.warning("⚠️ No scene prompts available for music description")
                return "upbeat corporate background music, modern electronic, professional, energetic, inspirational, no vocals"
            
            prompt = f"""Based on this video content, describe the perfect background music for it.

VIDEO SCENES:
{scenes_summary}

Generate a detailed music description that includes:
1. Genre/style (e.g., corporate, upbeat electronic, ambient, cinematic, pop, indie, lo-fi)
2. Tempo (fast/medium/slow, approximate BPM)
3. Mood/emotion (energetic, calm, inspiring, dramatic, playful, sophisticated, warm)
4. Key instruments to feature (synths, piano, guitar, drums, strings, brass, etc.)
5. Overall vibe that matches the video content and would work as background music

The music MUST be:
- INSTRUMENTAL (absolutely no vocals or singing)
- Suitable as background music (not overpowering)
- Matching the energy and mood of the video scenes

Respond with ONLY the music description in 2-3 sentences, nothing else. Be specific and creative."""

            logger.info("🎵 Generating dynamic music description with OpenAI...")
            
            response = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a professional music director who describes background music for videos. You always describe instrumental music only, no vocals."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=250
            )
            
            description = response.choices[0].message.content.strip()
            logger.info(f"🎵 Generated music description: {description}")
            return description
            
        except Exception as e:
            logger.warning(f"⚠️ Could not generate music description: {e}")
            return "upbeat corporate background music, modern electronic synths, professional and energetic, inspirational mood, no vocals"
    
    def generate_music_description_from_text(self, content_text: str, vo_script: str = "") -> str:
        """Generate a music description based on content text and VO script (for influencer mode).
        
        Args:
            content_text: The free text content describing the product/experience.
            vo_script: Optional voice-over script. Music mood MUST match the VO tone and arc.
            
        Returns:
            A detailed music style description for Suno generation.
        """
        try:
            vo_section = ""
            if vo_script and len(vo_script.strip()) > 20:
                vo_section = f"""

VOICE-OVER SCRIPT (the music plays BEHIND this — mood MUST match):
{vo_script[:2000]}

CRITICAL: Match the music to the VO emotional arc. If VO is warm/personal → warm music. If tense/dramatic → build tension. Do NOT default to generic upbeat if the VO is emotional."""
            
            prompt = f"""Based on this content and voice-over, describe the perfect background music for an influencer recommendation video.

CONTENT:
{content_text[:1500]}
{vo_section}

Generate a detailed music description that includes:
1. Genre/style that matches the VO mood (e.g., warm acoustic, emotional piano, upbeat pop, modern electronic)
2. Tempo (match the VO energy and pacing)
3. Mood/emotion (must match the VO emotional arc)
4. Key instruments to feature
5. Overall vibe that supports the VO narration

The music MUST be:
- INSTRUMENTAL (absolutely no vocals or singing)
- Suitable as background music (not overpowering the VO)
- Matching the EMOTIONAL TONE of the voice-over
- Supporting the story arc

Respond with ONLY the music description in 2-3 sentences, nothing else. Be specific and creative."""

            logger.info("🎵 Generating music description for influencer mode...")
            
            response = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a professional music director for social media content. You describe trendy, engaging background music for influencer videos. Always instrumental only, no vocals."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=250
            )
            
            description = response.choices[0].message.content.strip()
            logger.info(f"🎵 Generated music description: {description}")
            return description
            
        except Exception as e:
            logger.warning(f"⚠️ Could not generate music description: {e}")
            return "upbeat trendy electronic music, modern synths with punchy drums, energetic and positive vibe, social media style, no vocals"
    
    def generate_opening_text(
        self, 
        article_text: str, 
        language: str = "en",
        video_description: str = None
    ) -> Optional[str]:
        """Generate a short, compelling opening text based on VIDEO content with cultural adaptation.
        
        Creates a brief, attention-grabbing headline that matches what's shown in the video
        AND is culturally appropriate for the target region/language.
        
        Args:
            article_text: Article content for context.
            language: Target language for the text.
            video_description: Description of what's shown in the video (from scene analysis).
            
        Returns:
            Short opening text string, or None if failed.
        """
        try:
            logger.info(f"📝 Generating opening text (language: {language})...")
            
            language_names = {
                # Major World Languages
                "en": "English", "de": "German", "es": "Spanish", "fr": "French",
                "it": "Italian", "pt": "Portuguese", "nl": "Dutch", "ru": "Russian",
                "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "ar": "Arabic",
                # European Languages
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
                # Middle Eastern & South Asian Languages
                "he": "Hebrew", "fa": "Persian", "ur": "Urdu", "hi": "Hindi",
                "bn": "Bengali", "pa": "Punjabi", "gu": "Gujarati", "mr": "Marathi",
                "ta": "Tamil", "te": "Telugu", "kn": "Kannada", "ml": "Malayalam",
                "si": "Sinhala", "ne": "Nepali", "ps": "Pashto", "ku": "Kurdish",
                # Southeast Asian Languages
                "th": "Thai", "vi": "Vietnamese", "id": "Indonesian", "ms": "Malay",
                "tl": "Filipino", "my": "Burmese", "km": "Khmer", "lo": "Lao",
                # African Languages
                "sw": "Swahili", "am": "Amharic", "ha": "Hausa", "yo": "Yoruba",
                "ig": "Igbo", "zu": "Zulu", "xh": "Xhosa", "af": "Afrikaans",
                # Regional Variants
                "pt-BR": "Brazilian Portuguese", "zh-CN": "Simplified Chinese",
                "zh-TW": "Traditional Chinese", "en-US": "American English",
                "en-GB": "British English", "es-MX": "Mexican Spanish",
                "fr-CA": "Canadian French"
            }
            lang_name = language_names.get(language, "English")
            
            # Get cultural region and hook style from config
            region = config.REGION_MAPPING.get(language, 'namer')
            hook_style = config.HOOK_STYLES.get(region, 'aspirational messaging, personal success')
            cultural_info = config.CULTURAL_STYLES.get(region, {})
            style_description = cultural_info.get('style', 'confident, aspirational')
            
            logger.info(f"   Region: {region}, Hook style: {hook_style[:50]}...")
            
            # Build context - prioritize video content
            context_parts = []
            if video_description:
                context_parts.append(f"VIDEO CONTENT (MOST IMPORTANT - text must match this!):\n{video_description}")
            if article_text:
                context_parts.append(f"Article context:\n{article_text[:300]}")
            
            context = "\n\n".join(context_parts) if context_parts else "General promotional content"
            
            response = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": f"""You are an expert copywriter specializing in {lang_name} marketing content.
Generate a short, compelling opening headline in {lang_name}.

CULTURAL ADAPTATION (VERY IMPORTANT):
- Target language: {lang_name}
- Cultural region: {region}
- Preferred hook style for this region: {hook_style}
- Cultural tone: {style_description}

The headline should feel NATIVE to {lang_name} speakers - use idioms, expressions, and emotional triggers that resonate with this culture.

CRITICAL RULES:
1. The text MUST match what's shown in the VIDEO (not just the article)
2. 3-6 words maximum
3. Attention-grabbing and suitable for a video intro overlay
4. If video shows workers/jobs → text about THAT job type
5. If video shows products → text about THOSE products
6. If video shows a location/activity → text about THAT
7. DO NOT use generic text that doesn't match the video visuals
8. Use culturally appropriate hook style: {hook_style}
9. The text MUST be in {lang_name} - no English unless target is English"""
                    },
                    {
                        "role": "user",
                        "content": f"Generate a short opening headline (3-6 words only) in {lang_name} that MATCHES the video content and uses a {hook_style} approach:\n\n{context}"
                    }
                ],
                max_tokens=50,
                temperature=0.7
            )
            
            text = response.choices[0].message.content.strip()
            # Remove quotes if present
            text = text.strip('"\'')
            logger.info(f"✅ Generated opening text ({region} style): '{text}'")
            return text
            
        except Exception as e:
            logger.warning(f"⚠️ Could not generate opening text: {e}")
            return None
    
    def generate_vo_script_from_article(
        self,
        article_text: str,
        vertical: str,
        target_duration: float,
        target_language: str = "en",
        original_vo_transcript: str = None,
        scene_prompts: List[Dict] = None,
        gemini_vo_recommendations: Dict = None
    ) -> str:
        """Generate a new voice-over script based on article content and scene visuals.
        
        Creates a script suitable for TTS that matches the visuals AND content.
        The VO MUST match what's shown in the generated images.
        
        Args:
            article_text: Full article text content.
            vertical: The vertical/offer name (headline).
            target_duration: Target duration in seconds for the VO.
            target_language: ISO 639-1 language code for the script.
            original_vo_transcript: Optional original VO transcript for reference.
            scene_prompts: Optional list of scene prompts (image_prompt) to match VO with visuals.
            gemini_vo_recommendations: Optional Gemini analysis with VO recommendations including:
                - audio_analysis: voiceover_style, voiceover_tone, selling_approach
                - recommended_new_vo: style_to_match, tone_to_match, key_messages, avoid, structure
                - scene_breakdown: per-scene recommended_vo_for_new_video
            
        Returns:
            Voice-over script text suitable for TTS.
        """
        try:
            logger.info(f"📝 Generating VO script from article (target: {target_duration:.1f}s, language: {target_language})...")
            
            # Estimate word count based on duration
            # Average speaking rate: ~150 words per minute (2.5 words/second)
            target_words = int(target_duration * 2.5)
            
            # Language name mapping for the prompt
            language_names = {
                # Major World Languages
                "en": "English", "de": "German", "es": "Spanish", "fr": "French",
                "it": "Italian", "pt": "Portuguese", "nl": "Dutch", "ru": "Russian",
                "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "ar": "Arabic",
                # European Languages
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
                # Middle Eastern & South Asian Languages
                "he": "Hebrew", "fa": "Persian", "ur": "Urdu", "hi": "Hindi",
                "bn": "Bengali", "pa": "Punjabi", "gu": "Gujarati", "mr": "Marathi",
                "ta": "Tamil", "te": "Telugu", "kn": "Kannada", "ml": "Malayalam",
                "si": "Sinhala", "ne": "Nepali", "ps": "Pashto", "ku": "Kurdish",
                # Southeast Asian Languages
                "th": "Thai", "vi": "Vietnamese", "id": "Indonesian", "ms": "Malay",
                "tl": "Filipino", "my": "Burmese", "km": "Khmer", "lo": "Lao",
                # African Languages
                "sw": "Swahili", "am": "Amharic", "ha": "Hausa", "yo": "Yoruba",
                "ig": "Igbo", "zu": "Zulu", "xh": "Xhosa", "af": "Afrikaans",
                # Regional Variants
                "pt-BR": "Brazilian Portuguese", "zh-CN": "Simplified Chinese",
                "zh-TW": "Traditional Chinese", "en-US": "American English",
                "en-GB": "British English", "es-MX": "Mexican Spanish",
                "fr-CA": "Canadian French"
            }
            language_name = language_names.get(target_language, "English")
            
            # Build the prompt
            reference_section = ""
            if original_vo_transcript:
                reference_section = f"""
ORIGINAL VIDEO VOICE-OVER (for style reference only):
{original_vo_transcript[:500]}

Use the STRUCTURE and STYLE of the original VO as inspiration.
"""
            
            # Build scene visuals section from scene prompts
            visuals_section = ""
            if scene_prompts:
                visuals_section = """
🎬 VIDEO SCENES (THE VISUALS THE VIEWER WILL SEE):
The VO you write MUST match these visuals EXACTLY. The viewer will see these scenes while hearing your script.

"""
                for i, prompt in enumerate(scene_prompts[:8], 1):  # Max 8 scenes
                    image_prompt = prompt.get("image_prompt", "")[:300]
                    if image_prompt:
                        visuals_section += f"Scene {i}: {image_prompt}\n\n"
                
                visuals_section += """
🚨 ABSOLUTE RULE - VO MUST MATCH WHAT'S SHOWN!
- The scenes above show what the viewer will SEE
- Your VO must talk about EXACTLY what's shown
- If scenes show garbage collection worker → VO talks about waste collection jobs
- If scenes show office work → VO talks about office careers
- If scenes show delivery driver → VO talks about delivery/logistics jobs
- NEVER write VO about Topic A when the video shows Topic B!
- The article text is just REFERENCE - if it doesn't match the video, IGNORE IT!

Example of WRONG: Video shows nurse in hospital, VO talks about "babysitting jobs" ❌
Example of RIGHT: Video shows nurse in hospital, VO talks about "healthcare careers" ✅
"""
            
            # Determine style guidance based on original VO and Gemini recommendations
            style_guidance = ""
            
            # Add Gemini recommendations if available
            gemini_guidance = ""
            if gemini_vo_recommendations:
                audio_analysis = gemini_vo_recommendations.get("audio_analysis", {})
                recommended_vo = gemini_vo_recommendations.get("recommended_new_vo", {})
                
                if audio_analysis or recommended_vo:
                    gemini_guidance = """
🎯 AI ANALYSIS OF ORIGINAL VIDEO:
"""
                    if audio_analysis.get("voiceover_style"):
                        gemini_guidance += f"- VO Style: {audio_analysis.get('voiceover_style')}\n"
                    if audio_analysis.get("voiceover_tone"):
                        gemini_guidance += f"- VO Tone: {audio_analysis.get('voiceover_tone')}\n"
                    if audio_analysis.get("selling_approach"):
                        gemini_guidance += f"- Selling Approach: {audio_analysis.get('selling_approach')}\n"
                    if audio_analysis.get("speaking_pace"):
                        gemini_guidance += f"- Speaking Pace: {audio_analysis.get('speaking_pace')}\n"
                    if audio_analysis.get("key_phrases"):
                        key_phrases = audio_analysis.get("key_phrases", [])[:5]
                        if key_phrases:
                            gemini_guidance += f"- Key Phrases to Include: {', '.join(key_phrases)}\n"
                    
                    if recommended_vo:
                        gemini_guidance += "\n📋 RECOMMENDED NEW VO STRUCTURE:\n"
                        if recommended_vo.get("style_to_match"):
                            gemini_guidance += f"- Style: {recommended_vo.get('style_to_match')}\n"
                        if recommended_vo.get("tone_to_match"):
                            gemini_guidance += f"- Tone: {recommended_vo.get('tone_to_match')}\n"
                        if recommended_vo.get("key_messages_to_include"):
                            messages = recommended_vo.get("key_messages_to_include", [])[:3]
                            if messages:
                                gemini_guidance += f"- Key Messages: {', '.join(messages)}\n"
                        if recommended_vo.get("avoid"):
                            avoid_list = recommended_vo.get("avoid", [])[:3]
                            if avoid_list:
                                gemini_guidance += f"- AVOID: {', '.join(avoid_list)}\n"
                        if recommended_vo.get("suggested_structure"):
                            gemini_guidance += f"- Structure: {recommended_vo.get('suggested_structure')}\n"
                
                # Add per-scene VO recommendations
                scene_breakdown = gemini_vo_recommendations.get("scene_breakdown", [])
                if scene_breakdown:
                    gemini_guidance += "\n🎬 PER-SCENE VO SUGGESTIONS:\n"
                    for scene in scene_breakdown[:6]:
                        scene_num = scene.get("scene_number", "?")
                        rec_vo = scene.get("recommended_vo_for_new_video", "")
                        if rec_vo:
                            gemini_guidance += f"Scene {scene_num}: {rec_vo[:100]}...\n"
            
            if original_vo_transcript and len(original_vo_transcript) > 20:
                style_guidance = f"""
🎬 ORIGINAL VIDEO VO (MATCH THIS STYLE EXACTLY):
"{original_vo_transcript[:800]}"
{gemini_guidance}

ANALYZE THE ORIGINAL VO AND MATCH:
- Tone: Is it energetic? Calm? Urgent? Friendly? Professional?
- Structure: How does it flow? Hook → Story → CTA? Question → Answer?
- Pacing: Short punchy sentences? Longer flowing narrative?
- Voice: First person "I"? Second person "You"? Third person narrator?
- Language style: Casual? Formal? Conversational? Dramatic?

Your new VO MUST feel like it belongs to the SAME video. 
Same energy. Same rhythm. Same vibe. Just new content.
"""
            else:
                # No original transcript, but we might have Gemini guidance
                style_guidance = f"""
🎬 NO ORIGINAL VO DETECTED - USE AI-ANALYZED STYLE:
{gemini_guidance if gemini_guidance else '''
- Professional, engaging product advertisement tone
- Direct and benefit-focused
- Clear call-to-action at the end
'''}
"""
            
            hebrew_nikud_note = ""
            if target_language and target_language.lower().startswith("he"):
                hebrew_nikud_note = "\n\nHEBREW: Do NOT use full nikud. Write Hebrew without vowel points. Only add nikud (e.g. dagesh בּ כּ פּ) on letters that can be read in more than one way, in Masoretic style. Otherwise unpointed Hebrew."
            
            prompt = f"""Create a voice-over script that matches the style of the original video.

{style_guidance}

{visuals_section}

CONTENT TO ADAPT (from article):
{article_text[:1500]}

YOUR TASK:
1. If original VO exists: MATCH its exact style, tone, energy, and structure
2. Adapt the CONTENT from the article above
3. Keep same length: ~{target_words} words ({target_duration:.0f} seconds)
4. Language: {language_name}
5. The VO must feel natural with the video visuals

IMPORTANT:
- Match the original's energy and pacing
- If original uses character names, you can too
- If original is casual, be casual
- If original is dramatic, be dramatic
- NO brackets or stage directions
- ONLY output the spoken words{hebrew_nikud_note}

Generate the voice-over:"""

            response = self.client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": f"You are an expert voice-over writer who can perfectly match any style. When given an original VO, you analyze its tone, rhythm, and structure and create new content that feels like it belongs to the same video. You write in {language_name} and your scripts sound natural when spoken."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=800,
                temperature=0.7
            )
            
            script = response.choices[0].message.content.strip()
            
            # Clean up stage directions but KEEP ElevenLabs v3 Audio Tags [excited], [whispers], [laughs] etc.
            script = re.sub(r'\[Scene\s*\d+\]', '', script, flags=re.IGNORECASE)
            script = re.sub(r'\(.*?\)', '', script)
            script = script.strip()
            
            word_count = len(script.split())
            logger.info(f"✅ Generated VO script: {word_count} words (target: {target_words})")
            
            return script
            
        except Exception as e:
            logger.error(f"❌ Failed to generate VO script: {e}")
            # Return a simple fallback (don't use vertical - it's just metadata)
            return "Discover something amazing today. Click to learn more."

    def generate_influencer_prompts(
        self,
        free_text: str,
        reference_images: List[Dict[str, Any]],
        scene_count: int,
        manual_instructions: str = "",
        cta_text: str = "",
        language: str = "en",
        existing_influencer_description: str = ""
    ) -> Dict[str, Any]:
        """Generate influencer-style video prompts for each scene.
        
        Creates prompts for an influencer recommendation video where:
        - Scene 1: Influencer with strong hook
        - Scene 4, 7, 10...: Influencer appears again (identical appearance)
        - Last scene: Influencer with CTA
        - Other scenes: Product/experience with cycling reference images
        
        Args:
            free_text: Content describing the product/experience to promote.
            reference_images: List of dicts with 'url' and optional 'base64' and 'analysis'.
            scene_count: Number of scenes to generate.
            manual_instructions: Optional custom instructions.
            cta_text: Call-to-action text for the last scene.
            language: ISO 639-1 language code.
            existing_influencer_description: If provided, use this description instead of generating one.
            
        Returns:
            Dict with 'influencer_description', 'scene_prompts' list.
        """
        try:
            logger.info(f"🎭 Generating influencer prompts for {scene_count} scenes...")
            
            # Language name mapping
            language_names = {
                # Major World Languages
                "en": "English", "de": "German", "es": "Spanish", "fr": "French",
                "it": "Italian", "pt": "Portuguese", "nl": "Dutch", "ru": "Russian",
                "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "ar": "Arabic",
                # European Languages
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
                # Middle Eastern & South Asian Languages
                "he": "Hebrew", "fa": "Persian", "ur": "Urdu", "hi": "Hindi",
                "bn": "Bengali", "pa": "Punjabi", "gu": "Gujarati", "mr": "Marathi",
                "ta": "Tamil", "te": "Telugu", "kn": "Kannada", "ml": "Malayalam",
                "si": "Sinhala", "ne": "Nepali", "ps": "Pashto", "ku": "Kurdish",
                # Southeast Asian Languages
                "th": "Thai", "vi": "Vietnamese", "id": "Indonesian", "ms": "Malay",
                "tl": "Filipino", "my": "Burmese", "km": "Khmer", "lo": "Lao",
                # African Languages
                "sw": "Swahili", "am": "Amharic", "ha": "Hausa", "yo": "Yoruba",
                "ig": "Igbo", "zu": "Zulu", "xh": "Xhosa", "af": "Afrikaans",
                # Regional Variants
                "pt-BR": "Brazilian Portuguese", "zh-CN": "Simplified Chinese",
                "zh-TW": "Traditional Chinese", "en-US": "American English",
                "en-GB": "British English", "es-MX": "Mexican Spanish",
                "fr-CA": "Canadian French"
            }
            language_name = language_names.get(language, "English")
            
            # Influencer is the host and recommender: appear in every scene
            influencer_scenes = list(range(1, scene_count + 1))
            
            # Build reference image descriptions (include every Image 1-N)
            ref_image_descriptions = []
            for i, img in enumerate(reference_images):
                if img.get('analysis'):
                    ref_image_descriptions.append(f"Reference Image {i+1}: {img['analysis'][:500]}")
                else:
                    ref_image_descriptions.append(f"Reference Image {i+1}: [Use for scenes that match the video topic when this image can support the story]")
            
            ref_images_text = "\n".join(ref_image_descriptions) if ref_image_descriptions else "No reference images provided."
            
            # Build the prompt
            system_prompt = f"""You are an expert video content strategist who creates compelling influencer recommendation videos. The influencer is the HOST—they appear in (almost) every scene, integrated into the location, showing and recommending the place to the viewer.
You will generate detailed image prompts for AI image generation (Nano Banana) and motion prompts for video generation (Runway).

CRITICAL IMAGE STYLE REQUIREMENTS (first_prompt):
- ALL images MUST be HYPER-REALISTIC and look like REAL PHOTOGRAPHS taken by a professional photographer
- ALWAYS start every first_prompt with: "Ultra photorealistic professional photograph, shot on Canon EOS R5, 85mm lens, natural studio lighting"
- Describe REAL skin textures, natural imperfections, genuine facial features
- Avoid any cartoonish, illustrated, AI-generated, or overly polished/synthetic looking styles
- Images should look INDISTINGUISHABLE from real photos
- Include realistic details: natural hair strands, skin pores, fabric textures, environmental reflections
- Use natural lighting setups common in professional photography

CRITICAL MOTION/ANIMATION REQUIREMENTS (second_prompt):
- ALWAYS include specific CAMERA MOVEMENTS: slow zoom in, dolly forward, subtle pan, crane shot, tracking shot
- ALWAYS describe HUMAN EXPRESSIONS and EMOTIONS that match the scene context:
  * If the influencer is near food: show them EATING, tasting, savoring, chewing, reacting to flavor
  * If the influencer is at an entrance/exterior: show them looking around in AWE, surprise, excitement, pointing
  * If the influencer is showcasing a product: show them HOLDING it, examining it, showing it off with excitement
  * NEVER describe the influencer as "talking", "speaking", or "addressing the camera" - the VO is added separately
  * Instead use: shocked expression, delighted reaction, impressed look, enjoying food, exploring the place
- Include NATURAL BODY LANGUAGE: subtle head tilts, hand gestures, shoulder movements, breathing motion
- Describe MICRO-EXPRESSIONS: eye movements, eyebrow raises, lip movements (reacting, NOT talking), blinking
- Add ENVIRONMENTAL MOTION: hair flowing, clothes moving slightly, background elements
- Keep motions SUBTLE and NATURAL - avoid exaggerated or robotic movements
- IMPORTANT: The influencer should NEVER appear to be talking/speaking in the animation. Voice-over is added later.

IMPORTANT RULES:
1. Generate prompts in {language_name} context but write the prompts themselves in English (for AI generation).
2. Create a DETAILED and CONSISTENT influencer appearance that must be IDENTICAL in all scenes where the influencer appears.
4. The influencer should be a relatable, attractive person with NATURAL, REALISTIC features.
5. All scenes should be vertical format (9:16 aspect ratio).
6. Use cinematic lighting and high-quality REALISTIC photography style.
7. A reference image of the influencer will be provided to the image generation AI - your description must match that reference exactly.

CRITICAL - INFLUENCER ACTIONS IN SCENES (be creative and dynamic!):
- The influencer must be ACTIVELY DOING SOMETHING in each scene, not just standing/posing
- NEVER show the influencer holding a phone, taking a selfie, or filming
- NEVER show the influencer talking or speaking - voice-over is added separately
- Each scene should show a DIFFERENT action that fits the context:
  * At a restaurant: EATING food, tasting a dish, picking up food with chopsticks, sipping a drink, smelling food
  * At a store/entrance: walking IN through the door, touching the sign, looking up in awe, spinning around to see everything
  * With a product: holding it up, trying it on, unboxing it, comparing items, using it
  * In a scenic location: walking through, sitting down to enjoy the view, pointing at something interesting
  * CTA/ending scene: waving goodbye, blowing a kiss, giving thumbs up, holding up a business card
- Make each influencer scene feel like a CANDID moment from real life, not a posed photo
- The influencer should feel IMMERSED in the environment, interacting with the surroundings"""

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
                influencer_section = f"""

IMPORTANT - USE THIS EXACT INFLUENCER DESCRIPTION:
The influencer has been pre-defined. You MUST use this exact description in all influencer scenes:
"{existing_influencer_description}"

Do NOT create a new influencer appearance. Copy this description exactly into the "influencer_description" field and use it in all scene prompts where shows_influencer is true."""
            
            user_prompt = f"""Generate prompts for a {scene_count}-scene influencer recommendation video.{influencer_section}

PRODUCT/EXPERIENCE TO PROMOTE:
{free_text[:3000]}

REFERENCE IMAGES FOR PRODUCT/EXPERIENCE:
{ref_images_text}

SCENE STRUCTURE:
- Scenes where INFLUENCER appears: {influencer_scenes}
- Other scenes: Show the product/experience only (influencer may be holding item or in location but not focal point)
- Scene 1: Start with a STRONG HOOK - influencer should show excitement/surprise
- Last scene (Scene {scene_count}): Include visual CTA concept (pointing, gesturing to click, etc.){manual_section}{cta_section}

SMART REFERENCE IMAGE MATCHING:
For each scene, you MUST set "reference_image_index" to the index (0-based) of the MOST relevant reference image for that scene.
Think about it logically:
- If a reference image shows an entrance/exterior, use it for the opening/hook scene
- If a reference image shows food/products, use it for product showcase scenes
- If a reference image shows interior/ambiance, use it for atmosphere scenes
- If a reference image shows people/staff, use it for service/experience scenes
- Each reference image can be used for multiple scenes if relevant
- Set to null ONLY if no reference images are available at all

FOR EACH SCENE, PROVIDE:
1. **first_prompt**: Ultra-realistic photograph description. Start with "Ultra photorealistic professional photograph, shot on Canon EOS R5, 85mm lens". Describe the scene as if describing a real photo.
2. **second_prompt**: Cinematic motion/animation prompt. MUST include: specific camera movement (zoom, pan, dolly, etc.), human facial expressions (smile, excitement, surprise), natural body micro-movements, and environmental motion.

RESPONSE FORMAT (JSON):
{{
    "influencer_description": "DETAILED description of the influencer's appearance (face, hair, body type, skin tone, distinctive features) - this MUST be used identically in all influencer scenes",
    "scene_prompts": [
        {{
            "scene_number": 1,
            "shows_influencer": true,
            "reference_image_index": 0,
            "first_prompt": "detailed image prompt here",
            "second_prompt": "motion/animation prompt here"
        }},
        ...
    ]
}}

Generate {scene_count} scenes now:"""

            response = self.client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                max_tokens=4000,
                temperature=0.7,
                response_format={"type": "json_object"}
            )
            
            result_text = response.choices[0].message.content.strip()
            result = json.loads(result_text)
            
            # Validate and enhance prompts with influencer description
            # Use existing description if provided, otherwise use the AI-generated one
            influencer_desc = existing_influencer_description if existing_influencer_description else result.get("influencer_description", "")
            scene_prompts = result.get("scene_prompts", [])
            
            # Ensure influencer description is embedded in all influencer scenes
            for prompt in scene_prompts:
                if prompt.get("shows_influencer", False) and influencer_desc:
                    # Prepend influencer description to first_prompt
                    original_prompt = prompt.get("first_prompt", "")
                    prompt["first_prompt"] = f"INFLUENCER: {influencer_desc}. SCENE: {original_prompt}"
            
            logger.info(f"✅ Generated {len(scene_prompts)} influencer scene prompts")
            return {
                "influencer_description": influencer_desc,
                "scene_prompts": scene_prompts
            }
            
        except Exception as e:
            logger.error(f"❌ Failed to generate influencer prompts: {e}")
            return {
                "influencer_description": "",
                "scene_prompts": []
            }

    def generate_influencer_vo_script(
        self,
        free_text: str,
        scene_count: int,
        target_duration: float,
        manual_instructions: str = "",
        language: str = "en",
        original_vo_transcript: str = ""
    ) -> str:
        """Generate a first-person voice-over script for an influencer video.
        
        Creates a compelling VO script that matches the original video's style.
        If original VO exists, the new VO will match its tone, rhythm and energy.
        
        Args:
            free_text: Content describing the product/experience.
            scene_count: Number of scenes for pacing reference.
            target_duration: Target duration in seconds.
            manual_instructions: Optional custom instructions.
            language: ISO 639-1 language code.
            original_vo_transcript: The original video's VO for style matching.
            
        Returns:
            Voice-over script text suitable for TTS.
        """
        try:
            logger.info(f"🎤 Generating influencer VO script (target: {target_duration:.1f}s, language: {language})...")
            
            # Estimate word count based on duration
            target_words = int(target_duration * 2.5)
            
            # Language name mapping
            language_names = {
                # Major World Languages
                "en": "English", "de": "German", "es": "Spanish", "fr": "French",
                "it": "Italian", "pt": "Portuguese", "nl": "Dutch", "ru": "Russian",
                "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "ar": "Arabic",
                # European Languages
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
                # Middle Eastern & South Asian Languages
                "he": "Hebrew", "fa": "Persian", "ur": "Urdu", "hi": "Hindi",
                "bn": "Bengali", "pa": "Punjabi", "gu": "Gujarati", "mr": "Marathi",
                "ta": "Tamil", "te": "Telugu", "kn": "Kannada", "ml": "Malayalam",
                "si": "Sinhala", "ne": "Nepali", "ps": "Pashto", "ku": "Kurdish",
                # Southeast Asian Languages
                "th": "Thai", "vi": "Vietnamese", "id": "Indonesian", "ms": "Malay",
                "tl": "Filipino", "my": "Burmese", "km": "Khmer", "lo": "Lao",
                # African Languages
                "sw": "Swahili", "am": "Amharic", "ha": "Hausa", "yo": "Yoruba",
                "ig": "Igbo", "zu": "Zulu", "xh": "Xhosa", "af": "Afrikaans",
                # Regional Variants
                "pt-BR": "Brazilian Portuguese", "zh-CN": "Simplified Chinese",
                "zh-TW": "Traditional Chinese", "en-US": "American English",
                "en-GB": "British English", "es-MX": "Mexican Spanish",
                "fr-CA": "Canadian French"
            }
            language_name = language_names.get(language, "English")
            
            # Determine style based on original VO if available
            style_guidance = ""
            if original_vo_transcript and len(original_vo_transcript) > 20:
                style_guidance = f"""
🎬 ORIGINAL VIDEO VO - MATCH THIS STYLE EXACTLY:
"{original_vo_transcript[:1000]}"

ANALYZE AND MATCH:
- Tone: Casual? Professional? Excited? Calm? Urgent?
- Voice: First person "I"? Second person "You"? Narrator style?
- Energy: High energy hype? Chill recommendation? Dramatic reveal?
- Pacing: Quick punchy lines? Flowing narrative? Emotional build-up?
- Language: Slang and casual? Formal? Conversational?
- Structure: How does it open? How does it close? Story arc?

Your new VO must feel like the SAME person in the SAME video.
Match the vibe exactly. Just new content.
"""
            else:
                style_guidance = """
🎬 NO ORIGINAL VO - USE AUTHENTIC INFLUENCER STYLE:
- Genuine, relatable voice
- As if personally recommending to a friend
- Natural flow, conversational
"""
            
            # Check for special instructions about voice style
            voice_style = ""
            if manual_instructions:
                if "third person" in manual_instructions.lower():
                    voice_style = "Override: Use third person narrator style."
                elif "narrator" in manual_instructions.lower():
                    voice_style = "Override: Use professional narrator style."
            
            hebrew_nikud_note = ""
            if language and language.lower().startswith("he"):
                hebrew_nikud_note = "\n\nHEBREW: Do NOT use full nikud. Write Hebrew without vowel points. Only add nikud (e.g. dagesh בּ כּ פּ) on letters that can be read in more than one way, in Masoretic style. Otherwise unpointed Hebrew."
            
            prompt = f"""Create an influencer voice-over script that matches the original video's style.

{style_guidance}

PRODUCT/CONTENT INFO:
{free_text[:2000]}

YOUR TASK:
1. If original VO exists: MATCH its exact style, tone, energy, and structure
2. Create NEW content about the product/experience above
3. Keep the same energy and feeling as the original
4. Length: ~{target_words} words ({target_duration:.0f} seconds)
5. Language: {language_name}

{f"SPECIAL INSTRUCTIONS: {manual_instructions}" if manual_instructions else ""}
{voice_style}

IMPORTANT:
- If original uses names, you can use names
- If original is casual, be casual
- If original is hype, be hype
- If original is calm, be calm
- NO brackets or stage directions
- ONLY output the spoken words{hebrew_nikud_note}

Generate the voice-over:"""

            response = self.client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": f"You are an expert at matching voice-over styles. When given an original VO, you create new content that feels like it belongs to the same video - same energy, same rhythm, same personality. You write naturally in {language_name}."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=800,
                temperature=0.7
            )
            
            script = response.choices[0].message.content.strip()
            
            # Clean up stage directions but KEEP ElevenLabs v3 Audio Tags [excited], [whispers], [laughs] etc.
            script = re.sub(r'\[Scene\s*\d+\]', '', script, flags=re.IGNORECASE)
            script = re.sub(r'\(.*?\)', '', script)
            script = script.strip()
            
            word_count = len(script.split())
            logger.info(f"✅ Generated influencer VO script: {word_count} words (target: {target_words})")
            logger.info(f"📝 VO Script preview: {script[:200]}...")
            
            return script
            
        except Exception as e:
            logger.error(f"❌ Failed to generate influencer VO script: {e}")
            return "Check this out! I recently discovered something amazing and I had to share it with you. Click below to learn more!"
