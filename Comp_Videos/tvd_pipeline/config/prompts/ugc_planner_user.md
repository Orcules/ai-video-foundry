Design the story arc for a {scene_count}-scene UGC influencer video.

PRODUCT/EXPERIENCE TO PROMOTE:
{free_text}

{vo_timing_block}

YOUR TASK: Create a scene-by-scene STORY PLAN with VO splitting. You decide the narrative structure ONLY — no visuals, no media matching, no influencer placement.

RULES:
1. Produce EXACTLY {scene_count} scenes. Not fewer, not more.
2. Each scene gets a `narrative_role` — exactly one of: "hook", "problem", "discovery", "solution", "outcome", "cta".
3. Each scene gets `vo_text` — the exact words spoken during that scene. First person (I / me / my).
4. Each scene gets `scene_intent` — one sentence describing the NARRATIVE PURPOSE (what the viewer should feel/understand), NOT a visual description.
5. Each scene gets `duration` — the VISUAL duration in seconds (how long the scene is shown on screen). Each scene should be {scene_duration_range} seconds. CRITICAL: keep each scene between 3-8 seconds so animation clips can cover it. NEVER make a single scene longer than 10 seconds. The total duration of all scenes MUST sum to approximately {target_duration} seconds.
6. The story arc must follow: RELATABLE PROBLEM → TENSION/DISCOVERY → SOLUTION → POSITIVE OUTCOME.
7. Scene 1 = the HOOK. It must grab attention in the first 3 seconds. A bold claim, a surprising statement, or an emotionally charged opening.
8. The LAST scene = CTA or satisfying close. Its VO must be a complete sentence ending with proper punctuation.
9. Language: {language_name}. Write the VO naturally in {language_name}.

VO SPLITTING RULES:
- If a VO timing block is provided above with scene_segments, use those segments directly as vo_text for each scene — preserve the exact words, just assign them to scenes in order.
- If only full_text is provided (no scene_segments), split the text into {scene_count} roughly equal segments yourself. Each segment must be a complete thought.
- If no VO is provided, write original VO text from scratch following the story arc.
- Every scene MUST have non-empty vo_text.
- Do NOT repeat the product/service name in every scene. Mention it at most once (discovery or CTA). The rest = pure storytelling.

RESPONSE FORMAT (JSON only, no other text):
{{
  "scene_plan": [
    {{
      "scene_number": 1,
      "narrative_role": "hook",
      "vo_text": "The spoken words for this scene in {language_name}",
      "scene_intent": "One sentence: what the viewer should feel or understand at this moment",
      "duration": 4.0
    }},
    {{
      "scene_number": 2,
      "narrative_role": "problem",
      "vo_text": "...",
      "scene_intent": "...",
      "duration": 3.5
    }}
  ]
}}

Now design a {scene_count}-scene story that will make viewers watch until the very end:
