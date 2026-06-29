Analyze this advertising video's structure and plan content adaptation.

**ARTICLE CONTENT TO ADAPT:**
Title: {title}
First Paragraph: {first_para}
Rest of Content: {rest_content}
Free Text (if provided, use this instead): {free_text}

**MANUAL INSTRUCTIONS (MUST FOLLOW):**
{manual_instructions}

{product_context}

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
}}