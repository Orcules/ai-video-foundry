Analyze these {frame_count} frames from a video and create a DETAILED VISUAL STYLE GUIDE.

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
}}