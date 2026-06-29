"""Prompt enhancement tasks -- enrich image/motion prompts with product and style context.

Free functions extracted from ``OpenAIService``.  Functions that need an LLM
take ``call_fn`` as their first parameter.  Pure string-building helpers take
no provider argument.
"""

import json
import logging
from typing import Any, Callable, Dict, List, Optional

from tvd_pipeline.prompt_loader import get_prompt_loader

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LLM-based prompt enhancement
# ---------------------------------------------------------------------------

def enhance_prompt_with_product(
    call_fn: Callable,
    original_prompt: str,
    product_description: str,
    article_text: str = "",
    product_info: Optional[Dict[str, Any]] = None,
    scene_context: Optional[str] = None,
    video_style: Optional[Dict[str, Any]] = None,
) -> str:
    """Enhance an image generation prompt with product details, usage context, and video style.

    Args:
        call_fn: ``call_fn(messages, **kwargs) -> Dict[str, Any]``.
        original_prompt: The original image prompt from scene analysis.
        product_description: Detailed product description from detection.
        article_text: Optional new article content to adapt the scene to.
        product_info: Full product detection result with usage contexts.
        scene_context: Specific context for this scene (e.g. ``"being_applied"``).
        video_style: Visual style analysis from ``analyze_video_style()``.

    Returns:
        Enhanced prompt string with product emphasis, context, and style matching.
    """
    try:
        logger.info("[PRODUCT] Enhancing prompt with product details and context...")

        # Extract additional context from product_info
        product_purpose = ""
        product_usage_method = ""
        usage_contexts: List[Dict] = []
        scene_plan_info = None

        if product_info:
            product_purpose = product_info.get("product_purpose", "")
            product_usage_method = product_info.get("product_usage_method", "")
            usage_contexts = product_info.get("usage_contexts", [])
            scene_plan_info = product_info.get("scene_plan")

        # Extract story context info if available (with type safety)
        story_context_info = product_info.get("story_context", {}) if product_info else {}
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

        # System prompt for enhancement
        system_prompt = get_prompt_loader().get("shared_visual_director_system")

        # Determine product type for specific logic
        product_type = product_info.get("product_detected", "unknown").lower() if product_info else "unknown"
        is_patch = any(word in product_type for word in ["patch", "sticker", "adhesive", "bandage"])
        is_cream = any(word in product_type for word in ["cream", "lotion", "gel", "serum", "ointment"])
        is_pet_product = (
            any(word in product_type for word in ["dog", "cat", "pet", "toy"])
            or (product_purpose and any(word in product_purpose.lower() for word in ["dog", "cat", "pet"]))
        )

        # Build specific warning based on product type
        product_specific_warning = ""
        if is_patch:
            product_specific_warning = (
                "\n\u26a0\ufe0f\u26a0\ufe0f\u26a0\ufe0f THIS IS AN ADHESIVE PATCH - CRITICAL RULES \u26a0\ufe0f\u26a0\ufe0f\u26a0\ufe0f\n"
                "This product STICKS TO BARE SKIN. It CANNOT stick to fabric/clothing.\n\n"
                "YOU MUST SHOW:\n"
                "- BARE SKIN visible (stomach, arm, thigh, back)\n"
                "- Patch applied DIRECTLY to skin surface\n"
                "- If showing application: person lifts clothing to expose bare skin\n\n"
                "YOU MUST NOT SHOW:\n"
                "- Patch on top of shirt/clothing\n"
                "- Patch on fabric of any kind\n"
                "- Patch floating or not adhered to anything\n"
            )
        elif is_cream:
            product_specific_warning = (
                "\n\u26a0\ufe0f THIS IS A CREAM/GEL - IT GOES ON BARE SKIN\n"
                "Show application to visible bare skin (face, arms, body).\n"
                "Do NOT show cream on clothing.\n"
            )
        elif is_pet_product:
            product_specific_warning = (
                "\n\u26a0\ufe0f THIS IS A PET PRODUCT - THE PET MUST BE VISIBLE\n"
                "Show a real dog/cat actively interacting with the product.\n"
                "Do NOT show the product alone without the pet.\n"
            )

        # Build story context instruction
        story_instruction = ""
        if story_context_info:
            story_instruction = (
                f"\n\U0001f3ac VIDEO STORY CONTEXT:\n"
                f"Story Type: {story_type if story_type else 'commercial/advertisement'}\n"
                f"Story Summary: {story_summary if story_summary else 'Product advertisement'}\n\n"
            )
            if scene_subject_appearance:
                story_instruction += (
                    "\u26a0\ufe0f\u26a0\ufe0f\u26a0\ufe0f CRITICAL - SUBJECT APPEARANCE FOR THIS SCENE \u26a0\ufe0f\u26a0\ufe0f\u26a0\ufe0f\n"
                    f"In THIS specific scene, the subject(s) MUST appear as:\n"
                    f"{scene_subject_appearance}\n\n"
                    "This is EXACTLY how they should look - follow this description precisely!\n"
                )
            elif scene_physical_state:
                story_instruction += (
                    f"\u26a0\ufe0f SUBJECT STATE IN THIS SCENE:\n"
                    f"Physical state: {scene_physical_state}\n"
                    f"Action: {scene_action if scene_action else 'As shown in original'}\n"
                )

            if scene_purpose:
                story_instruction += (
                    f"\n\U0001f4cd SCENE PURPOSE: {scene_purpose}\n"
                    f"Emotional beat: {scene_emotional_beat if scene_emotional_beat else 'Match the original mood'}\n"
                )

            if has_visible_change and (start_state or end_state):
                story_instruction += (
                    f"\n\U0001f4ca NOTE - Subject changes throughout video:\n"
                    f"- Start of video: {start_state}\n"
                    f"- End of video: {end_state}\n"
                    "Make sure this scene matches the CORRECT state for its position in the story!\n"
                )

            if must_preserve:
                story_instruction += (
                    f"\n\U0001f512 MUST PRESERVE in this scene: {', '.join(must_preserve[:3])}\n"
                )

        # Build the user prompt with STORY CONTEXT and LOGIC CHECK
        user_prompt = get_prompt_loader().get(
            "shared_scene_recreation_user",
            story_instruction=story_instruction,
            product_specific_warning=product_specific_warning,
            product_type=product_type,
            product_description=product_description,
            product_purpose=product_purpose if product_purpose else "Commercial product",
            product_usage_method=product_usage_method if product_usage_method else "Standard usage",
            original_prompt=original_prompt,
            scene_context=scene_context,
        )

        # Add specific context instructions WITH STRICT LOGIC
        if scene_context == "being_applied":
            if is_patch:
                user_prompt += (
                    "\n**APPLICATION SCENE FOR PATCH:**\n"
                    "\U0001fa79 REQUIRED: Show patch being applied to BARE SKIN\n"
                    "- Person lifts shirt -> bare stomach visible -> patch placed on bare stomach\n"
                    "- OR bare arm/shoulder visible -> patch on bare arm\n"
                    "- The SKIN must be VISIBLE where the patch is placed\n"
                    "- \u274c NEVER show patch on clothing/shirt/fabric\n"
                )
            elif is_pet_product:
                user_prompt += (
                    "\n**APPLICATION SCENE FOR PET PRODUCT:**\n"
                    "\U0001f415 REQUIRED: Show pet actively interacting with the product\n"
                    "- Dog/cat must be visible in the scene\n"
                    "- Pet is playing with, chewing, or using the product\n"
                    "- \u274c NEVER show product alone without pet\n"
                )
            else:
                user_prompt += (
                    "\n**APPLICATION SCENE:**\n"
                    "- Show product being used for its actual purpose\n"
                    "- Realistic hand/body positioning\n"
                    "- Logical, believable action\n"
                )
        elif scene_context == "in_hand":
            user_prompt += (
                "\n**IN-HAND SCENE:**\n"
                "- Natural hand grip, product clearly visible\n"
                "- Correct scale relative to hand\n"
            )
        elif scene_context == "static_display":
            user_prompt += (
                "\n**STATIC DISPLAY:**\n"
                "- Product prominently displayed\n"
                "- Clear, detailed view\n"
            )
        elif scene_context == "close_up":
            user_prompt += (
                "\n**CLOSE-UP:**\n"
                "- Detailed view of product features\n"
                "- Match original product exactly\n"
            )
        elif scene_context == "lifestyle":
            if is_patch:
                user_prompt += (
                    "\n**LIFESTYLE SCENE FOR PATCH:**\n"
                    "\U0001fa79 If patch is visible, it MUST be on BARE SKIN\n"
                    "- Person going about daily life with patch on bare stomach/arm\n"
                    "- Skin must be exposed where patch is shown\n"
                )
            elif is_pet_product:
                user_prompt += (
                    "\n**LIFESTYLE SCENE FOR PET PRODUCT:**\n"
                    "\U0001f415 Pet must be visible and happy with the product\n"
                )
            else:
                user_prompt += (
                    "\n**LIFESTYLE SCENE:**\n"
                    "- Product in natural, everyday context\n"
                    "- Realistic usage scenario\n"
                )

        # Add scene plan information if available
        if scene_plan_info:
            user_prompt += (
                f"\n**SCENE NARRATIVE ROLE:** {scene_plan_info.get('narrative_role', 'general')}\n"
                f"**KEY MESSAGE FOR THIS SCENE:** {scene_plan_info.get('key_message', 'Show the product')}\n"
                f"**VISUAL SUGGESTION:** {scene_plan_info.get('visual_suggestion', '')}\n"
            )

        # Add video style matching instructions if available
        if video_style and not video_style.get("error"):
            style_prefix = video_style.get("style_prompt_prefix", "")
            style_suffix = video_style.get("style_prompt_suffix", "")
            color_info = video_style.get("color_palette", {})
            lighting_info = video_style.get("lighting", {})
            composition_info = video_style.get("composition", {})
            mood_info = video_style.get("mood_atmosphere", {})

            user_prompt += (
                f"\n\n\U0001f3a8 **CRITICAL: MATCH ORIGINAL VIDEO STYLE** \U0001f3a8\n"
                "The generated image MUST match the visual style of the original video:\n\n"
                "**COLOR STYLE:**\n"
                f"- Temperature: {color_info.get('color_temperature', 'neutral')}\n"
                f"- Saturation: {color_info.get('saturation', 'medium')}\n"
                f"- Contrast: {color_info.get('contrast', 'medium')}\n"
                f"- {color_info.get('color_description', '')}\n\n"
                "**LIGHTING:**\n"
                f"- Type: {lighting_info.get('type', 'natural')}\n"
                f"- Direction: {lighting_info.get('direction', 'front')}\n"
                f"- Intensity: {lighting_info.get('intensity', 'medium')}\n"
                f"- {lighting_info.get('lighting_description', '')}\n\n"
                "**COMPOSITION:**\n"
                f"- Framing: {composition_info.get('primary_framing', 'medium')}\n"
                f"- Subject placement: {composition_info.get('subject_placement', 'centered')}\n"
                f"- Depth of field: {composition_info.get('depth_of_field', 'medium')}\n"
                f"- {composition_info.get('composition_description', '')}\n\n"
                "**MOOD:**\n"
                f"- Overall: {mood_info.get('overall_mood', 'professional')}\n"
                f"- Energy: {mood_info.get('energy_level', 'medium')}\n\n"
                f"**USE THIS STYLE PREFIX:** {style_prefix}\n"
                f"**USE THIS STYLE SUFFIX:** {style_suffix}\n\n"
                "INCORPORATE these style elements into your enhanced prompt!\n"
            )

        if article_text:
            article_summary = article_text[:800] + "..." if len(article_text) > 800 else article_text
            user_prompt += f"\n**NEW CONTEXT/ARTICLE TO ADAPT TO:**\n{article_summary}\n"

        user_prompt += "\n" + get_prompt_loader().get(
            "shared_enhance_final_instructions",
            scene_context=scene_context,
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        result = call_fn(messages, temperature=0.3)
        content = result.get("text", "")
        if not content:
            logger.warning("[PRODUCT] Enhancement returned empty, using original prompt")
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

        # Wrap with emphasis for image generator
        final_prompt = (
            f"[CRITICAL - PRODUCT SIZE AND APPEARANCE MUST BE EXACT]\n"
            f"PRODUCT VISUAL: {product_description[:350]}\n"
            f"{size_info}\n"
            f"SCENE TYPE: {scene_context}\n"
            f"{usage_action if scene_context == 'being_applied' else ''}\n\n"
            f"{enhanced_prompt}\n\n"
            f"[IMPORTANT: Product must be shown at CORRECT PROPORTIONAL SIZE relative to hands/body. "
            f"If being applied, show the EXACT application action described above.]"
        )

        # Truncate if needed (Nano Banana limit is 4000 chars)
        if len(final_prompt) > 4000:
            final_prompt = final_prompt[:3997] + "..."

        logger.info(f"[PRODUCT] Prompt enhanced ({len(final_prompt)} chars) - context: {scene_context}")
        return final_prompt

    except Exception as e:
        logger.error(f"[PRODUCT] Enhancement error: {e}")
        return f"[Product: {product_description[:200]}] {original_prompt}"


# ---------------------------------------------------------------------------
# Pure string-building (no LLM call)
# ---------------------------------------------------------------------------

def enhance_motion_prompt_with_product(
    product_info: Dict[str, Any],
    original_motion_prompt: str,
    scene_context: Optional[str] = None,
    video_style: Optional[Dict[str, Any]] = None,
) -> str:
    """Enhance motion/animation prompt to accurately show product usage and match video style.

    This is a pure string-building function -- no LLM call is made.

    Args:
        product_info: Product detection results with usage details.
        original_motion_prompt: The original motion prompt.
        scene_context: How the product appears in this scene.
        video_style: Visual style analysis from ``analyze_video_style()``.

    Returns:
        Enhanced motion prompt with accurate product usage and style matching.
    """
    if not product_info or not product_info.get("has_product"):
        return original_motion_prompt

    try:
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
            is_patch_product = any(w in product_type.lower() for w in ["patch", "sticker", "adhesive"])
            is_cream_product = any(w in product_type.lower() for w in ["cream", "lotion", "gel"])
            is_pet_toy = (
                "toy" in product_type.lower()
                and (
                    "dog" in product_type.lower()
                    or "pet" in product_type.lower()
                    or (usage_method and any(pet in usage_method.lower() for pet in ["dog", "cat", "pet"]))
                )
            )

            if is_patch_product:
                motion_instruction = (
                    f"MOTION: Patch application to BARE SKIN\n\n"
                    "\u26a0\ufe0f\u26a0\ufe0f\u26a0\ufe0f ABSOLUTE RULE: PATCH GOES ON BARE SKIN, NOT CLOTHING \u26a0\ufe0f\u26a0\ufe0f\u26a0\ufe0f\n\n"
                    "REQUIRED SEQUENCE:\n"
                    f"1. Hands holding {product_type} ({size_info})\n"
                    "2. Person LIFTS SHIRT to expose BARE STOMACH (or bare arm/thigh visible)\n"
                    "3. Hands move patch toward BARE SKIN surface\n"
                    "4. Press patch onto BARE SKIN with gentle pressure\n"
                    "5. Smooth edges onto BARE SKIN\n"
                    "6. Patch is now ADHERED TO SKIN, not floating\n\n"
                    "\u274c FORBIDDEN: Patch touching any fabric/clothing\n"
                    "\u2705 REQUIRED: Visible bare skin where patch is applied\n\n"
                    "Camera: Close-up showing bare skin clearly"
                )
            elif is_cream_product:
                motion_instruction = (
                    f"MOTION: Cream application to BARE SKIN\n"
                    "1. Dispense product onto fingertips\n"
                    "2. Apply to BARE SKIN (face/arms/body - skin must be visible)\n"
                    "3. Gentle massage motion\n"
                    "Camera: Focus on bare skin and application"
                )
            elif is_pet_toy:
                motion_instruction = (
                    f"MOTION: Dog/Pet playing with toy\n\n"
                    "\u26a0\ufe0f REQUIRED: A DOG/PET MUST BE VISIBLE AND INTERACTING \u26a0\ufe0f\n\n"
                    "SEQUENCE:\n"
                    f"1. Dog sees the {product_type} ({size_info})\n"
                    "2. Dog excitedly approaches/grabs the toy\n"
                    "3. Dog plays - tugging, chewing, shaking\n"
                    "4. Joyful pet interaction throughout\n"
                    "5. Toy and dog move together dynamically\n\n"
                    "\u274c FORBIDDEN: Toy alone without pet\n"
                    "\u2705 REQUIRED: Happy dog actively playing with toy\n\n"
                    "Camera: Follow dog and toy interaction"
                )
            else:
                motion_instruction = (
                    f"MOTION: Product in realistic use\n"
                    f"1. Product ({product_type}) held/used naturally\n"
                    "2. Show actual intended purpose\n"
                    "3. Logical, believable movement\n"
                    f"USAGE: {usage_method[:150] if usage_method else 'Standard usage'}"
                )

        elif scene_context == "in_hand":
            motion_instruction = (
                f"MOTION: Product showcase in hand:\n"
                f"1. Hand holding {product_type} ({size_info}) - product fills frame appropriately\n"
                "2. Slight rotation or movement to show product details\n"
                "3. Stable, professional presentation\n"
                "Camera: Focus on product, slight movement for dynamism"
            )

        elif scene_context == "static_display":
            motion_instruction = (
                f"MOTION: Static product beauty shot:\n"
                f"1. {product_type} ({size_info}) displayed prominently\n"
                "2. Subtle camera movement (slow zoom or pan)\n"
                "3. Product remains centered and sharp\n"
                "Camera: Smooth, cinematic movement around product"
            )

        elif scene_context == "lifestyle":
            motion_instruction = (
                f"MOTION: Lifestyle scene with product:\n"
                "1. Natural environment movement\n"
                f"2. {product_type} visible and in-scale with surroundings\n"
                "3. Organic camera movement\n"
                f"USAGE: {usage_method[:100] if usage_method else 'Product in natural context'}"
            )

        else:
            motion_instruction = (
                f"MOTION: Show {product_type} ({size_info}):\n"
                "- Product clearly visible and correctly sized\n"
                "- Smooth, professional camera movement\n"
                f"{f'USAGE: {usage_method[:100]}' if usage_method else ''}"
            )

        # Add video style matching if available
        style_motion_guide = ""
        if video_style and not video_style.get("error"):
            camera_style = video_style.get("camera_style", {})
            mood = video_style.get("mood_atmosphere", {})
            motion_guide = video_style.get("motion_style_guide", "")

            style_motion_guide = (
                f"\nCAMERA STYLE TO MATCH:\n"
                f"- Movement: {camera_style.get('movement_tendency', 'subtle')}\n"
                f"- Typical angles: {', '.join(camera_style.get('typical_angles', ['eye-level']))}\n"
                f"- Energy: {mood.get('energy_level', 'medium')}\n"
                f"- {motion_guide if motion_guide else ''}\n"
            )

        # Combine with original prompt
        enhanced_motion = (
            f"{motion_instruction}\n\n"
            f"ORIGINAL SCENE: {original_motion_prompt}\n"
            f"{style_motion_guide}\n"
            f"[CRITICAL: Product must be CORRECT SIZE relative to hands/body. "
            f"{product_type} is {size_info}]"
        )

        # Truncate if too long
        if len(enhanced_motion) > 2500:
            enhanced_motion = enhanced_motion[:2497] + "..."

        logger.info(f"[PRODUCT] Motion prompt enhanced for {scene_context}")
        return enhanced_motion

    except Exception as e:
        logger.error(f"[PRODUCT] Motion enhancement error: {e}")
        return original_motion_prompt
