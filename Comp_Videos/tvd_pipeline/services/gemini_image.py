"""Gemini image generation service via Vertex AI REST API."""

import io
import re
import time
import base64
import random
import logging
import threading
from typing import List, Optional, Tuple

import requests
from PIL import Image

from tvd_pipeline.config import Config
from tvd_pipeline.data_loader import get_style_prompts

config = Config()
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
        use_flash: bool = False,
        model_override: Optional[str] = None,
        image_size: Optional[str] = None,
    ) -> Optional[str]:
        """Generate an image using Gemini models.
        
        Args:
            prompt: Text prompt for image generation.
            reference_image_urls: Optional list of reference image URLs (up to 3).
            aspect_ratio: Aspect ratio for the output image.
            use_flash: If True, use Flash model (fast). If False, use Pro model (high quality).
            model_override: If set, use this Vertex model ID (e.g. gemini-3.1-flash-image-preview) instead of product/scene default.
            image_size: Optional Vertex imageConfig.imageSize (e.g. 1K, 2K, 4K) for models that support it (e.g. Nano Banana 2).
            
        Returns:
            URL of the generated image (uploaded to GCS), or None if failed.
        """
        if not self.initialized:
            logger.error("Gemini Image service not initialized")
            return None
        
        # Select model and settings: model_override takes precedence, then use_flash, else Pro
        # Rate limits: 2.5 Flash = very high quota; 3.1 Flash = moderate; Pro / Nano Banana 2 = strict (low parallelism)
        if model_override:
            model = model_override
            _m25 = getattr(config, "GEMINI_25_FLASH_IMAGE_MODEL", "")
            if model and _m25 and model == _m25:
                rate_limit_delay = getattr(config, "GEMINI_25_FLASH_IMAGE_RATE_LIMIT_DELAY", 0)
            elif config.GEMINI_31_FLASH_IMAGE_MODEL in (model or ""):
                rate_limit_delay = getattr(config, "GEMINI_31_FLASH_IMAGE_RATE_LIMIT_DELAY", 0)
            else:
                rate_limit_delay = config.GEMINI_SCENE_IMAGE_RATE_LIMIT_DELAY
            retry_delay_base = config.GEMINI_SCENE_IMAGE_RETRY_DELAY
            max_retries = config.GEMINI_SCENE_IMAGE_MAX_RETRIES
            model_type = "2.5 Flash" if (model and _m25 and model == _m25) else "Override"
        elif use_flash:
            model = self.scene_model
            if config.GEMINI_31_FLASH_IMAGE_MODEL in (model or ""):
                rate_limit_delay = getattr(config, "GEMINI_31_FLASH_IMAGE_RATE_LIMIT_DELAY", 0)
            else:
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
        image_config = {"aspectRatio": aspect_ratio}
        if image_size:
            _sz = str(image_size).strip()
            _norm = {"1k": "1K", "2k": "2K", "4k": "4K"}.get(_sz.lower(), _sz)
            image_config["imageSize"] = _norm

        payload = {
            "contents": {
                "role": "user",
                "parts": parts
            },
            "generationConfig": {
                "responseModalities": ["IMAGE", "TEXT"],
                "imageConfig": image_config
            }
        }
        
        # Make API request with retry logic for rate limits
        for attempt in range(max_retries + 1):
            post_t0 = None
            try:
                request_timeout = getattr(config, "GEMINI_IMAGE_REQUEST_TIMEOUT", 480)
                logger.info(f"Generating image with Gemini {model_type} ({model}) - attempt {attempt + 1}...")
                from tvd_pipeline.external_api_log import log_external_api_call, log_external_api_result

                log_external_api_call(
                    "vertex_gemini_image",
                    "generateContent",
                    method="POST",
                    model=model,
                )
                post_t0 = time.perf_counter()
                response = requests.post(
                    url,
                    headers=self.headers,
                    json=payload,
                    timeout=request_timeout  # Under load or after 429 Vertex can be slow (default 8 min)
                )
                _ms = int((time.perf_counter() - post_t0) * 1000)
                try:
                    response.raise_for_status()
                except requests.exceptions.HTTPError as e:
                    status_code = e.response.status_code
                    err_txt = (e.response.text or "")[:200]
                    log_external_api_result(
                        "vertex_gemini_image",
                        "generateContent",
                        duration_ms=_ms,
                        method="POST",
                        model=model,
                        http_status=status_code,
                        ok=False,
                        error=err_txt,
                        detail="retry_429" if status_code == 429 and attempt < max_retries else "",
                    )
                    if status_code == 429 and attempt < max_retries:
                        retry_delay = retry_delay_base * (attempt + 1)  # Exponential backoff
                        logger.warning(f"Rate limit hit (429). Waiting {retry_delay}s before retry {attempt + 1}/{max_retries}...")
                        time.sleep(retry_delay)
                        continue

                    self.last_failure_reason = f"Gemini API {status_code}: {err_txt}"
                    logger.error(f"Gemini Image API error: {status_code} - {e.response.text[:500]}")
                    return None

                result = response.json()

                # Extract generated image from response
                candidates = result.get("candidates", [])
                if not candidates:
                    self.last_failure_reason = "No candidates in Gemini response (safety block or empty response)"
                    logger.error("No candidates in Gemini response")
                    log_external_api_result(
                        "vertex_gemini_image",
                        "generateContent",
                        duration_ms=_ms,
                        method="POST",
                        model=model,
                        http_status=response.status_code,
                        ok=False,
                        detail="no_candidates",
                    )
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
                                    log_external_api_result(
                                        "vertex_gemini_image",
                                        "generateContent",
                                        duration_ms=_ms,
                                        method="POST",
                                        model=model,
                                        http_status=response.status_code,
                                        ok=True,
                                    )

                                    # Rate limit delay to avoid 429 errors
                                    if rate_limit_delay > 0:
                                        logger.info(f"   Waiting {rate_limit_delay}s for rate limit...")
                                        time.sleep(rate_limit_delay)

                                    return image_url
                            else:
                                self.last_failure_reason = "No GCS service available to upload image"
                                logger.warning("No GCS service available to upload image")
                                log_external_api_result(
                                    "vertex_gemini_image",
                                    "generateContent",
                                    duration_ms=_ms,
                                    method="POST",
                                    model=model,
                                    http_status=response.status_code,
                                    ok=False,
                                    detail="no_gcs",
                                )
                                return None

                # Extract diagnostic info Vertex actually returns when safety blocks an image:
                # candidates[0].finish_reason ("SAFETY" / "RECITATION" / "OTHER" / etc.)
                # candidates[0].safety_ratings (per-category severity scores)
                # result.prompt_feedback.block_reason / .safety_ratings (when prompt itself blocked)
                # Without surfacing these, the caller sees "No image" and can't tell whether to
                # rephrase, switch model, or retry.
                cand0 = candidates[0] if candidates else {}
                finish_reason = cand0.get("finishReason") or cand0.get("finish_reason") or "(unset)"
                cand_safety = cand0.get("safetyRatings") or cand0.get("safety_ratings") or []
                pf = result.get("promptFeedback") or result.get("prompt_feedback") or {}
                pf_block = pf.get("blockReason") or pf.get("block_reason")
                pf_safety = pf.get("safetyRatings") or pf.get("safety_ratings") or []

                def _fmt_ratings(ratings):
                    out = []
                    for r in ratings or []:
                        cat = (r.get("category") or "").replace("HARM_CATEGORY_", "")
                        sev = r.get("severity") or r.get("probability") or ""
                        score = r.get("severityScore") or r.get("probabilityScore") or ""
                        if r.get("blocked"):
                            cat += "[BLOCKED]"
                        out.append(f"{cat}={sev}" + (f"({score:.2f})" if isinstance(score, (int, float)) else ""))
                    return ", ".join(out) if out else "(none)"

                safety_summary = (
                    f"finish_reason={finish_reason} | candidate_safety=[{_fmt_ratings(cand_safety)}]"
                    + (f" | prompt_block={pf_block}" if pf_block else "")
                    + (f" | prompt_safety=[{_fmt_ratings(pf_safety)}]" if pf_safety else "")
                )
                # Echo the first 240 chars of the prompt so we can see WHICH scene tripped safety.
                _p_for_log = prompt or ""
                prompt_excerpt = (_p_for_log[:240] + "…") if len(_p_for_log) > 240 else _p_for_log

                self.last_failure_reason = f"Gemini blocked: {safety_summary}"
                logger.error(
                    "No image in Gemini response — %s | prompt[:240]=%r",
                    safety_summary,
                    prompt_excerpt,
                )
                log_external_api_result(
                    "vertex_gemini_image",
                    "generateContent",
                    duration_ms=_ms,
                    method="POST",
                    model=model,
                    http_status=response.status_code,
                    ok=False,
                    detail=f"no_image_part finish={finish_reason}",
                )
                return None

            except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectTimeout) as e:
                from tvd_pipeline.external_api_log import log_external_api_result

                _ms = int((time.perf_counter() - post_t0) * 1000) if post_t0 is not None else 0
                log_external_api_result(
                    "vertex_gemini_image",
                    "generateContent",
                    duration_ms=_ms,
                    method="POST",
                    model=model,
                    ok=False,
                    error=str(e)[:200],
                    detail="retry_timeout" if attempt < max_retries else "",
                )
                # Under load or after 429 Vertex can respond very slowly; retry instead of failing
                if attempt < max_retries:
                    retry_delay = retry_delay_base * (attempt + 1)
                    logger.warning(f"Gemini image request timeout. Waiting {retry_delay}s before retry {attempt + 1}/{max_retries}...")
                    time.sleep(retry_delay)
                    continue
                self.last_failure_reason = str(e)[:200]
                logger.error(f"Error generating image with Gemini (timeout): {e}")
                return None

            except Exception as e:
                from tvd_pipeline.external_api_log import log_external_api_result

                _ms = int((time.perf_counter() - post_t0) * 1000) if post_t0 is not None else 0
                self.last_failure_reason = str(e)[:200]
                logger.error(f"Error generating image with Gemini: {e}")
                log_external_api_result(
                    "vertex_gemini_image",
                    "generateContent",
                    duration_ms=_ms,
                    method="POST",
                    model=model,
                    ok=False,
                    error=str(e)[:200],
                )
                return None
        
        # All retries exhausted
        self.last_failure_reason = "All retries exhausted (likely rate limit 429)"
        logger.error("All retry attempts exhausted for image generation")
        return None
    
    def generate_clean_product_image(
        self,
        reference_image_urls: List[str],
        product_description: str,
        resolution: str = "1K",
        image_model: Optional[str] = None,
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
        
        # Use selected Vertex model if provided (2.5 Flash, 3.1 Flash, Nano Banana 2), else Pro
        override = None
        if image_model == "gemini-25-flash-image":
            override = getattr(config, "GEMINI_25_FLASH_IMAGE_MODEL", "gemini-2.5-flash-image")
        elif image_model == "nano-banana-2":
            override = getattr(config, "GEMINI_NANO_BANANA_2_IMAGE_MODEL", config.GEMINI_31_FLASH_IMAGE_MODEL)
        elif image_model and image_model == config.GEMINI_31_FLASH_IMAGE_MODEL:
            override = image_model
        return self.generate_image(
            prompt=prompt,
            reference_image_urls=reference_image_urls,
            aspect_ratio="9:16",
            use_flash=False,
            model_override=override
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
        image_model: Optional[str] = None,
        image_edit_mode: bool = False,
        scene_context_for_edit: Optional[str] = None,
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
            reference_image_urls: Extra reference URLs (API/wrapper); appended to ref list, deduped.
            prepend_reference_urls: Sent first in reference list (e.g. UGC Real blank 3×3 layout template).
            is_cta_scene: If True, this is the last/CTA scene (accepted for API compatibility; Gemini uses logo_reference_url for CTA).
            **kwargs: Ignored extras for dispatch compatibility.

        Returns:
            URL of the generated scene image, or None if failed.
        """
        if character_reference_urls is None and character_reference_url:
            character_reference_urls = [character_reference_url]
        _vertex_resolution = kwargs.pop("resolution", None)
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
The FIRST attached image is a structural template: vertical 9:16 with exactly nine equal cells in a perfect 3×3 grid (thin dividers, white/light cells). Replicate this geometry exactly in the output — equal cells, straight lines, no merged panels. Fill each cell with the scene content from the prompt.

"""
        # Apply visual style if specified (not "Auto")
        style_prefix = ""
        style_suffix = ""
        if visual_style and visual_style != "Auto":
            # Style-specific prompt prefixes and suffixes loaded from data_maps.json
            _style_map = get_style_prompts("gemini_image")
            style_data = _style_map.get(visual_style)
            if style_data:
                style_prefix, style_suffix = style_data
            else:
                # Custom style name from sheet – use it directly
                style_prefix = f"MANDATORY STYLE: {visual_style}. "
                style_suffix = f"\n\nSTYLE REMINDER: This image MUST be in {visual_style} style throughout."
        
        if image_edit_mode and product_reference_urls:
            ctx = (scene_context_for_edit or "").strip()
            user_instructions = (image_prompt or "").strip() or (
                "Create a visibly improved variant while keeping the same story."
            )
            enhanced_prompt = f"""{style_prefix}
SCENE IMAGE RE-RENDER (USER EDIT):
The first reference is the current frame. Generate a NEW image applying the instructions. Do not return an unchanged copy.

USER INSTRUCTIONS:
{user_instructions}

SCENE CONTEXT:
{ctx if ctx else "Infer from the reference."}

Rules: Apply every requested change clearly; vary lighting or composition if needed so the result is obviously new. 9:16 vertical.{style_suffix}"""
        elif product_visible and product_description:
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
                "Character photo(s) follow — use those for identity only.\n"
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
- BEAUTIFUL & AESTHETIC: The image must look refined and elegant—like a high-end magazine spread or premium film still. Minimal elements (2–4 key subjects only). Generous negative space; soft or simple background (bokeh, gradient, clean wall). No clutter, no busy backgrounds, no visual noise. Premium, pleasing to the eye.
- One clear focal point. The character(s) must be FULLY IMMERSED and INTERACTING with the scene environment
- DO NOT copy the pose, angle, or composition from the reference photo(s)
- Create a CANDID, NATURAL moment - as if caught by a photographer in real life
- The character(s) should be the MAIN SUBJECT but deeply integrated into the setting
- Examples of good integration: leaning over a table to smell food, reaching for a product on a shelf, sitting cross-legged on the floor examining something, walking through a doorway mid-step, laughing while holding chopsticks with food
- The scene should look like a REAL MOMENT, not a posed photo with the character pasted in
- Be CREATIVE - every scene should feel unique and alive"""
        else:
            # No character in scene: enforce beautiful, minimal composition for all scene images
            enhanced_prompt += """

AESTHETIC & COMPOSITION: Beautiful, refined, elegant—like a premium magazine or film still. Minimal elements (2–4 only). Generous negative space; soft or simple background (bokeh, gradient, clean wall). No clutter, no busy visuals, no crowded frame. Premium and pleasing to the eye."""

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
        
        # Use selected Vertex model if provided (2.5 Flash, 3.1 Flash, Nano Banana 2), else Pro
        override = None
        if image_model == "gemini-25-flash-image":
            override = getattr(config, "GEMINI_25_FLASH_IMAGE_MODEL", "gemini-2.5-flash-image")
        elif image_model == "nano-banana-2":
            override = getattr(config, "GEMINI_NANO_BANANA_2_IMAGE_MODEL", config.GEMINI_31_FLASH_IMAGE_MODEL)
        elif image_model and image_model == config.GEMINI_31_FLASH_IMAGE_MODEL:
            override = image_model
        _img_size = None
        if image_model == "nano-banana-2" and _vertex_resolution:
            _sz = str(_vertex_resolution).strip()
            _img_size = {"1k": "1K", "2k": "2K", "4k": "4K"}.get(_sz.lower(), _sz)
        return self.generate_image(
            prompt=enhanced_prompt,
            reference_image_urls=ref_urls if ref_urls else None,
            aspect_ratio="9:16",
            use_flash=False,
            model_override=override,
            image_size=_img_size,
        )
