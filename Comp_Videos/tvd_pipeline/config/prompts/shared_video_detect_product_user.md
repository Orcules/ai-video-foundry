Analyze these {frame_count} video frames SEQUENTIALLY to understand the COMPLETE VIDEO.
{audio_context}

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

Return JSON:
{{
    "has_product": true/false,
    "product_detected": "patch/cream/device/supplement/toy/accessory/etc",

    "video_narrative": {{
        "video_type": "product_demo/testimonial/lifestyle/before_after/tutorial/ugc",
        "opening_hook": "What happens in the first 2-3 seconds to grab attention",
        "main_story": "The core narrative/message of the video",
        "climax": "The key moment - product reveal, transformation, or benefit demonstration",
        "closing": "How the video ends - CTA, final message",
        "emotional_journey": "The emotional arc: curiosity to problem to hope to solution to action",
        "pacing": "fast/medium/slow",
        "style": "professional/ugc/influencer/cinematic/casual"
    }},

    "sequential_breakdown": [
        {{
            "segment": "opening/build_up/core/resolution/closing",
            "frame_range": [0, 10],
            "timestamp_range": "0:00-0:05",
            "what_happens": "Detailed description of what happens in this segment",
            "product_visibility": "none/glimpse/partial/full/close_up",
            "audio_content": "What is being said during this segment (from transcript)",
            "key_visuals": ["visual element 1", "visual element 2"],
            "purpose": "hook/problem/solution/demo/benefit/cta"
        }}
    ],

    "audio_visual_sync": [
        {{
            "vo_text": "The exact text being spoken",
            "frame_range": [15, 25],
            "visual_description": "What is shown while this is said",
            "sync_quality": "perfect/good/loose",
            "key_message": "The main point being communicated"
        }}
    ],

    "product_description": "EXTREMELY DETAILED 300+ word VISUAL description for AI image generation. Include exact shape, dimensions, colors (with specific shades), materials, textures, branding, and unique features.",

    "product_purpose": "DETAILED explanation of what the product does, benefits, target audience, and problem it solves.",

    "product_usage_method": "STEP-BY-STEP usage instructions with body positioning and actions.",

    "product_details": {{
        "type": "specific product type",
        "brand": "brand name if visible, or unbranded",
        "shape": "exact shape description",
        "dimensions": "approximate dimensions",
        "colors": {{
            "primary": "main color with exact shade",
            "secondary": "secondary color",
            "accent": "accent colors",
            "packaging_colors": ["packaging colors"]
        }},
        "materials": ["material descriptions"],
        "textures": ["texture descriptions"],
        "packaging": "detailed packaging description",
        "branding_elements": ["logo", "text", "patterns"],
        "distinctive_features": ["unique features"]
    }},

    "usage_contexts": [
        {{
            "context_type": "static_display/being_applied/in_hand/close_up/lifestyle/before_after",
            "description": "How product appears in this context",
            "visual_elements": "Other elements in frame",
            "action_description": "Movement/action happening",
            "frame_indices": [0, 3, 5],
            "vo_during_context": "What is said during this context"
        }}
    ],

    "key_frames": {{
        "best_product_frame": 0,
        "best_usage_frame": 0,
        "best_result_frame": 0,
        "hook_frame": 0,
        "cta_frame": 0
    }},

    "overall_confidence": 0.0-1.0,

    "recreation_notes": "Key insights for recreating a similar video - what makes this video effective, what elements to preserve"
}}

If NO product detected:
{{
    "has_product": false,
    "product_detected": null,
    ...
}}

REMEMBER: The product_description will be used DIRECTLY for image generation. Make it so detailed that an artist could draw the exact product without ever seeing it!