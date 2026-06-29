"""GeminiImageService — extracted verbatim from Comp_Videos/video_scene_processor.py.

Lines 5311-5759 of the monolith.
"""

import os
import re
import json
import time
import base64
import logging
import random
import requests
from typing import Dict, Any, List, Optional, Tuple

from api_pipeline.services.base.config import config

logger = logging.getLogger(__name__)


class GeminiImageService:
    """Service for image generation using Gemini 3 Pro Image Preview via Vertex AI REST API.
    
    Uses gemini-3-pro-image-preview for ALL image generation (product + scene).
    This model provides the best quality for reference-image-grounded generation,
    including product photos, scene images with product/character references, and CTA scenes.
    
    Uses direct REST API calls with API key authentication for Vertex AI.
    Supports multimodal input: text instructions + one or more reference images.
    """
    
    def __init__(self, gcs_storage_service=None):
        """Initialize Gemini Image service using gemini-3-pro-image-preview for all generation.
        
        Args:
            gcs_storage_service: GCS storage service for uploading generated images.
        """
        self.gcs_storage_service = gcs_storage_service
        self.initialized = False
        
        # Use Vertex AI API key from config
        self.api_key = config.VERTEX_AI_API_KEY
        self.project_id = config.GEMINI_IMAGE_PROJECT_ID
        
        # Both models now point to gemini-3-pro-image-preview for best reference-image quality
        self.product_model = config.GEMINI_PRODUCT_IMAGE_MODEL  # Pro - high quality
        self.scene_model = config.GEMINI_SCENE_IMAGE_MODEL      # Pro - same model for consistency
        
        # Base endpoint template (model inserted at call time)
        self.endpoint_template = f"https://aiplatform.googleapis.com/v1/projects/{self.project_id}/locations/global/publishers/google/models/{{model}}:generateContent"
        
        if not self.api_key:
            logger.warning("Gemini Image not available - VERTEX_AI_API_KEY not set")
            return
        
        self.last_failure_reason = ""  # Set when generate_image returns None (for scene skip logging)
        self.headers = {
            "Content-Type": "application/json"
        }
        self.initialized = True
        logger.info(f"Gemini Image service initialized (Product: {self.product_model}, Scenes: {self.scene_model})")
    
    def _fetch_image_as_base64(self, image_url: str) -> Optional[Tuple[str, str]]:
        """Fetch an image from URL and encode as base64.
        
        Args:
            image_url: URL of the image to fetch.
            
        Returns:
            Tuple of (base64_data, mime_type) or None if failed.
        """
        try:
            # Use browser-like headers to avoid 403 Forbidden from websites
            fetch_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": image_url
            }
            response = requests.get(image_url, headers=fetch_headers, timeout=30)
            response.raise_for_status()
            
            # Determine MIME type from content-type header or URL
            content_type = response.headers.get("Content-Type", "").lower()
            if "png" in content_type or image_url.lower().endswith(".png"):
                mime_type = "image/png"
            elif "gif" in content_type or image_url.lower().endswith(".gif"):
                mime_type = "image/gif"
            elif "webp" in content_type or image_url.lower().endswith(".webp"):
                mime_type = "image/webp"
            else:
                mime_type = "image/jpeg"
            
            # Encode to base64
            base64_data = base64.b64encode(response.content).decode("utf-8")
            return (base64_data, mime_type)
            
        except Exception as e:
            logger.warning(f"Failed to fetch image {image_url[:60]}...: {e}")
            return None
    
    def generate_image(
        self,
        prompt: str,
        reference_image_urls: List[str] = None,
        aspect_ratio: str = "9:16",
        use_flash: bool = False
    ) -> Optional[str]:
        """Generate an image using Gemini models.
        
        Args:
            prompt: Text prompt for image generation.
            reference_image_urls: Optional list of reference image URLs (up to 3).
            aspect_ratio: Aspect ratio for the output image.
            use_flash: If True, use Flash model (fast). If False, use Pro model (high quality).
            
        Returns:
            URL of the generated image (uploaded to GCS), or None if failed.
        """
        if not self.initialized:
            logger.error("Gemini Image service not initialized")
            return None
        
        # Select model and settings based on use_flash flag
        if use_flash:
            model = self.scene_model
            rate_limit_delay = config.GEMINI_SCENE_IMAGE_RATE_LIMIT_DELAY
            retry_delay_base = config.GEMINI_SCENE_IMAGE_RETRY_DELAY
            max_retries = config.GEMINI_SCENE_IMAGE_MAX_RETRIES
            model_type = "Flash"
        else:
            model = self.product_model
            rate_limit_delay = config.GEMINI_PRODUCT_IMAGE_RATE_LIMIT_DELAY
            retry_delay_base = config.GEMINI_PRODUCT_IMAGE_RETRY_DELAY
            max_retries = config.GEMINI_PRODUCT_IMAGE_MAX_RETRIES
            model_type = "Pro"
        
        # Build endpoint URL for selected model
        endpoint = self.endpoint_template.format(model=model)
        url = f"{endpoint}?key={self.api_key}"
        
        # Build parts array - start with text prompt
        parts = [{"text": f"Generate an image: {prompt}"}]
        
        # Add reference images if provided (up to 3)
        ref_added = 0
        if reference_image_urls:
            ref_urls = reference_image_urls[:config.GEMINI_IMAGE_MAX_REFERENCE_IMAGES]
            for i, url_ref in enumerate(ref_urls):
                if url_ref:
                    image_data = self._fetch_image_as_base64(url_ref)
                    if image_data:
                        base64_data, mime_type = image_data
                        parts.append({
                            "inline_data": {
                                "mime_type": mime_type,
                                "data": base64_data
                            }
                        })
                        ref_added += 1
                        logger.info(f"   Added reference image {ref_added}/{len(ref_urls)}")
            _wanted = [u for u in ref_urls if u and str(u).strip()]
            if _wanted and ref_added == 0:
                self.last_failure_reason = (
                    "Could not download any reference image (404, timeout, or blocked). "
                    "Use public URLs or re-upload assets to stable storage."
                )
                logger.error(self.last_failure_reason)
                return None
        
        # Build request payload with proper imageConfig structure
        payload = {
            "contents": {
                "role": "user",
                "parts": parts
            },
            "generationConfig": {
                "responseModalities": ["IMAGE", "TEXT"],
                "imageConfig": {
                    "aspectRatio": aspect_ratio
                }
            }
        }
        
        # Make API request with retry logic for rate limits
        for attempt in range(max_retries + 1):
            try:
                logger.info(f"Generating image with Gemini {model_type} ({model}) - attempt {attempt + 1}...")
                response = requests.post(
                    url,
                    headers=self.headers,
                    json=payload,
                    timeout=300  # 5 minutes timeout for slow image generation
                )
                response.raise_for_status()
                
                result = response.json()
                
                # Extract generated image from response
                candidates = result.get("candidates", [])
                if not candidates:
                    self.last_failure_reason = "No candidates in Gemini response (safety block or empty response)"
                    logger.error("No candidates in Gemini response")
                    return None
                
                content = candidates[0].get("content", {})
                response_parts = content.get("parts", [])
                
                # Find image part in response
                for part in response_parts:
                    if "inlineData" in part:
                        inline_data = part["inlineData"]
                        image_base64 = inline_data.get("data", "")
                        mime_type = inline_data.get("mimeType", "image/png")
                        
                        if image_base64:
                            # Decode and upload to GCS
                            image_bytes = base64.b64decode(image_base64)
                            
                            # Determine file extension
                            ext = "png" if "png" in mime_type else "jpg"
                            key = f"generated_images/gemini_{int(time.time())}_{random.randint(1000, 9999)}.{ext}"
                            
                            if self.gcs_storage_service:
                                image_url = self.gcs_storage_service.upload_image_bytes(
                                    image_data=image_bytes,
                                    key_name=key,
                                    make_public=True
                                )
                                if image_url:
                                    self.last_failure_reason = ""
                                    logger.info(f"Image generated and uploaded: {image_url[:60]}...")
                                    
                                    # Rate limit delay to avoid 429 errors
                                    if rate_limit_delay > 0:
                                        logger.info(f"   Waiting {rate_limit_delay}s for rate limit...")
                                        time.sleep(rate_limit_delay)
                                    
                                    return image_url
                            else:
                                self.last_failure_reason = "No GCS service available to upload image"
                                logger.warning("No GCS service available to upload image")
                                return None
                
                self.last_failure_reason = "No image found in Gemini response (candidates had no image data)"
                logger.error("No image found in Gemini response (not rate limit—Gemini returned no image data; possible content/safety filter)")
                return None
                
            except requests.exceptions.HTTPError as e:
                status_code = e.response.status_code
                
                # Handle rate limit (429) with retry
                if status_code == 429 and attempt < max_retries:
                    retry_delay = retry_delay_base * (attempt + 1)  # Exponential backoff
                    logger.warning(f"Rate limit hit (429). Waiting {retry_delay}s before retry {attempt + 1}/{max_retries}...")
                    time.sleep(retry_delay)
                    continue
                
                self.last_failure_reason = f"Gemini API {status_code}: {e.response.text[:200]}"
                logger.error(f"Gemini Image API error: {status_code} - {e.response.text[:500]}")
                return None
                
            except Exception as e:
                self.last_failure_reason = str(e)[:200]
                logger.error(f"Error generating image with Gemini: {e}")
                return None
        
        # All retries exhausted
        self.last_failure_reason = "All retries exhausted (likely rate limit 429)"
        logger.error("All retry attempts exhausted for image generation")
        return None
    
    def generate_clean_product_image(
        self,
        reference_image_urls: List[str],
        product_description: str
    ) -> Optional[str]:
        """Generate a clean, isolated product image from reference images.
        
        Takes multiple reference images and generates a clean product image
        with no background, text, or overlays.
        
        Args:
            reference_image_urls: List of reference image URLs (up to 3).
            product_description: Detailed description of the product.
            
        Returns:
            URL of the clean product image, or None if failed.
        """
        if not reference_image_urls:
            logger.warning("No reference images provided for clean product generation")
            return None
        
        prompt = f"""Create a clean, professional product image that is an EXACT copy of the product shown in the reference images.

PRODUCT: {product_description}

CRITICAL - EXACT MATCHING REQUIRED:
- You MUST recreate the EXACT product from the reference images
- Copy the EXACT shape, proportions, colors, and materials
- Do NOT create a similar or generic product - create THIS EXACT product
- Every detail must match: color shades, textures, design elements

PRESENTATION REQUIREMENTS:
- Show ONLY the product on a pure white or light neutral background
- Remove ALL text, logos, watermarks, and overlays from the background
- Remove ALL hands, people, and background elements
- Product should be centered and well-lit with soft studio lighting
- High quality, sharp focus on the product
- Professional product photography style - Amazon/e-commerce quality
- Show the product from the best angle to highlight its features

The output must look like a professional product photo of the EXACT same product from the references."""
        
        logger.info(f"Generating clean product image from {len(reference_image_urls)} references...")
        
        # Use Pro model (gemini-3-pro-image-preview) for highest quality product images
        return self.generate_image(
            prompt=prompt,
            reference_image_urls=reference_image_urls,
            aspect_ratio="9:16",
            use_flash=False  # Pro model for all images - better reference image handling
        )
    
    def generate_scene_image(
        self,
        image_prompt: str,
        product_reference_urls: List[str] = None,
        product_description: str = None,
        product_visible: bool = True,
        visual_style: str = "Auto",
        character_reference_url: str = None,
        character_reference_urls: List[str] = None,
        has_character: bool = False,
        logo_reference_url: str = None,
        reference_image_urls: List[str] = None,
        is_cta_scene: bool = False,
        prepend_reference_urls: Optional[List[str]] = None,
        **kwargs,
    ) -> Optional[str]:
        """Generate a scene image for product video.
        
        Args:
            image_prompt: The scene image prompt.
            product_reference_urls: Optional list of product reference image URLs.
            product_description: Optional description of the product.
            product_visible: Whether the product should appear in this scene.
            visual_style: Visual style for the image ("Auto" or specific style name).
            character_reference_url: Optional single URL to character reference image (legacy).
            character_reference_urls: Optional list of character reference image URLs (multiple people).
            has_character: Whether the character(s) should appear in this scene.
            logo_reference_url: Optional URL to logo image for CTA/ending scenes.
            prepend_reference_urls: Sent first in reference list (e.g. UGC Real blank 3×3 layout).
            is_cta_scene: If True, this is the last/CTA scene (accepted for API compatibility; Gemini uses logo_reference_url for CTA).
            
        Returns:
            URL of the generated scene image, or None if failed.
        """
        if character_reference_urls is None and character_reference_url:
            character_reference_urls = [character_reference_url]
        _kw_prepend = kwargs.pop("prepend_reference_urls", None)
        if prepend_reference_urls is None and _kw_prepend is not None:
            prepend_reference_urls = _kw_prepend
        prep_clean: List[str] = []
        if prepend_reference_urls:
            for u in prepend_reference_urls:
                if u and isinstance(u, str):
                    s = u.strip()
                    if s and s not in prep_clean:
                        prep_clean.append(s)
        layout_ref_block = ""
        if prep_clean:
            layout_ref_block = """
LAYOUT REFERENCE IMAGE (FIRST reference):
The FIRST attached image is a structural template: vertical 9:16 with exactly nine equal cells in a perfect 3×3 grid. Replicate this geometry exactly — equal cells, straight lines, no merged panels.

"""
        # Apply visual style if specified (not "Auto")
        style_prefix = ""
        style_suffix = ""
        if visual_style and visual_style != "Auto":
            # Style-specific prompt prefixes and suffixes for each visual style
            style_prompts = {
                "Modern flat 2d": (
                    "MANDATORY STYLE: Modern flat 2D illustration. Clean vector graphics, bold solid colors, minimal shadows, geometric shapes, digital art aesthetic. Every element must look like a flat 2D vector illustration, NOT a photograph. ",
                    "\n\nSTYLE REMINDER: This MUST be a flat 2D vector illustration – no photorealism, no 3D rendering, no photographs. Clean lines, solid fills, minimal gradients."
                ),
                "Minimal line art": (
                    "MANDATORY STYLE: Minimal line art illustration. Elegant single-weight continuous lines, monochromatic or very limited color palette (1-3 colors max), clean white or light background, hand-drawn artistic look, simplistic beauty with negative space. ",
                    "\n\nSTYLE REMINDER: This MUST be minimal line art – thin elegant lines, mostly white space, very few colors. NOT a photograph or detailed rendering."
                ),
                "Futuristic isometric Tech Glow": (
                    "MANDATORY STYLE: Futuristic isometric 3D with tech glow effects. Neon accents on dark background, cyberpunk aesthetic, glowing edges and outlines, holographic elements, tech UI overlays, isometric camera angle. ",
                    "\n\nSTYLE REMINDER: This MUST have isometric perspective, dark background, neon glow effects, and futuristic tech aesthetic throughout."
                ),
                "Modern semi flat 2d": (
                    "MANDATORY STYLE: Modern semi-flat 2D illustration. Soft gradients, subtle drop shadows, contemporary digital illustration, clean design, vibrant yet harmonious color palette, slightly rounded shapes. ",
                    "\n\nSTYLE REMINDER: This MUST be a semi-flat 2D illustration with soft gradients – not photorealistic, not fully flat."
                ),
                "Cinematic photography": (
                    "MANDATORY STYLE: Cinematic photography. Dramatic lighting with strong contrast, shallow depth of field (f/1.4-2.8), professional color grading (teal-orange or moody tones), film-like quality, anamorphic lens flares, high production value, shot on Arri Alexa or RED camera. ",
                    "\n\nSTYLE REMINDER: This MUST look like a still from a high-budget film – dramatic lighting, shallow DoF, cinematic color grading."
                ),
                "Soft 3d clay": (
                    "MANDATORY STYLE: Soft 3D clay render (claymation). Smooth rounded shapes, matte clay materials with subtle fingerprint texture, pastel colors, Pixar/Disney-like aesthetic, charming and friendly, soft ambient occlusion, studio lighting. ",
                    "\n\nSTYLE REMINDER: This MUST look like a 3D clay/plasticine render – round soft shapes, matte pastel materials, charming Pixar aesthetic."
                ),
                "isometric soft vector": (
                    "MANDATORY STYLE: Isometric soft vector illustration. Clean geometric isometric perspective (30° angle), pastel color palette, minimal flat shadows, infographic-style clarity, modern digital art, no perspective distortion. ",
                    "\n\nSTYLE REMINDER: This MUST be an isometric vector illustration – strict isometric angle, clean geometric shapes, pastel colors."
                ),
                "Paper Cut": (
                    "MANDATORY STYLE: Paper cut art (paper craft). Layered cut paper effect with visible depth between layers, subtle shadows between paper layers, colorful craft paper textures, whimsical handmade aesthetic, 3D paper diorama look. ",
                    "\n\nSTYLE REMINDER: This MUST look like paper cut art – visible paper layers with shadows between them, craft paper textures, handmade feel."
                )
            }
            style_data = style_prompts.get(visual_style)
            if style_data:
                style_prefix, style_suffix = style_data
            else:
                # Custom style name from sheet – use it directly
                style_prefix = f"MANDATORY STYLE: {visual_style}. "
                style_suffix = f"\n\nSTYLE REMINDER: This image MUST be in {visual_style} style throughout."
        
        # Build the enhanced prompt
        if product_visible and product_description:
            _ip = layout_ref_block + (image_prompt or "") if prep_clean else (image_prompt or "")
            enhanced_prompt = f"""{style_prefix}{_ip}

CRITICAL PRODUCT REQUIREMENTS:
You MUST include the EXACT product shown in the reference images. This is mandatory.

Product Description: {product_description}

IMPORTANT MATCHING RULES:
- The product in this scene MUST be identical to the product in the reference images
- Match the EXACT shape, color, materials, and proportions of the product
- Do NOT create a similar product - use the EXACT product from the references
- The product should be clearly visible and recognizable
- Maintain the product's brand identity and distinctive features
- If the reference shows a grey office chair, show that EXACT grey office chair
- If it shows a specific bottle design, show that EXACT bottle design{style_suffix}"""
        else:
            _ip = layout_ref_block + (image_prompt or "") if prep_clean else (image_prompt or "")
            enhanced_prompt = f"{style_prefix}{_ip}{style_suffix}"
        
        # Build reference URLs list
        ref_urls = []
        
        # Add product references if product is visible
        if product_visible and product_reference_urls:
            ref_urls.extend(product_reference_urls)
        
        # Add character reference(s) if character(s) should appear in this scene
        if has_character and character_reference_urls:
            ref_urls.extend(character_reference_urls)
            num_chars = len(character_reference_urls)
            _order_note = (
                "\nORDERING: The FIRST reference is the blank 3×3 grid layout (geometry only). "
                "Character photo(s) follow.\n"
                if prep_clean
                else ""
            )
            if num_chars == 1:
                char_instruction = f"""
CHARACTER REFERENCE IMAGE INSTRUCTIONS:{_order_note}
A reference photo of the character/influencer is attached. Use it to match their APPEARANCE and CLOTHING STYLE:
- Face features, skin tone, facial structure
- Hair color, style, length  
- Clothing style and colors (but adapt to the scene context naturally)
- General body type and age
"""
            else:
                char_instruction = f"""
CHARACTER REFERENCE IMAGE INSTRUCTIONS:{_order_note}
Multiple reference photos of the characters are attached (Person 1, Person 2, etc.). Use each photo to match THAT person's APPEARANCE and CLOTHING STYLE. Include all of these people in the scene when the prompt calls for it.
- Face features, skin tone, facial structure for each person
- Hair color, style, length for each
- Clothing style and colors (adapt to scene context naturally)
- General body type and age for each
"""
            # CRITICAL: Tell the model to use reference ONLY for appearance, NOT pose
            enhanced_prompt += char_instruction + """
CRITICAL CREATIVE DIRECTION:
- The character(s) must be FULLY IMMERSED and INTERACTING with the scene environment
- DO NOT copy the pose, angle, or composition from the reference photo(s)
- Create a CANDID, NATURAL moment - as if caught by a photographer in real life
- The character(s) should be the MAIN SUBJECT but deeply integrated into the setting
- Examples of good integration: leaning over a table to smell food, reaching for a product on a shelf, sitting cross-legged on the floor examining something, walking through a doorway mid-step, laughing while holding chopsticks with food
- The scene should look like a REAL MOMENT, not a posed photo with the character pasted in
- Be CREATIVE - every scene should feel unique and alive"""
        
        # Add logo reference for CTA/ending scenes
        if logo_reference_url:
            ref_urls.append(logo_reference_url)

        if reference_image_urls:
            for u in reference_image_urls:
                if not u or not isinstance(u, str):
                    continue
                s = u.strip()
                if s and s not in ref_urls:
                    ref_urls.append(s)

        if prep_clean:
            _seen_p = set(prep_clean)
            ref_urls = list(prep_clean) + [u for u in ref_urls if u not in _seen_p]
        
        # Use Pro model (gemini-3-pro-image-preview) for high-quality scene images with reference grounding
        return self.generate_image(
            prompt=enhanced_prompt,
            reference_image_urls=ref_urls if ref_urls else None,
            aspect_ratio="9:16",
            use_flash=False  # Pro model for all images - better reference image handling
        )
