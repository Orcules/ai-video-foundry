"""KieAIService — extracted verbatim from Comp_Videos/video_scene_processor.py.

Lines 9003-10338 of the monolith.
"""

import os
import re
import json
import time
import base64
import logging
import random
import requests
import urllib.parse
from PIL import Image, ImageFilter, ImageEnhance
from typing import Dict, Any, List, Optional, Tuple

from api_pipeline.services.base.config import config

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
        
        # Context detection patterns
        is_sale_offer = any(word in article_lower for word in [
            'sale', 'discount', 'off', '%', 'מבצע', 'הנחה', 'rabat', 'indirim', 'promo', 'offer', 'deal'
        ])
        is_job_offer = any(word in article_lower for word in [
            'job', 'career', 'work', 'hiring', 'employment', 'עבודה', 'משרה', 'קריירה', 'job opening'
        ])
        is_health_product = any(word in article_lower for word in [
            'health', 'wellness', 'supplement', 'vitamin', 'weight', 'slim', 'בריאות', 'דיאטה'
        ])
        is_learning = any(word in article_lower for word in [
            'learn', 'course', 'training', 'education', 'tutorial', 'קורס', 'לימוד'
        ])
        
        # Language-specific text translations
        text_translations = {
            "sale": {"en": "SALE", "he": "מבצע", "da": "TILBUD", "tr": "İNDİRİM", "de": "ANGEBOT", "fr": "PROMO", "es": "OFERTA", "it": "OFFERTA", "nl": "AANBIEDING", "pt": "PROMOÇÃO", "ru": "СКИДКА", "ar": "عرض", "ja": "セール", "ko": "할인", "zh": "促销"},
            "apply": {"en": "APPLY", "he": "הגש מועמדות", "da": "ANSØG", "tr": "BAŞVUR", "de": "BEWERBEN", "fr": "POSTULER", "es": "APLICAR", "it": "CANDIDATI", "nl": "SOLLICITEER", "pt": "APLICAR", "ru": "ПОДАТЬ", "ar": "تقدم", "ja": "応募", "ko": "지원", "zh": "申请"},
            "try": {"en": "TRY NOW", "he": "נסה עכשיו", "da": "PRØV NU", "tr": "ŞİMDİ DENE", "de": "JETZT TESTEN", "fr": "ESSAYER", "es": "PROBAR", "it": "PROVA ORA", "nl": "PROBEER NU", "pt": "EXPERIMENTE", "ru": "ПОПРОБУЙ", "ar": "جرب الآن", "ja": "今すぐ試す", "ko": "지금 시도", "zh": "立即尝试"},
            "learn": {"en": "LEARN MORE", "he": "למד עוד", "da": "LÆR MERE", "tr": "DAHA FAZLA", "de": "MEHR ERFAHREN", "fr": "EN SAVOIR PLUS", "es": "SABER MÁS", "it": "SCOPRI DI PIÙ", "nl": "MEER INFO", "pt": "SAIBA MAIS", "ru": "УЗНАТЬ БОЛЬШЕ", "ar": "اعرف المزيد", "ja": "詳しく見る", "ko": "더 알아보기", "zh": "了解更多"},
        }
        
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
        aspect_ratio: str = "9:16"
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
            logger.info(f"🍌 Generating image with Nano Banana Pro ({len(prompt)} chars){ref_info}...")
            
            url = f"{self.base_url}/api/v1/jobs/createTask"
            
            input_params = {
                "prompt": prompt,
                "aspect_ratio": aspect_ratio,
                "resolution": "1K",
                "output_format": "png"
            }
            
            # Add reference image if provided (re-upload to GCS if needed to avoid 403)
            if reference_image_url:
                fetchable = self._ensure_ref_urls_fetchable([reference_image_url], prefix="single")
                input_params["image_input"] = fetchable if fetchable else [reference_image_url]
                logger.info(f"🎯 Using reference image: {(fetchable[0] if fetchable else reference_image_url)[:60]}...")
            
            payload = {
                "model": "nano-banana-pro",
                "input": input_params
            }
            
            response = requests.post(url, headers=self.headers, json=payload, timeout=60)
            response.raise_for_status()
            
            result = response.json()
            
            if result.get("code") == 200 and "taskId" in result.get("data", {}):
                task_id = result["data"]["taskId"]
                logger.info(f"✅ Nano Banana Pro task created: {task_id}")
                return self._wait_for_image_task(task_id)
            else:
                logger.error(f"❌ Nano Banana Pro API error: {result}")
                return None
                
        except Exception as e:
            logger.error(f"❌ Error generating image with Nano Banana Pro: {e}")
            return None
    
    def generate_clean_product_image(
        self, 
        reference_image_urls: List[str],
        product_description: str
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
            
            url = f"{self.base_url}/api/v1/jobs/createTask"
            
            input_params = {
                "prompt": prompt,
                "image_input": ref_urls,
                "aspect_ratio": "1:1",
                "resolution": "1K",
                "output_format": "png"
            }
            
            logger.info(f"🎯 Using Nano Banana Pro with {len(ref_urls)} reference images")
            for i, ref_url in enumerate(ref_urls):
                logger.info(f"   Reference {i+1}: {ref_url[:60]}...")
            
            payload = {
                "model": "nano-banana-pro",
                "input": input_params
            }
            
            response = requests.post(url, headers=self.headers, json=payload, timeout=60)
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
        image_edit_mode: bool = False,
        scene_context_for_edit: Optional[str] = None,
        resolution: str = "1K",
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
            reference_image_urls: Extra reference URLs (e.g. UGC Real); merged into ref list, deduped.
            prepend_reference_urls: Sent first in image_input (e.g. blank 3×3 layout template for UGC Real).
            is_cta_scene: If True, this is the last/CTA scene (one short phrase allowed). If False, body scene — NO on-screen text.
            image_edit_mode: Studio re-render mode (uses scene_context_for_edit).
            scene_context_for_edit: Full scene context when image_edit_mode is True.
            resolution: Kie output resolution (e.g. 1K, 2K).
            **kwargs: Ignored extras / duplicate reference_image_urls (parity with monolith KieAIService).
            
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
            style_prompts = {
                "Modern flat 2d": (
                    "MANDATORY STYLE: Modern flat 2D illustration. Clean vector graphics, bold solid colors, minimal shadows, geometric shapes, digital art aesthetic. Every element must look like a flat 2D vector illustration, NOT a photograph. ",
                    "\n\nSTYLE REMINDER: This MUST be a flat 2D vector illustration – no photorealism, no 3D rendering, no photographs."
                ),
                "Minimal line art": (
                    "MANDATORY STYLE: Minimal line art illustration. Elegant single-weight continuous lines, monochromatic or very limited color palette (1-3 colors max), clean white or light background, hand-drawn artistic look, simplistic beauty. ",
                    "\n\nSTYLE REMINDER: This MUST be minimal line art – thin elegant lines, mostly white space, very few colors. NOT a photograph."
                ),
                "Futuristic isometric Tech Glow": (
                    "MANDATORY STYLE: Futuristic isometric 3D with tech glow effects. Neon accents on dark background, cyberpunk aesthetic, glowing edges, holographic elements, isometric camera angle. ",
                    "\n\nSTYLE REMINDER: This MUST have isometric perspective, dark background, neon glow effects, and futuristic tech aesthetic."
                ),
                "Modern semi flat 2d": (
                    "MANDATORY STYLE: Modern semi-flat 2D illustration. Soft gradients, subtle shadows, contemporary illustration, clean design, vibrant yet harmonious colors. ",
                    "\n\nSTYLE REMINDER: This MUST be a semi-flat 2D illustration with soft gradients – not photorealistic."
                ),
                "Cinematic photography": (
                    "MANDATORY STYLE: Cinematic photography. Dramatic lighting, shallow depth of field, professional color grading, film-like quality, anamorphic lens, high production value. ",
                    "\n\nSTYLE REMINDER: This MUST look like a still from a high-budget film – dramatic lighting, shallow DoF, cinematic color grading."
                ),
                "Soft 3d clay": (
                    "MANDATORY STYLE: Soft 3D clay render (claymation). Smooth rounded shapes, matte clay materials, pastel colors, Pixar-like aesthetic, charming and friendly, studio lighting. ",
                    "\n\nSTYLE REMINDER: This MUST look like a 3D clay/plasticine render – round soft shapes, matte pastel materials."
                ),
                "isometric soft vector": (
                    "MANDATORY STYLE: Isometric soft vector illustration. Clean geometric isometric perspective, pastel colors, minimal shadows, infographic-style, modern digital art. ",
                    "\n\nSTYLE REMINDER: This MUST be an isometric vector illustration – strict isometric angle, clean shapes, pastels."
                ),
                "Paper Cut": (
                    "MANDATORY STYLE: Paper cut art (paper craft). Layered cut paper effect, subtle shadows between layers, colorful craft paper textures, whimsical handmade aesthetic. ",
                    "\n\nSTYLE REMINDER: This MUST look like paper cut art – visible paper layers with shadows, craft textures."
                )
            }
            style_data = style_prompts.get(visual_style)
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
        
        # Build the enhanced prompt (align with tvd_pipeline.services.kie.KieAIService)
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
        
        # Collect all reference image URLs (Nano Banana Pro supports up to 8). Character first when not
        # in edit mode so identity refs are not truncated when many product refs are present.
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
            url = f"{self.base_url}/api/v1/jobs/createTask"
            
            input_params = {
                "prompt": enhanced_prompt,
                "aspect_ratio": "9:16",
                "resolution": resolution or "1K",
                "output_format": "png"
            }
            
            if ref_urls:
                input_params["image_input"] = ref_urls[:8]  # Max 8 images
                logger.info(f"🎯 Using Nano Banana Pro with {len(ref_urls)} reference images")
            
            payload = {
                "model": "nano-banana-pro",
                "input": input_params
            }
            
            response = requests.post(url, headers=self.headers, json=payload, timeout=60)
            response.raise_for_status()
            
            result = response.json()
            
            if result.get("code") == 200 and "taskId" in result.get("data", {}):
                task_id = result["data"]["taskId"]
                logger.info(f"✅ Scene image task created: {task_id}")
                out = self._wait_for_image_task(task_id)
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

            url = "https://api.kie.ai/gemini-3-flash/v1/chat/completions"
            payload = {
                "messages": [
                    {"role": "user", "content": content_parts}
                ],
                "stream": False,
                "include_thoughts": False,
                "reasoning_effort": "low"
            }

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
                logger.info(f"✅ Kie Flash image generated: {image_url[:80]}...")
                return image_url
            else:
                logger.error(f"❌ Kie Flash: Could not extract image URL from response")
                logger.debug(f"   Response content: {str(msg_content)[:500]}")
                return None

        except Exception as e:
            logger.error(f"❌ Error generating scene image with Kie Flash: {e}")
            return None

    def _wait_for_image_task(self, task_id: str, timeout: int = 900) -> Optional[str]:
        """Wait for image generation task to complete.
        
        Args:
            task_id: Task ID to poll.
            timeout: Maximum wait time in seconds (default 900 = 15 min; some images take 10+ min).
            
        Returns:
            URL of the generated image, or None if failed/timeout.
        """
        start_time = time.time()
        check_interval = 5  # Poll every 5s
        
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
                        if result_json_str:
                            result_json = json.loads(result_json_str)
                            result_urls = result_json.get("resultUrls", [])
                            if result_urls:
                                logger.info(f"✅ Image generated successfully")
                                return result_urls[0]
                    
                    elif state == "fail":
                        fail_msg = data.get("failMsg") or data.get("fail_msg") or data.get("message") or "Unknown error"
                        self.last_failure_reason = str(fail_msg)[:300]
                        logger.error(f"❌ Image generation failed: {fail_msg}")
                        return None
                    
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
            # Runway supports 5 or 10 second durations
            # Request 10s for any scene > 5s so slow-motion can cover longer scenes.
            # A 10s clip with 2x slow-motion covers up to 20s scenes.
            # A 5s clip only covers up to 10s with slow-motion.
            runway_duration = 5 if duration <= 5.0 else 10
            logger.info(f"🎬 Generating video with Runway (duration: {runway_duration}s, original scene: {duration:.1f}s)...")
            
            url = f"{self.base_url}/api/v1/runway/generate"
            
            payload = {
                "prompt": prompt,
                "imageUrl": image_url,
                "duration": runway_duration,
                "quality": "720p",
                "waterMark": ""
            }
            
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
        check_interval = 10
        
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
            
            time.sleep(check_interval)
        
        logger.error("❌ Video generation timeout")
        return None
    
    def generate_video_kling(
        self,
        prompt: str,
        image_url: str,
        duration: float = 5.0,
        negative_prompt: str = "blur, distort, and low quality",
        cfg_scale: float = 0.5
    ) -> Optional[str]:
        """Generate a video using Kling V2.5 Turbo Image-to-Video Pro.
        
        Args:
            prompt: Motion/animation prompt.
            image_url: URL of the source image.
            duration: Video duration in seconds (5 or 10).
            negative_prompt: What to avoid in the video.
            cfg_scale: Config scale (0.0-1.0, default 0.5).
            
        Returns:
            URL of the generated video, or None if failed.
        """
        try:
            # Kling supports 5 or 10 second durations
            # Request based on target scene duration + buffer for slow motion
            # Request 10s for any scene > 5s so slow-motion (max 2x) can cover it.
            # A 10s clip with 2x slow-motion can cover up to 20s scenes.
            kling_duration = "5" if duration <= 5.0 else "10"
            logger.info(f"🎬 Generating video with Kling V2.5 (duration: {kling_duration}s, original scene: {duration:.1f}s)...")
            
            url = f"{self.base_url}/api/v1/jobs/createTask"
            
            payload = {
                "model": "kling/v2-5-turbo-image-to-video-pro",
                "input": {
                    "prompt": prompt,
                    "image_url": image_url,
                    "duration": kling_duration,
                    "negative_prompt": negative_prompt,
                    "cfg_scale": cfg_scale
                }
            }
            
            response = requests.post(url, headers=self.headers, json=payload, timeout=60)
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
        check_interval = 10
        
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
                        except json.JSONDecodeError:
                            logger.error(f"❌ Failed to parse Kling resultJson: {result_json_str}")
                            return None
                    
                    elif state == "fail":
                        fail_msg = data.get("failMsg", "Unknown error")
                        logger.error(f"❌ Kling video generation failed: {fail_msg}")
                        return None
                    
                    # States: waiting, queuing, generating - continue polling
                    
            except Exception as e:
                logger.error(f"❌ Error polling Kling task status: {e}")
            
            time.sleep(check_interval)
        
        logger.error("❌ Kling video generation timeout")
        return None
    
    def generate_cta_button(self, cta_text: str) -> Optional[str]:
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
            url = f"{self.base_url}/api/v1/jobs/createTask"
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            
            payload = {
                "model": "nano-banana-pro",
                "input": {
                    "prompt": prompt,
                    "aspect_ratio": "16:9",
                    "resolution": "1K",
                    "output_format": "png"
                }
            }
            
            response = requests.post(url, headers=headers, json=payload, timeout=60)
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
    
    def generate_opening_text(self, text: str) -> Optional[str]:
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
            url = f"{self.base_url}/api/v1/jobs/createTask"
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            
            payload = {
                "model": "nano-banana-pro",
                "input": {
                    "prompt": prompt,
                    "aspect_ratio": "16:9",
                    "resolution": "1K",
                    "output_format": "png"
                }
            }
            
            response = requests.post(url, headers=headers, json=payload, timeout=60)
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
