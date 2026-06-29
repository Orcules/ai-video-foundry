"""Kie.ai service for image generation, video animation, and music."""

import io
import os
import re
import json
import time
import base64
import hashlib
import logging
import tempfile
from typing import Any, Dict, List, Optional
from PIL import Image, ImageFilter, ImageEnhance, ImageOps

import requests

from tvd_pipeline.config import Config
from tvd_pipeline.data_loader import (
    get_style_prompts,
    get_text_translations,
    get_context_detection_keywords,
    get_kie_config,
)
from tvd_pipeline.utils import snap_duration

config = Config()
logger = logging.getLogger(__name__)

class KieAIService:
    """Service for Kie.ai API interactions (Nano Banana and Runway)."""

    @staticmethod
    def _probe_reference_image_url(url: str, timeout: float = 20.0) -> bool:
        """Return True if the URL returns a readable image body (Kie downloads refs server-side)."""
        u = (url or "").strip()
        if not u or not u.startswith(("http://", "https://")):
            return False
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        }
        try:
            r = requests.get(u, headers=headers, timeout=timeout, stream=True)
            if r.status_code != 200:
                return False
            chunk = next(r.iter_content(chunk_size=2048), None)
            return bool(chunk)
        except Exception as e:
            logger.debug("Reference URL probe failed for %s: %s", u[:80], e)
            return False
    
    def __init__(self, api_key: str, gcs_storage_service=None):
        """Initialize Kie.ai service.
        
        Args:
            api_key: Kie.ai API key.
            gcs_storage_service: Optional GCS storage service for uploading CTA buttons.
        """
        self.api_key = api_key
        self.base_url = config.KIE_BASE_URL
        self.gcs_storage_service = gcs_storage_service
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        self.last_failure_reason = ""  # Set when image generation fails (for logging)
        logger.info("✅ Kie.ai client initialized")

    def _kie_create_task(self, payload: dict, operation: str, timeout: int = 60):
        from tvd_pipeline.external_api_log import log_external_api_call, log_external_api_result

        model = (payload.get("model") or "")[:100]
        log_external_api_call("kie", operation, method="POST", model=model, url_hint="/api/v1/jobs/createTask")
        url = f"{self.base_url}/api/v1/jobs/createTask"
        t0 = time.perf_counter()
        try:
            response = requests.post(url, headers=self.headers, json=payload, timeout=timeout)
            ms = int((time.perf_counter() - t0) * 1000)
            err = ""
            if not response.ok:
                try:
                    err = (response.text or "")[:300]
                except Exception:
                    err = "non-ok response"
            log_external_api_result(
                "kie",
                operation,
                duration_ms=ms,
                method="POST",
                model=model,
                http_status=response.status_code,
                ok=response.ok,
                error=err,
            )
            return response
        except Exception as e:
            log_external_api_result(
                "kie",
                operation,
                duration_ms=int((time.perf_counter() - t0) * 1000),
                method="POST",
                model=model,
                ok=False,
                error=str(e)[:300],
            )
            raise

    # Nano Banana Pro max prompt length
    NANO_BANANA_MAX_PROMPT_LENGTH: int = 20000
    
    def _ensure_ref_urls_fetchable(self, ref_urls: List[str], prefix: str = "ref") -> List[str]:
        """Re-upload reference image URLs to GCS if they are not already GCS. Kie API gets 403 when fetching some URLs (e.g. Instagram/Facebook CDN)."""
        if not ref_urls:
            return []
        out: List[str] = []
        for i, u in enumerate(ref_urls):
            u = (u or "").strip()
            if not u:
                continue
            if not self.gcs_storage_service:
                if self._probe_reference_image_url(u):
                    out.append(u)
                else:
                    logger.error(
                        "Kie: skipping reference URL (not reachable — HTTP error or timeout; "
                        "configure GCS to mirror uploads): %s",
                        u[:120],
                    )
                continue
            # Public GCS: still verify — dead keys cause Kie 500 ("get file base64 from url ... 404").
            if "storage.googleapis.com" in u:
                if self._probe_reference_image_url(u):
                    out.append(u)
                else:
                    logger.error(
                        "Kie: skipping storage.googleapis.com reference (404 or blocked): %s",
                        u[:120],
                    )
                continue
            key = f"ref_images/{prefix}_{i}_{int(time.time())}"
            gcs_url = self.gcs_storage_service.upload_image_from_url(u, key_name=key, timeout=25)
            if gcs_url:
                out.append(gcs_url)
                logger.info(f"   📤 Re-uploaded ref image to GCS (avoid 403): {gcs_url[:60]}...")
            elif self._probe_reference_image_url(u):
                out.append(u)
                logger.warning(
                    "   ⚠️ GCS re-upload failed; original URL responds 200 — passing through (Kie may still reject some hosts)"
                )
            else:
                logger.error(
                    "Kie: skipping reference URL (re-upload failed and URL not reachable): %s",
                    u[:120],
                )
        return out
    
    def _sanitize_prompt_remove_brands(self, text: str, target_language: str = "en", article_text: str = "") -> str:
        """Replace brand names with generic terms while keeping product/scene descriptions accurate.
        
        This replaces specific brand names with language-appropriate generic alternatives,
        but keeps all product physical descriptions (shape, color, size, etc.) intact.
        
        The replacement text is context-aware based on the article content:
        - Sale/Discount offers → "SALE" or localized version
        - Job/Career offers → "APPLY"
        - Health/Wellness → "TRY NOW"
        - Learning/Course → "LEARN MORE"
        - Other/Unclear → Remove branding completely (no replacement)
        
        Args:
            text: The prompt text to sanitize.
            target_language: Target language code (e.g., 'en', 'he', 'da', 'tr').
            article_text: The article content to determine context-appropriate text.
            
        Returns:
            Sanitized text with brand names replaced by context-appropriate terms.
        """
        if not text:
            return text
        
        import re
        
        # Determine context-appropriate text based on article content
        article_lower = article_text.lower() if article_text else ""

        # Context detection patterns loaded from data_maps.json
        _ctx_kw = get_context_detection_keywords()
        is_sale_offer = any(word in article_lower for word in _ctx_kw.get("sale", []))
        is_job_offer = any(word in article_lower for word in _ctx_kw.get("job", []))
        is_health_product = any(word in article_lower for word in _ctx_kw.get("health", []))
        is_learning = any(word in article_lower for word in _ctx_kw.get("learning", []))

        # Language-specific text translations loaded from data_maps.json
        text_translations = get_text_translations()
        
        # Select appropriate text based on context
        lang_key = target_language.lower()
        if is_sale_offer:
            replacement_text = text_translations["sale"].get(lang_key, "SALE")
        elif is_job_offer:
            replacement_text = text_translations["apply"].get(lang_key, "APPLY")
        elif is_health_product:
            replacement_text = text_translations["try"].get(lang_key, "TRY NOW")
        elif is_learning:
            replacement_text = text_translations["learn"].get(lang_key, "LEARN MORE")
        else:
            # No clear context - remove branding without replacement
            replacement_text = ""
        
        # Only replace specific brand-related patterns with generic alternatives
        # Keep product descriptions intact! Remove professional/service/person names from being rendered as text.
        brand_patterns = [
            # Replace "brand: X" or "brand name: X" with generic
            (r'\bbrand\s*(?:name)?\s*[:=]\s*["\']?[\w\s\-\.]+["\']?', ''),
            # Replace "logo" with "design element"
            (r'\b(?:brand\s+)?logo\b', 'design element'),
            # Remove trademark/registered symbols
            (r'[™®©]', ''),
            # Remove "X's INTEGRATED REAL ESTATE" / "NAME'S SERVICE" type phrases (no professional name in image)
            (r"\b[A-Za-z\u0590-\u05ff\u0600-\u06ff]+'s\s+(?:INTEGRATED\s+)?(?:REAL\s+ESTATE\s+)?(?:&?\s*MORTGAGE\s+)?(?:SERVICE|BUSINESS|AGENCY)\b", 'professional service'),
            (r"\b(?:professional'?s?|service\s+provider'?s?|person'?s?)\s+name\s+(?:as\s+text|on\s+(?:sign|plaque|screen)|visible)\b", ''),
        ]
        
        # Only add text replacement patterns if we have a replacement text
        if replacement_text:
            brand_patterns.extend([
                # Replace brand text on screens/packaging with contextual text
                (r'\btext\s+(?:saying|showing|displaying)\s*["\'][\w\s\-\.%]+["\']', f'text showing "{replacement_text}"'),
                # Replace specific percentages like "20% off" with contextual text (only for sale offers)
                (r'\b\d+%\s*(?:off|discount|rabat|indirim|הנחה)\b', replacement_text if is_sale_offer else ''),
            ])
        else:
            # No replacement - just remove the branding text
            brand_patterns.extend([
                (r'\btext\s+(?:saying|showing|displaying)\s*["\'][\w\s\-\.%]+["\']', ''),
                (r'\b\d+%\s*(?:off|discount|rabat|indirim|הנחה)\b', ''),
            ])
        
        sanitized = text
        for pattern, replacement in brand_patterns:
            sanitized = re.sub(pattern, replacement, sanitized, flags=re.IGNORECASE)
        
        return sanitized
    
    def generate_image_nano_banana(
        self,
        prompt: str,
        reference_image_url: Optional[str] = None,
        reference_description: Optional[str] = None,
        target_language: str = "en",
        article_text: str = "",
        aspect_ratio: str = "9:16",
        resolution: str = "1K"
    ) -> Optional[str]:
        """Generate an image using Nano Banana Pro.
        
        Args:
            prompt: Text prompt for image generation (max 20000 characters).
            reference_image_url: Optional URL of a reference image for style/content guidance.
            reference_description: Optional description of the reference image to include in prompt.
            target_language: Target language for any text on the image (e.g., 'en', 'he', 'da').
            article_text: Article content for context-aware text replacement.
            aspect_ratio: Aspect ratio (e.g. '9:16', '1:1', '16:9'). Default '9:16'.
            
        Returns:
            URL of the generated image, or None if failed.
        """
        kie_cfg = get_kie_config()
        nb_cfg = kie_cfg["nano_banana"]
        try:
            # Lightly sanitize prompt
            prompt = self._sanitize_prompt_remove_brands(prompt, target_language, article_text)
            if reference_description:
                reference_description = self._sanitize_prompt_remove_brands(reference_description, target_language, article_text)

            # If we have a reference description, prepend it to the prompt
            if reference_description:
                enhanced_ref = f"""REFERENCE PRODUCT (match physical appearance exactly):
{reference_description}

ACCURACY REQUIREMENTS:
- Match EXACT shape, colors, size, materials from reference
- Recreate the product's physical appearance precisely
- If text/branding appears on product, REMOVE it completely - product must be clean

SCENE:"""
                prompt = f"{enhanced_ref}\n\n{prompt}"

            # Truncate prompt if exceeds max length (20000 chars for Nano Banana Pro)
            max_prompt = 20000
            if len(prompt) > max_prompt:
                logger.warning(f"⚠️ Prompt too long ({len(prompt)} chars), truncating to {max_prompt}")
                prompt = prompt[:max_prompt - 3] + "..."

            ref_info = f" (with ref image)" if reference_image_url else ""
            logger.info(f"🍌 Generating image with {nb_cfg['model']} ({len(prompt)} chars){ref_info}...")

            input_params = {
                "prompt": prompt,
                "aspect_ratio": aspect_ratio,
                "resolution": resolution,
                "output_format": nb_cfg["output_format"],
            }

            # Add reference image if provided (re-upload to GCS if needed to avoid 403)
            if reference_image_url:
                fetchable = self._ensure_ref_urls_fetchable([reference_image_url], prefix="single")
                input_params["image_input"] = fetchable if fetchable else [reference_image_url]
                logger.info(f"🎯 Using reference image: {(fetchable[0] if fetchable else reference_image_url)[:60]}...")

            payload = {
                "model": nb_cfg["model"],
                "input": input_params
            }

            response = self._kie_create_task(payload, "nano_banana_image", 60)
            response.raise_for_status()
            
            result = response.json()
            
            if result.get("code") == 200 and "taskId" in result.get("data", {}):
                task_id = result["data"]["taskId"]
                logger.info(f"✅ {nb_cfg['model']} task created: {task_id}")
                return self._wait_for_image_task(task_id)
            else:
                logger.error(f"❌ {nb_cfg['model']} API error: {result}")
                return None

        except Exception as e:
            logger.error(f"❌ Error generating image with {nb_cfg['model']}: {e}")
            return None

    def composite_influencer_in_venue(
        self,
        venue_image_url: str,
        influencer_image_urls: List[str],
        prompt: str = "Put the person in the venue.",
        resolution: str = "1K",
    ) -> Optional[str]:
        """Composite an influencer into a venue image using Nano Banana 2.

        Uses a minimal prompt to preserve the venue faithfully — no
        ``generate_scene_image()`` wrapping which causes hallucinations
        (invented windows, changed architecture).

        Args:
            venue_image_url: URL of the venue/location image.
            influencer_image_urls: URLs of the influencer character image(s).
            prompt: Simple compositing instruction (default: "Put the person in the venue.").
            resolution: Image resolution ("1K", "2K", "4K").

        Returns:
            URL of the composited image, or None if failed.
        """
        kie_cfg = get_kie_config()
        nb_cfg = kie_cfg["nano_banana"]
        try:
            # Combine venue (first) + influencer images for NB2 image_input
            all_urls = [venue_image_url] + list(influencer_image_urls or [])
            fetchable = self._ensure_ref_urls_fetchable(all_urls, prefix="venue_composite")
            if not fetchable:
                logger.warning("⚠️ No fetchable images for venue compositing")
                return None

            logger.info(f"🏠 Compositing influencer into venue ({len(fetchable)} images, {resolution})...")

            payload = {
                "model": nb_cfg["model"],
                "input": {
                    "prompt": prompt,
                    "image_input": fetchable,
                    "resolution": resolution,
                    "output_format": nb_cfg["output_format"],
                    **kie_cfg["nano_banana_scene"],
                },
            }

            response = self._kie_create_task(payload, "nano_banana_venue_composite", 60)
            response.raise_for_status()

            result = response.json()

            if result.get("code") == 200 and "taskId" in result.get("data", {}):
                task_id = result["data"]["taskId"]
                logger.info(f"✅ Venue composite task created: {task_id}")
                return self._wait_for_image_task(task_id)
            else:
                logger.error(f"❌ Venue composite API error: {result}")
                return None

        except Exception as e:
            logger.error(f"❌ Error compositing influencer in venue: {e}")
            return None

    def generate_clean_product_image(
        self,
        reference_image_urls: List[str],
        product_description: str,
        resolution: str = "1K"
    ) -> Optional[str]:
        """Generate a clean, isolated product image from reference frames using Nano Banana Pro.
        
        Takes multiple reference frames and generates a clean,
        professional product image with no background, text, or overlays.
        
        Args:
            reference_image_urls: List of reference image URLs (up to 8).
            product_description: Detailed description of the product.
            
        Returns:
            URL of the clean product image, or None if failed.
        """
        try:
            if not reference_image_urls:
                logger.warning("⚠️ No reference images provided for clean product generation")
                return None
            
            # Use up to 8 reference images (Nano Banana Pro supports up to 8). Re-upload to GCS if needed to avoid 403.
            ref_urls = self._ensure_ref_urls_fetchable(reference_image_urls[:8], prefix="clean_product")
            if not ref_urls:
                logger.warning("⚠️ No reference images available after fetchable check")
                return None
            logger.info(f"🧹 Generating clean product image from {len(ref_urls)} reference frames...")
            
            sanitized_description = self._sanitize_prompt_remove_brands(product_description) if product_description else ""
            
            prompt = f"""Create a product photo that looks EXACTLY like the product in the reference images. Your job is to REPRODUCE the product with zero changes to its appearance.

CRITICAL – IDENTICAL PRODUCT ONLY:
- The output product must be a PIXEL-FAITHFUL reproduction of the product shown in the reference images
- Do NOT reinterpret, redesign, or "improve" the product – copy it exactly: same shape, same colors, same text, same logos, same labels, same packaging, same materials and textures
- Do NOT add or remove any text, graphics, or branding on the product
- Do NOT change colors, proportions, or any visual detail of the product itself
- The product in your image must be indistinguishable from the product in the references – a viewer should see the SAME product

TASK:
- Isolate the product from the reference images (remove only: people, hands, background, environment, other objects)
- Place that SAME product – unchanged – on a clean white or neutral background
- Show the ENTIRE product in full view (nothing cropped or cut off)
- Use professional product lighting and sharp focus

PRODUCT CONTEXT (for reference only – do not alter the product to match this; the reference images are the source of truth):
{sanitized_description}

OUTPUT: One image. The product in it must look exactly like the product in the reference images. Only the background is clean; the product itself is unchanged."""

            # Truncate if needed (20000 char limit)
            if len(prompt) > 20000:
                prompt = prompt[:19997] + "..."
            
            kie_cfg = get_kie_config()
            nb_cfg = kie_cfg["nano_banana"]
            input_params = {
                "prompt": prompt,
                "image_input": ref_urls,
                "resolution": resolution,
                "output_format": nb_cfg["output_format"],
                **kie_cfg["nano_banana_product"],
            }

            logger.info(f"🎯 Using Nano Banana Pro with {len(ref_urls)} reference images")
            for i, ref_url in enumerate(ref_urls):
                logger.info(f"   Reference {i+1}: {ref_url[:60]}...")

            payload = {
                "model": nb_cfg["model"],
                "input": input_params
            }

            response = self._kie_create_task(payload, "nano_banana_clean_product", 60)
            response.raise_for_status()
            
            result = response.json()
            
            if result.get("code") == 200 and "taskId" in result.get("data", {}):
                task_id = result["data"]["taskId"]
                logger.info(f"✅ Clean product image task created: {task_id}")
                
                clean_url = self._wait_for_image_task(task_id)
                if clean_url:
                    logger.info(f"✅ Clean product image generated: {clean_url[:60]}...")
                return clean_url
            else:
                logger.error(f"❌ Clean product image API error: {result}")
                return None
                
        except Exception as e:
            logger.error(f"❌ Error generating clean product image: {e}")
            return None
    
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
        reference_image_urls: Optional[List[str]] = None,
        is_cta_scene: bool = False,
        resolution: str = "1K",
        image_edit_mode: bool = False,
        scene_context_for_edit: Optional[str] = None,
        prepend_reference_urls: Optional[List[str]] = None,
        **kwargs,
    ) -> Optional[str]:
        """Generate a scene image using Nano Banana Pro.
        
        Drop-in replacement for GeminiImageService.generate_scene_image.
        Builds the same enhanced prompt but uses Nano Banana for generation.
        
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
            reference_image_urls: Extra reference URLs (e.g. UGC Real grids / uploads); merged with
                product and character refs, deduped. Same name as GeminiImageService for API routing.
            prepend_reference_urls: If set, these URLs are sent FIRST in image_input (e.g. blank 3×3
                layout template). Used by UGC Real master grid so Nano Banana matches exact grid geometry.
            is_cta_scene: If True, this is the last/CTA scene (one short phrase allowed). If False, body scene — NO on-screen text.
            **kwargs: Absorbs duplicate or legacy keys (e.g. reference_image_urls passed twice) and
                forwards-compatible extras from dispatch layers without crashing older deployments.
            
        Returns:
            URL of the generated scene image, or None if failed.
        """
        _dup_refs = kwargs.pop("reference_image_urls", None)
        if _dup_refs is not None:
            if not reference_image_urls:
                reference_image_urls = _dup_refs
            elif isinstance(reference_image_urls, list) and isinstance(_dup_refs, list):
                _merged = list(reference_image_urls)
                for u in _dup_refs:
                    if u and u not in _merged:
                        _merged.append(u)
                reference_image_urls = _merged
        _kw_prepend = kwargs.pop("prepend_reference_urls", None)
        if prepend_reference_urls is None and _kw_prepend is not None:
            prepend_reference_urls = _kw_prepend
        if kwargs:
            logger.debug(
                "KieAIService.generate_scene_image: ignoring unsupported kwargs: %s",
                sorted(kwargs.keys()),
            )
        if character_reference_urls is None and character_reference_url:
            character_reference_urls = [character_reference_url]
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
LAYOUT REFERENCE IMAGE (FIRST in image_input):
The FIRST attached image is a structural template only: a vertical 9:16 frame with exactly nine equal rectangular cells in a perfect 3×3 grid (three rows × three columns), thin straight divider lines on a white/light field. It defines geometry only, not final art.

Mandatory output geometry (non-negotiable):
- Overall canvas: aspect ratio 9:16 (portrait), full bleed.
- Exactly 9 cells of equal size in a rigid 3×3 layout — no merged cells, masonry layouts, uneven tiles, missing borders, or extra panels.
- Straight, consistent grid lines; symmetric layout; cell order is left-to-right, top-to-bottom (positions 1–9).
- Render photorealistic scene content inside each cell per the text prompt; keep cell boundaries crisp so the image can be split into 9 equal crops.

Later reference images (if any) supply character identity, product, or logo — use them for appearance only; they MUST NOT change the 3×3 structure.

"""
        # Apply visual style if specified (not "Auto")
        style_prefix = ""
        style_suffix = ""
        if visual_style and visual_style != "Auto":
            # Style-specific prompt prefixes and suffixes loaded from data_maps.json
            _style_map = get_style_prompts("kie")
            style_data = _style_map.get(visual_style)
            if style_data:
                style_prefix, style_suffix = style_data
            else:
                style_prefix = f"MANDATORY STYLE: {visual_style}. "
                style_suffix = f"\n\nSTYLE REMINDER: This image MUST be in {visual_style} style throughout."
        
        # Body scene: absolutely no on-screen text. CTA scene: no names, one short phrase allowed.
        if is_cta_scene:
            no_names_rule = """
CRITICAL - NO NAMES IN IMAGE:
Do NOT include the professional's name, service provider's name, business name, or any person's name as visible text in this image. You MAY show one short generic phrase (e.g. "Contact me", "Try now") in the target language. No signs or labels with names."""
        else:
            no_names_rule = """
CRITICAL - THIS IS A BODY SCENE (NOT THE ENDING):
Do NOT add ANY on-screen text in this image: no CTA, no slogan, no "contact me", no "try now", no signs, no words, no captions. Pure visual storytelling only. If the scene description below mentions any slogan, CTA, or text to display — IGNORE IT; this scene must have ZERO text."""
        
        # Studio Fix / re-render: reference is the current frame — do NOT use "identical to product" wording (that copies the image).
        if image_edit_mode and product_reference_urls:
            ctx = (scene_context_for_edit or "").strip()
            user_instructions = (image_prompt or "").strip() or (
                "Create a visibly improved variant: adjust lighting, composition, or detail while keeping the same story and subjects."
            )
            enhanced_prompt = f"""{no_names_rule}{style_prefix}
SCENE IMAGE RE-RENDER (USER EDIT / FIX):
The first reference image is the CURRENT scene frame. Produce a NEW image that applies the instructions below. The result must NOT be a pixel-identical or near-identical copy.

USER INSTRUCTIONS (apply every point):
{user_instructions}

SCENE CONTEXT (what this shot should convey overall):
{ctx if ctx else "Infer from the reference image."}

RULES:
- Implement all user instructions clearly (wardrobe, props, expression, background, color grade, etc.).
- If the user asked for small fixes, those areas must look clearly changed.
- If instructions are vague, still vary lighting, angle, or secondary details so the output is obviously a new render.
- Preserve recognizable identity of people/products unless the user asked to change them.
- Vertical 9:16 aspect.{style_suffix}"""
        elif product_visible and product_description:
            _ip = layout_ref_block + (image_prompt or "") if prep_clean else (image_prompt or "")
            enhanced_prompt = f"""{no_names_rule}
{style_prefix}{_ip}

CRITICAL PRODUCT REQUIREMENTS:
You MUST include the EXACT product shown in the reference images. This is mandatory.

Product Description: {product_description}

IMPORTANT MATCHING RULES:
- The product in this scene MUST be identical to the product in the reference images
- Match the EXACT shape, color, materials, and proportions of the product
- Do NOT create a similar product - use the EXACT product from the references
- The product should be clearly visible and recognizable
- Maintain the product's brand identity and distinctive features{style_suffix}"""
        else:
            _ip = layout_ref_block + (image_prompt or "") if prep_clean else (image_prompt or "")
            enhanced_prompt = f"{no_names_rule}\n{style_prefix}{_ip}{style_suffix}"
        
        # Add character instructions
        if has_character and character_reference_urls:
            _order_note = ""
            if prep_clean:
                _order_note = """
ORDERING: The FIRST reference image is the blank 3×3 grid layout template (geometry only). Character reference photo(s) follow — use those only for face, hair, skin, and body identity."""
            if len(character_reference_urls) == 1:
                enhanced_prompt += f"""

CHARACTER REFERENCE IMAGE INSTRUCTIONS:{_order_note}
A reference photo of the character/influencer is attached. Use it to match their APPEARANCE:
- Face features, skin tone, facial structure
- Hair color, style, length
- Clothing style and colors (adapt to scene context)
- General body type and age

CRITICAL: Create a CANDID, NATURAL moment - as if caught by a photographer in real life.
The character should be the MAIN SUBJECT but deeply integrated into the setting."""
            else:
                enhanced_prompt += f"""

CHARACTER REFERENCE IMAGE INSTRUCTIONS:{_order_note}
Multiple reference photos of the characters are attached (Person 1, Person 2, etc.). Use each photo to match THAT person's APPEARANCE. Include all of these people in the scene when the prompt calls for it.
- Face, skin tone, hair, clothing, body type for each person
CRITICAL: Create a CANDID, NATURAL moment with all characters integrated into the setting."""
        
        # Collect all reference image URLs (Nano Banana Pro supports up to 8). Character first when not in
        # edit mode so identity refs are never truncated when many product/UGC refs are present.
        ref_urls: List[str] = []
        if image_edit_mode and product_reference_urls:
            ref_urls.extend(product_reference_urls)
            if has_character and character_reference_urls:
                ref_urls.extend(character_reference_urls)
            if logo_reference_url:
                ref_urls.append(logo_reference_url)
            if reference_image_urls:
                for u in reference_image_urls:
                    if not u or not isinstance(u, str):
                        continue
                    s = u.strip()
                    if s and s not in ref_urls:
                        ref_urls.append(s)
        else:
            if has_character and character_reference_urls:
                ref_urls.extend(character_reference_urls)
            if logo_reference_url:
                ref_urls.append(logo_reference_url)
            if product_visible and product_reference_urls:
                ref_urls.extend(product_reference_urls)
            if reference_image_urls:
                for u in reference_image_urls:
                    if not u or not isinstance(u, str):
                        continue
                    s = u.strip()
                    if s and s not in ref_urls:
                        ref_urls.append(s)

        if prep_clean:
            _seen_pre = set(prep_clean)
            ref_urls = list(prep_clean) + [u for u in ref_urls if u not in _seen_pre]

        _ref_count_in = sum(1 for x in ref_urls if x and str(x).strip())
        ref_urls = self._ensure_ref_urls_fetchable(ref_urls, prefix="scene_ref")
        if _ref_count_in > 0 and not ref_urls:
            self.last_failure_reason = (
                "All reference image URLs were invalid or unreachable (e.g. HTTP 404). "
                "Kie cannot download them — re-upload assets or use fresh public URLs."
            )
            logger.error(self.last_failure_reason)
            return None

        # Sanitize prompt
        enhanced_prompt = self._sanitize_prompt_remove_brands(enhanced_prompt)
        self.last_failure_reason = ""  # Reset so this call's failure reason is accurate
        
        # Truncate if needed (20000 char limit for Nano Banana Pro)
        if len(enhanced_prompt) > 20000:
            enhanced_prompt = enhanced_prompt[:19997] + "..."
        
        logger.info(f"🍌 Generating scene image with Nano Banana Pro ({len(enhanced_prompt)} chars, {len(ref_urls)} refs)...")
        
        try:
            kie_cfg = get_kie_config()
            nb_cfg = kie_cfg["nano_banana"]
            input_params = {
                "prompt": enhanced_prompt,
                "resolution": resolution,
                "output_format": nb_cfg["output_format"],
                **kie_cfg["nano_banana_scene"],
            }

            if ref_urls:
                input_params["image_input"] = ref_urls[:8]  # Max 8 images
                logger.info(f"🎯 Using Nano Banana Pro with {len(ref_urls)} reference images")

            payload = {
                "model": nb_cfg["model"],
                "input": input_params
            }

            response = self._kie_create_task(payload, "nano_banana_scene_image", 60)
            response.raise_for_status()
            
            result = response.json()
            
            if result.get("code") == 200 and "taskId" in result.get("data", {}):
                task_id = result["data"]["taskId"]
                logger.info(f"✅ Scene image task created: {task_id}")
                out = self._wait_for_image_task(task_id, exclude_urls=ref_urls if ref_urls else None)
                if out is None and self.last_failure_reason:
                    logger.error(f"❌ Nano Banana Pro task failed: {self.last_failure_reason}")
                return out
            else:
                err_msg = result.get("message") or result.get("msg") or str(result)[:200]
                self.last_failure_reason = f"API error: {err_msg}"
                logger.error(f"❌ Nano Banana Pro scene image API error: {result}")
                return None
                
        except Exception as e:
            self.last_failure_reason = str(e)[:300]
            logger.error(f"❌ Error generating scene image with Nano Banana Pro: {e}")
            return None
    
    def generate_scene_image_flash(
        self,
        image_prompt: str,
        character_reference_url: Optional[str] = None,
        character_reference_urls: Optional[List[str]] = None,
        product_reference_urls: Optional[List[str]] = None,
        reference_image_urls: Optional[List[str]] = None,
        **kwargs
    ) -> Optional[str]:
        """Generate a scene image using Gemini 3 Flash via Kie.ai Chat Completions API.

        Uses ``POST https://api.kie.ai/gemini-3-flash/v1/chat/completions``
        with the same Kie API key.  Reference images are sent as ``image_url``
        content blocks.  The model returns an image URL inside the response.

        Args:
            image_prompt: The scene image prompt.
            character_reference_url: Optional single character reference image URL (legacy).
            character_reference_urls: Optional list of character reference image URLs (multiple people).
            product_reference_urls: Optional list of product/environment reference image URLs.
            reference_image_urls: Extra refs merged after character/product lists (deduped).
            **kwargs: Ignored (keeps signature compatible with other generators).

        Returns:
            URL of the generated image, or None on failure.
        """
        if character_reference_urls is None and character_reference_url:
            character_reference_urls = [character_reference_url]
        try:
            # Build content array for the user message
            content_parts: List[Dict[str, Any]] = []

            # Add reference images first so the model sees them before the text. Re-upload to GCS if needed to avoid 403.
            ref_urls = []
            if character_reference_urls:
                ref_urls.extend([u for u in character_reference_urls if u])
            if product_reference_urls:
                ref_urls.extend([u for u in product_reference_urls if u])
            if reference_image_urls:
                for u in reference_image_urls:
                    if not u or not isinstance(u, str):
                        continue
                    s = u.strip()
                    if s and s not in ref_urls:
                        ref_urls.append(s)
            ref_urls = self._ensure_ref_urls_fetchable(ref_urls, prefix="flash_ref")
            for ref_url in ref_urls:
                content_parts.append({
                    "type": "image_url",
                    "image_url": {"url": ref_url.strip()}
                })

            # Add the text prompt. Body scene: no text at all. CTA scene: no names, one short phrase OK.
            image_prompt_sanitized = self._sanitize_prompt_remove_brands(image_prompt)
            is_cta_scene = kwargs.get("is_cta_scene", False)
            if is_cta_scene:
                no_names_instruction = "CRITICAL: Do NOT include any person's name, business name, or service provider's name as visible text. You MAY show one short phrase (e.g. Contact me, Try now). "
            else:
                no_names_instruction = "CRITICAL - THIS IS A BODY SCENE: Do NOT add ANY on-screen text — no CTA, no slogan, no signs, no words. Pure visual storytelling only. "
            if kwargs.get("image_edit_mode") and ref_urls:
                ctx = (kwargs.get("scene_context_for_edit") or "").strip()
                user_part = (image_prompt or "").strip() or (
                    "Create a clearly new variant of the first image (lighting/composition/detail); not a copy."
                )
                edit_text = (
                    no_names_instruction
                    + "RE-RENDER / FIX: The first image is the current scene. Produce a NEW image that applies these instructions — output must differ visibly, not a duplicate.\n\n"
                    + "INSTRUCTIONS:\n"
                    + user_part
                    + "\n\nSCENE CONTEXT:\n"
                    + (ctx if ctx else "(from reference)")
                )
                content_parts.append({"type": "text", "text": edit_text})
            else:
                content_parts.append({
                    "type": "text",
                    "text": (
                        no_names_instruction
                        + "Generate an image based on the following description. "
                        "Use the reference images (if provided) for visual style and character consistency.\n\n"
                        f"{image_prompt_sanitized}"
                    )
                })

            ref_info = f" (+{len(ref_urls)} refs)" if ref_urls else ""
            logger.info(f"⚡ Generating scene image with Kie Flash ({len(image_prompt)} chars{ref_info})...")

            kie_cfg = get_kie_config()
            url = "https://api.kie.ai/gemini-3-flash/v1/chat/completions"
            payload = {
                "messages": [
                    {"role": "user", "content": content_parts}
                ],
                **kie_cfg["flash"],
            }

            from tvd_pipeline.external_api_log import log_external_api_call

            log_external_api_call(
                "kie",
                "gemini_3_flash_chat_completions",
                method="POST",
                url_hint="/gemini-3-flash/v1/chat/completions",
            )
            response = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                },
                json=payload,
                timeout=120
            )
            response.raise_for_status()
            result = response.json()

            # Extract image URL from the response
            # The model may return an image URL in the content or as an inline image
            choices = result.get("choices", [])
            if not choices:
                logger.error("❌ Kie Flash returned no choices")
                return None

            message = choices[0].get("message", choices[0].get("delta", {}))
            msg_content = message.get("content", "")

            # The content may be a string with a URL, or a list of content blocks
            image_url = None
            if isinstance(msg_content, list):
                for block in msg_content:
                    if isinstance(block, dict):
                        if block.get("type") == "image_url":
                            image_url = block.get("image_url", {}).get("url")
                            break
                        elif block.get("type") == "image":
                            image_url = block.get("url") or block.get("image_url", {}).get("url")
                            break
            elif isinstance(msg_content, str):
                # Try to find a URL in the text response
                import re as _re
                url_match = _re.search(r'https?://[^\s\)"\']+\.(?:png|jpg|jpeg|webp|gif)', msg_content)
                if url_match:
                    image_url = url_match.group(0)

            if image_url:
                if ref_urls and any(
                    ref and (image_url == ref or ref in image_url or image_url.startswith(ref.rstrip("/") + "/"))
                    for ref in ref_urls
                ):
                    logger.warning(
                        "Kie Flash returned reference URL as result; treating as no new image (Regenerate/Fix)."
                    )
                    return None
                logger.info(f"✅ Kie Flash image generated: {image_url[:80]}...")
                return image_url
            else:
                logger.error(f"❌ Kie Flash: Could not extract image URL from response")
                logger.debug(f"   Response content: {str(msg_content)[:500]}")
                return None

        except Exception as e:
            logger.error(f"❌ Error generating scene image with Kie Flash: {e}")
            return None

    def _wait_for_image_task(
        self,
        task_id: str,
        timeout: int = 900,
        exclude_urls: Optional[List[str]] = None,
    ) -> Optional[str]:
        """Wait for image generation task to complete.

        Args:
            task_id: Task ID to poll.
            timeout: Maximum wait time in seconds (default 900 = 15 min; some images take 10+ min).
            exclude_urls: If provided, do not return any URL that equals or is contained in these
                (e.g. reference image URLs). Return the first result URL that is not in the list,
                so Regenerate/Fix this image always returns the newly generated image, not the input.

        Returns:
            URL of the generated image, or None if failed/timeout or only excluded URLs returned.
        """
        start_time = time.time()
        check_interval = 4  # Poll every 4s (after initial 8s wait for task to start)
        time.sleep(8)  # Nano Banana tasks take ~10-15s; skip first few useless polls
        exclude_set = set((exclude_urls or []))
        success_without_json_polls = 0

        def _is_excluded(result_url: str) -> bool:
            if not result_url or not exclude_set:
                return False
            if result_url in exclude_set:
                return True
            for ref in exclude_set:
                if ref and (result_url == ref or result_url.startswith(ref.rstrip("/") + "/") or ref in result_url):
                    return True
            return False

        while time.time() - start_time < timeout:
            try:
                url = f"{self.base_url}/api/v1/jobs/recordInfo"
                response = requests.get(
                    f"{url}?taskId={task_id}",
                    headers=self.headers,
                    timeout=30
                )
                response.raise_for_status()
                
                result = response.json()
                
                if result.get("code") == 200:
                    data = result.get("data", {})
                    state = data.get("state", "").lower()
                    
                    if state == "success":
                        result_json_str = data.get("resultJson")
                        if not result_json_str:
                            success_without_json_polls += 1
                            if success_without_json_polls >= 12:
                                self.last_failure_reason = (
                                    "Kie reported success but resultJson stayed empty after ~60s"
                                )
                                logger.error(f"❌ {self.last_failure_reason}")
                                return None
                        else:
                            success_without_json_polls = 0
                            try:
                                result_json = json.loads(result_json_str)
                            except json.JSONDecodeError as je:
                                self.last_failure_reason = f"Kie resultJson parse error: {je}"
                                logger.error(f"❌ {self.last_failure_reason}")
                                return None
                            result_urls = result_json.get("resultUrls") or []
                            if not result_urls:
                                self.last_failure_reason = (
                                    "Kie reported success but resultUrls was empty (no image link)"
                                )
                                logger.error(f"❌ {self.last_failure_reason}")
                                return None
                            for candidate in result_urls:
                                if candidate and not _is_excluded(candidate):
                                    logger.info(f"✅ Image generated successfully")
                                    return candidate
                            if exclude_set and result_urls:
                                logger.warning(
                                    "Kie returned only reference URL(s); treating as no new image. "
                                    "resultUrls may contain input reference."
                                )
                                self.last_failure_reason = (
                                    "Generated image URL matched reference only (Regenerate/Fix may need different refs)"
                                )
                                return None
                            if result_urls:
                                return result_urls[0] or None

                    elif state == "fail":
                        success_without_json_polls = 0
                        fail_msg = data.get("failMsg") or data.get("fail_msg") or data.get("message") or "Unknown error"
                        self.last_failure_reason = str(fail_msg)[:300]
                        logger.error(f"❌ Image generation failed: {fail_msg}")
                        return None
                    else:
                        success_without_json_polls = 0

                    # Still processing, continue polling
                    elapsed = int(time.time() - start_time)
                    if elapsed > 0 and elapsed % 30 < check_interval:
                        logger.info(f"   🔄 Image generation in progress... ({elapsed}s elapsed, state={state})")
                    
            except Exception as e:
                self.last_failure_reason = str(e)[:300]
                logger.error(f"❌ Error polling task status: {e}")
            
            time.sleep(check_interval)
        
        elapsed = int(time.time() - start_time)
        self.last_failure_reason = f"Timeout after {elapsed}s waiting for image task"
        logger.error(f"❌ Image generation timeout after {elapsed}s")
        return None
    
    def generate_video_runway(
        self, 
        prompt: str, 
        image_url: str,
        duration: float = 5.0
    ) -> Optional[str]:
        """Generate a video using Runway.
        
        Args:
            prompt: Motion/animation prompt.
            image_url: URL of the source image.
            duration: Video duration in seconds (will be rounded to 5 or 10).
            
        Returns:
            URL of the generated video, or None if failed.
        """
        try:
            # Runway supports 5 or 10 second durations — use snap_duration for clean mapping
            runway_duration = snap_duration("gen4-turbo", duration)
            logger.info(f"🎬 Generating video with Runway (duration: {runway_duration}s, original scene: {duration:.1f}s)...")
            
            url = f"{self.base_url}/api/v1/runway/generate"

            kie_cfg = get_kie_config()
            payload = {
                "prompt": prompt,
                "imageUrl": image_url,
                "duration": runway_duration,
                **kie_cfg["runway"],
            }

            from tvd_pipeline.external_api_log import log_external_api_call

            log_external_api_call("kie", "runway_generate", method="POST", url_hint="/api/v1/runway/generate")
            response = requests.post(url, headers=self.headers, json=payload, timeout=60)
            response.raise_for_status()
            
            result = response.json()
            
            if result.get("code") == 200 and "taskId" in result.get("data", {}):
                task_id = result["data"]["taskId"]
                logger.info(f"✅ Runway task created: {task_id}")
                
                # Poll for completion
                return self._wait_for_video_task(task_id)
            else:
                logger.error(f"❌ Runway API error: {result}")
                return None
                
        except Exception as e:
            logger.error(f"❌ Error generating video with Runway: {e}")
            return None
    
    def _wait_for_video_task(self, task_id: str, timeout: int = 600) -> Optional[str]:
        """Wait for Runway video generation task to complete.
        
        Uses the dedicated Runway record-detail endpoint for accurate status.
        
        Args:
            task_id: Task ID to poll.
            timeout: Maximum wait time in seconds.
            
        Returns:
            URL of the generated video, or None if failed/timeout.
        """
        start_time = time.time()
        check_interval = 5  # adaptive: starts at 5s, grows to 8s after 60s elapsed

        while time.time() - start_time < timeout:
            try:
                # Use the dedicated Runway record-detail endpoint
                url = f"{self.base_url}/api/v1/runway/record-detail"
                response = requests.get(
                    f"{url}?taskId={task_id}",
                    headers=self.headers,
                    timeout=30
                )
                response.raise_for_status()
                
                result = response.json()
                
                if result.get("code") == 200:
                    data = result.get("data", {})
                    state = data.get("state", "").lower()
                    
                    logger.debug(f"Runway task {task_id} status: {state}")
                    
                    if state == "success":
                        # Get video URL from videoInfo object
                        video_info = data.get("videoInfo", {})
                        video_url = video_info.get("videoUrl")
                        if video_url:
                            logger.info(f"✅ Video generated successfully")
                            return video_url
                    
                    elif state == "fail":
                        fail_msg = data.get("failMsg", "Unknown error")
                        logger.error(f"❌ Video generation failed: {fail_msg}")
                        return None
                    
                    # States: wait, queueing, generating - continue polling

            except Exception as e:
                logger.error(f"❌ Error polling Runway task status: {e}")

            elapsed = time.time() - start_time
            if elapsed > 60:
                check_interval = min(check_interval + 1, 8)
            time.sleep(check_interval)

        logger.error("❌ Video generation timeout")
        return None

    # Kling API model string mapping: video_model value -> Kie API model ID
    _KLING_MODEL_MAP = {
        "kling-2.5": "kling/v2-5-turbo-image-to-video-pro",
        "kling-2.6": "kling/v2-6-turbo-image-to-video-pro",
    }

    def _normalize_still_for_avatar_pro(self, image_url: str) -> str:
        """Resize/re-encode still for Kling ai-avatar-pro (Kie: jpeg/png/webp, max 10MB).

        Grid crops are often small (few hundred px) or very large — both can trigger Kie 5xx or task fail.
        """
        url = (image_url or "").strip()
        if not url or not self.gcs_storage_service:
            return url
        avatar_cfg = get_kie_config().get("avatar_pro") or {}
        if not avatar_cfg.get("normalize_input_image", True):
            return url
        max_edge = int(avatar_cfg.get("avatar_max_edge_px", 1920))
        min_short = int(avatar_cfg.get("avatar_min_short_edge_px", 512))
        max_bytes = int(avatar_cfg.get("avatar_max_image_bytes", 9_500_000))
        timeout = int(avatar_cfg.get("avatar_image_download_timeout_sec", 45))
        max_download = int(avatar_cfg.get("avatar_max_download_bytes", 24 * 1024 * 1024))

        try:
            r = requests.get(
                url,
                timeout=timeout,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; TVD-Pipeline/1.0)",
                    "Accept": "image/*,*/*;q=0.8",
                },
            )
            r.raise_for_status()
            raw = r.content
            if len(raw) > max_download:
                logger.warning(
                    "Avatar Pro: input image download %s bytes exceeds cap %s — using original URL",
                    len(raw),
                    max_download,
                )
                return url
        except Exception as e:
            logger.warning("Avatar Pro: could not download still for normalize (%s) — using original URL", e)
            return url

        try:
            img = Image.open(io.BytesIO(raw))
            img = ImageOps.exif_transpose(img)
            if img.mode in ("RGBA", "P"):
                base = Image.new("RGB", img.size, (255, 255, 255))
                src = img.convert("RGBA") if img.mode == "P" else img
                base.paste(src, mask=src.split()[-1] if src.mode == "RGBA" else None)
                img = base
            else:
                img = img.convert("RGB")
        except Exception as e:
            logger.warning("Avatar Pro: could not decode image (%s) — using original URL", e)
            return url

        w, h = img.size
        if w < 16 or h < 16:
            return url

        try:
            resample = Image.Resampling.LANCZOS
        except AttributeError:
            resample = Image.LANCZOS  # type: ignore[attr-defined]

        mmin, mmax = min(w, h), max(w, h)
        if mmin < min_short and mmin > 0:
            scale = float(min_short) / float(mmin)
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), resample)
            w, h = img.size
            mmax = max(w, h)
        if mmax > max_edge and mmax > 0:
            scale = float(max_edge) / float(mmax)
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), resample)

        qualities = [92, 88, 82, 76, 70, 65, 60, 55, 50]
        jpeg_bytes: Optional[bytes] = None
        for q in qualities:
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=q, optimize=True)
            jpeg_bytes = buf.getvalue()
            if len(jpeg_bytes) <= max_bytes:
                break
        if jpeg_bytes is not None and len(jpeg_bytes) > max_bytes:
            for _ in range(5):
                w, h = img.size
                img = img.resize((max(1, int(w * 0.88)), max(1, int(h * 0.88))), resample)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=68, optimize=True)
                jpeg_bytes = buf.getvalue()
                if len(jpeg_bytes) <= max_bytes:
                    break

        if jpeg_bytes is None or len(jpeg_bytes) == 0:
            return url

        if len(jpeg_bytes) > max_bytes:
            logger.warning(
                "Avatar Pro: JPEG still %.2f MB after normalize — uploading best effort (Kie limit 10MB)",
                len(jpeg_bytes) / 1_000_000.0,
            )

        short_hash = hashlib.sha256(url.encode("utf-8", errors="ignore")).hexdigest()[:10]
        key = f"ugc_real/kling_avatar_still_{int(time.time())}_{short_hash}.jpg"
        out_url = self.gcs_storage_service.upload_image_bytes(
            jpeg_bytes, key_name=key, content_type="image/jpeg"
        )
        if out_url:
            logger.info(
                "Avatar Pro: normalized still for API → %s×%s, %s bytes JPEG → GCS",
                img.size[0],
                img.size[1],
                len(jpeg_bytes),
            )
            return out_url
        logger.warning("Avatar Pro: GCS upload of normalized still failed — using original URL")
        return url

    def generate_avatar_video(
        self,
        image_url: str,
        audio_url: str,
        prompt: str = "",
        callback_url: Optional[str] = None,
    ) -> Optional[str]:
        """Generate lip-sync avatar video using Kling AI Avatar Pro."""
        try:
            self.last_failure_reason = ""
            if not image_url or not audio_url:
                self.last_failure_reason = "Avatar generation requires image_url and audio_url"
                return None

            kie_cfg = get_kie_config()
            avatar_cfg = kie_cfg.get("avatar_pro", {})
            model = avatar_cfg.get("model", "kling/ai-avatar-pro")
            max_prompt_len = int(avatar_cfg.get("max_prompt_length", 5000))
            prompt = (prompt or "")[:max_prompt_len]

            image_for_api = self._normalize_still_for_avatar_pro(image_url)

            payload: Dict[str, Any] = {
                "model": model,
                "input": {
                    "image_url": image_for_api,
                    "audio_url": audio_url,
                    "prompt": prompt,
                },
            }
            if callback_url:
                payload["callBackUrl"] = callback_url

            response = self._kie_create_task(payload, "kling_avatar_pro", 60)
            if not response.ok:
                err_body = (response.text or "")[:1200]
                self.last_failure_reason = f"Kling Avatar Pro HTTP {response.status_code}: {err_body}"
                logger.error("❌ Avatar createTask HTTP error: %s", self.last_failure_reason)
                return None
            try:
                result = response.json()
            except Exception as je:
                self.last_failure_reason = f"Kling Avatar Pro response not JSON: {(response.text or '')[:500]} ({je})"
                logger.error("❌ Avatar createTask invalid JSON: %s", self.last_failure_reason)
                return None

            if result.get("code") == 200 and "taskId" in result.get("data", {}):
                task_id = result["data"]["taskId"]
                logger.info(f"✅ Kling Avatar Pro task created: {task_id}")
                video_url = self._wait_for_kling_task(task_id)
                if not video_url and not (self.last_failure_reason or "").strip():
                    self.last_failure_reason = (
                        "Kling Avatar Pro finished without a video URL (check recordInfo failMsg on Kie dashboard)"
                    )
                return video_url

            self.last_failure_reason = (
                f"Kling Avatar Pro createTask code={result.get('code')!r} msg={result.get('msg')!r} body={str(result)[:800]}"
            )
            logger.error("❌ Avatar createTask failed: %s", self.last_failure_reason)
            return None

        except Exception as e:
            self.last_failure_reason = str(e)
            logger.error(f"❌ Error generating avatar video: {e}")
            return None

    def generate_video_kling(
        self,
        prompt: str,
        image_url: str,
        duration: float = 5.0,
        video_model: str = "kling-2.5",
        **kwargs,
    ) -> Optional[str]:
        """Generate a video using Kling Image-to-Video Pro.

        Args:
            prompt: Motion/animation prompt.
            image_url: URL of the source image.
            duration: Video duration in seconds (5 or 10).
            video_model: Kling model version ("kling-2.5" or "kling-2.6").
            **kwargs: Ignored (keeps signature compatible).

        Returns:
            URL of the generated video, or None if failed.
        """
        try:
            # Kling supports 5 or 10 second durations — use snap_duration for clean mapping
            kling_duration = str(snap_duration(video_model, duration))
            api_model = self._KLING_MODEL_MAP.get(video_model, "kling/v2-5-turbo-image-to-video-pro")
            logger.info(f"🎬 Generating video with {video_model} (duration: {kling_duration}s, original scene: {duration:.1f}s)...")

            kie_cfg = get_kie_config()

            payload = {
                "model": api_model,
                "input": {
                    "prompt": prompt,
                    "image_url": image_url,
                    "duration": kling_duration,
                    **kie_cfg["kling"],
                }
            }

            response = self._kie_create_task(payload, "kling_image_to_video", 60)
            response.raise_for_status()
            
            result = response.json()
            
            if result.get("code") == 200 and "taskId" in result.get("data", {}):
                task_id = result["data"]["taskId"]
                logger.info(f"✅ Kling task created: {task_id}")
                
                # Poll for completion using generic endpoint
                return self._wait_for_kling_task(task_id)
            else:
                logger.error(f"❌ Kling API error: {result}")
                return None
                
        except Exception as e:
            logger.error(f"❌ Error generating video with Kling: {e}")
            return None
    
    def _wait_for_kling_task(self, task_id: str, timeout: int = 600) -> Optional[str]:
        """Wait for Kling video generation task to complete.
        
        Uses the generic jobs/recordInfo endpoint for status.
        
        Args:
            task_id: Task ID to poll.
            timeout: Maximum wait time in seconds.
            
        Returns:
            URL of the generated video, or None if failed/timeout.
        """
        start_time = time.time()
        check_interval = 5  # adaptive: starts at 5s, grows to 8s after 60s elapsed

        while time.time() - start_time < timeout:
            try:
                url = f"{self.base_url}/api/v1/jobs/recordInfo"
                response = requests.get(
                    f"{url}?taskId={task_id}",
                    headers=self.headers,
                    timeout=30
                )
                response.raise_for_status()
                
                result = response.json()
                
                if result.get("code") == 200:
                    data = result.get("data", {})
                    state = data.get("state", "").lower()
                    
                    logger.debug(f"Kling task {task_id} status: {state}")
                    
                    if state == "success":
                        # Get video URL from resultJson
                        result_json_str = data.get("resultJson", "{}")
                        try:
                            result_json = json.loads(result_json_str) if isinstance(result_json_str, str) else result_json_str
                            result_urls = result_json.get("resultUrls", [])
                            if result_urls:
                                video_url = result_urls[0]
                                logger.info(f"✅ Kling video generated successfully")
                                return video_url
                            self.last_failure_reason = (
                                "Kling state=success but resultUrls empty — " + str(result_json_str)[:500]
                            )
                            logger.error("❌ Kling success with empty resultUrls: %s", result_json_str)
                            return None
                        except json.JSONDecodeError:
                            self.last_failure_reason = ("Invalid Kling resultJson: " + str(result_json_str))[:2000]
                            logger.error(f"❌ Failed to parse Kling resultJson: {result_json_str}")
                            return None
                    
                    elif state == "fail":
                        fail_msg = data.get("failMsg", "Unknown error")
                        fail_code = data.get("failCode", "")
                        detail = f"{fail_msg}" + (f" (code={fail_code})" if fail_code else "")
                        self.last_failure_reason = detail[:2000]
                        logger.error(
                            "❌ Kling task %s failed: %s | raw data keys=%s",
                            task_id,
                            detail,
                            list(data.keys()),
                        )
                        return None
                    
                    # States: waiting, queuing, generating - continue polling

            except Exception as e:
                logger.error(f"❌ Error polling Kling task status: {e}")

            elapsed = time.time() - start_time
            if elapsed > 60:
                check_interval = min(check_interval + 1, 8)
            time.sleep(check_interval)

        self.last_failure_reason = f"Kling task timeout after {timeout}s (taskId={task_id})"
        logger.error("❌ Kling video generation timeout")
        return None

    def generate_video_seedance(
        self,
        prompt: str,
        *,
        first_frame_url: Optional[str] = None,
        last_frame_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
        reference_video_urls: Optional[List[str]] = None,
        reference_audio_urls: Optional[List[str]] = None,
        duration: int = None,
        resolution: str = None,
        aspect_ratio: str = None,
        generate_audio: bool = None,
        web_search: bool = False,
        nsfw_checker: bool = None,
        **kwargs,
    ) -> Optional[str]:
        """Generate a video using ByteDance Seedance 2.0 via Kie.

        Seedance 2.0 supports three mutually-exclusive input modes:
          1. Multimodal references — up to 9 images + 3 videos + 3 audios.
          2. First frame — single image-to-video.
          3. First + last frame — bookended generation.

        Pick the mode by which kwargs are populated. Defaults come from
        ``kie.json:seedance``.

        Args:
            prompt: Text description (3-20000 chars).
            first_frame_url: Optional initial frame URL (mode 2/3).
            last_frame_url: Optional final frame URL (mode 3).
            reference_image_urls: Up to 9 reference image URLs (mode 1).
            reference_video_urls: Up to 3 reference video URLs (mode 1).
            reference_audio_urls: Up to 3 reference audio URLs (mode 1).
            duration: 4-15 seconds. Defaults from config.
            resolution: '480p' | '720p' | '1080p'. Defaults from config.
            aspect_ratio: '1:1' | '4:3' | '3:4' | '16:9' | '9:16' | '21:9' | 'adaptive'.
            generate_audio: Whether to generate native audio. We default to False
                (we mix our own VO + music downstream).
            web_search: Enable online search for prompt enrichment.
            nsfw_checker: Content filtering toggle.

        Returns:
            URL of the generated video, or None on failure.
        """
        try:
            cfg = get_kie_config().get("seedance", {})
            api_model = cfg.get("model", "bytedance/seedance-2")

            # Defaults from config
            duration = int(duration if duration is not None else cfg.get("duration", 5))
            resolution = resolution or cfg.get("resolution", "720p")
            aspect_ratio = aspect_ratio or cfg.get("aspect_ratio", "9:16")
            if generate_audio is None:
                generate_audio = bool(cfg.get("generate_audio", False))
            if nsfw_checker is None:
                nsfw_checker = bool(cfg.get("nsfw_checker", False))

            # Clamp duration to API limits
            d_min = int(cfg.get("min_duration_seconds", 4))
            d_max = int(cfg.get("max_duration_seconds", 15))
            duration = max(d_min, min(d_max, duration))

            # Enforce reference count caps (Kie returns 422 if exceeded)
            ref_imgs = list(reference_image_urls or [])[: int(cfg.get("max_reference_images", 9))]
            ref_vids = list(reference_video_urls or [])[: int(cfg.get("max_reference_videos", 3))]
            ref_auds = list(reference_audio_urls or [])[: int(cfg.get("max_reference_audios", 3))]

            # Enforce mutual exclusion. Priority: multimodal refs > first+last > first.
            has_refs = bool(ref_imgs or ref_vids or ref_auds)
            has_first = bool(first_frame_url)
            has_last = bool(last_frame_url)
            mode = "text_only"
            input_block: Dict[str, Any] = {
                "prompt": prompt,
                "resolution": resolution,
                "aspect_ratio": aspect_ratio,
                "duration": duration,
                "generate_audio": generate_audio,
            }
            if has_refs:
                mode = "multimodal_refs"
                if has_first or has_last:
                    logger.info(
                        "[seedance] Both refs and first/last frame supplied — Kie requires "
                        "mutual exclusion; using multimodal_refs and dropping first/last."
                    )
                if ref_imgs:
                    input_block["reference_image_urls"] = ref_imgs
                if ref_vids:
                    input_block["reference_video_urls"] = ref_vids
                if ref_auds:
                    input_block["reference_audio_urls"] = ref_auds
            elif has_first and has_last:
                mode = "first_last_frame"
                input_block["first_frame_url"] = first_frame_url
                input_block["last_frame_url"] = last_frame_url
            elif has_first:
                mode = "first_frame"
                input_block["first_frame_url"] = first_frame_url

            if web_search:
                input_block["web_search"] = True
            if nsfw_checker:
                input_block["nsfw_checker"] = True

            payload = {"model": api_model, "input": input_block}

            logger.info(
                "🎬 Seedance 2.0 generating (%s mode, dur=%ss, res=%s, ar=%s, refs=%d/%d/%d)...",
                mode, duration, resolution, aspect_ratio,
                len(ref_imgs), len(ref_vids), len(ref_auds),
            )

            response = self._kie_create_task(payload, "seedance_video", 60)
            response.raise_for_status()

            result = response.json()
            if result.get("code") == 200 and "taskId" in result.get("data", {}):
                task_id = result["data"]["taskId"]
                logger.info(f"✅ Seedance task created: {task_id}")
                # The generic Kie recordInfo polling works for Seedance too.
                return self._wait_for_kling_task(task_id)
            else:
                self.last_failure_reason = f"Seedance create_task failed: {result}"
                logger.error(f"❌ Seedance API error: {result}")
                return None

        except Exception as e:
            self.last_failure_reason = f"Seedance exception: {e}"
            logger.error(f"❌ Error generating video with Seedance: {e}")
            return None

    def generate_cta_button(self, cta_text: str, resolution: str = "1K") -> Optional[str]:
        """Generate a CTA button image using Nano Banana AI model.
        
        Creates a beautiful CTA button with the specified text using the Kie.ai Nano Banana model.
        
        Args:
            cta_text: Text to display on the button.
            
        Returns:
            URL of the generated button image, or None if failed.
        """
        try:
            logger.info(f"🔘 Generating CTA button with Nano Banana: '{cta_text}'...")
            
            # Create the prompt for a beautiful CTA button with green screen background
            # Green screen color: #00FF00 (pure chroma key green) for easy removal
            prompt = f"""Create a beautiful, modern call-to-action button with the exact text "{cta_text}" prominently displayed.

CRITICAL REQUIREMENTS:
- The ENTIRE background MUST be a perfectly flat, solid, uniform bright green color (hex #00FF00)
- The green background must be completely smooth and uniform - NO gradients, NO textures, NO variations
- NO shadows anywhere in the image - the button must have NO drop shadow, NO glow, NO blur effects
- The green must extend to all edges of the image with zero variation

Button design:
- A vibrant gradient background on the button itself (blue to purple or orange - NO GREEN)
- Sharp, clean rounded corners
- The text "{cta_text}" clearly visible in white color
- NO shadow effects on the button or text
- NO glow effects
- Flat design style - clean and simple
- The button should be centered in the image

IMPORTANT: The background must be 100% flat solid green (#00FF00) with absolutely no shadows, gradients, or effects.
The text on the button must be exactly: {cta_text}"""

            # Create task with Nano Banana model
            kie_cfg = get_kie_config()
            nb_cfg = kie_cfg["nano_banana"]

            payload = {
                "model": nb_cfg["model"],
                "input": {
                    "prompt": prompt,
                    "resolution": resolution,
                    "output_format": nb_cfg["output_format"],
                    **kie_cfg["nano_banana_cta"],
                }
            }

            response = self._kie_create_task(payload, "nano_banana_cta_button", 60)
            response.raise_for_status()
            result = response.json()

            if result.get("code") != 200:
                logger.error(f"❌ Nano Banana API error: {result.get('message', 'Unknown error')}")
                return None

            task_id = result.get("data", {}).get("taskId")
            if not task_id:
                logger.error("❌ No task ID returned from Nano Banana")
                return None

            logger.info(f"📋 Nano Banana CTA task created: {task_id}")

            # Poll for completion
            query_url = f"{self.base_url}/api/v1/jobs/recordInfo"
            max_attempts = 60  # 5 minutes max
            
            for attempt in range(max_attempts):
                time.sleep(5)
                
                query_response = requests.get(
                    query_url,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    params={"taskId": task_id},
                    timeout=30
                )
                query_response.raise_for_status()
                status_result = query_response.json()
                
                if status_result.get("code") != 200:
                    continue
                
                data = status_result.get("data", {})
                state = data.get("state", "")
                
                if state == "success":
                    result_json = data.get("resultJson", "{}")
                    try:
                        result_data = json.loads(result_json)
                        result_urls = result_data.get("resultUrls", [])
                        if result_urls:
                            image_url = result_urls[0]
                            logger.info(f"✅ CTA button generated: {image_url}")
                            return image_url
                    except json.JSONDecodeError:
                        logger.error(f"❌ Failed to parse result JSON: {result_json}")
                    break
                    
                elif state == "fail":
                    fail_msg = data.get("failMsg", "Unknown error")
                    logger.error(f"❌ Nano Banana CTA generation failed: {fail_msg}")
                    break
                    
                elif state in ["waiting", "queuing", "generating"]:
                    if attempt % 6 == 0:  # Log every 30 seconds
                        logger.info(f"⏳ CTA generation status: {state}...")
                    continue
            
            logger.error("❌ CTA button generation timeout")
            return None
                
        except Exception as e:
            logger.error(f"❌ Error generating CTA button with Nano Banana: {e}")
            return None
    
    def generate_opening_text(self, text: str, resolution: str = "1K") -> Optional[str]:
        """Generate an opening text overlay image using Nano Banana AI model.
        
        Creates a stylish text overlay for the opening scene with green screen background.
        
        Args:
            text: Text to display in the opening.
            
        Returns:
            URL of the generated text image, or None if failed.
        """
        try:
            logger.info(f"🎬 Generating opening text with Nano Banana: '{text}'...")
            
            # Create the prompt for opening text with green screen background
            prompt = f"""Create a stylish, modern text overlay with the exact text "{text}" prominently displayed.

CRITICAL REQUIREMENTS:
- The ENTIRE background MUST be a perfectly flat, solid, uniform bright green color (hex #00FF00)
- The green background must be completely smooth and uniform - NO gradients, NO textures, NO variations
- NO shadows anywhere in the image - NO drop shadow, NO glow, NO blur effects
- The green must extend to all edges of the image with zero variation

Text design:
- Large, bold, eye-catching typography
- White or light colored text for visibility
- Modern sans-serif font style
- The text should be centered in the image
- Can have a subtle gradient or bold style on the text itself (NOT the background)
- NO shadow effects on the text
- NO glow effects
- Clean, professional look suitable for video opening

IMPORTANT: The background must be 100% flat solid green (#00FF00) with absolutely no shadows, gradients, or effects.
The text must be exactly: {text}"""

            # Create task with Nano Banana model
            kie_cfg = get_kie_config()
            nb_cfg = kie_cfg["nano_banana"]

            payload = {
                "model": nb_cfg["model"],
                "input": {
                    "prompt": prompt,
                    "resolution": resolution,
                    "output_format": nb_cfg["output_format"],
                    **kie_cfg["nano_banana_cta"],
                }
            }

            response = self._kie_create_task(payload, "nano_banana_opening_text", 60)
            response.raise_for_status()
            result = response.json()

            if result.get("code") != 200:
                logger.error(f"❌ Nano Banana API error: {result.get('message', 'Unknown error')}")
                return None

            task_id = result.get("data", {}).get("taskId")
            if not task_id:
                logger.error("❌ No task ID returned from Nano Banana")
                return None

            logger.info(f"📋 Nano Banana opening text task created: {task_id}")
            
            # Poll for completion
            query_url = f"{self.base_url}/api/v1/jobs/recordInfo"
            max_attempts = 60  # 5 minutes max
            
            for attempt in range(max_attempts):
                time.sleep(5)
                
                query_response = requests.get(
                    query_url,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    params={"taskId": task_id},
                    timeout=30
                )
                query_response.raise_for_status()
                status_result = query_response.json()
                
                if status_result.get("code") != 200:
                    continue
                
                data = status_result.get("data", {})
                state = data.get("state", "")
                
                if state == "success":
                    result_json = data.get("resultJson", "{}")
                    try:
                        result_data = json.loads(result_json)
                        result_urls = result_data.get("resultUrls", [])
                        if result_urls:
                            image_url = result_urls[0]
                            logger.info(f"✅ Opening text generated: {image_url}")
                            return image_url
                    except json.JSONDecodeError:
                        logger.error(f"❌ Failed to parse result JSON: {result_json}")
                    break
                    
                elif state == "fail":
                    fail_msg = data.get("failMsg", "Unknown error")
                    logger.error(f"❌ Nano Banana opening text generation failed: {fail_msg}")
                    break
                    
                elif state in ["waiting", "queuing", "generating"]:
                    if attempt % 6 == 0:  # Log every 30 seconds
                        logger.info(f"⏳ Opening text generation status: {state}...")
                    continue
            
            logger.error("❌ Opening text generation timeout")
            return None
                
        except Exception as e:
            logger.error(f"❌ Error generating opening text with Nano Banana: {e}")
            return None


# =============================================================================
# IMAGE PROCESSING UTILITIES (for CTA button)
# =============================================================================

def remove_green_background(image_path: str, output_path: str) -> bool:
    """Remove green background from an image and save with transparency.
    
    Samples the actual green color from the image corners for accurate removal.
    
    Args:
        image_path: Path to the input image.
        output_path: Path to save the output PNG with transparency.
        
    Returns:
        True if successful, False otherwise.
    """
    try:
        logger.info("🎨 Removing green background from CTA button...")
        
        # Open the image
        img = Image.open(image_path).convert("RGBA")
        pixels = img.load()
        
        width, height = img.size
        
        # Sample the green color from the corners (where background should be)
        corner_samples = []
        sample_size = 10  # Sample a small area from each corner
        
        # Top-left corner
        for y in range(min(sample_size, height)):
            for x in range(min(sample_size, width)):
                r, g, b, a = pixels[x, y]
                corner_samples.append((r, g, b))
        
        # Top-right corner
        for y in range(min(sample_size, height)):
            for x in range(max(0, width - sample_size), width):
                r, g, b, a = pixels[x, y]
                corner_samples.append((r, g, b))
        
        # Bottom-left corner
        for y in range(max(0, height - sample_size), height):
            for x in range(min(sample_size, width)):
                r, g, b, a = pixels[x, y]
                corner_samples.append((r, g, b))
        
        # Bottom-right corner
        for y in range(max(0, height - sample_size), height):
            for x in range(max(0, width - sample_size), width):
                r, g, b, a = pixels[x, y]
                corner_samples.append((r, g, b))
        
        # Calculate average green color from samples
        if corner_samples:
            avg_r = sum(s[0] for s in corner_samples) // len(corner_samples)
            avg_g = sum(s[1] for s in corner_samples) // len(corner_samples)
            avg_b = sum(s[2] for s in corner_samples) // len(corner_samples)
            sampled_green = (avg_r, avg_g, avg_b)
            logger.info(f"🎨 Sampled background color: RGB{sampled_green}")
        else:
            sampled_green = (46, 204, 113)  # Default fallback
            logger.info("🎨 Using default green color")
        
        # More strict tolerance to avoid removing the button itself
        # Only remove pixels that are clearly green (high G, low R and B relative to G)
        tolerance = 40
        
        for y in range(height):
            for x in range(width):
                r, g, b, a = pixels[x, y]
                
                # Check if pixel is close to the sampled green color
                # AND the pixel is predominantly green (g > r and g > b)
                is_green_bg = (
                    abs(r - sampled_green[0]) < tolerance and 
                    abs(g - sampled_green[1]) < tolerance and 
                    abs(b - sampled_green[2]) < tolerance and
                    g > r * 0.8 and  # Green channel should be significant
                    g > b * 0.8
                )
                
                if is_green_bg:
                    # Make pixel transparent
                    pixels[x, y] = (r, g, b, 0)
        
        # Save with transparency
        img.save(output_path, "PNG")
        logger.info(f"✅ Green background removed: {output_path}")
        return True
        
    except Exception as e:
        logger.error(f"❌ Error removing green background: {e}")
        return False


def add_glow_effect(image_path: str, output_path: str, glow_color: tuple = (255, 255, 255), glow_radius: int = 15) -> bool:
    """Add a glow effect around the non-transparent parts of an image.
    
    Args:
        image_path: Path to the input PNG with transparency.
        output_path: Path to save the output image with glow.
        glow_color: RGB tuple for the glow color (default: white).
        glow_radius: Blur radius for the glow effect.
        
    Returns:
        True if successful, False otherwise.
    """
    try:
        logger.info("✨ Adding glow effect to CTA button...")
        
        # Open the image with transparency
        img = Image.open(image_path).convert("RGBA")
        
        # Create a copy for the glow
        # Extract alpha channel
        alpha = img.split()[3]
        
        # Create a solid color image for glow
        glow_base = Image.new("RGBA", img.size, glow_color + (255,))
        
        # Apply alpha mask to glow base
        glow_base.putalpha(alpha)
        
        # Blur the glow
        glow_blurred = glow_base.filter(ImageFilter.GaussianBlur(radius=glow_radius))
        
        # Enhance the glow brightness
        enhancer = ImageEnhance.Brightness(glow_blurred)
        glow_blurred = enhancer.enhance(1.5)
        
        # Create final image with glow behind the button
        final_size = (img.width + glow_radius * 2, img.height + glow_radius * 2)
        final_img = Image.new("RGBA", final_size, (0, 0, 0, 0))
        
        # Paste glow (centered)
        final_img.paste(glow_blurred, (glow_radius, glow_radius), glow_blurred)
        
        # Paste original button on top
        final_img.paste(img, (glow_radius, glow_radius), img)
        
        # Save result
        final_img.save(output_path, "PNG")
        logger.info(f"✅ Glow effect added: {output_path}")
        return True
        
    except Exception as e:
        logger.error(f"❌ Error adding glow effect: {e}")
        return False


from tvd_pipeline.services.local_ffmpeg import LocalFFmpegFallback  # noqa: E402 — extracted
