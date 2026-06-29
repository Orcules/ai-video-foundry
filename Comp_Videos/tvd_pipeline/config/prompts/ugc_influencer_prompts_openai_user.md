Generate prompts for a {scene_count}-scene influencer recommendation video.{influencer_section}

PRODUCT/EXPERIENCE TO PROMOTE:
{free_text}

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

Generate {scene_count} scenes now: